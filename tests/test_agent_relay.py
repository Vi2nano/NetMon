import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.agent_relay import AgentRelayService


class _Runtime:
    def __init__(self, device_id: int, live: dict):
        self.device = {"id": device_id}
        self._live = live

    def stats(self):
        return self._live


class _FakeEngine:
    def __init__(self):
        self.runtimes = {}


class _FakeDB:
    def __init__(self, devices=None, alerts=None):
        self.devices = devices or []
        self.alerts = alerts or []

    async def list_devices(self):
        return self.devices

    async def list_alerts_since(self, alert_id=0, limit=500):
        filtered = sorted([a for a in self.alerts if a["id"] > alert_id], key=lambda item: item["id"])
        return filtered[:limit]


class _Response:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, calls: list, responses: list):
        self._calls = calls
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        self._calls.append((url, json))
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class AgentRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_disabled_without_required_env(self):
        db = _FakeDB()
        engine = _FakeEngine()
        with patch.dict(os.environ, {}, clear=True):
            relay = AgentRelayService(db, engine)
            self.assertFalse(relay.enabled)
            await relay.start()
            self.assertIsNone(relay._task)

        with patch.dict(os.environ, {"NETMON_AGENT_UPSTREAM_URL": "https://noc.example.com"}, clear=True):
            relay = AgentRelayService(db, engine)
            self.assertFalse(relay.enabled)

        with patch.dict(os.environ, {"NETMON_AGENT_PAIRING_TOKEN": "token-1"}, clear=True):
            relay = AgentRelayService(db, engine)
            self.assertFalse(relay.enabled)

    async def test_relay_posts_expected_payload_shape(self):
        calls = []
        responses = [_Response(200)]
        db = _FakeDB(
            devices=[{"id": 10, "name": "edge-1", "ip_address": "10.0.0.10", "enabled": 1}],
            alerts=[{"id": 12, "device_id": 10, "type": "down", "message": "down", "created_at": "t"}],
        )
        engine = _FakeEngine()
        engine.runtimes[10] = _Runtime(10, {"device_id": 10, "status": "down", "latency_ms": None})

        with patch.dict(
            os.environ,
            {
                "NETMON_AGENT_UPSTREAM_URL": "https://noc.example.com",
                "NETMON_AGENT_PAIRING_TOKEN": "pair-123",
                "NETMON_AGENT_NAME": "branch-agent",
            },
            clear=True,
        ), patch("app.agent_relay.httpx.AsyncClient", return_value=_Client(calls, responses)):
            relay = AgentRelayService(db, engine)
            await relay._relay_once()

        self.assertEqual(len(calls), 1)
        url, payload = calls[0]
        self.assertEqual(url, "https://noc.example.com/api/agent-relay")
        self.assertEqual(payload["pairing_token"], "pair-123")
        self.assertEqual(payload["agent_name"], "branch-agent")
        self.assertEqual(len(payload["endpoints"]), 1)
        self.assertIn("live", payload["endpoints"][0])
        self.assertEqual(payload["endpoints"][0]["live"]["status"], "down")
        self.assertEqual(len(payload["alerts"]), 1)
        self.assertEqual(payload["alerts"][0]["id"], 12)

    async def test_relay_loop_continues_after_single_failure(self):
        db = _FakeDB()
        engine = _FakeEngine()
        with patch.dict(
            os.environ,
            {
                "NETMON_AGENT_UPSTREAM_URL": "https://noc.example.com",
                "NETMON_AGENT_PAIRING_TOKEN": "pair-123",
            },
            clear=True,
        ):
            relay = AgentRelayService(db, engine)

        relay._relay_once = AsyncMock(side_effect=[RuntimeError("boom"), None, None])
        sleep_calls = {"count": 0}

        async def fake_sleep(_seconds):
            sleep_calls["count"] += 1
            if sleep_calls["count"] >= 2:
                raise asyncio.CancelledError()

        with patch("app.agent_relay.asyncio.sleep", side_effect=fake_sleep):
            await relay._run_loop()

        self.assertGreaterEqual(relay._relay_once.await_count, 2)

    async def test_alerts_are_incremental_between_cycles(self):
        calls = []
        responses = [_Response(200), _Response(200)]
        db = _FakeDB(
            devices=[{"id": 1, "name": "router", "ip_address": "192.168.1.1", "enabled": 1}],
            alerts=[
                {"id": 2, "device_id": 1, "type": "down", "message": "down", "created_at": "t2"},
                {"id": 1, "device_id": 1, "type": "latency", "message": "slow", "created_at": "t1"},
            ],
        )
        engine = _FakeEngine()
        engine.runtimes[1] = _Runtime(1, {"device_id": 1, "status": "up"})

        with patch.dict(
            os.environ,
            {
                "NETMON_AGENT_UPSTREAM_URL": "https://noc.example.com",
                "NETMON_AGENT_PAIRING_TOKEN": "pair-123",
            },
            clear=True,
        ), patch("app.agent_relay.httpx.AsyncClient", return_value=_Client(calls, responses)):
            relay = AgentRelayService(db, engine)
            await relay._relay_once()
            db.alerts.insert(0, {"id": 3, "device_id": 1, "type": "recovered", "message": "up", "created_at": "t3"})
            await relay._relay_once()

        self.assertEqual([alert["id"] for alert in calls[0][1]["alerts"]], [1, 2])
        self.assertEqual([alert["id"] for alert in calls[1][1]["alerts"]], [3])

    async def test_alert_pagination_uses_last_seen_id(self):
        calls = []
        responses = [_Response(200)]
        alerts = [
            {"id": alert_id, "device_id": 1, "type": "down", "message": f"alert-{alert_id}", "created_at": "t"}
            for alert_id in range(1, 651)
        ]
        db = _FakeDB(
            devices=[{"id": 1, "name": "router", "ip_address": "192.168.1.1", "enabled": 1}],
            alerts=alerts,
        )
        engine = _FakeEngine()
        engine.runtimes[1] = _Runtime(1, {"device_id": 1, "status": "up"})

        original_list_alerts_since = db.list_alerts_since
        request_count = {"count": 0}

        async def dynamic_list_alerts_since(alert_id=0, limit=500):
            request_count["count"] += 1
            if request_count["count"] == 2:
                db.alerts.append(
                    {"id": 651, "device_id": 1, "type": "recovered", "message": "up", "created_at": "t"}
                )
            return await original_list_alerts_since(alert_id=alert_id, limit=limit)

        with patch.dict(
            os.environ,
            {
                "NETMON_AGENT_UPSTREAM_URL": "https://noc.example.com",
                "NETMON_AGENT_PAIRING_TOKEN": "pair-123",
            },
            clear=True,
        ), patch("app.agent_relay.httpx.AsyncClient", return_value=_Client(calls, responses)):
            relay = AgentRelayService(db, engine)
            db.list_alerts_since = dynamic_list_alerts_since
            await relay._relay_once()

        sent_ids = [alert["id"] for alert in calls[0][1]["alerts"]]
        self.assertEqual(len(sent_ids), 651)
        self.assertEqual(sent_ids[0], 1)
        self.assertEqual(sent_ids[-1], 651)


if __name__ == "__main__":
    unittest.main()
