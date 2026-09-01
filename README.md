# tessera_exporter

A configurable Prometheus exporter for Brompton Tessera LED processors. On large Tessera systems, monitoring performance, health, and configuration can be a challenge, and the built-in alerting is pretty minimal. This exporter scrapes `http://[processor_ip]/api/` and delivers the data to your visualization tool of choice.

I have also included an optional stack for collecting and managing the Tessera syslog output, which can offer some verbose insights to processor and panel health.

Tested against Tessera SX40 processors running firmware **3.5.2**.

---

## Disclosure

Parts of this code have been created or assisted by generative large language models. The structure of this project, as well as all READMEs and docs, have been considered and written by hand.

---

## Adding to an existing Prometheus installation

Add your processors to `targets/tessera.yml`. Prometheus picks up changes to this file automatically with no reload needed.

```yaml
- targets: ["192.0.2.50"]
  labels:
    site: "STUDIO-1"
    location: "MAIN"
```

Add to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: tessera
    scrape_interval: 6s
    scrape_timeout: 6s
    metrics_path: /probe
    file_sd_configs:
      - files: [/etc/prometheus/tessera_targets/*.yml]
        refresh_interval: 30s
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
        regex: "([^:]+)(:\\d+)?"
        replacement: "$1"
      # "<ip> - <location>", falling back to bare "<ip>". The overview dashboard
      # repeats on this; without it you get no rows.
      - source_labels: [instance, location]
        separator: " - "
        regex: "(.+) - (.+)"
        target_label: processor
        replacement: "$1 - $2"
      - source_labels: [processor, instance]
        separator: ";"
        regex: ";(.+)"
        target_label: processor
        replacement: "$1"
      - target_label: __address__
        replacement: "tessera-exporter:19800"
  - job_name: tessera_exporter
    static_configs:
      - targets: ["tessera-exporter:19800"]
```

Mount the targets directory into your Prometheus container:

```yaml
volumes:
  - ./targets:/etc/prometheus/tessera_targets:ro
```

---

## Syslog (optional)

Tessera processors send their operational log over UDP syslog. By default `docker compose up` also brings up `alloy` (receiver + noise filtering), `loki` (log storage, 30d retention), and `grafana` for visualization.

Point your processors’ syslog target at the IP of this server, port `514`.

Severity is normalized into a `level` label (RFC5424's `informational`/`notice`/`warning`/etc. mapped to Grafana's `debug`/`info`/`warn`/`error`/`critical`) so the Logs panel's built-in level coloring works instead of showing everything as `UNK` (unknown).

Retention is 30 days by default, but `debug`/`info`-level lines expire after 24h. See `retention_stream` in `te-syslog-loki/loki-config.yaml` to adjust.

Two more dashboards are statically provisioned alongside it. "Tessera Overview" shows one repeated row per configured processor; clicking any stat in a row opens "Tessera Processor Detail" scoped to that processor. Both key on the `processor` label built by the relabel rules above rather than on serial number, because serial comes from `tessera_info` and a processor stops publishing that the moment it goes offline — so an unreachable processor keeps its row and shows as OFFLINE instead of silently disappearing.
### Verbatim archive

The stream the dashboard reads is deliberately lossy: routine noise is dropped by the `stage.drop` blocks in `te-syslog-alloy/config.alloy`, and syslog's severity names are remapped into Grafana's vocabulary so the Logs panel colours correctly. That is the right trade for day-to-day monitoring and the wrong one for a fault report.

So the listener also fans out to a second, unedited branch under `job="tessera_syslog_raw"`. Nothing is dropped, and severity is kept as the original RFC5424 name (`informational`, `notice`, `warning`, ...) in a `syslog_severity` label rather than being collapsed into `level`. The message hostname, app name, and process ID are attached as structured metadata, which Loki does not index as streams — so `proc_id` changing on every processor restart costs nothing.

**Tessera sends a minimal syslog frame.** In testing against 3.5.2 the processors emit only a priority, a tag and a message — no timestamp, no hostname and no process ID. Entry timestamps are therefore Alloy's receipt time, and there is no processor-side clock recorded anywhere in the pipeline. Do not set `use_incoming_timestamp` on the listener to try to recover one: with no timestamp to parse, Alloy produces the zero time and Loki rejects the entire batch with `before 0001-01-01`, which silently stops **both** streams.

Both branches share one listener and one Loki writer; the dashboard selects `job="tessera_syslog"` and is unaffected.

Because it keeps everything, this stream is the larger of the two. It defaults to 7 days (vs 30 for the dashboard stream) via `retention_stream` in `te-syslog-loki/loki-config.yaml`.

To pull it out for the manufacturer as standard RFC3164 wire frames — the format any syslog server can re-ingest:

```bash
./export-raw-syslog.sh 192.0.2.50            # last 24h, one processor
./export-raw-syslog.sh 192.0.2.50 72         # last 72h
./export-raw-syslog.sh all 6 > incident.log  # every processor, last 6h
```

```
<132>Sep  1 14:36:34 192.0.2.50 tessera: Panel 3 reporting cable loop fault
<11>Sep  1 14:36:34 192.0.2.50 kernel: Genlock reference lost
```

`PRI` is reconstructed as `facility * 8 + severity` from the stored names. Since the processors send no hostname, the `HOSTNAME` field is filled with the source IP — valid RFC3164, and more use to whoever opens the file.

Set `LOKI_URL` if Loki isn't on `http://localhost:3101`. A single query is capped at 5000 entries; the script warns on stderr if you hit it, in which case narrow the window or use [logcli](https://grafana.com/docs/loki/latest/query/logcli/).


A "Tessera Syslog" dashboard is statically provisioned in Grafana with dropdown filters for serial number, processor name, type, version, and project name. These are the same identity fields `tessera_info` exposes for Prometheus, with an added severity filter derived from the syslog input itself.

**On macOS or Windows**, identity filtering won't work if alloy runs in Docker. This is because Docker Desktop rewrites the source IP of all UDP traffic arriving on a published port; as a result, every log line looks like it came from the same address. Native Linux Docker hosts aren't susceptible to these rewrites.

When running on macOS/Windows, simply run Alloy natively on the host instead. The remaining Loki, Grafana, Prometheus, and tessera-exporter images can remain in Docker.

```bash
# On macOS:
# ~/tessera_exporter/

# Install Alloy with homebrew:
brew install grafana-alloy

# Install the tessera_exporter config (backing up any existing one first)
[ -f /opt/homebrew/etc/grafana-alloy/config.alloy ] && cp /opt/homebrew/etc/grafana-alloy/config.alloy /opt/homebrew/etc/grafana-alloy/config.alloy.old
cp te-syslog-alloy/config-native.alloy /opt/homebrew/etc/grafana-alloy/config.alloy

# To bring up the service
sudo brew services start grafana-alloy

# To bring down the service:
sudo brew services stop grafana-alloy

# To RESTART after editing the config, go through launchd directly:
sudo launchctl kickstart -k system/homebrew.mxcl.grafana-alloy
```

`brew services restart` is unreliable here. Started with `sudo`, Alloy is a root-owned system daemon in `/Library/LaunchDaemons`, so `brew services` run as your own user reports `Running: false` and may not restart anything. Confirm a restart actually happened by checking that the PID changed:

```bash
pgrep -f '/opt/homebrew/opt/grafana-alloy/bin/alloy'
```

`te-syslog-alloy/config-native.alloy` is the same pipeline as `te-syslog-alloy/config.alloy`, just listening on `:514` directly and pushing to Loki's published port (`127.0.0.1:3101`) instead of compose DNS.

---

## Notes

**IP control must be enabled on each processor.** Go to the Live Control tile in the Tessera UI and enable IP control for the loaded project. If `/probe` returns `tessera_up 0` with `reason="ip_control_disabled"`, this is why.

**scrape_interval is 6s by default.** Brompton's only stated limit is to not poll multiple times per second. They do warn that frequent polling may cause "adverse performance" on the Tessera hardware, so back off if your processors are heavily loaded — but 6s has been solid in practice. `scrape_timeout` is pinned to match the interval: left unset it inherits the 10s global default, which Prometheus silently clamps down to the interval anyway. Setting it *above* the interval is the one thing Prometheus rejects outright.

`/metrics` **may look empty.** Processor data is on `/probe`. `/metrics` is exporter self-instrumentation only. Use `/probe?target=192.0.2.10&debug=1` for a human-readable summary of a single scrape.

The Tessera IP Control API (3.5.2) sadly does not currently expose per-panel telemetry. `devices/items/{serial}` provides only `firmware` and `type`. Panel temperature, voltage, and per-panel error detail are visible in the Tessera UI but are not queryable via IP control.