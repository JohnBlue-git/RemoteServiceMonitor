# RemoteServiceMonitor

Monitor CPU and memory usage of remote systemd services over SSH.  
One SSH connection is opened per run, all services are sampled, then the connection is closed.  
Results are printed as a summary table and optionally appended to a CSV file.

---

## Requirements

- Python 3.10+
- [paramiko](https://www.paramiko.org/)

```bash
pip install -r requirements.txt
```

---

## Quick Start

```bash
# Edit config.json to point at your host and services
python3 monitor.py              # uses config.json by default
python3 monitor.py my.json      # use a custom config file
```

---

## Configuration (`config.json`)

```json
{
  "host": {
    "host": "192.168.1.100",
    "port": 22,
    "username": "root",
    "passwd": "yourpassword",
    "keyfile": null,
    "timeout": 10,
    "verify_host_key": false
  },
  "services": [
    "bmcweb.service",
    "phosphor-logging.service"
  ],
  "sampling": {
    "range_mins": 1,
    "interval_secs": 5
  },
  "output": {
    "csv_file": "results/monitor_results.csv"
  }
}
```

### `host`

| Field             | Type    | Required | Description |
|-------------------|---------|----------|-------------|
| `host`            | string  | yes      | IP address or hostname of the remote machine |
| `port`            | integer | no       | SSH port (default `22`) |
| `username`        | string  | yes      | SSH login username |
| `passwd`          | string  | *        | Password — used when `keyfile` is null/empty |
| `keyfile`         | string  | *        | Path to private key file, e.g. `~/.ssh/id_rsa` — takes priority over `passwd` |
| `timeout`         | number  | no       | SSH connect timeout in seconds (default `10`) |
| `verify_host_key` | boolean | no       | Reject unknown host keys when `true` (default `false`) |

\* At least one of `passwd` or `keyfile` must be set (non-null, non-empty).  
Auth priority: `keyfile` → `passwd` → SSH agent (when both are null).

### `services`

A JSON array of systemd unit names to monitor. Only `*.service` units are accepted.

```json
"services": ["bmcweb.service", "phosphor-logging.service"]
```

### `sampling`

| Field           | Type   | Description |
|-----------------|--------|-------------|
| `range_mins`    | number | Total monitoring duration in minutes |
| `interval_secs` | number | Seconds between each sample |

The number of samples taken is `round(range_mins × 60 / interval_secs)`.

### `output`

| Field      | Type   | Description |
|------------|--------|-------------|
| `csv_file` | string | Path to append CSV results to. Set to `null` or `""` to disable CSV output. |

The directory is created automatically if it does not exist.

---

## How It Works

```
main()
  ├── validate config
  ├── open SSH connection     (once)
  ├── check service status    → systemctl show --property=ActiveState
  ├── filter to active services
  ├── sampling loop
  │     └── per service, per interval:
  │           query_service() → reads /proc/<MainPID>/stat + /proc/<MainPID>/status
  ├── close SSH connection    (once)
  ├── ServiceSummary.compute()   → aggregate min/max/avg per service
  ├── ServiceSummary.print_all() → print summary table
  └── ServiceSummary.save_csv()  → append to CSV (if enabled)
```

**Why `/proc` instead of `ps`?**  
OpenBMC uses BusyBox `ps`, which has no column-selection (`-o`) support.  
Reading `/proc/<pid>/stat` and `/proc/<pid>/status` directly is always available.

**CPU% formula:**  
`(utime + stime) / CLK_TCK / process_age × 100`  
This is the lifetime-average CPU%, the same value `ps %cpu` reports.

**Non-active services** are detected before sampling and skipped — their rows appear in the summary with `---` for all stats.

---

## Example Output

```
Connecting to 192.168.9.227 ...
Connected.

  Checking service status ...
  Service                           Status
  --------------------------------  ------
  bmcweb.service                    active
  miniupnpd.service                 inactive  [SKIP — not active]

  Sampling 1 service(s) every 5.0s for 60s (12 sample(s)) ...
  [ 1/12] 2026-04-22 16:58:43
  ...
  [12/12] 2026-04-22 16:59:39

Connection to 192.168.9.227 closed.

========================================================================================================
  REMOTE SERVICE MONITOR — SUMMARY   host=192.168.9.227   interval=5s   range=1min
========================================================================================================
  Service                     Status       N  PIDs CPU% min CPU% max CPU% avg MEM% min MEM% max MEM% avg RSS avg MB
--------------------------------------------------------------------------------------------------------
  bmcweb.service              active      12   1.0     0.11     0.11     0.11     2.26     2.26     2.26      21.15
  miniupnpd.service           inactive     0   ---      ---      ---      ---      ---      ---      ---        ---
========================================================================================================

Summary appended to: results/monitor_results.csv
```

---

## CSV Output

Each run appends one row per service to the CSV file. A header is written automatically on first creation.

| Column          | Description |
|-----------------|-------------|
| `run_timestamp` | Wall-clock time when the run completed |
| `host`          | Host IP/name from config |
| `service`       | Systemd unit name |
| `svc_status`    | `active`, `inactive`, `failed`, etc. |
| `samples`       | Number of successful samples collected |
| `pid_avg`       | Average PID count across samples |
| `cpu_min/max/avg` | CPU% statistics across samples |
| `mem_min/max/avg` | Memory% (RSS / MemTotal) statistics |
| `rss_min/max/avg_mb` | RSS (Resident Set Size) in megabytes statistics |

---

## Project Structure

```
RemoteServiceMonitor/
├── monitor.py       # main script
├── config.json      # host / service / sampling configuration
├── requirements.txt
├── .gitignore
└── results/         # CSV output directory (auto-created)
```

---

## Security Notes

- For production use, set `"verify_host_key": true` and ensure the host is in `~/.ssh/known_hosts`.
- Avoid committing `config.json` with real credentials. Use a gitignored copy (e.g. `config.local.json`) and pass it as an argument:
  ```bash
  python3 monitor.py config.local.json
  ```
- Service names are validated against `^[\w:.@-]+\.service$` before being passed to any remote command.
