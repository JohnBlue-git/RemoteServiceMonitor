#!/usr/bin/env python3
"""
Remote Service Monitor
Connects once via SSH, samples systemd service CPU/memory at a configured
interval for a configured duration, prints a summary table, and optionally
appends results to a CSV file.

Usage:
    python monitor.py [config.json]

Auth priority: keyfile (if non-empty) → passwd (if non-empty) → SSH agent
CSV output:    set output.csv_file to a path to enable; leave "" to disable.
"""

import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import paramiko


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_SERVICE_RE = re.compile(r'^[\w:.@\-]+\.service$')
_CLK_TCK = 100  # standard Linux clock ticks per second (sysconf _SC_CLK_TCK)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------

def build_ssh_client(host_cfg: dict) -> paramiko.SSHClient:
    """Open and return a connected SSH client. Caller is responsible for close()."""
    client = paramiko.SSHClient()

    if host_cfg.get("verify_host_key", False):
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = {
        "hostname": host_cfg["host"],
        "port":     int(host_cfg.get("port", 22)),
        "username": host_cfg["username"],
        "timeout":  float(host_cfg.get("timeout", 10)),
        "banner_timeout": 30,
    }

    keyfile = (host_cfg.get("keyfile") or "").strip()
    passwd  = (host_cfg.get("passwd")  or "").strip()

    if keyfile:
        connect_kwargs["key_filename"] = os.path.expanduser(keyfile)
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"]   = False
    elif passwd:
        connect_kwargs["password"]      = passwd
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"]   = False
    else:
        # Should not reach here — validated in main()
        raise ValueError("host config requires either 'keyfile' or 'passwd' to be set")

    client.connect(**connect_kwargs)
    return client


# ---------------------------------------------------------------------------
# Service status check
# ---------------------------------------------------------------------------

def check_all_services(ssh: paramiko.SSHClient, services: list[str]) -> dict[str, str]:
    """
    Query ActiveState for every service in one systemctl show command.
    Parses KEY=VALUE output (works on all systemd versions; does not require
    the --value flag which older builds may not support).
    Returns {service: "active"|"inactive"|"failed"|...}.
    """
    svc_args = " ".join(services)
    cmd = f"systemctl show --property=ActiveState {svc_args} 2>/dev/null || true"
    _, stdout, _ = ssh.exec_command(cmd)
    raw = stdout.read().decode(errors="replace")

    # systemctl show prints one KEY=VALUE block per unit, separated by blank lines.
    result: dict[str, str] = {}
    blocks = [b.strip() for b in re.split(r'\n\s*\n', raw) if b.strip()]
    for svc, block in zip(services, blocks):
        state = "unknown"
        for line in block.splitlines():
            if line.startswith("ActiveState="):
                state = line.split("=", 1)[1].strip()
                break
        result[svc] = state
    # Fill in any services that had no output block
    for svc in services:
        result.setdefault(svc, "unknown")
    return result


# ---------------------------------------------------------------------------
# Data classes for samples and summaries
# ---------------------------------------------------------------------------

@dataclass
class ProcessSample:
    """One point-in-time measurement of a single process."""
    timestamp:   str
    pid_count:   int
    cpu_percent: float
    mem_percent: float
    rss_mb:      float


@dataclass
class ServiceSummary:
    """Aggregated statistics for one service over the full sampling window."""
    host:        str
    service:     str
    svc_status:  str
    samples:     int
    pid_avg:     float | None
    cpu_min:     float | None
    cpu_max:     float | None
    cpu_avg:     float | None
    mem_min:     float | None
    mem_max:     float | None
    mem_avg:     float | None
    rss_min_mb:  float | None
    rss_max_mb:  float | None
    rss_avg_mb:  float | None

    @classmethod
    def from_samples(
        cls,
        host: str,
        service: str,
        status: str,
        samples: list["ProcessSample"],
    ) -> "ServiceSummary":
        """Build a summary from a list of ProcessSamples (may be empty)."""
        if not samples:
            return cls(
                host=host, service=service, svc_status=status, samples=0,
                pid_avg=None,
                cpu_min=None, cpu_max=None, cpu_avg=None,
                mem_min=None, mem_max=None, mem_avg=None,
                rss_min_mb=None, rss_max_mb=None, rss_avg_mb=None,
            )
        cpu_vals = [s.cpu_percent for s in samples]
        mem_vals = [s.mem_percent for s in samples]
        rss_vals = [s.rss_mb      for s in samples]
        pid_vals = [s.pid_count   for s in samples]
        n = len(samples)
        return cls(
            host=host, service=service, svc_status=status, samples=n,
            pid_avg=round(sum(pid_vals) / n, 1),
            cpu_min=min(cpu_vals), cpu_max=max(cpu_vals), cpu_avg=round(sum(cpu_vals) / n, 2),
            mem_min=min(mem_vals), mem_max=max(mem_vals), mem_avg=round(sum(mem_vals) / n, 2),
            rss_min_mb=min(rss_vals), rss_max_mb=max(rss_vals), rss_avg_mb=round(sum(rss_vals) / n, 2),
        )

    _SVC_COL = 27  # display width reserved for the service name column

    @staticmethod
    def _fmt_service(name: str, width: int = _SVC_COL) -> str:
        """
        Fit a service name into *width* chars for table display.
        Strategy:
          1. Try the full name  (e.g. "bmcweb.service")
          2. Strip the ".service" suffix  (e.g. "bmcweb")
          3. If still too long, truncate with a trailing '…'
        """
        if len(name) <= width:
            return name.ljust(width)
        short = name.removesuffix(".service")
        if len(short) <= width:
            return short.ljust(width)
        return (short[:width - 1] + "\u2026").ljust(width)

    def format_row(self) -> str:
        """Return a fixed-width table row string for console output."""
        pid_str = "---".rjust(4) if self.pid_avg is None else f"{self.pid_avg:>4}"
        c   = ServiceSummary._fmt_col
        svc = ServiceSummary._fmt_service(self.service)
        return (
            f"  {svc} {self.svc_status:<9} {self.samples:>4}  {pid_str} "
            f"{c(self.cpu_min,8)} {c(self.cpu_max,8)} {c(self.cpu_avg,8)} "
            f"{c(self.mem_min,8)} {c(self.mem_max,8)} {c(self.mem_avg,8)} {c(self.rss_avg_mb,10)}"
        )

    def to_csv_dict(self, run_timestamp: str) -> dict:
        """Return a flat dict matching _CSV_FIELDS for csv.DictWriter."""
        return {
            "run_timestamp": run_timestamp,
            "host":          self.host,
            "service":       self.service,
            "svc_status":    self.svc_status,
            "samples":       self.samples,
            "pid_avg":       self.pid_avg,
            "cpu_min":       self.cpu_min,
            "cpu_max":       self.cpu_max,
            "cpu_avg":       self.cpu_avg,
            "mem_min":       self.mem_min,
            "mem_max":       self.mem_max,
            "mem_avg":       self.mem_avg,
            "rss_min_mb":    self.rss_min_mb,
            "rss_max_mb":    self.rss_max_mb,
            "rss_avg_mb":    self.rss_avg_mb,
        }

    # class-level constant (not a dataclass field — no annotation)
    _CSV_FIELDS = (
        "run_timestamp", "host", "service", "svc_status", "samples", "pid_avg",
        "cpu_min", "cpu_max", "cpu_avg",
        "mem_min", "mem_max", "mem_avg",
        "rss_min_mb", "rss_max_mb", "rss_avg_mb",
    )

    @staticmethod
    def _fmt_col(val: float | None, width: int, decimals: int = 2) -> str:
        """Right-justify a float to *width* chars; return '---' when None."""
        if val is None:
            return "---".rjust(width)
        return f"{val:.{decimals}f}".rjust(width)

    @staticmethod
    def print_all(summary: list["ServiceSummary"], host: str, sampling: dict) -> None:
        """Print a fixed-width summary table for all services to stdout."""
        W = 104
        print()
        print("=" * W)
        print(
            f"  REMOTE SERVICE MONITOR \u2014 SUMMARY"
            f"   host={host}"
            f"   interval={sampling.get('interval_secs')}s"
            f"   range={sampling.get('range_mins')}min"
        )
        print("=" * W)
        print(
            f"  {'Service':<27} {'Status':<9} {'N':>4}  {'PIDs':>4} "
            f"{'CPU% min':>8} {'CPU% max':>8} {'CPU% avg':>8} "
            f"{'MEM% min':>8} {'MEM% max':>8} {'MEM% avg':>8} {'RSS avg MB':>10}"
        )
        print("-" * W)
        for s in summary:
            print(s.format_row())
        print("=" * W)

    @staticmethod
    def save_csv(summary: list["ServiceSummary"], csv_path: str) -> None:
        """Append summary rows to a CSV file (creates file + header if new)."""
        path = Path(csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = path.exists()
        run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=ServiceSummary._CSV_FIELDS)
            if not file_exists:
                writer.writeheader()
            for s in summary:
                writer.writerow(s.to_csv_dict(run_ts))

        print(f"\nSummary appended to: {csv_path}")

    @staticmethod
    def compute(
        data: dict[str, list["ProcessSample"]],
        host: str,
        svc_status: dict[str, str],
    ) -> list["ServiceSummary"]:
        """Aggregate per-service samples into ServiceSummary objects."""
        summary: list[ServiceSummary] = []
        for svc, status in svc_status.items():
            samples = data.get(svc, [])
            summary.append(ServiceSummary.from_samples(host, svc, status, samples))
        return summary


# ---------------------------------------------------------------------------
# Service stat query — helpers
# ---------------------------------------------------------------------------

def _build_query_cmd(service: str) -> str:
    """
    Build a single shell command that:
      1. Discovers all PIDs in *service*'s cgroup
         (tries cgroup v2, then cgroup v1 systemd hierarchy, then MainPID).
      2. Reads /proc/<pid>/stat and VmRSS for every discovered PID.
      3. Reads /proc/uptime and /proc/meminfo once (shared across all PIDs).

    Sentinel lines (__STAT_<pid>__, __RSS_<pid>__, __UPTIME__, __MEMTOTAL__)
    delimit each section for unambiguous parsing by _parse_query_output().
    """
    return (
        # Resolve the cgroup path for this service
        f'CGROUP=$(systemctl show --property=ControlGroup {service} 2>/dev/null'
        " | grep '^ControlGroup=' | cut -d= -f2 | tr -d '[:space:]'); "
        '[ -z "$CGROUP" ] && exit 0; '
        # Read cgroup.procs: try cgroup v2, then cgroup v1 (systemd hierarchy)
        'PIDS=$(cat /sys/fs/cgroup"$CGROUP"/cgroup.procs 2>/dev/null'
        '    || cat /sys/fs/cgroup/systemd"$CGROUP"/cgroup.procs 2>/dev/null); '
        # Fall back to MainPID when cgroup.procs is unavailable
        'if [ -z "$PIDS" ]; then '
        f'  M=$(systemctl show --property=MainPID {service} 2>/dev/null'
        "  | grep '^MainPID=' | cut -d= -f2 | tr -d '[:space:]'); "
        '  [ -n "$M" ] && [ "$M" != "0" ] && PIDS=$M; '
        'fi; '
        '[ -z "$PIDS" ] && exit 0; '
        # Per-PID /proc data — unique sentinel per PID so the parser can match them
        'for P in $PIDS; do '
        '  echo "__STAT_${P}__"; cat /proc/$P/stat 2>/dev/null; '
        '  echo "__RSS_${P}__"; grep VmRSS /proc/$P/status 2>/dev/null; '
        'done; '
        # System-wide data (read once, shared across all PIDs)
        'echo __UPTIME__; cat /proc/uptime; '
        'echo __MEMTOTAL__; grep MemTotal /proc/meminfo'
    )


def _parse_query_output(
    raw: str,
) -> tuple[dict[int, dict[str, str]], float | None, int | None]:
    """
    Parse the sentinel-delimited output produced by _build_query_cmd().

    Returns:
        pid_data     — {pid: {'stat': <stat line>, 'rss_line': <VmRSS line>}}
        uptime_sec   — float from /proc/uptime, or None on parse failure
        mem_total_kb — int  from /proc/meminfo MemTotal, or None on parse failure
    """
    pid_data:     dict[int, dict[str, str]] = {}
    uptime_sec:   float | None = None
    mem_total_kb: int   | None = None

    current_pid:   int  | None = None
    current_field: str  | None = None  # 'stat' or 'rss_line'
    current_key:   str  | None = None  # '__UPTIME__' or '__MEMTOTAL__'

    for raw_line in raw.splitlines():
        line = raw_line.rstrip()

        # Per-PID STAT sentinel
        m = re.match(r'^__STAT_(\d+)__$', line)
        if m:
            current_pid, current_field, current_key = int(m.group(1)), 'stat', None
            pid_data.setdefault(current_pid, {})
            continue

        # Per-PID RSS sentinel
        m = re.match(r'^__RSS_(\d+)__$', line)
        if m:
            current_pid, current_field, current_key = int(m.group(1)), 'rss_line', None
            pid_data.setdefault(current_pid, {})
            continue

        # System-wide sentinels
        if line == '__UPTIME__':
            current_key, current_pid, current_field = '__UPTIME__', None, None
            continue
        if line == '__MEMTOTAL__':
            current_key, current_pid, current_field = '__MEMTOTAL__', None, None
            continue

        if not line:  # skip blank lines between sentinel and content
            continue

        # Capture the first non-empty content line after each sentinel
        if current_pid is not None and current_field is not None:
            if current_field not in pid_data[current_pid]:
                pid_data[current_pid][current_field] = line
            current_field = None
        elif current_key == '__UPTIME__' and uptime_sec is None:
            try:
                uptime_sec = float(line.split()[0])
            except (ValueError, IndexError):
                pass
            current_key = None
        elif current_key == '__MEMTOTAL__' and mem_total_kb is None:
            try:
                mem_total_kb = int(line.split()[1])
            except (ValueError, IndexError):
                pass
            current_key = None

    return pid_data, uptime_sec, mem_total_kb


def _parse_cpu_percent(stat_line: str, uptime_sec: float) -> float | None:
    """
    Compute lifetime-average CPU% from a /proc/<pid>/stat line and system uptime.

    The comm field (field 2) may contain spaces and parentheses, so fields after
    it are located via the last ')' in the line.
    Fields used (0-indexed after comm+state): 11=utime, 12=stime, 19=starttime.
    Returns None if parsing fails or the computed process age is non-positive.
    """
    try:
        rp        = stat_line.rfind(")")
        fields    = stat_line[rp + 2:].split()
        utime     = int(fields[11])
        stime     = int(fields[12])
        starttime = int(fields[19])
        age = uptime_sec - starttime / _CLK_TCK
        if age <= 0:
            return None
        return round(((utime + stime) / _CLK_TCK) / age * 100.0, 2)
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Service stat query
# ---------------------------------------------------------------------------

def query_service(ssh: paramiko.SSHClient, service: str) -> "ProcessSample | None":
    """
    Return a ProcessSample aggregated over all PIDs in the service's cgroup.

    Why /proc instead of ps:
      - BusyBox ps (used on OpenBMC) has no column-selection support.
      - /proc/<pid>/stat and /proc/<pid>/status are always available.

    CPU%  = sum of per-PID (utime + stime) / CLK_TCK / process_age * 100
            (lifetime average per process, summed across all cgroup PIDs).
    MEM%  = total VmRSS across all PIDs / MemTotal * 100.
    """
    cmd = _build_query_cmd(service)
    _, stdout, _ = ssh.exec_command(cmd)
    raw = stdout.read().decode(errors="replace")

    if not raw.strip():
        return None

    pid_data, uptime_sec, mem_total_kb = _parse_query_output(raw)

    if not pid_data or uptime_sec is None or mem_total_kb is None:
        return None

    total_rss_kb  = 0
    total_cpu_pct = 0.0
    valid_pids    = 0

    for entry in pid_data.values():
        stat_line = entry.get('stat', '')
        rss_line  = entry.get('rss_line', '')
        if not stat_line or not rss_line:
            continue
        cpu_pct = _parse_cpu_percent(stat_line, uptime_sec)
        if cpu_pct is None:
            continue
        try:
            rss_kb = int(rss_line.split()[1])
        except (ValueError, IndexError):
            continue
        total_rss_kb  += rss_kb
        total_cpu_pct += cpu_pct
        valid_pids    += 1

    if valid_pids == 0:
        return None

    mem_pct = round(total_rss_kb / mem_total_kb * 100.0, 2) if mem_total_kb else 0.0

    return ProcessSample(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        pid_count=valid_pids,
        cpu_percent=round(total_cpu_pct, 2),
        mem_percent=mem_pct,
        rss_mb=round(total_rss_kb / 1024.0, 2),
    )


# ---------------------------------------------------------------------------
# Sampling loop
# ---------------------------------------------------------------------------

def run_sampling(
    ssh: paramiko.SSHClient,
    services: list[str],
    sampling: dict,
) -> dict[str, list[ProcessSample]]:
    """
    Sample every service at the configured interval for the configured range.
    Returns {service: [ProcessSample, ...]}
    """
    range_secs    = float(sampling.get("range_mins", 1)) * 60
    interval_secs = float(sampling.get("interval_secs", 5))
    total_samples = max(1, round(range_secs / interval_secs))

    print(
        f"  Sampling {len(services)} service(s) "
        f"every {interval_secs}s for {range_secs:.0f}s "
        f"({total_samples} sample(s)) ..."
    )

    data: dict[str, list[ProcessSample]] = {svc: [] for svc in services}
    w = len(str(total_samples))

    for i in range(total_samples):
        for svc in services:
            sample = query_service(ssh, svc)
            if sample is not None:
                data[svc].append(sample)
        print(f"  [{i+1:>{w}}/{total_samples}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        if i < total_samples - 1:
            time.sleep(interval_secs)

    return data


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Load and validate config
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    if not Path(config_path).exists():
        print(f"Error: config file not found: {config_path}")
        sys.exit(1)
    config   = load_config(config_path)

    # Validate config values and set defaults
    host_cfg: dict      = config.get("host", {})
    services: list[str] = [s.strip() for s in config.get("services", []) if s.strip()]
    sampling: dict      = config.get("sampling", {"range_mins": 1, "interval_secs": 5})
    csv_file: str       = (config.get("output", {}).get("csv_file") or "").strip()
    if not host_cfg:
        print("Error: no host configured.")
        sys.exit(1)
    if not services:
        print("Error: no services configured.")
        sys.exit(1)
    # Validate auth config: must have either non-empty keyfile or non-empty passwd
    keyfile = (host_cfg.get("keyfile") or "").strip()
    passwd  = (host_cfg.get("passwd")  or "").strip()
    if not keyfile and not passwd:
        print("Error: host config requires either 'keyfile' or 'passwd' to be set (both are null/empty).")
        sys.exit(1)
    # Validate service names to prevent shell injection via config values
    for svc in services:
        if not _VALID_SERVICE_RE.match(svc):
            print(f"Error: invalid service name: {svc!r}  (must match *.service)")
            sys.exit(1)

    # Connect via SSH
    host_addr = host_cfg["host"]
    print(f"Connecting to {host_addr} ...")
    client = build_ssh_client(host_cfg)
    print("Connected.\n")

    svc_status: dict[str, str]            = {}
    data:       dict[str, list[ProcessSample]] = {}
    try:
        # check service status (one batch command)
        print("  Checking service status ...")
        svc_status = check_all_services(client, services)
        active_services = [s for s in services if svc_status[s] == "active"]
        print(f"  {'Service':<32}  Status")
        print(f"  {'-'*32}  {'------'}")
        for svc in services:
            status = svc_status[svc]
            note = "" if status == "active" else "  [SKIP — not active]"
            print(f"  {svc:<32}  {status}{note}")
        print()

        # Warn and skip sampling if no services are active
        if not active_services:
            print("Warning: no active services found — skipping sampling.")

        else:
            # Step 2: sample only active services
            data = run_sampling(client, active_services, sampling)
    finally:
        client.close()
        print(f"\nConnection to {host_addr} closed.")

    # Compute and print summary, save CSV if configured
    summary = ServiceSummary.compute(data, host_addr, svc_status)
    ServiceSummary.print_all(summary, host_addr, sampling)
    # CSV output is optional since not everyone needs/wants it and it requires a valid path in the config;
    if csv_file is None or csv_file == "":
        print("\n(CSV output disabled \u2014 set output.csv_file to a path to enable)")
    else:#!/usr/bin/env python3
"""
Remote Service Monitor
Connects once via SSH, samples systemd service CPU/memory at a configured
interval for a configured duration, prints every measurement, appends the
measurements to a CSV file, and saves a time-series plot.

Usage:
    python monitor.py [config.json]

Auth priority: keyfile (if non-empty) → passwd (if non-empty) → SSH agent
CSV output:    set output.csv_file to a path to enable; leave "" to disable.
"""

import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import paramiko


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_SERVICE_RE = re.compile(r'^[\w:.@\-]+\.service$')
_CLK_TCK = 100  # standard Linux clock ticks per second (sysconf _SC_CLK_TCK)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------

def build_ssh_client(host_cfg: dict) -> paramiko.SSHClient:
    """Open and return a connected SSH client. Caller is responsible for close()."""
    client = paramiko.SSHClient()

    if host_cfg.get("verify_host_key", False):
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = {
        "hostname": host_cfg["host"],
        "port":     int(host_cfg.get("port", 22)),
        "username": host_cfg["username"],
        "timeout":  float(host_cfg.get("timeout", 10)),
        "banner_timeout": 30,
    }

    keyfile = (host_cfg.get("keyfile") or "").strip()
    passwd  = (host_cfg.get("passwd")  or "").strip()

    if keyfile:
        connect_kwargs["key_filename"] = os.path.expanduser(keyfile)
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"]   = False
    elif passwd:
        connect_kwargs["password"]      = passwd
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"]   = False
    else:
        # Should not reach here — validated in main()
        raise ValueError("host config requires either 'keyfile' or 'passwd' to be set")

    client.connect(**connect_kwargs)
    return client


# ---------------------------------------------------------------------------
# Service status check
# ---------------------------------------------------------------------------

def check_all_services(ssh: paramiko.SSHClient, services: list[str]) -> dict[str, str]:
    """
    Query ActiveState for every service in one systemctl show command.
    Parses KEY=VALUE output (works on all systemd versions; does not require
    the --value flag which older builds may not support).
    Returns {service: "active"|"inactive"|"failed"|...}.
    """
    svc_args = " ".join(services)
    cmd = f"systemctl show --property=ActiveState {svc_args} 2>/dev/null || true"
    _, stdout, _ = ssh.exec_command(cmd)
    raw = stdout.read().decode(errors="replace")

    # systemctl show prints one KEY=VALUE block per unit, separated by blank lines.
    result: dict[str, str] = {}
    blocks = [b.strip() for b in re.split(r'\n\s*\n', raw) if b.strip()]
    for svc, block in zip(services, blocks):
        state = "unknown"
        for line in block.splitlines():
            if line.startswith("ActiveState="):
                state = line.split("=", 1)[1].strip()
                break
        result[svc] = state
    # Fill in any services that had no output block
    for svc in services:
        result.setdefault(svc, "unknown")
    return result


# ---------------------------------------------------------------------------
# Data class for individual samples
# ---------------------------------------------------------------------------

@dataclass
class ProcessSample:
    """One point-in-time measurement of a single process."""
    timestamp:   str
    pid_count:   int
    cpu_percent: float
    mem_percent: float
    rss_mb:      float


def print_samples(data: dict[str, list[ProcessSample]], host: str) -> None:
    """Print each collected measurement without aggregating it."""
    print(f"\nREMOTE SERVICE MONITOR \u2014 SAMPLES   host={host}")
    print(f"  {'Timestamp':<19} {'Service':<32} {'PIDs':>4} {'CPU%':>8} {'MEM%':>8} {'RSS MB':>10}")
    print("  " + "-" * 89)
    rows = sorted(
        ((sample.timestamp, service, sample) for service, samples in data.items() for sample in samples),
        key=lambda row: row[0],
    )
    for timestamp, service, sample in rows:
        print(
            f"  {timestamp:<19} {service:<32} {sample.pid_count:>4} "
            f"{sample.cpu_percent:>8.2f} {sample.mem_percent:>8.2f} {sample.rss_mb:>10.2f}"
        )
    if not rows:
        print("  No successful samples collected.")


_SAMPLE_CSV_FIELDS = (
    "timestamp", "host", "service", "svc_status", "pid_count",
    "cpu_percent", "mem_percent", "rss_mb",
)


def save_samples_csv(
    data: dict[str, list[ProcessSample]],
    host: str,
    svc_status: dict[str, str],
    csv_path: str,
) -> None:
    """Append one CSV row for every collected measurement."""
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    rows = sorted(
        ((sample.timestamp, service, sample) for service, samples in data.items() for sample in samples),
        key=lambda row: row[0],
    )

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_SAMPLE_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        for timestamp, service, sample in rows:
            writer.writerow({
                "timestamp": timestamp,
                "host": host,
                "service": service,
                "svc_status": svc_status.get(service, "unknown"),
                "pid_count": sample.pid_count,
                "cpu_percent": sample.cpu_percent,
                "mem_percent": sample.mem_percent,
                "rss_mb": sample.rss_mb,
            })

    print(f"\nSamples appended to: {csv_path}")


def plot_samples(data: dict[str, list[ProcessSample]], plot_path: str) -> None:
    """Plot each service's CPU, memory, and RSS measurements over time."""
    series = {
        service: sorted(samples, key=lambda sample: sample.timestamp)
        for service, samples in data.items() if samples
    }
    if not series:
        print("\nNo samples available; skipping plot.")
        return

    path = Path(plot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    metrics = (
        ("cpu_percent", "CPU usage", "CPU (%)"),
        ("mem_percent", "Memory usage", "Memory (%)"),
        ("rss_mb", "Resident memory", "RSS (MB)"),
    )
    fig, axes = plt.subplots(len(metrics), 1, figsize=(12, 9), sharex=True)
    for ax, (attribute, title, ylabel) in zip(axes, metrics):
        for service, samples in series.items():
            timestamps = [datetime.strptime(sample.timestamp, "%Y-%m-%d %H:%M:%S") for sample in samples]
            values = [getattr(sample, attribute) for sample in samples]
            ax.plot(timestamps, values, marker="o", markersize=3, label=service)
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
        ax.legend()
    axes[-1].set_xlabel("Time")
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"\nTime-series plot saved to: {plot_path}")


# ---------------------------------------------------------------------------
# Service stat query — helpers
# ---------------------------------------------------------------------------

def _build_query_cmd(service: str) -> str:
    """
    Build a single shell command that:
      1. Discovers all PIDs in *service*'s cgroup
         (tries cgroup v2, then cgroup v1 systemd hierarchy, then MainPID).
      2. Reads /proc/<pid>/stat and VmRSS for every discovered PID.
      3. Reads /proc/uptime and /proc/meminfo once (shared across all PIDs).

    Sentinel lines (__STAT_<pid>__, __RSS_<pid>__, __UPTIME__, __MEMTOTAL__)
    delimit each section for unambiguous parsing by _parse_query_output().
    """
    return (
        # Resolve the cgroup path for this service
        f'CGROUP=$(systemctl show --property=ControlGroup {service} 2>/dev/null'
        " | grep '^ControlGroup=' | cut -d= -f2 | tr -d '[:space:]'); "
        '[ -z "$CGROUP" ] && exit 0; '
        # Read cgroup.procs: try cgroup v2, then cgroup v1 (systemd hierarchy)
        'PIDS=$(cat /sys/fs/cgroup"$CGROUP"/cgroup.procs 2>/dev/null'
        '    || cat /sys/fs/cgroup/systemd"$CGROUP"/cgroup.procs 2>/dev/null); '
        # Fall back to MainPID when cgroup.procs is unavailable
        'if [ -z "$PIDS" ]; then '
        f'  M=$(systemctl show --property=MainPID {service} 2>/dev/null'
        "  | grep '^MainPID=' | cut -d= -f2 | tr -d '[:space:]'); "
        '  [ -n "$M" ] && [ "$M" != "0" ] && PIDS=$M; '
        'fi; '
        '[ -z "$PIDS" ] && exit 0; '
        # Per-PID /proc data — unique sentinel per PID so the parser can match them
        'for P in $PIDS; do '
        '  echo "__STAT_${P}__"; cat /proc/$P/stat 2>/dev/null; '
        '  echo "__RSS_${P}__"; grep VmRSS /proc/$P/status 2>/dev/null; '
        'done; '
        # System-wide data (read once, shared across all PIDs)
        'echo __UPTIME__; cat /proc/uptime; '
        'echo __MEMTOTAL__; grep MemTotal /proc/meminfo'
    )


def _parse_query_output(
    raw: str,
) -> tuple[dict[int, dict[str, str]], float | None, int | None]:
    """
    Parse the sentinel-delimited output produced by _build_query_cmd().

    Returns:
        pid_data     — {pid: {'stat': <stat line>, 'rss_line': <VmRSS line>}}
        uptime_sec   — float from /proc/uptime, or None on parse failure
        mem_total_kb — int  from /proc/meminfo MemTotal, or None on parse failure
    """
    pid_data:     dict[int, dict[str, str]] = {}
    uptime_sec:   float | None = None
    mem_total_kb: int   | None = None

    current_pid:   int  | None = None
    current_field: str  | None = None  # 'stat' or 'rss_line'
    current_key:   str  | None = None  # '__UPTIME__' or '__MEMTOTAL__'

    for raw_line in raw.splitlines():
        line = raw_line.rstrip()

        # Per-PID STAT sentinel
        m = re.match(r'^__STAT_(\d+)__$', line)
        if m:
            current_pid, current_field, current_key = int(m.group(1)), 'stat', None
            pid_data.setdefault(current_pid, {})
            continue

        # Per-PID RSS sentinel
        m = re.match(r'^__RSS_(\d+)__$', line)
        if m:
            current_pid, current_field, current_key = int(m.group(1)), 'rss_line', None
            pid_data.setdefault(current_pid, {})
            continue

        # System-wide sentinels
        if line == '__UPTIME__':
            current_key, current_pid, current_field = '__UPTIME__', None, None
            continue
        if line == '__MEMTOTAL__':
            current_key, current_pid, current_field = '__MEMTOTAL__', None, None
            continue

        if not line:  # skip blank lines between sentinel and content
            continue

        # Capture the first non-empty content line after each sentinel
        if current_pid is not None and current_field is not None:
            if current_field not in pid_data[current_pid]:
                pid_data[current_pid][current_field] = line
            current_field = None
        elif current_key == '__UPTIME__' and uptime_sec is None:
            try:
                uptime_sec = float(line.split()[0])
            except (ValueError, IndexError):
                pass
            current_key = None
        elif current_key == '__MEMTOTAL__' and mem_total_kb is None:
            try:
                mem_total_kb = int(line.split()[1])
            except (ValueError, IndexError):
                pass
            current_key = None

    return pid_data, uptime_sec, mem_total_kb


def _parse_cpu_percent(stat_line: str, uptime_sec: float) -> float | None:
    """
    Compute lifetime-average CPU% from a /proc/<pid>/stat line and system uptime.

    The comm field (field 2) may contain spaces and parentheses, so fields after
    it are located via the last ')' in the line.
    Fields used (0-indexed after comm+state): 11=utime, 12=stime, 19=starttime.
    Returns None if parsing fails or the computed process age is non-positive.
    """
    try:
        rp        = stat_line.rfind(")")
        fields    = stat_line[rp + 2:].split()
        utime     = int(fields[11])
        stime     = int(fields[12])
        starttime = int(fields[19])
        age = uptime_sec - starttime / _CLK_TCK
        if age <= 0:
            return None
        return round(((utime + stime) / _CLK_TCK) / age * 100.0, 2)
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Service stat query
# ---------------------------------------------------------------------------

def query_service(ssh: paramiko.SSHClient, service: str) -> "ProcessSample | None":
    """
    Return a ProcessSample aggregated over all PIDs in the service's cgroup.

    Why /proc instead of ps:
      - BusyBox ps (used on OpenBMC) has no column-selection support.
      - /proc/<pid>/stat and /proc/<pid>/status are always available.

    CPU%  = sum of per-PID (utime + stime) / CLK_TCK / process_age * 100
            (lifetime average per process, summed across all cgroup PIDs).
    MEM%  = total VmRSS across all PIDs / MemTotal * 100.
    """
    cmd = _build_query_cmd(service)
    _, stdout, _ = ssh.exec_command(cmd)
    raw = stdout.read().decode(errors="replace")

    if not raw.strip():
        return None

    pid_data, uptime_sec, mem_total_kb = _parse_query_output(raw)

    if not pid_data or uptime_sec is None or mem_total_kb is None:
        return None

    total_rss_kb  = 0
    total_cpu_pct = 0.0
    valid_pids    = 0

    for entry in pid_data.values():
        stat_line = entry.get('stat', '')
        rss_line  = entry.get('rss_line', '')
        if not stat_line or not rss_line:
            continue
        cpu_pct = _parse_cpu_percent(stat_line, uptime_sec)
        if cpu_pct is None:
            continue
        try:
            rss_kb = int(rss_line.split()[1])
        except (ValueError, IndexError):
            continue
        total_rss_kb  += rss_kb
        total_cpu_pct += cpu_pct
        valid_pids    += 1

    if valid_pids == 0:
        return None

    mem_pct = round(total_rss_kb / mem_total_kb * 100.0, 2) if mem_total_kb else 0.0

    return ProcessSample(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        pid_count=valid_pids,
        cpu_percent=round(total_cpu_pct, 2),
        mem_percent=mem_pct,
        rss_mb=round(total_rss_kb / 1024.0, 2),
    )


# ---------------------------------------------------------------------------
# Sampling loop
# ---------------------------------------------------------------------------

def run_sampling(
    ssh: paramiko.SSHClient,
    services: list[str],
    sampling: dict,
) -> dict[str, list[ProcessSample]]:
    """
    Sample every service at the configured interval for the configured range.
    Returns {service: [ProcessSample, ...]}
    """
    range_secs    = float(sampling.get("range_mins", 1)) * 60
    interval_secs = float(sampling.get("interval_secs", 5))
    total_samples = max(1, round(range_secs / interval_secs))

    print(
        f"  Sampling {len(services)} service(s) "
        f"every {interval_secs}s for {range_secs:.0f}s "
        f"({total_samples} sample(s)) ..."
    )

    data: dict[str, list[ProcessSample]] = {svc: [] for svc in services}
    w = len(str(total_samples))

    for i in range(total_samples):
        for svc in services:
            sample = query_service(ssh, svc)
            if sample is not None:
                data[svc].append(sample)
        print(f"  [{i+1:>{w}}/{total_samples}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        if i < total_samples - 1:
            time.sleep(interval_secs)

    return data


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Load and validate config
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    if not Path(config_path).exists():
        print(f"Error: config file not found: {config_path}")
        sys.exit(1)
    config   = load_config(config_path)

    # Validate config values and set defaults
    host_cfg: dict      = config.get("host", {})
    services: list[str] = [s.strip() for s in config.get("services", []) if s.strip()]
    sampling: dict      = config.get("sampling", {"range_mins": 1, "interval_secs": 5})
    output_cfg: dict     = config.get("output", {})
    csv_file: str       = (output_cfg.get("csv_file") or "").strip()
    plot_file: str      = (output_cfg.get("plot_file") or "results/monitor_timeseries.png").strip()
    if not host_cfg:
        print("Error: no host configured.")
        sys.exit(1)
    if not services:
        print("Error: no services configured.")
        sys.exit(1)
    # Validate auth config: must have either non-empty keyfile or non-empty passwd
    keyfile = (host_cfg.get("keyfile") or "").strip()
    passwd  = (host_cfg.get("passwd")  or "").strip()
    if not keyfile and not passwd:
        print("Error: host config requires either 'keyfile' or 'passwd' to be set (both are null/empty).")
        sys.exit(1)
    # Validate service names to prevent shell injection via config values
    for svc in services:
        if not _VALID_SERVICE_RE.match(svc):
            print(f"Error: invalid service name: {svc!r}  (must match *.service)")
            sys.exit(1)

    # Connect via SSH
    host_addr = host_cfg["host"]
    print(f"Connecting to {host_addr} ...")
    client = build_ssh_client(host_cfg)
    print("Connected.\n")

    svc_status: dict[str, str]            = {}
    data:       dict[str, list[ProcessSample]] = {}
    try:
        # check service status (one batch command)
        print("  Checking service status ...")
        svc_status = check_all_services(client, services)
        active_services = [s for s in services if svc_status[s] == "active"]
        print(f"  {'Service':<32}  Status")
        print(f"  {'-'*32}  {'------'}")
        for svc in services:
            status = svc_status[svc]
            note = "" if status == "active" else "  [SKIP — not active]"
            print(f"  {svc:<32}  {status}{note}")
        print()

        # Warn and skip sampling if no services are active
        if not active_services:
            print("Warning: no active services found — skipping sampling.")

        else:
            # Step 2: sample only active services
            data = run_sampling(client, active_services, sampling)
    finally:
        client.close()
        print(f"\nConnection to {host_addr} closed.")

    # Print and plot each sample; CSV output remains optional.
    print_samples(data, host_addr)
    if csv_file is None or csv_file == "":
        print("\n(CSV output disabled \u2014 set output.csv_file to a path to enable)")
    else:
        save_samples_csv(data, host_addr, svc_status, csv_file)
    if plot_file:
        plot_samples(data, plot_file)


if __name__ == "__main__":
    main()

