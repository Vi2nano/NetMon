# Step 1: Build dependencies using Python 3.13 Alpine
FROM python:3.13-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install compilation tools needed for lightning-fast build wheels if necessary
RUN apk add --no-cache gcc musl-dev libffi-dev

RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# Step 2: Final ultra-slim Python 3.13 runtime stage
FROM python:3.13-alpine AS runner

WORKDIR /app

# Install standard iputils-ping utility and libcap for non-root execution
RUN apk add --no-cache iputils-ping libcap

# Bring over pip modules built in stage 1
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

COPY . .

# Explicitly setup the data directory and grant the 'guest' user ownership 
# so it can write and read the SQLite netmon.db file.
RUN mkdir -p /app/data && chown -R guest:users /app/data
ENV NETMON_DB_PATH=/app/data/netmon.db
VOLUME /app/data

# Give standard system ping binary capabilities to open raw sockets safely,
# then drop all root user privileges entirely.
RUN setcap cap_net_raw+ep /usr/bin/ping || true
USER guest

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
