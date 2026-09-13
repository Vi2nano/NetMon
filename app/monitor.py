"""The monitoring engine: one asyncio task per device, polling at an
interval determined by its priority level, maintaining an in-memory
rolling window for live jitter/loss stats, checking any configured TCP
ports alongside the ping, persisting every sample to SQLite for historical
analytics, and evaluating alert rules (both the built-in latency/loss
thresholds on the device, and any custom rules from the alert_rules table).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Callable, Optional

from .alerts import AlertManager
from .database import Database
from .ping_utils import compute_jitter_ms, compute_loss_pct, ping_once, tcp_port_check

# How often each priority level is polled.
PRIORITY_INTERVALS = {
    "critical": 5,
    "high": 15,
    "normal": 60,
    "low": 300,
}

WINDOW_SIZE = 20            # rolling samples kept in memory per device/port for live stats
DOWN_AFTER_FAILS = 3        # consecutive failed pings before a device is marked "down"
MIN_SAMPLES_FOR_LOSS_ALERT = 8
RULES_RELOAD_SECONDS = 15   # how often the engine re-reads alert_rules from the DB


class PortRuntime:
    def __init__(self, port_row: dict):
        self.port = port_row
        self.window: deque[tuple[float, bool, Optional[float]]] = deque(maxlen=WINDOW_SIZE)
        self.consecutive_fail = 0
        self.consecutive_success = 0
        self.is_down = False

    def stats(self) -> dict:
        entries = list(self.window)
        latest = entries[-1] if entries else None
        return {
            "port_id": self.port["id"],
            "port": self.port["port"],
            "label": self.port["label"],
            "status": "unknown" if not entries else ("down" if (self.is_down or not entries[-1][1]) else "up"),
            "latency_ms": latest[2] if latest else None,
            "loss_pct": compute_loss_pct([e[1] for e in entries]),
        }


class DeviceRuntime:
    def __init__(self, device: dict):
        self.device = device
        self.window: deque[tuple[float, bool, Optional[float]]] = deque(maxlen=WINDOW_SIZE)
        self.consecutive_fail = 0
        self.consecutive_success = 0
        self.is_down = False
        self.active_conditions: set[str] = set()
        self.ports: dict[int, PortRuntime] = {}
        # rule_id -> {"active": bool, "last_fired": float epoch seconds}
        self.rule_states: dict[int, dict] = {}
        self.task: Optional[asyncio.Task] = None

    def stats(self) -> dict:
        entries = list(self.window)
        successes = [e for e in entries if e[1]]
        latencies = [e[2] for e in successes if e[2] is not None]
        loss_pct = compute_loss_pct([e[1] for e in entries])
        jitter = compute_jitter_ms(latencies)
        latest_latency = entries[-1][2] if entries else None
        status = "unknown"
        if entries:
            status = "down" if (self.is_down or not entries[-1][1]) else "up"
        return {
            "device_id": self.device["id"],
            "status": status,
            "latency_ms": latest_latency,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "jitter_ms": jitter,
            "loss_pct": loss_pct,
            "sparkline": [e[2] for e in entries],
            "samples": len(entries),
            "ports": [p.stats() for p in self.ports.values()],
        }


def _rule_applies(rule: dict, device: dict) -> bool:
    if not rule["enabled"]:
        return False
    if rule["scope_type"] == "all":
        return True
    if rule["scope_type"] == "device":
        return rule["scope_id"] == device["id"]
    if rule["scope_type"] == "group":
        return rule["scope_id"] is not None and rule["scope_id"] == device.get("group_id")
    return False


def _compare(value: Optional[float], operator: str, threshold: Optional[float]) -> bool:
    if value is None or threshold is None:
        return False
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
    }.get(operator, False)


class MonitorEngine:
    def __init__(self, db: Database, alerts: AlertManager, broadcast_fn: Callable):
        self.db = db
        self.alerts = alerts
        self.broadcast = broadcast_fn
        self.runtimes: dict[int, DeviceRuntime] = {}
        self.rules: list[dict] = []
        self._rules_task: Optional[asyncio.Task] = None

    async def start(self):
        self.rules = await self.db.list_rules()
        devices = await self.db.list_devices()
        for device in devices:
            await self.add_device(device)
        self._rules_task = asyncio.create_task(self._rules_reload_loop())

    async def stop(self):
        if self._rules_task:
            self._rules_task.cancel()
        for rt in self.runtimes.values():
            if rt.task:
                rt.task.cancel()
        await asyncio.gather(
            *(rt.task for rt in self.runtimes.values() if rt.task),
            return_exceptions=True,
        )

    async def _rules_reload_loop(self):
        try:
            while True:
                await asyncio.sleep(RULES_RELOAD_SECONDS)
                await self.reload_rules()
        except asyncio.CancelledError:
            pass

    async def reload_rules(self):
        self.rules = await self.db.list_rules()

    async def add_device(self, device: dict):
        rt = DeviceRuntime(device)
        for port_row in await self.db.list_ports(device["id"]):
            if port_row["enabled"]:
                rt.ports[port_row["id"]] = PortRuntime(port_row)
        self.runtimes[device["id"]] = rt
        if device["enabled"]:
            rt.task = asyncio.create_task(self._poll_loop(rt))

    async def remove_device(self, device_id: int):
        rt = self.runtimes.pop(device_id, None)
        if rt and rt.task:
            rt.task.cancel()

    async def refresh_device(self, device: dict):
        """Call after a device's config changes (priority, enabled, thresholds, ports...)."""
        await self.remove_device(device["id"])
        await self.add_device(device)

    def snapshot_all(self) -> list[dict]:
        return [rt.stats() for rt in self.runtimes.values()]

    async def _poll_loop(self, rt: DeviceRuntime):
        interval = PRIORITY_INTERVALS.get(rt.device["priority"], 60)
        try:
            while True:
                await self._check_device(rt)
                await self._check_ports(rt)

                stats = rt.stats()
                await self._evaluate_builtin_thresholds(rt, stats)
                await self._evaluate_custom_rules(rt, stats)
                await self.broadcast({"kind": "update", "stats": stats})

                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    async def _check_device(self, rt: DeviceRuntime):
        result = await ping_once(rt.device["ip_address"])
        rt.window.append((time.time(), result.success, result.latency_ms))

        if result.success:
            rt.consecutive_fail = 0
            rt.consecutive_success += 1
        else:
            rt.consecutive_fail += 1
            rt.consecutive_success = 0

        await self.db.insert_ping(rt.device["id"], result.success, result.latency_ms)

        device = rt.device
        if rt.consecutive_fail >= DOWN_AFTER_FAILS and not rt.is_down:
            rt.is_down = True
            await self.alerts.fire(
                device, "down", f"{device['name']} ({device['ip_address']}) is not responding"
            )
        elif rt.is_down and rt.consecutive_success >= 1:
            rt.is_down = False
            await self.alerts.resolve(device, "down")
            await self.alerts.fire(
                device, "recovered", f"{device['name']} ({device['ip_address']}) is back online"
            )

    async def _check_ports(self, rt: DeviceRuntime):
        for port_rt in rt.ports.values():
            result = await tcp_port_check(rt.device["ip_address"], port_rt.port["port"])
            port_rt.window.append((time.time(), result.success, result.latency_ms))
            if result.success:
                port_rt.consecutive_fail = 0
                port_rt.consecutive_success += 1
            else:
                port_rt.consecutive_fail += 1
                port_rt.consecutive_success = 0

            device = rt.device
            label = port_rt.port["label"] or f"port {port_rt.port['port']}"
            if port_rt.consecutive_fail >= DOWN_AFTER_FAILS and not port_rt.is_down:
                port_rt.is_down = True
                await self.alerts.fire(
                    device, "port_down",
                    f"{device['name']} — {label} ({port_rt.port['port']}) is not accepting connections",
                )
            elif port_rt.is_down and port_rt.consecutive_success >= 1:
                port_rt.is_down = False
                await self.alerts.resolve(device, "port_down")
                await self.alerts.fire(
                    device, "port_recovered",
                    f"{device['name']} — {label} ({port_rt.port['port']}) is accepting connections again",
                )

    async def _evaluate_builtin_thresholds(self, rt: DeviceRuntime, stats: dict):
        """The original per-device latency/loss thresholds — unchanged behavior."""
        device = rt.device

        loss_threshold = device.get("loss_threshold_pct", 20)
        high_loss = stats["samples"] >= MIN_SAMPLES_FOR_LOSS_ALERT and stats["loss_pct"] >= loss_threshold
        if high_loss and "high_loss" not in rt.active_conditions:
            rt.active_conditions.add("high_loss")
            await self.alerts.fire(
                device, "high_loss",
                f"{device['name']} packet loss at {stats['loss_pct']}% (threshold {loss_threshold}%)",
            )
        elif not high_loss and "high_loss" in rt.active_conditions:
            rt.active_conditions.discard("high_loss")
            await self.alerts.resolve(device, "high_loss")

        latency_threshold = device.get("latency_threshold_ms", 200)
        high_latency = stats["latency_ms"] is not None and stats["latency_ms"] >= latency_threshold
        if high_latency and "high_latency" not in rt.active_conditions:
            rt.active_conditions.add("high_latency")
            await self.alerts.fire(
                device, "high_latency",
                f"{device['name']} latency at {stats['latency_ms']:.0f}ms (threshold {latency_threshold}ms)",
            )
        elif not high_latency and "high_latency" in rt.active_conditions:
            rt.active_conditions.discard("high_latency")
            await self.alerts.resolve(device, "high_latency")

    async def _evaluate_custom_rules(self, rt: DeviceRuntime, stats: dict):
        """The user-configurable alert_rules table — additive on top of the
        built-in thresholds above, so existing behavior never regresses."""
        device = rt.device
        now = time.time()

        for rule in self.rules:
            if not _rule_applies(rule, device):
                continue

            state = rt.rule_states.setdefault(rule["id"], {"active": False, "last_fired": 0})
            cooldown_s = rule.get("cooldown_minutes", 10) * 60
            alert_type = f"rule_{rule['id']}"  # unique per rule so resolving one never touches another

            if rule["metric"] == "port_down":
                port_stat = next((p for p in stats["ports"] if p["port"] == rule.get("port")), None)
                condition = bool(port_stat and port_stat["status"] == "down")
            else:
                value = stats.get(rule["metric"])
                condition = _compare(value, rule["operator"], rule["threshold"])

            if condition:
                if not state["active"] and (now - state["last_fired"] > cooldown_s):
                    state["active"] = True
                    state["last_fired"] = now
                    detail = self._rule_message(rule, stats)
                    await self.alerts.fire(device, alert_type, f"[{rule['name']}] {device['name']}: {detail}")
            else:
                if state["active"]:
                    state["active"] = False
                    await self.alerts.resolve(device, alert_type)

    @staticmethod
    def _rule_message(rule: dict, stats: dict) -> str:
        if rule["metric"] == "port_down":
            return f"port {rule.get('port')} is down"
        value = stats.get(rule["metric"])
        label = {"latency_ms": "latency", "jitter_ms": "jitter", "loss_pct": "packet loss"}.get(rule["metric"], rule["metric"])
        unit = "%" if rule["metric"] == "loss_pct" else "ms"
        return f"{label} is {value}{unit} (threshold {rule['operator']} {rule['threshold']}{unit})"
