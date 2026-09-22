# Desk Dash

Desk Dash runs on **Capo-Bot** and serves a lightweight eight-page monitoring UI to a Galaxy tablet. Capo-Bot gathers its own Linux/NVIDIA/NVMe metrics, checks websites in the background, and polls read-only agents on other computers. The browser contacts only Capo-Bot:

```text
Second PC agent → Capo-Bot backend + SQLite → Galaxy tablet
```

The backend and UI use the Python standard library and plain JavaScript. No build step or cloud service is required. The target host is a Ryzen 5 7600 with 32 GB RAM, an RTX 3080 10 GB, and a Lexar NM790 1 TB NVMe. These hardware names are **detected at runtime**; they are not hardcoded into collectors. The separate Windows agent requires LibreHardwareMonitor for actual CPU/GPU temperatures.

## Update and inspect on Capo-Bot

```sh
cd ~/Desk_Dash
git pull origin main
python3 -m desk_dash.discover > discovery.json
python3 -m unittest discover -s tests -v
cp -n config.example.json config.json
cp -n .env.example .env
```

Review `discovery.json` locally. It is read-only and identifies CPU, RAM, GPU, sensors, NVMe, mounted filesystems, utilities, systemd candidates, Docker/PM2 apps, and Alfred processes. Do not publish it if it contains private host details. Existing `config.json` and `.env` files are ignored by Git; compare new example settings if upgrading.

For a manual run, set `DASH_HOST` to Capo-Bot's specific LAN IP and a long unique `DASH_PASSWORD` in `.env`, then:

```sh
chmod 600 .env
python3 -m desk_dash.server
```

Open `http://<CAPO-BOT-LAN-IP>:8765` on the tablet. `GET /health` reports backend readiness. The tablet does not need to stay connected for website or agent monitoring. The backend samples local metrics every ~10 seconds, websites every ~60 seconds, and remote agents every 15 seconds by default. The tablet fetches snapshots every 60 seconds and chart history every three minutes. Background updates change existing values in place without replaying entrance animations; the backend monitoring schedule stays independent of the tablet. SQLite keeps 30 days of samples, site checks, remote reports, and activity events. The first utilization/rate sample may be unavailable until the next sample.

### Start at boot with systemd

`deploy/desk-dash.service` is an example for `/opt/desk-dash`. Replace the username and paths if your actual checkout is `~/Desk_Dash`. Create a writable data directory, keep `.env` private, and edit `WorkingDirectory`, `ReadWritePaths`, and `ExecStart` before installing the unit:

```sh
sudo cp deploy/desk-dash.service /etc/systemd/system/desk-dash.service
sudo systemctl daemon-reload
sudo systemctl enable --now desk-dash.service
systemctl status desk-dash.service
journalctl -u desk-dash.service -n 50 --no-pager
```

The example runs as a non-root user and limits filesystem writes. If the storage test folder is outside its writable path, add that exact folder to `ReadWritePaths=`. Do not enable a unit with the placeholder username. No deployment or service restart on Capo-Bot was performed while developing this update.

## Websites

`config.example.json` includes Bellmore Web Design at `https://bellmorewebdesign.com` and an **UNCONFIGURED** Coursen entry. The Coursen domain was not provided, so enter its real health-check URL in `config.json` with `enabled: true`, or set `DASH_COURSEN_URL=<real URL>` in `.env` to enable the existing entry automatically. Add more entries to `sites` and restart the backend. No other sites are preconfigured.

Each website has `timeout_seconds`, `degraded_ms`, `expected_statuses`, `allow_redirects`, and `failure_threshold`. Redirects are followed by default (maximum five), and the final HTTP code is checked. An expected HTTP response counts as a successful check; slow responses show **DEGRADED**. With the default threshold of three, the first two failed checks show **DEGRADED**, and the third shows **OFFLINE**. A valid response recovers immediately. Each backend check writes its code, latency, outcome, timestamp, and status to SQLite. The UI shows last check, last accepted response, uptime from recorded checks, response history, consecutive failures, and outage/recovery events. Checks continue while the browser is closed.

## Read-only agents for other computers

Capo-Bot polls `GET /api/agent/v1` with a bearer token and accepts only versioned normalized JSON. The agent reports hostname, CPU/GPU model, temperature and utilization, RAM, VRAM, GPU power/fan if available, disks, uptime, interface throughput, and measurement time. The central server checks types and timestamps, recomputes temperature classifications, stores samples and check states, and never sends secrets to the tablet.

On a failed poll, **current temperatures disappear immediately**. The status moves through **DEGRADED**, **STALE** (after `stale_after_seconds`), then **OFFLINE** (after `offline_after_seconds`). The last successful report time stays visible. A delayed or malformed sample does not count as a successful report. Historic readings remain available in the Systems page graph and `GET /api/history?machine=<id>`; they are explicitly historic, not live. When communication recovers, the event feed records it.

On the remote computer, generate a different strong secret for each agent:

```sh
python3 -m desk_dash.agent --generate-token
```

Store that secret in the agent machine's `.env` as `DASH_AGENT_TOKEN=...`. Set `DASH_AGENT_HOST` to that PC's LAN IP, and optionally `DASH_AGENT_PORT=8766`. On Capo-Bot, store the same secret **only** in its `.env`, for example `DASH_AGENT_SECOND_TOKEN=...`, and add this to `remote_agents` in `config.json`:

```json
{
  "id": "second-pc",
  "name": "Second PC",
  "url": "http://192.168.1.60:8766",
  "token_env": "DASH_AGENT_SECOND_TOKEN",
  "stale_after_seconds": 45,
  "offline_after_seconds": 120,
  "timeout_seconds": 3
}
```

`remote_agents` is a JSON **array** of such objects. Keep the token out of `config.json` and Git. Restart Capo-Bot after edits. The agent serves only `/health` and `/api/agent/v1`; every POST returns 405. It has no remote control or command-execution endpoint. Put both machines on a trusted private LAN; bearer tokens travel in cleartext over plain HTTP unless you configure local TLS. Never port-forward either service.

### Linux second PC

Python 3.10+ and Linux `/proc`/`/sys` are required. From its Desk Dash checkout, create `.env` with the agent settings above, then run `python3 -m desk_dash.agent`. The example `deploy/desk-dash-agent.service` starts it at boot after you replace the username and paths. The agent does not need `config.json` or access to Capo-Bot's SQLite file. `python3 -m desk_dash.server --agent` remains a compatibility alias, but the dedicated module is preferred.

### Windows second PC and real sensor readings

The Windows agent uses Windows CIM/OS counters for CPU load, RAM, disks, uptime, processes, and network traffic. For **actual CPU/GPU temperatures** it reads LibreHardwareMonitor's local `data.json` sensor feed. It also uses `nvidia-smi` when available for NVIDIA GPU fields. If CPU or installed GPU temperature is unavailable, the central dashboard marks the machine **DEGRADED** rather than presenting OS-only counters as a complete sensor report.

1. Install Python 3.10+ on the Windows PC and obtain LibreHardwareMonitor from its [official GitHub releases](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases). Keep LibreHardwareMonitor running and verify its CPU and GPU temperature readings in the app.
2. Enable its built-in web server, set its **listener IP to `127.0.0.1`** and port to `8085`, and verify `http://127.0.0.1:8085/data.json` returns a sensor tree containing `RawValue` temperature readings. Do not leave the LibreHardwareMonitor server bound to all interfaces: its built-in HTTP server includes sensor-control endpoints. Desk Dash reads only `data.json` over loopback and never exposes that server to the LAN. Some sensors require LibreHardwareMonitor to run with administrator rights; assess that on the Windows PC.
3. From the Desk Dash checkout in PowerShell, run `py -3 -m desk_dash.agent --generate-token`. Create a private `.env` with `DASH_AGENT_TOKEN`, `DASH_AGENT_HOST=<WINDOWS-LAN-IP>`, `DASH_AGENT_PORT=8766`, and `DASH_LHM_URL=http://127.0.0.1:8085/data.json`. Run `py -3 -m desk_dash.agent`. Allow only Capo-Bot's LAN IP to reach port 8766 in Windows Firewall.
4. After verifying the CPU and GPU temperatures on Capo-Bot's Systems page, add a Task Scheduler task for `py.exe -3 -m desk_dash.agent`, working directory set to the checkout, delayed until after LibreHardwareMonitor starts at login. Store `.env` with Windows permissions restricted to your account.

LibreHardwareMonitor's official code documents the JSON endpoint and `RawValue` fields; its older WMI integration has had version-specific reports of missing sensors, so this agent uses the local JSON feed. No Windows hardware has been available in this development environment, so confirm the sensor names and hardware compatibility on the second PC before treating it as fully monitored.

## NVMe and storage test

The NVMe collector discovers real namespaces from `/sys/class/block`, then tries read-only `nvme smart-log -o json` on the namespace and controller, followed by `smartctl -j -a` and hwmon temperature. The Storage page shows available spare and threshold, wear percentage, power cycles/hours, unsafe shutdowns, media and error-log counts, warning/critical temperature time, composite and per-sensor temperatures, data read/written, filesystem capacity, and current throughput where available. NVMe data units are converted at **512,000 bytes each**. **Unsafe shutdowns alone do not mark a healthy drive as failed.** Critical warnings, insufficient spare, or media errors do affect the health label.

`sudo nvme smart-log /dev/nvme0` succeeding interactively does **not** mean a non-root systemd process can read SMART. Desk Dash never runs `sudo` from its web backend. If SMART is inaccessible to the service user, hwmon temperature still appears and the unavailable health fields remain clearly labeled; arrange the least privilege read access separately on Capo-Bot if needed.

The optional storage test remains disabled until `storage_test.directory` points to a writable directory on a **directly mounted NVMe filesystem**. It uses an exclusive temporary file, writes 16–256 MiB (default 128), `fsync`s, verifies a read-back hash, deletes the file, and enforces a cooldown of at least ten minutes (default one hour). It never writes to a block device or an existing file. Read throughput may come from page cache. A crash can leave a `.desk-dash-test-*.tmp` file for manual cleanup. No storage test was run while developing this update.

## Controls and security

Controls are separate from read-only agent collection. The dashboard uses a password, HttpOnly SameSite cookie, CSRF token, Origin check, confirmations and cooldowns. `controls.service_units` is the exact allowlist for systemd start/stop/restart; reboot and Wake-on-LAN are disabled until explicitly configured. The agent has no such endpoints. Neither service exposes arbitrary shell or PowerShell execution. Never publish it through port forwarding or a tunnel without redesigning authentication and transport security.

## Project structure and checks

- `desk_dash/agent.py`, `protocol.py`, `remotes.py`, `windows.py`: authenticated read-only agent, versioned schema, central polling/history, Windows sensor mapping.
- `deploy/windows-metrics.ps1`: read-only Windows OS counters; LibreHardwareMonitor provides temperatures.
- `desk_dash/collectors.py`, `checks.py`, `store.py`: local Linux/SMART collection, website checks, SQLite history.
- `desk_dash/server.py`, `actions.py`: dashboard API, scheduler, login, allowlisted actions.
- `static/`: no-build, touch-friendly UI with text status labels and reduced-motion support.
- `deploy/`: example Linux systemd units.

Run `python3 -m unittest discover -s tests -v` and (if Node is installed) `node --check static/app.js`. The automated tests cover protocol validation, remote stale/offline/recovery behavior, agent authentication, website failure thresholds, and SMART interpretation. Windows hardware and Capo-Bot-specific sensor permissions must be verified on those machines.
