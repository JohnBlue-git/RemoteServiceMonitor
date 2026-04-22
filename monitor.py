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
# Service stat query
# ---------------------------------------------------------------------------

def query_service(ssh: paramiko.SSHClient, service: str) -> "ProcessSample | None":
    """
    Return (pid_count, cpu_pct, mem_pct, rss_mb) for the main process of the
    given systemd unit, read directly from /proc.

    Why /proc instead of ps:
      - BusyBox ps (used on OpenBMC) has no column-selection support.
      - /proc/<pid>/stat and /proc/<pid>/status are always available.

    CPU%  = (utime + stime) / CLK_TCK / process_age * 100  (lifetime average,
             same metric that `ps %cpu` reports).
    MEM%  = VmRSS / MemTotal * 100.
    """
    cmd = (
        # Get MainPID via KEY=VALUE (--value flag not available on old systemd)
        f"M=$(systemctl show --property=MainPID {service} 2>/dev/null"
        " | grep '^MainPID=' | cut -d= -f2 | tr -d '[:space:]'); "
        '[ -z "$M" ] || [ "$M" = "0" ] && exit 0; '
        # Read all needed /proc files in one command
        'echo __STAT__; cat /proc/$M/stat 2>/dev/null; '
        'echo __VMRSS__; grep VmRSS /proc/$M/status 2>/dev/null; '
        'echo __UPTIME__; cat /proc/uptime 2>/dev/null; '
        'echo __MEMTOTAL__; grep MemTotal /proc/meminfo 2>/dev/null'
    )
    _, stdout, _ = ssh.exec_command(cmd)
    output = stdout.read().decode(errors="replace").strip()

    if not output:
        return None

    try:
        sections: dict[str, str] = {}
        current: str | None = None
        for line in output.splitlines():
            if line in ("__STAT__", "__VMRSS__", "__UPTIME__", "__MEMTOTAL__"):
                current = line
            elif current is not None:
                sections[current] = line
                current = None

        if not all(k in sections for k in ("__STAT__", "__VMRSS__", "__UPTIME__", "__MEMTOTAL__")):
            return None

        # VmRSS from /proc/<pid>/status  →  "VmRSS:   12345 kB"
        rss_kb      = int(sections["__VMRSS__"].split()[1])

        # MemTotal from /proc/meminfo  →  "MemTotal:   524288 kB"
        mem_total_kb = int(sections["__MEMTOTAL__"].split()[1])
        mem_pct      = round(rss_kb / mem_total_kb * 100.0, 2) if mem_total_kb else 0.0

        # CPU% from /proc/<pid>/stat
        # comm field may contain spaces and parentheses; find its end by the last ')'
        stat_line = sections["__STAT__"]
        rp        = stat_line.rfind(")")
        fields    = stat_line[rp + 2:].split()
        # After comm+state: index 11=utime, 12=stime, 19=starttime
        utime     = int(fields[11])
        stime     = int(fields[12])
        starttime = int(fields[19])

        uptime_sec  = float(sections["__UPTIME__"].split()[0])
        process_age = uptime_sec - starttime / _CLK_TCK
        if process_age <= 0:
            return None

        cpu_pct = round(((utime + stime) / _CLK_TCK) / process_age * 100.0, 2)

        return ProcessSample(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            pid_count=1,
            cpu_percent=cpu_pct,
            mem_percent=mem_pct,
            rss_mb=round(rss_kb / 1024.0, 2),
        )

    except (ValueError, IndexError, KeyError, ZeroDivisionError):
        return None


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
    else:
        ServiceSummary.save_csv(summary, csv_file)


if __name__ == "__main__":
    main()
