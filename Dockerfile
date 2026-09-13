# Step 1: Build stage
FROM python:3.13-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache gcc musl-dev libffi-dev

RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# Step 2: Final Runtime Stage (Optimized for deep network diagnostics)
FROM python:3.13-alpine AS runner

WORKDIR /app

# Install dependencies required for ping, traceroute, MTU checks, and port scanning
RUN apk add --no-cache iputils-ping traceroute iproute2 libcap

# Bring over virtual environment packages
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

COPY . .

RUN mkdir -p /app/data && chown -R guest:users /app/data
ENV NETMON_DB_PATH=/app/data/netmon.db
VOLUME /app/data

# CRITICAL FOR DIAGNOSTICS:
# Give network capabilities directly to the Python binary so your async workers
# can open raw sockets for MTU path discovery, subnet sweeps, and raw port handling
# under the secure non-root guest user.
RUN setcap cap_net_raw+ep /usr/local/bin/python3.13 || true
USER guest

EXPOSE 8000

CMD ["uvicorn main:app", "--host", "0.0.0.0", "--port", "8000"]
