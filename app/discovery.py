import asyncio

async def ping_scan_host(ip: str) -> str | None:
    # Ultra-fast single-packet sweep check
    cmd = ["ping", "-c", "1", "-W", "1", ip]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL)
    await proc.communicate()
    return ip if proc.returncode == 0 else None


async def scan_subnet(subnet_prefix: str) -> list[str]:
    # Generates targets from .1 to .254
    tasks = [ping_scan_host(f"{subnet_prefix}.{i}") for i in range(1, 255)]
    results = await asyncio.gather(*tasks)
    return [ip for ip in results if ip is not None]