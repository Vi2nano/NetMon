"""
Cross-platform single-ping helper.

Rather than using a raw-socket ICMP library (which needs admin/root
privileges or special capabilities on most OSes), we shell out to the
system's own `ping` binary, which already has whatever privilege it needs
out of the box on Windows, macOS, and Linux. We parse its human-readable
output with a regex and enforce our own timeout with asyncio, since the
`ping` binary's own timeout flag differs in units across platforms
(seconds on Linux, milliseconds on macOS/Windows).
"""
from __future__ import annotations

import asyncio
import platform
import re
import time
from dataclasses import dataclass

# Matches "time=23.4 ms", "time=23.4ms", "time<1ms", "time=1ms" (Win/macOS/Linux variants)
_LATENCY_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)

_IS_WINDOWS = platform.system().lower() == "windows"


@dataclass
class PingResult:
    success: bool
    latency_ms: float | None
    error: str | None = None


def _build_command(ip: str, timeout_s: float) -> list[str]:
    if _IS_WINDOWS:
        # -n 1: one echo request, -w: timeout in milliseconds
        return ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), ip]
    system = platform.system().lower()
    if system == "darwin":
        # macOS ping: -W is in milliseconds
        return ["ping", "-c", "1", "-W", str(int(timeout_s * 1000)), ip]
    # Linux / other unix: -W is in seconds
    return ["ping", "-c", "1", "-W", str(max(1, int(round(timeout_s)))), ip]


async def ping_once(ip: str, timeout_s: float = 2.0) -> PingResult:
    """Run exactly one ICMP echo request against `ip` and return the result.

    Always enforces `timeout_s` (+1s grace) via asyncio.wait_for regardless
    of whether the platform's own ping timeout flag behaved as expected, so
    a single slow/hung ping can never stall the monitoring loop.
    """
    cmd = _build_command(ip, timeout_s)
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return PingResult(False, None, error="ping binary not found on this system")

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 1)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return PingResult(False, None, error="timeout")

    output = stdout.decode(errors="ignore")

    if proc.returncode != 0:
        return PingResult(False, None, error="unreachable")

    match = _LATENCY_RE.search(output)
    if match:
        return PingResult(True, float(match.group(1)))

    # Succeeded (return code 0) but we couldn't parse a latency out of the
    # output (unusual, but seen with some ping localizations) — fall back
    # to measured wall-clock elapsed time as an approximation.
    elapsed_ms = (time.monotonic() - start) * 1000
    return PingResult(True, round(elapsed_ms, 2))


def compute_jitter_ms(latencies: list[float]) -> float:
    """Mean absolute difference between consecutive latency samples.

    This is the same "mean interpacket delay variation" idea used by
    RFC 3550 jitter, simplified for discrete ping samples rather than an
    RTP stream.
    """
    if len(latencies) < 2:
        return 0.0
    diffs = [abs(latencies[i] - latencies[i - 1]) for i in range(1, len(latencies))]
    return round(sum(diffs) / len(diffs), 2)


def compute_loss_pct(results: list[bool]) -> float:
    """Percentage of failed pings in a rolling window of success/fail flags."""
    if not results:
        return 0.0
    fails = sum(1 for ok in results if not ok)
    return round(fails / len(results) * 100, 1)


# ---------------------------------------------------------------------------
# TCP port checks
# ---------------------------------------------------------------------------

async def tcp_port_check(ip: str, port: int, timeout_s: float = 3.0) -> PingResult:
    """Attempt a raw TCP connect to (ip, port). Success means something is
    listening and accepting connections."""
    start = time.monotonic()
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout_s
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        return PingResult(True, round(elapsed_ms, 2))
    except (asyncio.TimeoutError, OSError) as e:
        return PingResult(False, None, error=str(e) or "connection failed")
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Path MTU discovery (best-effort, via DF-bit ping + binary search)
# ---------------------------------------------------------------------------

_IP_ICMP_HEADER_BYTES = 28  # 20-byte IP header + 8-byte ICMP header


def _build_mtu_probe_command(ip: str, payload_size: int, timeout_s: float) -> list[str]:
    if _IS_WINDOWS:
        return ["ping", "-f", "-n", "1", "-l", str(payload_size), "-w", str(int(timeout_s * 1000)), ip]
    system = platform.system().lower()
    if system == "darwin":
        return ["ping", "-D", "-c", "1", "-s", str(payload_size), "-t", str(max(1, int(round(timeout_s)))), ip]
    return ["ping", "-M", "do", "-c", "1", "-s", str(payload_size), "-W", str(max(1, int(round(timeout_s)))), ip]


async def _mtu_probe(ip: str, mtu_candidate: int) -> bool:
    payload = max(mtu_candidate - _IP_ICMP_HEADER_BYTES, 8)
    cmd = _build_mtu_probe_command(ip, payload, 1.5)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except FileNotFoundError:
        return False
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.5)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return False
    return proc.returncode == 0 and bool(_LATENCY_RE.search(stdout.decode(errors="ignore")))


async def discover_path_mtu(ip: str, low: int = 512, high: int = 1500) -> dict:
    """Best-effort path MTU discovery via DF-flagged pings and binary search.

    Returns the largest MTU (including the 28-byte IP+ICMP header overhead)
    for which an unfragmented probe still got a reply. This is a heuristic:
    some routers silently drop oversized packets instead of sending back
    "fragmentation needed", which looks identical to ordinary packet loss
    to this technique — treat the result as indicative, not authoritative.
    """
    if not await _mtu_probe(ip, low):
        return {
            "ok": False,
            "mtu": None,
            "tested_range": [low, high],
            "output": "No reply even at the minimum test size (~512 bytes) — check connectivity to this host, "
                      "or confirm the `ping` binary has permission to send raw ICMP in this environment.",
        }

    lo, hi, best = low, high, low
    while lo <= hi:
        mid = (lo + hi) // 2
        if await _mtu_probe(ip, mid):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return {
        "ok": True,
        "mtu": best,
        "tested_range": [low, high],
        "output": f"Path MTU is {best} bytes (tested {low}-{high} bytes via DF-flagged probes).",
    }


# ---------------------------------------------------------------------------
# Traceroute
# ---------------------------------------------------------------------------

_HOP_LINE_RE = re.compile(r"^\s*(\d+)\s+(.*)$")
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_RTT_TOKEN_RE = re.compile(r"([\d.]+)\s*ms")


def _build_traceroute_command(ip: str, max_hops: int, timeout_s: float) -> list[str]:
    if _IS_WINDOWS:
        return ["tracert", "-h", str(max_hops), "-w", str(int(timeout_s * 1000)), ip]
    return ["traceroute", "-m", str(max_hops), "-w", str(max(1, int(round(timeout_s)))), ip]


def _parse_traceroute_output(output: str) -> list[dict]:
    hops = []
    for line in output.splitlines():
        m = _HOP_LINE_RE.match(line)
        if not m:
            continue
        hop_num = int(m.group(1))
        rest = m.group(2)
        ip_match = _IP_RE.search(rest)
        rtts = [float(x) for x in _RTT_TOKEN_RE.findall(rest)]
        hops.append({
            "hop": hop_num,
            "address": ip_match.group(1) if ip_match else None,
            "rtt_ms": round(sum(rtts) / len(rtts), 2) if rtts else None,
            "timeout": not rtts and not ip_match,
        })
    return hops


def _format_hops_as_text(hops: list[dict]) -> str:
    lines = []
    for h in hops:
        addr = "* * *" if h["timeout"] else (h["address"] or "unknown")
        rtt = f"{h['rtt_ms']:.1f} ms" if h["rtt_ms"] is not None else "—"
        lines.append(f"{h['hop']:>2}  {addr:<20} {rtt}")
    return "\n".join(lines) if lines else "No hops were returned."


async def run_traceroute(ip: str, max_hops: int = 20) -> dict:
    """Run a real traceroute/tracert and return both parsed hops and a
    plain-text rendering. Handles the binary being missing entirely (common
    in slim Docker base images) instead of letting the exception propagate
    into a bare 500 error.
    """
    cmd = _build_traceroute_command(ip, max_hops, timeout_s=2.0)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        tool = "tracert" if _IS_WINDOWS else "traceroute"
        return {
            "ok": False,
            "hops": [],
            "output": (
                f"'{tool}' is not installed in this environment. If you're running in Docker, "
                f"add it to your image, e.g. `apt-get install -y traceroute` (Debian/Ubuntu base) "
                f"or `apk add traceroute` (Alpine)."
            ),
        }

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max_hops * 3 + 10)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return {"ok": False, "hops": [], "output": "Traceroute timed out."}

    output = stdout.decode(errors="ignore")
    hops = _parse_traceroute_output(output)
    if not hops:
        err = stderr.decode(errors="ignore").strip()
        return {"ok": False, "hops": [], "output": err[:500] if err else "No hops were returned."}
    return {"ok": True, "hops": hops, "output": _format_hops_as_text(hops)}
