"""Alert creation + optional outbound webhook (Slack/Discord/generic JSON)."""
from __future__ import annotations

import httpx

from .database import Database


class AlertManager:
    def __init__(self, db: Database, broadcast_fn):
        self.db = db
        self.broadcast = broadcast_fn
        # push alert to connected websocket clients

    async def fire(self, device: dict, type_: str, message: str):
        alert = await self.db.create_alert(device["id"], type_, message)
        await self.broadcast({"kind": "alert", "alert": alert})
        await self._send_webhook(device, type_, message)

    async def resolve(self, device: dict, type_: str):
        await self.db.resolve_alerts(device["id"], type_)

    async def _send_webhook(self, device: dict, type_: str, message: str):
        url = await self.db.get_setting("webhook_url")
        if not url:
            return
        payload = {
            "text": f"[netmon] {message}",
            "device": device["name"],
            "ip_address": device["ip_address"],
            "type": type_,
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json=payload)
        except Exception:
            # Never let a webhook failure break the monitoring loop.
            pass
