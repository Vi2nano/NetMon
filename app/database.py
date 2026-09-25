"""Async SQLite access layer. One aiosqlite connection, reused everywhere,
with `PRAGMA journal_mode=WAL` so reads (dashboard/history queries) don't
block writes (the ping engine inserting new samples every few seconds).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    enabled INTEGER NOT NULL DEFAULT 1,
    latency_threshold_ms INTEGER NOT NULL DEFAULT 200,
    loss_threshold_pct REAL NOT NULL DEFAULT 20,
    latency_alert_consecutive_packets INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    ts TEXT DEFAULT (datetime('now')),
    success INTEGER NOT NULL,
    latency_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_pings_device_ts ON pings(device_id, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    resolved INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    port INTEGER NOT NULL,
    label TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'all',     -- 'all' | 'group' | 'device'
    scope_id INTEGER,                           -- group_id or device_id, depending on scope_type
    metric TEXT NOT NULL,                       -- 'latency_ms' | 'jitter_ms' | 'loss_pct' | 'port_down'
    operator TEXT NOT NULL DEFAULT '>=',        -- '>' | '>=' | '<' | '<='
    threshold REAL,
    port INTEGER,                               -- required when metric = 'port_down'
    cooldown_minutes INTEGER NOT NULL DEFAULT 10,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

PRIORITIES = ("critical", "high", "normal", "low")
RULE_METRICS = ("latency_ms", "jitter_ms", "loss_pct", "port_down")
RULE_OPERATORS = (">", ">=", "<", "<=")
RULE_SCOPE_TYPES = ("all", "group", "device")


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA foreign_keys=ON;")
        await self.conn.executescript(SCHEMA)
        await self._run_migrations()
        await self.conn.commit()

    async def _run_migrations(self):
        """Apply any schema migrations needed for existing databases."""
        # Migration: add latency_alert_consecutive_packets column if it doesn't exist
        try:
            await self.conn.execute(
                "ALTER TABLE devices ADD COLUMN latency_alert_consecutive_packets INTEGER NOT NULL DEFAULT 1"
            )
        except aiosqlite.OperationalError:
            # Column already exists, which is fine
            pass

    async def close(self):
        if self.conn:
            await self.conn.close()

    # ---------- groups ----------

    async def list_groups(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM groups ORDER BY name")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def create_group(self, name: str) -> dict:
        cur = await self.conn.execute("INSERT INTO groups (name) VALUES (?)", (name,))
        await self.conn.commit()
        return await self.get_group(cur.lastrowid)

    async def get_group(self, group_id: int) -> Optional[dict]:
        cur = await self.conn.execute("SELECT * FROM groups WHERE id=?", (group_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_group(self, group_id: int):
        await self.conn.execute("UPDATE devices SET group_id=NULL WHERE group_id=?", (group_id,))
        await self.conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
        await self.conn.commit()

    # ---------- devices ----------

    async def list_devices(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM devices ORDER BY name")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_device(self, device_id: int) -> Optional[dict]:
        cur = await self.conn.execute("SELECT * FROM devices WHERE id=?", (device_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def create_device(self, data: dict) -> dict:
        cur = await self.conn.execute(
            """INSERT INTO devices
               (name, ip_address, group_id, priority, enabled, latency_threshold_ms, loss_threshold_pct, latency_alert_consecutive_packets)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["name"],
                data["ip_address"],
                data.get("group_id"),
                data.get("priority", "normal"),
                int(data.get("enabled", True)),
                data.get("latency_threshold_ms", 200),
                data.get("loss_threshold_pct", 20),
                data.get("latency_alert_consecutive_packets", 1),
            ),
        )
        await self.conn.commit()
        return await self.get_device(cur.lastrowid)

    async def update_device(self, device_id: int, data: dict) -> Optional[dict]:
        existing = await self.get_device(device_id)
        if not existing:
            return None
        merged = {**existing, **data}
        await self.conn.execute(
            """UPDATE devices SET name=?, ip_address=?, group_id=?, priority=?,
               enabled=?, latency_threshold_ms=?, loss_threshold_pct=?, latency_alert_consecutive_packets=? WHERE id=?""",
            (
                merged["name"],
                merged["ip_address"],
                merged["group_id"],
                merged["priority"],
                int(merged["enabled"]),
                merged["latency_threshold_ms"],
                merged["loss_threshold_pct"],
                merged["latency_alert_consecutive_packets"],
                device_id,
            ),
        )
        await self.conn.commit()
        return await self.get_device(device_id)

    async def delete_device(self, device_id: int):
        await self.conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
        await self.conn.commit()

    # ---------- pings ----------

    async def insert_ping(self, device_id: int, success: bool, latency_ms: Optional[float]):
        await self.conn.execute(
            "INSERT INTO pings (device_id, success, latency_ms) VALUES (?, ?, ?)",
            (device_id, int(success), latency_ms),
        )
        await self.conn.commit()

    async def prune_old_pings(self, keep_days: int = 30):
        await self.conn.execute(
            "DELETE FROM pings WHERE ts < datetime('now', ?)", (f"-{keep_days} days",)
        )
        await self.conn.commit()

    async def history(self, device_id: int, hours: int, max_points: int = 300) -> list[dict]:
        """Return (possibly downsampled) ping history for a device.

        For short windows every raw sample is returned. For long windows we
        bucket samples into `max_points` buckets and average each bucket, so
        a 30-day chart stays a small, fast payload instead of tens of
        thousands of rows.
        """
        cur = await self.conn.execute(
            """SELECT ts, success, latency_ms FROM pings
               WHERE device_id=? AND ts >= datetime('now', ?)
               ORDER BY ts ASC""",
            (device_id, f"-{hours} hours"),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if len(rows) <= max_points:
            return rows

        bucket_size = len(rows) / max_points
        buckets: list[dict] = []
        for i in range(max_points):
            start = int(i * bucket_size)
            end = int((i + 1) * bucket_size) or (start + 1)
            chunk = rows[start:end]
            if not chunk:
                continue
            latencies = [r["latency_ms"] for r in chunk if r["success"] and r["latency_ms"] is not None]
            buckets.append({
                "ts": chunk[len(chunk) // 2]["ts"],
                "success": 1 if latencies else 0,
                "latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
                "loss_pct": round(sum(1 for r in chunk if not r["success"]) / len(chunk) * 100, 1),
            })
        return buckets

    # ---------- alerts ----------

    async def create_alert(self, device_id: int, type_: str, message: str) -> dict:
        cur = await self.conn.execute(
            "INSERT INTO alerts (device_id, type, message) VALUES (?, ?, ?)",
            (device_id, type_, message),
        )
        await self.conn.commit()
        row = await self.conn.execute("SELECT * FROM alerts WHERE id=?", (cur.lastrowid,))
        return dict(await row.fetchone())

    async def resolve_alerts(self, device_id: int, type_: str):
        await self.conn.execute(
            """UPDATE alerts SET resolved=1, resolved_at=datetime('now')
               WHERE device_id=? AND type=? AND resolved=0""",
            (device_id, type_),
        )
        await self.conn.commit()

    async def list_alerts(self, unresolved_only: bool = False, limit: int = 200) -> list[dict]:
        query = """SELECT alerts.*, devices.name as device_name, devices.ip_address
                   FROM alerts JOIN devices ON devices.id = alerts.device_id"""
        if unresolved_only:
            query += " WHERE alerts.resolved=0"
        query += " ORDER BY alerts.created_at DESC LIMIT ?"
        cur = await self.conn.execute(query, (limit,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_alerts_since(self, alert_id: int, limit: int = 500, offset: int = 0) -> list[dict]:
        cur = await self.conn.execute(
            """SELECT alerts.*, devices.name as device_name, devices.ip_address
               FROM alerts
               JOIN devices ON devices.id = alerts.device_id
               WHERE alerts.id > ?
               ORDER BY alerts.id ASC
               LIMIT ? OFFSET ?""",
            (alert_id, limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---------- settings ----------

    async def get_setting(self, key: str, default: Any = None) -> Any:
        cur = await self.conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    async def set_setting(self, key: str, value: Any):
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        await self.conn.commit()

    # ---------- ports ----------

    async def list_ports(self, device_id: Optional[int] = None) -> list[dict]:
        if device_id is not None:
            cur = await self.conn.execute("SELECT * FROM ports WHERE device_id=? ORDER BY port", (device_id,))
        else:
            cur = await self.conn.execute("SELECT * FROM ports ORDER BY device_id, port")
        return [dict(r) for r in await cur.fetchall()]

    async def get_port(self, port_id: int) -> Optional[dict]:
        cur = await self.conn.execute("SELECT * FROM ports WHERE id=?", (port_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def create_port(self, device_id: int, port: int, label: str = "") -> dict:
        cur = await self.conn.execute(
            "INSERT INTO ports (device_id, port, label) VALUES (?, ?, ?)",
            (device_id, port, label),
        )
        await self.conn.commit()
        return await self.get_port(cur.lastrowid)

    async def delete_port(self, port_id: int):
        await self.conn.execute("DELETE FROM ports WHERE id=?", (port_id,))
        await self.conn.commit()

    # ---------- alert rules ----------

    async def list_rules(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM alert_rules ORDER BY created_at DESC")
        return [dict(r) for r in await cur.fetchall()]

    async def get_rule(self, rule_id: int) -> Optional[dict]:
        cur = await self.conn.execute("SELECT * FROM alert_rules WHERE id=?", (rule_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def create_rule(self, data: dict) -> dict:
        cur = await self.conn.execute(
            """INSERT INTO alert_rules
               (name, scope_type, scope_id, metric, operator, threshold, port, cooldown_minutes, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["name"], data.get("scope_type", "all"), data.get("scope_id"),
                data["metric"], data.get("operator", ">="), data.get("threshold"),
                data.get("port"), data.get("cooldown_minutes", 10), int(data.get("enabled", True)),
            ),
        )
        await self.conn.commit()
        return await self.get_rule(cur.lastrowid)

    async def update_rule(self, rule_id: int, data: dict) -> Optional[dict]:
        existing = await self.get_rule(rule_id)
        if not existing:
            return None
        merged = {**existing, **data}
        await self.conn.execute(
            """UPDATE alert_rules SET name=?, scope_type=?, scope_id=?, metric=?, operator=?,
               threshold=?, port=?, cooldown_minutes=?, enabled=? WHERE id=?""",
            (
                merged["name"], merged["scope_type"], merged["scope_id"], merged["metric"],
                merged["operator"], merged["threshold"], merged["port"],
                merged["cooldown_minutes"], int(merged["enabled"]), rule_id,
            ),
        )
        await self.conn.commit()
        return await self.get_rule(rule_id)

    async def delete_rule(self, rule_id: int):
        await self.conn.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,))
        await self.conn.commit()
