"""Alert creation + optional outbound webhook notifications."""
from __future__ import annotations

import logging

import httpx

from .database import Database

log = logging.getLogger("netmon.alerts")
WEBHOOK_PROVIDERS = {"generic", "discord", "slack", "teams"}


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
        provider = await self.db.get_setting("webhook_provider", "generic")
        if provider not in WEBHOOK_PROVIDERS:
            provider = "generic"
        payload = self._payload_for(provider, device, type_, message)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
        except Exception:
            # Never let a webhook failure break the monitoring loop.
            log.exception(
                "Webhook delivery failed for host %s", httpx.URL(url).host
            )

    @staticmethod
    def _payload_for(
        provider: str, device: dict, type_: str, message: str
    ) -> dict:
        text = f"[netmon] {message}"
        if provider == "discord":
            return {"content": text}
        if provider == "slack":
            return {"text": text}
        if provider == "teams":
            return {
                "@type": "MessageCard",
                "@context": "https://schema.org/extensions",
                "summary": text,
                "themeColor": "E5484D",
                "sections": [{
                    "activityTitle": "NetMon alert",
                    "facts": [
                        {"name": "Device", "value": device["name"]},
                        {"name": "IP address", "value": device["ip_address"]},
                        {"name": "Type", "value": type_},
                    ],
                    "text": message,
                    "markdown": True,
                }],
            }
        return {
            "text": text,
            "device": device["name"],
            "ip_address": device["ip_address"],
            "type": type_,
        }
