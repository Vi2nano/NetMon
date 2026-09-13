# NetMon 📡

A lightweight, self-hosted network monitoring and IP tracking dashboard designed specifically for network administrators who need a fast, dependable, and configurable tool without the bloat or cost of heavy enterprise monitoring suites. 

NetMon provides real-time latency, packet loss metrics, and instant alert handling via a highly responsive FastAPI backend, SQLite data logging, and an active WebSocket-driven frontend.

---

## ✨ Features

- **Zero Bloat Monitoring:** Keep tabs on hostnames, raw IP addresses, public DNS targets (like `8.8.8.8` and `1.1.1.1`), or local gateway routing without dealing with complicated agent installations.
- **Real-Time Data Streaming:** Leverages WebSockets to push live snapshots, latency statistics, and diagnostic metrics directly to your browser instantly—no manual refreshing required.
- **Fail-Safe Target Validation:** When adding a new device, NetMon performs an isolated HTTP handshake checking for reachability, parsing clean hostnames/IPs automatically while ensuring that non-web assets (like pure ICMP nodes) are still safely registered.
- **Self-Healing Database Management:** Auto-seeds default deletable testing endpoints on its first run if the environment is empty, and runs an asynchronous background pruning routine every 6 hours to clear historical logs older than 30 days.
- **Custom Threshold Alerts & Webhooks:** Configurable latency thresholds (ms) and packet loss parameters (%) per device to dispatch system logs and trigger external communication streams.

---

## 🛠️ Architecture

NetMon is structured cleanly to ensure low resource overhead, making it perfect to run continuously in the background of your workspace, a local server, or a small VM:

```text
├── main.py            # FastAPI main entry point, schema models, and REST/WS routing
├── alerts.py          # Alert evaluation engine and webhook dispatcher
├── database.py        # SQLite management layer and historical log persistence
├── monitor.py         # Asynchronous monitoring engine handling polling loops
└── frontend/          # Single-page UI asset directory
    ├── index.html     # Real-time monitoring control panel
    └── static/        # Modular dashboard styling and script assets
```

---

## 🚀 Getting Started - Prerequisites
- **[Docker Engine and Docker CLI](https://docs.docker.com/engine/install/)**
    - [Docker installation guide](https://docs.docker.com/engine/install/)
    - [Post-install steps for running Docker as your user](https://docs.docker.com/engine/install/linux-postinstall/)
- **[Git](https://git-scm.com/downloads)**, only if you plan to build the image from source

Python, `ping`, and `traceroute` are included in the Docker image and do not
need to be installed on the host. Docker Compose is not required.


### Docker Installation

#### Install the published image

Pull the latest image from GitHub Container Registry:

```bash
docker pull ghcr.io/vi2nano/netmon:latest
```

#### Run the container

Create a persistent SQLite volume and start NetMon:

```bash
docker volume create netmon-data
docker run -d \
    --name netmon \
    --restart unless-stopped \
    -p 8000:8000 \
    -v netmon-data:/app/data \
    ghcr.io/vi2nano/netmon:latest
```

Open `http://localhost:8000` in your browser. The image includes `ping` and
`traceroute`, runs as a non-root user, and stores the database in `/app/data`.


#### Build locally from the source

Clone the repository and enter its directory:

```bash
git clone https://github.com/Vi2nano/NetMon.git
cd NetMon
```

Build the image from the repository root:

```bash
docker build -t netmon:local .
```

Run it with a persistent SQLite volume:

```bash
docker volume create netmon-data
docker run -d \
    --name netmon \
    --restart unless-stopped \
    -p 8000:8000 \
    -v netmon-data:/app/data \
    netmon:local
```

Open `http://localhost:8000`. The image includes `ping` and `traceroute`,
runs as a non-root user, and stores the database in `/app/data`.

On Linux, host networking can help with local-LAN discovery and diagnostics:

```bash
docker run -d \
    --name netmon \
    --restart unless-stopped \
    --network host \
    -v netmon-data:/app/data \
    netmon:local
```

With host networking, access the dashboard at `http://localhost:8000` and do
not also publish `-p 8000:8000`.

---

## ⚙️ Configuration & API Usage

NetMon exposes a fully documented REST API alongside its real-time WebSocket channel. You can view the automated interactive OpenAPI documentation directly at `http://localhost:8000/docs`.

### Primary Endpoints
* **`GET /api/devices`**: Fetches configuration profiles for all monitored hardware coupled with dynamic live performance runtime statistics.
* **`POST /api/devices`**: Safely register a new endpoint with custom urgency parameters (`high`, `normal`, `low`), max latency thresholds, and target drop-rate boundaries.
* **`GET /api/alerts`**: Query active or past network outages and threshold breaches.
* **`WS /ws`**: Establish a bidirectional WebSocket session for zero-latency metric broadcasts.

---

## 🔒 License

Distributed under the **GNU GPLv3 License**. See the `LICENSE` file in the root of this repository for full copyleft terms and conditions regarding modification and distribution.

## Example Main Overview
<img width="1542" height="650" alt="MainOverview" src="https://github.com/user-attachments/assets/f3095a15-fe42-4ded-b09c-26c150637d19" />

## Example Device Flyout/Manual Testing
<img width="606" height="1308" alt="image" src="https://github.com/user-attachments/assets/d40ab4d0-107c-4a22-a323-825b83884d50" />

## Example Add Device
<img width="1602" height="886" alt="AddDevice" src="https://github.com/user-attachments/assets/cca76e71-ce74-4aba-ab74-92ad2dbf2152" />

## Example Priorities Selector
<img width="1696" height="967" alt="Priorities" src="https://github.com/user-attachments/assets/14622018-3fa1-4053-8169-9951484b1783" />

## Example Settings/WebHook Entry
<img width="996" height="975" alt="image" src="https://github.com/user-attachments/assets/4337273f-9994-46ea-95c4-24dd60f4d299" />

## Example Alerts
<img width="2445" height="822" alt="image" src="https://github.com/user-attachments/assets/5452ce65-e3d4-42d6-955f-60abe42c8c41" />



