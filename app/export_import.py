"""Import/export configuration data (groups, devices, ports, alert rules)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .database import Database


class ExportImport:
    """Handles configuration export and import operations."""

    @staticmethod
    async def export_config(db: Database) -> dict[str, Any]:
        """Export all configuration as a dictionary.
        
        Does NOT export:
        - Ping history (pings table)
        - Alert history (alerts table)
        - Settings (webhook URLs, etc.)
        
        Exports:
        - Groups
        - Devices
        - Ports
        - Alert rules
        """
        groups = await db.list_groups()
        devices = await db.list_devices()
        ports = await db.list_ports()  # All ports
        rules = await db.list_rules()

        return {
            "version": "1.0",
            "exported_at": datetime.utcnow().isoformat(),
            "groups": [
                {
                    "name": g["name"],
                }
                for g in groups
            ],
            "devices": [
                {
                    "name": d["name"],
                    "ip_address": d["ip_address"],
                    "group_name": None,  # Will be filled below
                    "priority": d["priority"],
                    "enabled": bool(d["enabled"]),
                    "latency_threshold_ms": d["latency_threshold_ms"],
                    "loss_threshold_pct": d["loss_threshold_pct"],
                    "latency_alert_consecutive_packets": d["latency_alert_consecutive_packets"],
                }
                for d in devices
            ],
            "ports": [
                {
                    "device_name": None,  # Will be filled below
                    "port": p["port"],
                    "label": p["label"],
                    "enabled": bool(p["enabled"]),
                }
                for p in ports
            ],
            "alert_rules": [
                {
                    "name": r["name"],
                    "scope_type": r["scope_type"],
                    "scope_id_name": None,  # Will be filled below
                    "metric": r["metric"],
                    "operator": r["operator"],
                    "threshold": r["threshold"],
                    "port": r["port"],
                    "cooldown_minutes": r["cooldown_minutes"],
                    "enabled": bool(r["enabled"]),
                }
                for r in rules
            ],
        }

    @staticmethod
    async def import_config(
        db: Database, config: dict[str, Any], mode: str = "merge"
    ) -> dict[str, Any]:
        """Import configuration from a dictionary.
        
        Args:
            db: Database instance
            config: Configuration dictionary (from export_config)
            mode: "merge" (add new, skip existing) or "replace" (clear and reimport)
        
        Returns:
            Dictionary with import statistics and any errors.
        """
        if mode not in ("merge", "replace"):
            raise ValueError("mode must be 'merge' or 'replace'")

        stats = {
            "groups_created": 0,
            "groups_skipped": 0,
            "devices_created": 0,
            "devices_skipped": 0,
            "ports_created": 0,
            "ports_skipped": 0,
            "rules_created": 0,
            "rules_skipped": 0,
            "errors": [],
        }

        # Validate version
        version = config.get("version", "1.0")
        if version != "1.0":
            raise ValueError(f"Unsupported config version: {version}")

        if mode == "replace":
            # Delete existing config (not history)
            existing_rules = await db.list_rules()
            for rule in existing_rules:
                await db.delete_rule(rule["id"])

            existing_ports = await db.list_ports()
            for port in existing_ports:
                await db.delete_port(port["id"])

            existing_devices = await db.list_devices()
            for device in existing_devices:
                await db.delete_device(device["id"])

            existing_groups = await db.list_groups()
            for group in existing_groups:
                await db.delete_group(group["id"])

        # Import groups
        group_name_map: dict[str, int] = {}
        for group_data in config.get("groups", []):
            try:
                existing = await db.list_groups()
                if any(g["name"] == group_data["name"] for g in existing):
                    stats["groups_skipped"] += 1
                    group_name_map[group_data["name"]] = next(
                        g["id"] for g in existing if g["name"] == group_data["name"]
                    )
                else:
                    g = await db.create_group(group_data["name"])
                    group_name_map[group_data["name"]] = g["id"]
                    stats["groups_created"] += 1
            except Exception as e:
                stats["errors"].append(f"Error importing group '{group_data['name']}': {e}")

        # Import devices
        device_name_map: dict[str, int] = {}
        for device_data in config.get("devices", []):
            try:
                existing = await db.list_devices()
                if any(d["ip_address"] == device_data["ip_address"] for d in existing):
                    stats["devices_skipped"] += 1
                    device_name_map[device_data["name"]] = next(
                        d["id"] for d in existing if d["ip_address"] == device_data["ip_address"]
                    )
                else:
                    group_id = None
                    if device_data.get("group_name"):
                        group_id = group_name_map.get(device_data["group_name"])

                    device = await db.create_device({
                        "name": device_data["name"],
                        "ip_address": device_data["ip_address"],
                        "group_id": group_id,
                        "priority": device_data.get("priority", "normal"),
                        "enabled": device_data.get("enabled", True),
                        "latency_threshold_ms": device_data.get("latency_threshold_ms", 200),
                        "loss_threshold_pct": device_data.get("loss_threshold_pct", 20),
                        "latency_alert_consecutive_packets": device_data.get("latency_alert_consecutive_packets", 1),
                    })
                    device_name_map[device_data["name"]] = device["id"]
                    stats["devices_created"] += 1
            except Exception as e:
                stats["errors"].append(f"Error importing device '{device_data['name']}': {e}")

        # Import ports
        for port_data in config.get("ports", []):
            try:
                device_id = device_name_map.get(port_data["device_name"])
                if not device_id:
                    stats["ports_skipped"] += 1
                    continue

                existing = await db.list_ports(device_id)
                if any(p["port"] == port_data["port"] for p in existing):
                    stats["ports_skipped"] += 1
                else:
                    await db.create_port(device_id, port_data["port"], port_data.get("label", ""))
                    stats["ports_created"] += 1
            except Exception as e:
                stats["errors"].append(f"Error importing port {port_data.get('port')} for device '{port_data.get('device_name')}': {e}")

        # Import alert rules
        for rule_data in config.get("alert_rules", []):
            try:
                scope_id = None
                if rule_data.get("scope_type") == "group":
                    scope_id = group_name_map.get(rule_data.get("scope_id_name"))
                elif rule_data.get("scope_type") == "device":
                    scope_id = device_name_map.get(rule_data.get("scope_id_name"))

                rule = await db.create_rule({
                    "name": rule_data["name"],
                    "scope_type": rule_data.get("scope_type", "all"),
                    "scope_id": scope_id,
                    "metric": rule_data["metric"],
                    "operator": rule_data.get("operator", ">="),
                    "threshold": rule_data.get("threshold"),
                    "port": rule_data.get("port"),
                    "cooldown_minutes": rule_data.get("cooldown_minutes", 10),
                    "enabled": rule_data.get("enabled", True),
                })
                stats["rules_created"] += 1
            except Exception as e:
                stats["errors"].append(f"Error importing rule '{rule_data.get('name')}': {e}")

        return stats
