#!/usr/bin/env bash
# tessera_exporter export-raw-syslog.sh // Version 1.1.0
# https://github.com/sethdotfm/tessera_exporter
#
# Dumps the verbatim syslog archive (job="tessera_syslog_raw") as standard
# RFC3164 wire-format frames -- the same shape the processor put on the wire,
# so any syslog server (Syslog Watcher included) can re-ingest the file.
#
#   <PRI>MMM DD HH:MM:SS HOSTNAME TAG: MESSAGE
#
# PRI is reconstructed as facility*8 + severity from the stored names.
#
# The timestamp is Alloy's receipt time. Tessera's syslog frames carry no
# timestamp, hostname or proc_id of their own -- only PRI, tag and message --
# so receipt time is the only clock available. The HOSTNAME field is filled
# with the source IP, which is valid RFC3164 and more useful to a reader.
#
#   ./export-raw-syslog.sh 192.0.2.50                 # last 24h, one processor
#   ./export-raw-syslog.sh 192.0.2.50 72              # last 72h
#   ./export-raw-syslog.sh all 6 > incident.log       # all processors, last 6h
#
# Loki caps a single query at max_entries_limit_per_query (5000 by default).
# The script warns on stderr if you hit it; narrow the window, or use logcli.
set -euo pipefail

IP="${1:-all}"
HOURS="${2:-24}"
LOKI="${LOKI_URL:-http://localhost:3101}"
LIMIT=5000

if [ "$IP" = "all" ]; then
  SELECTOR='{job="tessera_syslog_raw"}'
else
  SELECTOR="{job=\"tessera_syslog_raw\", connection_ip_address=\"${IP}\"}"
fi

NOW=$(date -u +%s)
START=$(( NOW - HOURS * 3600 ))

curl -sfG "${LOKI}/loki/api/v1/query_range" \
  --data-urlencode "query=${SELECTOR}" \
  --data-urlencode "start=${START}000000000" \
  --data-urlencode "end=${NOW}000000000" \
  --data-urlencode "limit=${LIMIT}" \
  --data-urlencode "direction=forward" \
| LIMIT="$LIMIT" python3 -c '
import json, sys, os, datetime

# RFC3164 numeric codes. Alloy emits the RFC5424 names on the left.
FACILITY = {
    "kernel": 0, "kern": 0, "user": 1, "mail": 2, "daemon": 3, "auth": 4,
    "syslog": 5, "lpr": 6, "news": 7, "uucp": 8, "cron": 9, "authpriv": 10,
    "ftp": 11, "ntp": 12, "audit": 13, "alert": 14, "clock": 15,
    **{f"local{i}": 16 + i for i in range(8)},
}
SEVERITY = {
    "emergency": 0, "alert": 1, "critical": 2, "error": 3,
    "warning": 4, "notice": 5, "informational": 6, "debug": 7,
}
MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

limit = int(os.environ["LIMIT"])
d = json.load(sys.stdin)
rows = []
for st in d["data"]["result"]:
    s = st["stream"]
    for ts, line in st["values"]:
        rows.append((int(ts), s, line))
rows.sort()

for ns, s, line in rows:
    ts_local = datetime.datetime.fromtimestamp(ns / 1e9)

    fac = FACILITY.get(s.get("message_facility", ""), 1)
    sev = SEVERITY.get(s.get("syslog_severity", ""), 5)
    pri = fac * 8 + sev

    # RFC3164 wants a space-padded day, and a HOSTNAME field. The processors
    # send no hostname, so fall back to the source IP -- which is both valid
    # and more use to whoever reads the file.
    stamp = f"{MON[ts_local.month - 1]} {ts_local.day:2d} {ts_local:%H:%M:%S}"
    host = s.get("syslog_hostname") or s.get("connection_ip_address", "-")
    tag = s.get("syslog_app_name") or "tessera"
    pid = s.get("syslog_proc_id")
    tag = f"{tag}[{pid}]" if pid else tag

    print(f"<{pri}>{stamp} {host} {tag}: {line}")

if not rows:
    print("no entries in range", file=sys.stderr)
    sys.exit(0)

print(f"{len(rows)} entries", file=sys.stderr)
if len(rows) >= limit:
    print(f"WARNING: hit the {limit}-entry cap; output truncated. "
          "Use a shorter window or logcli.", file=sys.stderr)
'
