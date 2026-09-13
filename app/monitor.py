"""The monitoring engine: one asyncio task per device, polling at an
interval determined by its priority level, maintaining an in-memory
rolling window for live jitter/loss stats, persisting every sample to
SQLite for historical analytics, and evaluating alert rules.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Callable, Optional

from .alerts import AlertManager
from .database import Database
from .ping_utils import compute_jitter_ms, compute_loss_pct, ping_once

# How often each priority level is polled.
PRIORITY_INTERVALS = {
    "critical": 5,
    "high": 15,
    "normal": 60,
    "low": 300,
}

WINDOW_SIZE = 20          # rolling samples kept in memory per device for live stats
DOWN_AFTER_FAILS = 3      # consecutive failed pings before a device is marked "down"
MIN_SAMPLES_FOR_LOSS_ALERT = 8


class DeviceRuntime:
    def __init__(self, device: dict):
        self.device = device
        self.window: deque[tuple[float, bool, Optional[float]]] = deque(maxlen=WINDOW_SIZE)
        self.consecutive_fail = 0
        self.consecutive_success = 0
        self.is_down = False
        self.active_conditions: set[str] = set()
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
        }


class MonitorEngine:
    def __init__(self, db: Database, alerts: AlertManager, broadcast_fn: Callable):
        self.db = db
        self.alerts = alerts
        self.broadcast = broadcast_fn
        self.runtimes: dict[int, DeviceRuntime] = {}

    async def start(self):
        devices = await self.db.list_devices()
        for device in devices:
            await self.add_device(device)

    async def stop(self):
        for rt in self.runtimes.values():
            if rt.task:
                rt.task.cancel()
        await asyncio.gather(
            *(rt.task for rt in self.runtimes.values() if rt.task),
            return_exceptions=True,
        )

    async def add_device(self, device: dict):
        rt = DeviceRuntime(device)
        self.runtimes[device["id"]] = rt
        if device["enabled"]:
            rt.task = asyncio.create_task(self._poll_loop(rt))

    async def remove_device(self, device_id: int):
        rt = self.runtimes.pop(device_id, None)
        if rt and rt.task:
            rt.task.cancel()

    async def refresh_device(self, device: dict):
        """Call after a device's config changes (priority, enabled, thresholds...)."""
        await self.remove_device(device["id"])
        await self.add_device(device)

    def snapshot_all(self) -> list[dict]:
        return [rt.stats() for rt in self.runtimes.values()]

    async def _poll_loop(self, rt: DeviceRuntime):
        interval = PRIORITY_INTERVALS.get(rt.device["priority"], 60)
        try:
            while True:
                result = await ping_once(rt.device["ip_address"])
                rt.window.append((time.time(), result.success, result.latency_ms))

                if result.success:
                    rt.consecutive_fail = 0
                    rt.consecutive_success += 1
                else:
                    rt.consecutive_fail += 1
                    rt.consecutive_success = 0

                await self.db.insert_ping(rt.device["id"], result.success, result.latency_ms)

                stats = rt.stats()
                await self._evaluate_alerts(rt, stats)
                await self.broadcast({"kind": "update", "stats": stats})

                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    async def _evaluate_alerts(self, rt: DeviceRuntime, stats: dict):
        device = rt.device

        # Device down / recovered
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

        # High packet loss (needs a minimum sample count so a device that
        # just started monitoring doesn't immediately trip this on 1 failure)
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

        # High latency
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

        # Lightweight Async Port Checker
        async def check_port(ip: str, port: int, timeout: float = 2.0) -> bool:
            try:
                await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
                return True
            except Exception:
                return False

        # MTU Path MTU Discovery (PMTUD) worker tool
        async def discover_mtu(ip: str) -> int:
            # Linux alpine uses -M do to enforce Dont Fragment flags
            # We can binary-search target packet sizes from 1500 down to 576
            cmd = ["ping", "-c", "1", "-M", "do", "-s", "1472", ip]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
            await proc.communicate()
            return 1500 if proc.returncode == 0 else 1492  # Example fallback logic
