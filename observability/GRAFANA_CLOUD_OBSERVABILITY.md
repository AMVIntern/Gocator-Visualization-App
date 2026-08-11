# CHEP PQAS Gocator — Centralized Observability with Grafana Cloud

**Goal:** aggregate logs and operational metrics from the Gocator Live Inspector
app running on **4 independent Windows PCs at 4 factory sites** into a single
free **Grafana Cloud** stack, with a **site dropdown** and an **All-sites** view.

**Key constraint honoured:** `App.py` is **not modified**. Grafana Alloy only
*reads* the two files the app already writes — `monitor.log` and
`shift_state.json`.

---

## Table of contents

1. [Architecture](#1-architecture)
2. [Folder structure](#2-folder-structure)
3. [Data contract (what the app already emits)](#3-data-contract)
4. [Grafana Alloy configuration](#4-grafana-alloy-configuration)
5. [Windows installation steps](#5-windows-installation-steps)
6. [Windows service setup](#6-windows-service-setup)
7. [Grafana Cloud setup](#7-grafana-cloud-setup)
8. [Dashboard configuration](#8-dashboard-configuration)
9. [PromQL queries](#9-promql-queries)
10. [LogQL queries](#10-logql-queries)
11. [Alerting](#11-alerting)
12. [Rollout plan for 4 sites](#12-rollout-plan-for-4-sites)
13. [Security recommendations](#13-security-recommendations)
14. [Troubleshooting guide](#14-troubleshooting-guide)

---

## 1. Architecture

```
  ┌──────────────────────── Factory Site (×4) ────────────────────────┐
  │  Windows PC                                                        │
  │                                                                    │
  │   App.py  ──writes──►  monitor.log        (events, verdicts,       │
  │     │                                       errors, shift changes) │
  │     └─────writes──►   shift_state.json    (shift, total, assured)  │
  │                                                                    │
  │                       ▲ reads only (App.py untouched)              │
  │                       │                                            │
  │              ┌────────┴─────────┐                                  │
  │              │  Grafana Alloy   │  (single agent, Windows service) │
  │              │  - tail logs     │                                  │
  │              │  - parse JSON →  │                                  │
  │              │    Prom gauges   │                                  │
  │              │  - windows_exp.  │  (heartbeat / host health)       │
  │              └────────┬─────────┘                                  │
  └───────────────────────┼────────────────────────────────────────── ┘
                          │  Outbound HTTPS :443 only (no inbound)
                          ▼
            ┌───────────────────────────────┐
            │        Grafana Cloud (Free)    │
            │  ┌─────────┐    ┌────────────┐ │
            │  │  Loki   │    │ Prometheus │ │
            │  │ (logs)  │    │ (metrics)  │ │
            │  └────┬────┘    └─────┬──────┘ │
            │       └──────┬────────┘        │
            │         Grafana (dashboards,   │
            │         var: site, alerts)     │
            └───────────────────────────────┘
```

**Why Grafana Alloy (single agent) instead of Promtail + Telegraf:**

- One binary, one Windows service, one config file per PC — less to install,
  patch, and troubleshoot across 4 unattended factory machines.
- Alloy natively does both **log shipping** (replaces Promtail) and
  **Prometheus scraping / remote_write + `windows_exporter`** (replaces
  Telegraf), and can derive **Prometheus metrics from log/JSON pipelines** via
  `loki.process` → `stage.metrics`.
- All traffic is **outbound HTTPS on 443**; nothing listens for inbound
  connections from the internet. Alloy's local UI (`127.0.0.1:12345`) is
  loopback-only.

**How metrics are produced without touching App.py:** `shift_state.json` is the
authoritative source for the cumulative counters. Alloy tails it as a one-line
stream, `json`-parses it, and emits Prometheus **gauges**
(`gocator_total_pallets`, `gocator_assured_pallets`). The **assured %** is
computed in Grafana from those two gauges, so it never drifts from the raw
counts.

---

## 2. Folder structure

Added to the repo (no existing files changed):

```
observability/
├── GRAFANA_CLOUD_OBSERVABILITY.md      ← this document
├── alloy/
│   └── config.alloy                    ← the single Alloy config (all 4 PCs share it)
├── install/
│   └── install-alloy.ps1               ← per-PC installer (sets env + service)
└── dashboards/
    └── gocator-fleet.json              ← import into Grafana (section 8)
```

On each PC after install:

```
C:\Program Files\GrafanaLabs\Alloy\
├── alloy-windows-amd64.exe
└── config.alloy                        ← copied from observability/alloy/config.alloy
```

Per-site identity and Cloud credentials live in **machine environment
variables** (set by the installer), so the one `config.alloy` is identical on
every PC — only the env vars differ.

---

## 3. Data contract

Alloy depends on the exact formats `App.py` already produces. **Do not change
these without updating `config.alloy` and the queries below.**

### `monitor.log`

Logger format is `"%(asctime)s %(levelname)s %(message)s"` via a
`RotatingFileHandler` (10 MB × 5 backups). Example lines:

```
2026-06-22 14:03:21,123 INFO Combined verdict: top=1 bottom=1 -> Assured | Total: 305  Assured: 92 (30%)
2026-06-22 14:03:24,500 INFO Result top top_000305.csv -> 1
2026-06-22 14:05:00,007 INFO Shift changed Shift 2 -> Shift 3; counters reset
2026-06-22 14:06:10,221 WARNING Orphaned top result discarded (partner never arrived): top_000306.csv
2026-06-22 14:07:02,990 ERROR Image read failed (top) D:\...\img.png: <reason>
```

Stable substrings used by queries: `Combined verdict`, `-> Assured`,
`-> Standard`, `Shift changed`, `Orphaned`, `ERROR`, `WARNING`.

### `shift_state.json`

Written atomically (`json.dump` defaults → space after `:` and `,`). Single line:

```json
{"shift": "Shift 2", "shift_start": "2026-06-22T14:00:00", "total": 305, "assured": 92}
```

Fields: `shift` (string), `shift_start` (ISO 8601), `total` (int),
`assured` (int). Resets to 0 at each shift boundary (06:00 / 14:00 / 22:00).

---

## 4. Grafana Alloy configuration

The full file is `observability/alloy/config.alloy` (already in the repo). It
has four stages:

1. **Logs → Loki** — `loki.source.file` tails `monitor.log`; `loki.process`
   extracts `level` as a label and parses the app timestamp; `loki.write`
   pushes to Grafana Cloud Loki with external labels `site` / `site_name`.
2. **JSON → Prometheus gauges** — `loki.source.file` tails `shift_state.json`;
   `loki.process` → `stage.json` + `stage.metrics` emit
   `gocator_total_pallets` and `gocator_assured_pallets`; the raw JSON log line
   is dropped.
3. **Heartbeat / host** — `prometheus.exporter.windows` provides `up` and basic
   host health (CPU, disk, memory, net).
4. **Scrape + remote_write** — scrape `windows_exporter` and Alloy's own
   `/metrics` (for the custom gauges), attach `site` / `site_name` via
   `prometheus.relabel`, and `prometheus.remote_write` to Grafana Cloud.

Per-site values are read from environment variables with `sys.env(...)`:

| Variable | Example | Purpose |
|---|---|---|
| `SITE_ID` | `auomatally` | label value (lowercase, no spaces) |
| `SITE_NAME` | `Auomatally` | display name |
| `GCLOUD_LOKI_URL` / `GCLOUD_LOKI_USER` | from Cloud portal | Loki push endpoint + user id |
| `GCLOUD_PROM_URL` / `GCLOUD_PROM_USER` | from Cloud portal | Prometheus push endpoint + user id |
| `GCLOUD_API_TOKEN` | `glc_...` | Access Policy token (both endpoints) |
| `GOCATOR_LOG_PATH` | `...\monitor.log` | log file to tail |
| `GOCATOR_STATE_PATH` | `...\shift_state.json` | state file to tail |

> **Why labels, not log content:** `site` and `level` are **labels**, so
> `{site="auomatally"}` and `{site="auomatally"} | level="ERROR"` are fast,
> indexed selectors. Keep label cardinality low — never label by pallet id or
> timestamp.

---

## 5. Windows installation steps

Per PC, from an **elevated** PowerShell:

```powershell
# 1. Install Alloy (registers the "Alloy" Windows service automatically)
winget install --id Grafana.Alloy
#   — or download the MSI from https://github.com/grafana/alloy/releases
#     and run: msiexec /i alloy-installer-windows-amd64.msi /qn

# 2. Edit the CONFIG block at the top of install-alloy.ps1 for THIS site:
#      $SITE_ID, $SITE_NAME, the GCLOUD_* values, and the two file paths.

# 3. Run the installer (sets env vars, copies config, restarts the service)
cd "E:\AMV\CHEP Gocator App\Gocator_viz copy\Gocator_viz copy\observability\install"
Set-ExecutionPolicy -Scope Process Bypass -Force
.\install-alloy.ps1

# 4. Verify locally
Start-Process "http://127.0.0.1:12345"     # Alloy UI: all components should be healthy
Get-Service Alloy                          # Status: Running
```

> The installer sets **machine** environment variables. Because Alloy runs as a
> service (not your interactive shell), the values are picked up on service
> start — no logoff needed, but the service is restarted by the script so they
> take effect immediately.

---

## 6. Windows service setup

The MSI / winget package installs Alloy as a service named **`Alloy`** that
reads `C:\Program Files\GrafanaLabs\Alloy\config.alloy`. Standard management:

```powershell
Get-Service Alloy
Set-Service Alloy -StartupType Automatic     # survive reboots (default)
Restart-Service Alloy                         # after a config change
Get-Content "C:\ProgramData\GrafanaLabs\Alloy\logs\*.log" -Tail 50   # service logs
```

If you ever need to (re)create the service manually:

```powershell
New-Service -Name "Alloy" `
  -BinaryPathName '"C:\Program Files\GrafanaLabs\Alloy\alloy-windows-amd64.exe" run "C:\Program Files\GrafanaLabs\Alloy\config.alloy" --storage.path="C:\ProgramData\GrafanaLabs\Alloy\data"' `
  -DisplayName "Grafana Alloy" -StartupType Automatic
Start-Service Alloy
```

**Reload after editing `config.alloy`:** copy the new file into the Alloy
program directory and `Restart-Service Alloy` (the installer does both).

---

## 7. Grafana Cloud setup

1. **Create a free stack** at <https://grafana.com> → *My Account* → a Free
   stack gives you Grafana, Loki, and Prometheus (Free tier limits: ~10k series,
   ~50 GB logs, 14-day retention — comfortable for 4 quiet factory PCs).
2. **Get the push endpoints + user ids:** in the Cloud portal open the
   **Loki** and **Prometheus** "Details / Send Metrics" pages. Copy:
   - Loki URL (`.../loki/api/v1/push`) and its numeric **User**.
   - Prometheus URL (`.../api/prom/push`) and its numeric **User**.
3. **Create one Access Policy token** with scopes
   `logs:write` and `metrics:write` (Cloud portal → *Access Policies* →
   *Create token*). Use it as `GCLOUD_API_TOKEN` for both endpoints.
4. Paste all six values into `install-alloy.ps1` (or set the env vars directly).
5. After the first PC reports in, confirm in **Explore**:
   - Loki: `{site="auomatally"}`
   - Prometheus: `gocator_total_pallets`

---

## 8. Dashboard configuration

Create one dashboard, **"Gocator Fleet"**, with a templating variable so every
panel filters by site.

### Dashboard variable `site`

- **Type:** Query → data source **Prometheus**
- **Query:** `label_values(gocator_total_pallets, site)`
- **Multi-value:** on · **Include All option:** on · **All value:** `.*`
- Use it in PromQL as `site=~"$site"` and in LogQL as `site=~"$site"`.

This gives **All / Auomatally / Site2 / Site3 / Site4** automatically as sites
report in. Add a second variable `site_name` (`label_values(..., site_name)`)
only if you prefer the friendly names in the picker.

### Panels

| # | Panel | Type | Query (see sections 9/10) |
|---|---|---|---|
| 1 | Total Pallets Count | Stat | `sum(gocator_total_pallets{site=~"$site"})` |
| 2 | Potential Assured Count | Stat | `sum(gocator_assured_pallets{site=~"$site"})` |
| 3 | Assured % | Gauge | `100 * sum(gocator_assured_pallets{site=~"$site"}) / sum(gocator_total_pallets{site=~"$site"})` |
| 4 | Active Shift | Table/Logs | latest `Shift changed` line per site (LogQL) |
| 5 | Site Online Status | Stat/State-timeline | `up{job="windows", site=~"$site"}` |
| 6 | Recent Logs | Logs | `{site=~"$site", app="gocator"}` |
| 7 | Errors | Logs | `{site=~"$site"} | level="ERROR"` |
| 8 | All Sites Summary | Table | see section 9 (instant, format **Table**, by `site`) |

For the **All Sites Summary** table use Prometheus *instant* queries formatted
as **Table**, then a *Transform → Merge* / *Labels to fields* by `site` so each
row is a site with columns Total, Assured, Assured %, Online.

A ready-to-import starter dashboard is provided at
`observability/dashboards/gocator-fleet.json` (Grafana → *Dashboards* →
*New* → *Import* → upload the file → pick your Prometheus + Loki data sources).

---

## 9. PromQL queries

```promql
# Total pallets (current shift), selected site(s)
sum(gocator_total_pallets{site=~"$site"})

# Potential assured count
sum(gocator_assured_pallets{site=~"$site"})

# Assured % (computed — never drifts from raw counts)
100 * sum(gocator_assured_pallets{site=~"$site"})
    / clamp_min(sum(gocator_total_pallets{site=~"$site"}), 1)

# Per-site breakdown (All Sites Summary table)
sum by (site, site_name) (gocator_total_pallets)
sum by (site, site_name) (gocator_assured_pallets)
100 * sum by (site)(gocator_assured_pallets) / clamp_min(sum by (site)(gocator_total_pallets), 1)

# Site online status (1 = up, 0/absent = offline)
up{job="windows", site=~"$site"}

# Throughput — pallets per minute (rate of the cumulative gauge within a shift)
rate(gocator_total_pallets{site=~"$site"}[5m]) * 60
```

> `clamp_min(..., 1)` avoids divide-by-zero at shift start when `total = 0`.

---

## 10. LogQL queries

```logql
# All logs for a site
{site="auomatally"}

# Errors only (level is an indexed label)
{site="auomatally"} | level="ERROR"

# Every completed pallet verdict
{site="auomatally"} |= "Combined verdict"

# Only assured verdicts
{site="auomatally"} |= "Combined verdict" |= "-> Assured"

# Shift changes (use to show the Active Shift)
{site=~"$site"} |= "Shift changed"

# Orphaned / unpaired results (data-quality signal)
{site=~"$site"} |= "Orphaned"

# Error rate per site over time (for a graph panel / alerting)
sum by (site) (count_over_time({site=~"$site"} | level="ERROR" [5m]))

# Pallets counted from logs (cross-check against the gauge)
sum by (site) (count_over_time({site=~"$site"} |= "Combined verdict" [$__interval]))
```

For panel **#4 Active Shift**, use a *Logs* panel with
`{site=~"$site"} |= "Shift changed"` sorted descending and *Limit = 1*, or
add an instant table extracting the shift name with
`| regexp "-> (?P<shift>Shift \\d)"`.

---

## 11. Alerting

Create these in **Grafana Alerting** (free tier includes Grafana-managed
alerts). Route to email/Teams/Slack via a contact point.

### 11.1 Site offline for 5 minutes

- **Type:** Prometheus · **Query A:** `up{job="windows"}`
- **Condition:** `last(A) < 1` **OR** *No Data* → for **5m**, `by (site)`
- **Summary:** `Site {{ $labels.site }} offline (Alloy not reporting) for 5m`

> Because the windows_exporter is scraped every 30s and remote-written,
> "no data for 5m" reliably means the PC/agent is down or has lost internet —
> independent of whether pallets are flowing.

### 11.2 Error logs detected

- **Type:** Loki
- **Query:** `sum by (site) (count_over_time({site=~".+"} | level="ERROR" [5m]))`
- **Condition:** `> 0` for **0m** (fire immediately)
- **Summary:** `{{ $values.A }} ERROR log(s) at site {{ $labels.site }}`

### 11.3 Assured % below threshold (configurable)

- **Type:** Prometheus
- **Query:**
  `100 * sum by (site)(gocator_assured_pallets) / clamp_min(sum by (site)(gocator_total_pallets), 1)`
- **Condition:** `< $THRESHOLD` (e.g. `80`) for **10m**
- Guard against early-shift noise by ANDing with a minimum volume:
  add Query B `sum by (site)(gocator_total_pallets)` and require `B > 20`.
- **Summary:** `Assured % at {{ $labels.site }} below threshold`

> Make the threshold a dashboard/alert variable or edit it in one place so all
> sites share it; per-site overrides can be added later with label matchers.

---

## 12. Rollout plan for 4 sites

| Site | `SITE_ID` | `SITE_NAME` |
|---|---|---|
| 1 | `auomatally` | `Auomatally` |
| 2 | `site2` | `Site2` |
| 3 | `site3` | `Site3` |
| 4 | `site4` | `Site4` |

**Phase 0 — Cloud prep (once):** create the stack, Access Policy token, and
note the Loki/Prometheus URLs + user ids (section 7).

**Phase 1 — Pilot (Site 1):**
1. Install Alloy + run `install-alloy.ps1` with Site 1 values.
2. Confirm `{site="auomatally"}` logs and `gocator_total_pallets{site="auomatally"}`
   metrics appear in Explore.
3. Build the dashboard and the three alerts; validate over one full shift
   (watch counters reset at the shift boundary).

**Phase 2 — Roll to Sites 2–4:**
4. On each PC, change only the CONFIG block in `install-alloy.ps1`
   (`SITE_ID` / `SITE_NAME`; Cloud values are identical) and run it.
5. The `site` dropdown auto-populates as each site reports.

**Phase 3 — Operationalize:**
6. Verify alert routing (offline + error). Pull one PC's network to confirm the
   *offline* alert fires within ~5 min, then restore.
7. Document per-site file paths if any PC uses a drive other than the default.

> **Tip:** keep one `config.alloy` in git as the single source of truth; sites
> differ only by env vars, so a config change is a copy + `Restart-Service` on
> each PC (or push via your existing software-deployment tooling).

---

## 13. Security recommendations

- **Outbound-only:** Alloy initiates HTTPS:443 to Grafana Cloud. No inbound
  ports; the local UI binds `127.0.0.1:12345` (loopback). Don't expose it.
- **Least-privilege token:** the Access Policy token has only `logs:write` +
  `metrics:write` — it cannot read or delete data. Use a **separate** token for
  dashboards/admins.
- **Token handling:** store the token in **machine env vars** (as the installer
  does), not committed to git. `install-alloy.ps1` ships with a placeholder
  token — never commit a real `glc_...` value. Consider a per-site token so one
  can be revoked without touching the others.
- **Rotation:** rotate the token periodically via the Cloud portal; update the
  env var and `Restart-Service Alloy`.
- **Least data:** ship only `monitor.log` + the two gauges. No images/CSVs leave
  the PC. Log lines contain no PII.
- **Service account:** run Alloy under a least-privilege account that has
  **read** access to the log/state files and **write** to its data dir.
- **TLS:** Grafana Cloud endpoints are HTTPS with valid certs — leave TLS
  verification on (default).

---

## 14. Troubleshooting guide

| Symptom | Likely cause | Fix |
|---|---|---|
| No data in Grafana at all | Token/URL/user wrong, or no internet | Open `http://127.0.0.1:12345` → component health; check the `loki.write` / `prometheus.remote_write` components for auth (401/403) or DNS errors. Re-check the six `GCLOUD_*` env vars. |
| Logs appear, metrics don't | `alloy_self` scrape can't reach `127.0.0.1:12345`, or JSON not parsing | In the Alloy UI confirm `prometheus.scrape "alloy_self"` is healthy and `loki.process "state_json"` shows samples. Validate `shift_state.json` is one valid JSON line. |
| Metrics appear, logs don't | Wrong `GOCATOR_LOG_PATH`, or file not yet created | Confirm the path exists and the service account can read it; App.py must have run at least once to create `monitor.log`. |
| `up` flaps / site shows offline | windows_exporter scrape failing or PC asleep | Check `prometheus.exporter.windows` health; disable PC sleep/hibernate for the inspection workstation. |
| Counters look stale / frozen | App.py stopped, or no pallets this shift | Cross-check with logs: `{site="..."} |= "Combined verdict"`. Gauges hold last value (`max_idle_duration`) until the next state write. |
| Counters jump to 0 mid-day | Expected at 06:00/14:00/22:00 shift reset | Confirm against `{site="..."} |= "Shift changed"`. |
| `level` label missing on some lines | Multi-line stack traces / non-standard lines | Those lines still ship as logs; only the regex-matched `level` is added. Adjust the regex in `config.alloy` if the app's format changes. |
| Service won't start after edit | Invalid `config.alloy` | Run `alloy fmt config.alloy` and `alloy run config.alloy` interactively to see the parse error, fix, then `Restart-Service Alloy`. |
| Duplicate/old samples after `os.replace` | State file is atomically replaced (new inode) | Alloy re-tails replaced files by default; if you see gaps, ensure the storage/positions dir (`--storage.path`) is writable and not cleared on reboot. |
| Hit free-tier limits | Too many series / verbose logs | Keep labels low-cardinality (`site`, `site_name`, `level`, `app`, `job` only). Don't add per-pallet labels. 4 sites at 30s scrape is well within Free limits. |

---

### Quick reference

```powershell
# Reload config on a PC
Copy-Item "...\observability\alloy\config.alloy" "C:\Program Files\GrafanaLabs\Alloy\config.alloy" -Force
Restart-Service Alloy

# Validate config before deploying
& "C:\Program Files\GrafanaLabs\Alloy\alloy-windows-amd64.exe" fmt "config.alloy"
```

```logql
{site="auomatally"}                          # everything
{site="auomatally"} | level="ERROR"          # errors
{site="auomatally"} |= "Combined verdict"    # verdicts
```

```promql
sum(gocator_total_pallets{site=~"$site"})
sum(gocator_assured_pallets{site=~"$site"})
100 * sum(gocator_assured_pallets{site=~"$site"}) / clamp_min(sum(gocator_total_pallets{site=~"$site"}),1)
up{job="windows", site=~"$site"}
```
