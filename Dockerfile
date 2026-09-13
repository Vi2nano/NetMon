FROM python:3.13-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache gcc musl-dev libffi-dev

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.13-alpine AS runner

WORKDIR /app

# Install native tools required by the monitoring and diagnostic commands.
RUN apk add --no-cache iputils-ping traceroute libcap \
	&& addgroup -S netmon \
	&& adduser -S -G netmon netmon \
	&& setcap cap_net_raw+ep /bin/ping \
	&& setcap cap_net_raw+ep /usr/bin/traceroute

COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY . .

RUN mkdir -p /app/data && chown -R netmon:netmon /app/data
ENV NETMON_DB_PATH=/app/data/netmon.db
VOLUME /app/data

USER netmon

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
	CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/docs')"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
