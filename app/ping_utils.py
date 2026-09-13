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
