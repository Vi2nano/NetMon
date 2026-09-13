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

## 🚀 Getting Started

### 1. Prerequisites
- **Python 3.9+**
- **pip** (Python package installer)

### 2. Installation
Clone your repository and navigate into the project directory:
```bash
git clone <your-repo-url>
cd netmon
```

Install the required lightweight package dependencies:
```bash
pip install fastapi httpx pydantic uvicorn
```
*(Note: Ensure you install any additional dependencies required by your local database or monitoring files, such as `aiosqlite` if using an async SQLite driver).*

### 3. Environment Configuration (Optional)
By default, NetMon initializes its database path at `data/netmon.db` inside your application directory. You can easily override this path to store data on persistent server volumes using environment variables:

```bash
# On Linux/macOS
export NETMON_DB_PATH="/var/lib/netmon/production.db"

# On Windows (PowerShell)
\$env:NETMON_DB_PATH="C:\netmon\data\production.db"
```

### 4. Running the Server
Launch the asynchronous ASGI application layout via Uvicorn:
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open your browser and navigate to `http://localhost:8000` to access the dashboard.

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

