from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Optional

import httpx

from .database import Database
from .monitor import MonitorEngine

log = logging.getLogger("netmon.agent_relay")


class AgentRelayService:
    def __init__(self, db: Database, engine: MonitorEngine):
        self.db = db
        self.engine = engine
        self.upstream_url = os.environ.get("NETMON_AGENT_UPSTREAM_URL", "").rstrip("/")
        self.pairing_token = os.environ.get("NETMON_AGENT_PAIRING_TOKEN", "")
        self.agent_name = os.environ.get("NETMON_AGENT_NAME", socket.gethostname())
        try:
            interval = int(os.environ.get("NETMON_AGENT_SYNC_INTERVAL", "30"))
        except ValueError:
            interval = 30
        self.interval_seconds = max(10, interval)
        self._task: Optional[asyncio.Task] = None
        self._last_alert_id = 0

    @property
    def enabled(self) -> bool:
        return bool(self.upstream_url and self.pairing_token)

    async def start(self):
        if not self.enabled:
            log.info("Agent relay disabled (missing NETMON_AGENT_UPSTREAM_URL or NETMON_AGENT_PAIRING_TOKEN)")
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())
        log.info("Agent relay started (upstream=%s, interval=%ss)", self.upstream_url, self.interval_seconds)

    async def stop(self):
        if not self._task:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        log.info("Agent relay stopped")

    async def _run_loop(self):
        try:
            while True:
                try:
                    await self._relay_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Agent relay sync failed")
                await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            pass

    async def _relay_once(self):
        devices = await self.db.list_devices()
        live_by_device = {rt.device["id"]: rt.stats() for rt in self.engine.runtimes.values()}
        endpoints = [{**device, "live": live_by_device.get(device["id"])} for device in devices]

        all_alerts = await self.db.list_alerts(unresolved_only=False, limit=1000)
        new_alerts = [alert for alert in reversed(all_alerts) if alert["id"] > self._last_alert_id]
        payload = {
            "pairing_token": self.pairing_token,
            "agent_name": self.agent_name,
            "endpoints": endpoints,
            "alerts": new_alerts,
        }

        relay_url = f"{self.upstream_url}/api/agent-relay"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(relay_url, json=payload)
            response.raise_for_status()

        if new_alerts:
            self._last_alert_id = max(alert["id"] for alert in new_alerts)
