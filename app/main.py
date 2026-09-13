from __future__ import annotations

import asyncio
import json
import os
import ipaddress

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .alerts import AlertManager
from .database import PRIORITIES, Database
from .monitor import MonitorEngine

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("NETMON_DB_PATH", str(BASE_DIR / "data" / "netmon.db"))
FRONTEND_DIR = BASE_DIR / "frontend"

db = Database(DB_PATH)


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.active.append(ws)

    async def disconnect(self, ws: WebSocket):
        async with self.lock:
            if ws in self.active:
                self.active.remove(ws)

    async def broadcast(self, message: dict):
        data = json.dumps(message, default=str)
        dead = []
        async with self.lock:
            targets = list(self.active)
        for ws in targets:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        if dead:
            async with self.lock:
                for ws in dead:
                    if ws in self.active:
                        self.active.remove(ws)


manager = ConnectionManager()
alert_manager = AlertManager(db, manager.broadcast)
engine = MonitorEngine(db, alert_manager, manager.broadcast)


async def _pruning_loop():
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            await db.prune_old_pings(keep_days=30)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await engine.start()
    prune_task = asyncio.create_task(_pruning_loop())
    yield
    prune_task.cancel()
    await engine.stop()
    await db.close()

app = FastAPI(title="NetMon", lifespan=lifespan)

# ---------------------------------------------------------------- schemas

class GroupIn(BaseModel):
    name: str


class DeviceIn(BaseModel):
    name: str
    ip_address: str
    group_id: Optional[int] = None
    priority: str = "normal"
    enabled: bool = True
    latency_threshold_ms: int = Field(default=200, ge=1)
    loss_threshold_pct: float = Field(default=20, ge=0, le=100)


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    ip_address: Optional[str] = None
    group_id: Optional[int] = None
    priority: Optional[str] = None
    enabled: Optional[bool] = None
    latency_threshold_ms: Optional[int] = None
    loss_threshold_pct: Optional[float] = None


class SettingIn(BaseModel):
    value: str


class ScanPayload(BaseModel):
    subnet: str  # Expects CIDR like "192.168.1.0/24"


class PortCheckPayload(BaseModel):
    port: int = Field(default=443, ge=1, le=65535)

# ---------------------------------------------------------------- groups

@app.get("/api/groups")
async def get_groups():
    return await db.list_groups()


@app.post("/api/groups")
async def create_group(payload: GroupIn):
    try:
        return await db.create_group(payload.name)
    except Exception:
        raise HTTPException(400, "A group with that name already exists")


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: int):
    await db.delete_group(group_id)
    return {"ok": True}


# ---------------------------------------------------------------- devices

@app.get("/api/devices")
async def get_devices():
    devices = await db.list_devices()
    live = {rt.device["id"]: rt.stats() for rt in engine.runtimes.values()}
    for d in devices:
        d["live"] = live.get(d["id"])
    return devices


@app.post("/api/devices")
async def create_device(payload: DeviceIn):
    if payload.priority not in PRIORITIES:
        raise HTTPException(400, f"priority must be one of {PRIORITIES}")
    device = await db.create_device(payload.model_dump())
    await engine.add_device(device)
    return device


@app.patch("/api/devices/{device_id}")
async def update_device(device_id: int, payload: DeviceUpdate):
    existing = await db.get_device(device_id)
    if not existing:
        raise HTTPException(404, "Device not found")
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "priority" in data and data["priority"] not in PRIORITIES:
        raise HTTPException(400, f"priority must be one of {PRIORITIES}")
    device = await db.update_device(device_id, data)
    await engine.refresh_device(device)
    return device


@app.delete("/api/devices/{device_id}")
async def delete_device(device_id: int):
    await engine.remove_device(device_id)
    await db.delete_device(device_id)
    return {"ok": True}


@app.get("/api/devices/{device_id}/history")
async def device_history(device_id: int, hours: int = 24):
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    return await db.history(device_id, hours=hours)


@app.post("/api/discover")
async def discover_subnet(payload: ScanPayload):
    try:
        # Validate the CIDR network input string
        network = ipaddress.ip_network(payload.subnet, strict=False)
    except ValueError:
        raise HTTPException(400, "Invalid CIDR subnet format. Use e.g., 192.168.1.0/24")

    # Limit maximum batch allocation size to prevent container network resource choking
    if network.num_addresses > 256:
        raise HTTPException(400, "Subnet scan range limited to /24 networks (256 IPs) at a time")

    # Helper task for internal execution
    async def ping_scan_host(ip: str) -> str | None:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        return ip if proc.returncode == 0 else None

    # Run sweeps concurrently using async pools
    tasks = [ping_scan_host(str(host)) for host in network.hosts()]
    results = await asyncio.gather(*tasks)

    discovered_hosts = [ip for ip in results if ip is not None]
    return {"hosts": discovered_hosts}


# ---------------------------------------------------------------- devices manual actions

@app.post("/api/devices/{device_id}/action/traceroute")
async def action_traceroute(device_id: int):
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(404, "Device not found")

    target = device["ip_address"]
    # Run the native Alpine traceroute utility asynchronously
    # -w 1 limits hop timeout to 1 sec, -q 1 sends one packet per hop to speed it up
    proc = await asyncio.create_subprocess_exec(
        "traceroute", "-w", "1", "-q", "1", target,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    return {
        "ok": proc.returncode == 0,
        "output": stdout.decode(errors="replace") or stderr.decode(errors="replace")
    }


@app.post("/api/devices/{device_id}/action/mtu")
async def action_mtu(device_id: int):
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(404, "Device not found")

    target = device["ip_address"]
    # Linux ping: -M do sets Dont Fragment bit, -s sets payload size
    # 1472 bytes payload + 28 bytes ICMP/IP headers = 1500 byte standard frame
    cmd = ["ping", "-c", "1", "-M", "do", "-s", "1472", "-W", "1", target]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()

    if proc.returncode == 0:
        return {"ok": True, "mtu": 1500, "output": "Standard 1500 MTU verified successfully (DF bit unfragmented)."}

    # Fallback sweep boundary test check if 1500 drops
    fallback_cmd = ["ping", "-c", "1", "-M", "do", "-s", "1464", "-W", "1", target]
    fb_proc = await asyncio.create_subprocess_exec(*fallback_cmd)
    await fb_proc.communicate()

    if fb_proc.returncode == 0:
        return {"ok": True, "mtu": 1492,
                "output": "Detected 1492 MTU (Consistent with local PPPoE WAN framing encapsulation boundaries)."}

    return {"ok": False, "mtu": None,
            "output": "Path MTU check dropped packets or host is blocking fragmented diagnostic sweeps entirely."}


@app.post("/api/devices/{device_id}/action/portcheck")
async def action_portcheck(device_id: int, payload: PortCheckPayload):
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(404, "Device not found")

    target = device["ip_address"]
    try:
        # Avoid subprocess overhead; cleanly map using native async socket descriptors
        conn = asyncio.open_connection(target, payload.port)
        await asyncio.wait_for(conn, timeout=2.0)
        return {"ok": True, "output": f"Port {payload.port} is OPEN and actively listening."}
    except Exception as e:
        return {"ok": False, "output": f"Port {payload.port} is CLOSED or filtered. Reason: {type(e).__name__}"}

# ---------------------------------------------------------------- alerts

@app.get("/api/alerts")
async def get_alerts(unresolved_only: bool = False):
    return await db.list_alerts(unresolved_only=unresolved_only)


# ---------------------------------------------------------------- settings

@app.get("/api/settings/webhook_url")
async def get_webhook_url():
    return {"value": await db.get_setting("webhook_url", "")}


@app.put("/api/settings/webhook_url")
async def set_webhook_url(payload: SettingIn):
    await db.set_setting("webhook_url", payload.value)
    return {"ok": True}


# ---------------------------------------------------------------- websocket

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # Send an initial full snapshot so a newly connected client doesn't
        # have to wait for the next poll cycle of every device.
        await ws.send_text(json.dumps({"kind": "snapshot", "stats": engine.snapshot_all()}, default=str))
        while True:
            # We don't expect the client to send anything meaningful, but
            # keep the receive loop alive to detect disconnects promptly.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


# ---------------------------------------------------------------- frontend

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
