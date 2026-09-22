# Desk Dash

A lightweight, local infrastructure dashboard for an Ubuntu host and an older Android tablet. Python standard library backend, SQLite history, and a static HTML/CSS/JavaScript frontend; no `npm install`, build step, or public cloud service. Works in landscape and portrait, with reduced-motion support. Metrics come from the **machine running the backend**, not the tablet.

## Host discovery first

Run on the RTX 3080 Ubuntu PC **before editing the host configuration**:

```sh
python3 -m desk_dash.discover > discovery.json
```

This read-only report lists CPU/RAM/GPU, NVIDIA driver, block devices and mount points, NVMe SMART data, sensors, interfaces, installed utilities, relevant systemd units, Docker/PM2 apps, and Alfred processes. Review it locally; do not publish the report if it contains host details you consider private. This repository was developed in a separate container, so your 3080/NVMe specifics cannot be asserted until the script runs there. It does not install or modify packages.

## Start locally

Requires Python 3.10+ and Linux with `/proc` and `/sys` available. NVIDIA, SMART and sensor utilities are optional. Missing capabilities display **UNAVAILABLE**.

```sh
git clone https://github.com/Bellmorewebdesign/Desk_Dash.git
cd Desk_Dash
cp config.example.json config.json
cp .env.example .env
python3 -m desk_dash.server
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) on the host. `GET /health` returns server readiness; `GET /api/snapshot`, `/api/history`, `/api/events` return normalized live and historical data. The first CPU/network sample may lack a rate until the next sample. Background samples run about every 10 seconds; website/remote/network probes about every 60 seconds. SQLite retains 30 days of checks, samples and events.

### Access from the Galaxy tablet

1. Set a long unique `DASH_PASSWORD` and `DASH_HOST` to your 3080 PC's **specific LAN IP** in `.env`. Example: `DASH_HOST=192.168.1.50`. Keep `.env` readable only by the service user: `chmod 600 .env`.
2. Start the server. Open `http://<3080-PC-LAN-IP>:8765` on the tablet's browser. Place the tablet and PC on your trusted private Wi-Fi/LAN. A private LAN password is sent over ordinary HTTP unless you add a local TLS reverse proxy; do not port-forward this service or expose it to the internet.
3. To keep the display awake, configure the tablet's screen/charging settings separately.

For read-only browsing on `127.0.0.1`, a password is optional. **All controls require a password and a signed-in session**, including refresh actions. Binding to a non-loopback address requires a password. The login uses a 30-day HttpOnly SameSite cookie and a separate CSRF token; action POSTs validate the exact Origin and rate limits. Sessions are kept in memory, so a server restart requires signing in again. Five failed login attempts from one IP temporarily block more attempts. The service writes no secrets to logs or API responses. If you change the password, restart the process (existing in-memory sessions then expire).

## Host configuration

`config.json` is local configuration; it can contain private agent tokens, so it is excluded from Git. Start from `config.example.json`.

### Websites

Edit `sites` entries with a stable `id`, display `name`, URL and allowed HTTP codes. The example includes Bellmore Web Design and a Coursen entry marked **UNCONFIGURED** because its actual public address has not been verified. To monitor Coursen, set its real URL and change `enabled` to `true`. Redirects count as healthy only when their code appears in `expected_statuses`; response times at or above `degraded_ms` (default 1500) show DEGRADED. A 4xx also shows DEGRADED, a network error or 5xx OFFLINE. Remove or add entries, then restart. No other sites are preconfigured. Uptime is computed from recorded checks; it starts empty and is not a historical SLA.

### Services and approved actions

Set `services` to the **actual systemd unit names** from your discovery report, for example `{"id":"alfred","name":"Alfred","unit":"alfred.service","scope":"system"}`. A process whose `/proc/<pid>/comm` contains Alfred is shown as discovered if present. Systemd exposes current PID, memory, cumulative CPU time and activation timestamps; CPU percentage is estimated from the change in a unit's cumulative CPU time between samples. Other Node/Python/PM2/Docker applications can be monitored by adding their **systemd wrapper unit**. Container/PM2 discovery is read-only during setup; the dashboard does not enumerate all host services.

`controls.service_units` is a separate list of exact **system** unit names allowed for start/stop/restart. By default it is empty, as are Wake-on-LAN targets; reboot is disabled. Every action validates this list on the server. The `desk-dash` user needs appropriate permissions to manage an approved unit. Prefer a narrowly scoped policy; do not run Desk Dash as root merely to make every action work. User units can be monitored with `scope: "user"` but control actions currently target system units. Configure `controls.wake_targets` with `id`, colon-delimited `mac` and optional `broadcast`. Reboot requires `allow_reboot: true` plus appropriate systemd permissions. Confirm dialogs, a server-side confirmation phrase and cooldowns protect every sensitive action. **No arbitrary shell API exists.**

### NVMe SMART and storage test

NVMe namespaces are discovered from `/sys/class/block`, with directly mounted partitions mapped through `/proc/mounts`. SMART uses `nvme smart-log -o json` when available, then `smartctl -j -a`, then hwmon temperature. Without these tools or access, some fields say UNAVAILABLE; choose read-only tooling/permissions according to your OS. Drive health, wear, errors, power-on hours, temperatures, mount capacity and `/proc/diskstats` rates are displayed when possible. A SMART `data_units_read` or `data_units_written` value is a drive unit, typically 512,000 bytes, **not a byte count**.

The storage test remains disabled until `storage_test.directory` points to a writable directory **on a directly mounted detected NVMe partition**, such as `/mnt/my-nvme/desk-dash-test`. It rejects overlay mounts, a different filesystem, arbitrary devices, LVM/crypt paths it cannot verify, invalid sizes, and low free space. The browser previews exact drive, directory and size before asking for confirmation. The backend exclusively creates a new temporary file, writes a random 1 MiB block repeatedly (default 128 MiB; configurable 16–256 MiB), calls `fsync`, hashes the write and read, and unlinks the file in `finally`. It never writes to a block device or an existing filename. An SQLite-backed cooldown defaults to one hour and cannot be set below ten minutes. A real test still causes a limited amount of SSD wear. Read speed is an approximation and may reflect Linux page cache, **not raw disk read performance**. A machine that crashes mid-test may leave a `.desk-dash-test-*.tmp` file in the configured directory; remove it manually after verifying no test is active. No real test is run as part of development or automated tests.

### Second PC

On the second Linux machine, use this same project with `.env` containing `DASH_AGENT_TOKEN=<long-random-token>`, its LAN-specific `DASH_HOST`, and `DASH_PORT=8765`; run `python3 -m desk_dash.server --agent`. Configure `remote_agents` on the central host:

```json
[{"name":"Second PC","url":"http://192.168.1.60:8765","token":"same-long-random-token"}]
```

The host polls `/api/agent` every minute. The token is sent over HTTP on your LAN; use local TLS or a trusted network if needed. The agent serves only `/health` and `/api/agent`; it has no control UI. A Windows PC would require a separate Windows collector; this agent reads Linux `/proc` and `/sys`.

## systemd on the 3080 host

After copying or cloning the project into `/opt/desk-dash`, set ownership to the intended non-root Linux user. Copy example config/environment files and set host IP/password as above. The example unit assumes data in `/opt/desk-dash/data`; create it with write permission for that user. Edit `deploy/desk-dash.service` to replace `REPLACE_WITH_LINUX_USERNAME` and `WorkingDirectory`/`ReadWritePaths` if the project lives elsewhere. Install the unit with administrator privileges:

```sh
sudo cp deploy/desk-dash.service /etc/systemd/system/desk-dash.service
sudo systemctl daemon-reload
sudo systemctl enable --now desk-dash.service
systemctl status desk-dash.service
journalctl -u desk-dash.service -n 50 --no-pager
```

The sample uses filesystem hardening and grants write access only to the data folder; the storage test folder may need an additional `ReadWritePaths=` entry. It does not grant service-control privileges. The optional `deploy/desk-dash-agent.service` is for the second PC.

## Project structure

- `desk_dash/collectors.py`: read-only CPU, GPU, RAM, NVMe, sensor, network and systemd collectors.
- `desk_dash/checks.py`: backend website probes and redirect classification.
- `desk_dash/store.py`: SQLite checks, samples, activity and cooldowns.
- `desk_dash/actions.py`: allowlisted controls and limited temporary-file NVMe test.
- `desk_dash/server.py`: HTTP API, login, CSRF, scheduling and static file server.
- `desk_dash/discover.py`: read-only host discovery report.
- `static/`: no-build tablet UI with eight pages.
- `deploy/`: example systemd units.

Development checks: `python3 -m unittest discover -s tests -v`, `node --check static/app.js` (if Node is installed), and `python3 -m desk_dash.server` then open the dashboard. No third-party packages are required.
