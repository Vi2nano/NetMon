from __future__ import annotations

import asyncio
import socket
import ssl
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/widgets/cert-expiration", tags=["widgets"])


class CertCheckIn(BaseModel):
    hostname: str
    port: int = Field(default=443, ge=1, le=65535)


def _extract_name(parts: tuple[tuple[str, str], ...]) -> str:
    for part in parts:
        if part[0][0] == "commonName":
            return part[0][1]
    return "N/A"


def _check_cert_sync(hostname: str, port: int) -> dict:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((hostname, port), timeout=8.0) as sock:
        with context.wrap_socket(sock, server_hostname=hostname) as tls_sock:
            cert = tls_sock.getpeercert()

    not_before = datetime.strptime(cert["notBefore"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    not_after = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    days_remaining = int((not_after - now).total_seconds() // 86400)

    if days_remaining < 0:
        status = "expired"
    elif days_remaining < 30:
        status = "expiring_soon"
    else:
        status = "ok"

    return {
        "hostname": hostname,
        "port": port,
        "subject_cn": _extract_name(cert.get("subject", ())),
        "issuer_cn": _extract_name(cert.get("issuer", ())),
        "valid_from": not_before.isoformat(),
        "valid_until": not_after.isoformat(),
        "days_remaining": days_remaining,
        "status": status,
    }


@router.post("/check")
async def check_certificate(payload: CertCheckIn):
    hostname = payload.hostname.strip()
    if not hostname:
        raise HTTPException(400, "Hostname is required")

    try:
        return await asyncio.to_thread(_check_cert_sync, hostname, payload.port)
    except socket.gaierror:
        raise HTTPException(400, "Hostname could not be resolved")
    except TimeoutError:
        raise HTTPException(504, "Connection timed out while fetching certificate")
    except ssl.SSLError as exc:
        raise HTTPException(502, f"TLS handshake failed: {exc}")
    except ConnectionRefusedError:
        raise HTTPException(502, "Connection refused by target host/port")
    except OSError as exc:
        raise HTTPException(502, f"Certificate lookup failed: {exc}")
