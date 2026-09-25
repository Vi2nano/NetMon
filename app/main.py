from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .alerts import WEBHOOK_PROVIDERS, AlertManager
from .database import PRIORITIES, RULE_METRICS, RULE_OPERATORS, RULE_SCOPE_TYPES, Database
from .export_import import ExportImport
from .monitor import MonitorEngine
from .ping_utils import discover_path_mtu, ping_once, run_traceroute, tcp_port_check
from .widgets.cert_checker import router as cert_checker_router
from .widgets.dns_email import router as dns_email_router
from .widgets.ticket_closure import router as ticket_closure_router

logging.basicConfig(
    level=os.environ.get("NETMON_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("netmon.main")

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("NETMON_DB_PATH", str(BASE_DIR / "data" / "netmon.db"))
FRONTEND_DIR = BASE_DIR / "frontend"
WIDGETS_DIR = FRONTEND_DIR / "widgets"

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
    log.info("NetMon started — monitoring %d device(s)", len(engine.runtimes))
    for rt in engine.runtimes.values():
        log.info(
            "  - %s (%s) priority=%s enabled=%s polling=%s",
            rt.device["name"], rt.device["ip_address"], rt.device["priority"],
            rt.device["enabled"], "yes" if rt.task else "NO (disabled)",
        )
    prune_task = asyncio.create_task(_pruning_loop())
    yield
    prune_task.cancel()
    await engine.stop()
    await db.close()

app = FastAPI(title="NetMon", lifespan=lifespan)
app.include_router(dns_email_router)
app.include_router(ticket_closure_router)
app.include_router(cert_checker_router)

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
    latency_alert_consecutive_packets: int = Field(default=1, ge=1)


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    ip_address: Optional[str] = None
    group_id: Optional[int] = None
    priority: Optional[str] = None
    enabled: Optional[bool] = None
    latency_threshold_ms: Optional[int] = None
    loss_threshold_pct: Optional[float] = None
    latency_alert_consecutive_packets: Optional[int] = None


class SettingIn(BaseModel):
    value: str


class ScanPayload(BaseModel):
    subnet: str  # Expects CIDR like "192.168.1.0/24"


class PortCheckPayload(BaseModel):
    port: int = Field(default=443, ge=1, le=65535)


class PortIn(BaseModel):
    port: int = Field(ge=1, le=65535)
    label: str = ""


class RuleIn(BaseModel):
    name: str
    scope_type: str = "all"
    scope_id: Optional[int] = None
    metric: str
    operator: str = ">="
    threshold: Optional[float] = None
    port: Optional[int] = None
    cooldown_minutes: int = Field(default=10, ge=0)
    enabled: bool = True


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    scope_type: Optional[str] = None
    scope_id: Optional[int] = None
    metric: Optional[str] = None
    operator: Optional[str] = None
    threshold: Optional[float] = None
    port: Optional[int] = None
    cooldown_minutes: Optional[int] = None
    enabled: Optional[bool] = None


class ImportPayload(BaseModel):
    config: dict
    mode: str = "merge"  # "merge" or "replace"


def _validate_rule(payload: dict):
    if payload.get("scope_type") and payload["scope_type"] not in RULE_SCOPE_TYPES:
        raise HTTPException(400, f"scope_type must be one of {RULE_SCOPE_TYPES}")
    if payload.get("metric") and payload["metric"] not in RULE_METRICS:
        raise HTTPException(400, f"metric must be one of {RULE_METRICS}")
    if payload.get("operator") and payload["operator"] not in RULE_OPERATORS:
        raise HTTPException(400, f"operator must be one of {RULE_OPERATORS}")
    if payload.get("metric") == "port_down" and not payload.get("port"):
        raise HTTPException(400, "port is required when metric is 'port_down'")
    if payload.get("metric") and payload.get("metric") != "port_down" and payload.get("threshold") is None:
        raise HTTPException(400, "threshold is required for this metric")


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


# ---------------------------------------------------------------- discovery

DISCOVERY_CONCURRENCY = 40
# cap simultaneous in-flight pings so this doesn't
# exhaust file descriptors / CPU in a small container


@app.post("/api/discover")
async def discover_subnet(payload: ScanPayload):
    try:
        network = ipaddress.ip_network(payload.subnet, strict=False)
    except ValueError:
        raise HTTPException(400, "Invalid CIDR subnet format. Use e.g., 192.168.1.0/24")

    if network.num_addresses > 256:
        raise HTTPException(400, "Subnet scan range limited to /24 networks (256 IPs) at a time")

    sem = asyncio.Semaphore(DISCOVERY_CONCURRENCY)

    async def probe(ip: str) -> Optional[str]:
        async with sem:
            result = await ping_once(ip, timeout_s=0.8)
        return ip if result.success else None

    hosts = [str(h) for h in network.hosts()] or [str(network.network_address)]
    try:
        results = await asyncio.gather(*(probe(ip) for ip in hosts))
    except Exception as e:
        raise HTTPException(500, f"Scan failed: {e}")

    discovered_hosts = [ip for ip in results if ip is not None]
    return {"hosts": discovered_hosts}


# ---------------------------------------------------------------- device manual diagnostics

@app.post("/api/devices/{device_id}/action/traceroute")
async def action_traceroute(device_id: int):
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    return await run_traceroute(device["ip_address"])


@app.post("/api/devices/{device_id}/action/mtu")
async def action_mtu(device_id: int):
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    return await discover_path_mtu(device["ip_address"])


@app.post("/api/devices/{device_id}/action/portcheck")
async def action_portcheck(device_id: int, payload: PortCheckPayload):
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    result = await tcp_port_check(device["ip_address"], payload.port)
    if result.success:
        return {"ok": True, "output": f"Port {payload.port} is OPEN — connected in {result.latency_ms:.1f} ms."}
    return {"ok": False, "output": f"Port {payload.port} is CLOSED or filtered ({result.error})."}


# ---------------------------------------------------------------- persisted port monitoring

@app.get("/api/devices/{device_id}/ports")
async def list_ports(device_id: int):
    return await db.list_ports(device_id)


@app.post("/api/devices/{device_id}/ports")
async def add_port(device_id: int, payload: PortIn):
    device = await db.get_device(device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    await db.create_port(device_id, payload.port, payload.label)
    await engine.refresh_device(device)  # re-reads ports from the DB
    return await db.list_ports(device_id)


@app.delete("/api/ports/{port_id}")
async def delete_port(port_id: int):
    port = await db.get_port(port_id)
    if not port:
        raise HTTPException(404, "Port not found")
    device = await db.get_device(port["device_id"])
    await db.delete_port(port_id)
    if device:
        await engine.refresh_device(device)
    return {"ok": True}


# ---------------------------------------------------------------- alert rules

@app.get("/api/rules")
async def get_rules():
    return await db.list_rules()


@app.post("/api/rules")
async def create_rule(payload: RuleIn):
    data = payload.model_dump()
    _validate_rule(data)
    rule = await db.create_rule(data)
    await engine.reload_rules()
    return rule


@app.patch("/api/rules/{rule_id}")
async def update_rule(rule_id: int, payload: RuleUpdate):
    existing = await db.get_rule(rule_id)
    if not existing:
        raise HTTPException(404, "Rule not found")
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    _validate_rule({**existing, **data})
    rule = await db.update_rule(rule_id, data)
    await engine.reload_rules()
    return rule


@app.delete("/api/rules/{rule_id}")
async def delete_rule(rule_id: int):
    await db.delete_rule(rule_id)
    await engine.reload_rules()
    return {"ok": True}


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


@app.get("/api/settings/webhook_provider")
async def get_webhook_provider():
    return {"value": await db.get_setting("webhook_provider", "generic")}


@app.put("/api/settings/webhook_provider")
async def set_webhook_provider(payload: SettingIn):
    if payload.value not in WEBHOOK_PROVIDERS:
        raise HTTPException(
            400, f"provider must be one of {sorted(WEBHOOK_PROVIDERS)}"
        )
    await db.set_setting("webhook_provider", payload.value)
    return {"ok": True}


# ---------------------------------------------------------------- import/export

@app.get("/api/export/config")
async def export_config():
    """Export all configuration (groups, devices, ports, alert rules) as JSON."""
    try:
        config = await ExportImport.export_config(db)
        return config
    except Exception as e:
        raise HTTPException(500, f"Export failed: {e}")


@app.post("/api/import/config")
async def import_config(payload: ImportPayload):
    """Import configuration from JSON.
    
    Modes:
    - "merge": Add new items, skip existing ones (default)
    - "replace": Delete all existing config and import fresh
    """
    try:
        if payload.mode not in ("merge", "replace"):
            raise HTTPException(400, "mode must be 'merge' or 'replace'")
        
        stats = await ExportImport.import_config(db, payload.config, payload.mode)
        
        # Reload monitor engine with new configuration
        await engine.reload_rules()
        for device in await db.list_devices():
            await engine.add_device(device)
        
        return {
            "ok": True,
            "stats": stats,
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Import failed: {e}")


# ---------------------------------------------------------------- websocket

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_text(json.dumps({"kind": "snapshot", "stats": engine.snapshot_all()}, default=str))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


# ---------------------------------------------------------------- frontend

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "home.html"))


@app.get("/widgets/netmon")
@app.get("/widgets/netmon/")
async def netmon_widget():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/widgets/dns-email")
@app.get("/widgets/dns-email/")
async def dns_email_widget():
    return FileResponse(str(WIDGETS_DIR / "dns-email" / "index.html"))


@app.get("/widgets/ticket-closure")
@app.get("/widgets/ticket-closure/")
async def ticket_closure_widget():
    return FileResponse(str(WIDGETS_DIR / "ticket-closure" / "index.html"))


@app.get("/widgets/cert-expiration")
@app.get("/widgets/cert-expiration/")
async def cert_expiration_widget():
    return FileResponse(str(WIDGETS_DIR / "cert-expiration" / "index.html"))
