from __future__ import annotations

import ipaddress
import socket
from urllib.parse import quote, urlparse

import dns.resolver
import dns.reversename
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/widgets/dns-email", tags=["widgets"])

DNSBL_ZONES = [
    "zen.spamhaus.org",
    "bl.spamcop.net",
    "dnsbl.sorbs.net",
]


class LookupIn(BaseModel):
    target: str


def _clean_target(value: str) -> str:
    target = value.strip()
    if not target:
        return ""
    parsed = urlparse(target)
    if parsed.hostname:
        return parsed.hostname.strip()
    return target


def _resolve_ip(target: str) -> tuple[str, str]:
    try:
        ip = str(ipaddress.ip_address(target))
        return target, ip
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(target, None, type=socket.SOCK_STREAM)
        for info in infos:
            candidate = info[4][0]
            try:
                ip = str(ipaddress.ip_address(candidate))
                return target, ip
            except ValueError:
                continue
    except socket.gaierror:
        pass
    raise HTTPException(400, "Invalid hostname or public IP address")


def _lookup_ptr(ip: str) -> str | None:
    try:
        reverse_name = dns.reversename.from_address(ip)
        answers = dns.resolver.resolve(reverse_name, "PTR")
        if answers:
            return str(answers[0]).rstrip(".")
    except Exception:
        return None
    return None


def _check_dnsbl(ip: str) -> list[dict]:
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return []

    if parsed.version != 4:
        return [
            {
                "zone": "N/A",
                "listed": False,
                "reason": "DNSBL check is currently IPv4 only",
            }
        ]

    reversed_ip = ".".join(reversed(ip.split(".")))
    results = []
    for zone in DNSBL_ZONES:
        query = f"{reversed_ip}.{zone}"
        try:
            dns.resolver.resolve(query, "A")
            results.append({"zone": zone, "listed": True, "reason": "Listed"})
        except dns.resolver.NXDOMAIN:
            results.append({"zone": zone, "listed": False, "reason": "Not listed"})
        except dns.resolver.NoAnswer:
            results.append({"zone": zone, "listed": False, "reason": "No A record"})
        except Exception:
            results.append({"zone": zone, "listed": False, "reason": "Lookup error"})
    return results


@router.post("/lookup")
async def lookup(payload: LookupIn):
    target = _clean_target(payload.target)
    if not target:
        raise HTTPException(400, "Please enter a hostname or public IP")

    hostname, ip = _resolve_ip(target)

    geo_data: dict | None = None
    geo_error: str | None = None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                f"https://ipinfo.io/{quote(ip)}/json",
            )
            response.raise_for_status()
            geo_data = response.json()
    except Exception:
        geo_error = "Geolocation/ASN lookup is unavailable right now"

    asn_value = None
    if geo_data and isinstance(geo_data.get("org"), str):
        parts = geo_data["org"].split(maxsplit=1)
        if parts and parts[0].upper().startswith("AS"):
            asn_value = parts[0].upper()

    lat = None
    lon = None
    if geo_data and isinstance(geo_data.get("loc"), str) and "," in geo_data["loc"]:
        left, right = geo_data["loc"].split(",", 1)
        lat = left.strip()
        lon = right.strip()

    ptr_value = _lookup_ptr(ip)
    dnsbl_results = _check_dnsbl(ip)

    return {
        "input": payload.target,
        "target": hostname,
        "ip": ip,
        "isp": geo_data.get("org") if geo_data else None,
        "asn": asn_value,
        "org": geo_data.get("org") if geo_data else None,
        "geolocation": {
            "country": geo_data.get("country") if geo_data else None,
            "region": geo_data.get("region") if geo_data else None,
            "city": geo_data.get("city") if geo_data else None,
            "lat": lat,
            "lon": lon,
            "error": geo_error,
        },
        "ptr": ptr_value,
        "blacklists": dnsbl_results,
        "links": {
            "whois": f"https://who.is/whois-ip/ip-address/{quote(ip)}",
            "bgp": f"https://bgp.he.net/ip/{quote(ip)}",
            "geo": f"https://ipinfo.io/{quote(ip)}",
            "reverse_dns": f"https://dnschecker.org/reverse-dns.php?query={quote(ip)}",
            "blacklist": f"https://mxtoolbox.com/SuperTool.aspx?action=blacklist%3a{quote(ip)}&run=toolpage",
        },
    }
