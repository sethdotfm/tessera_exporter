# tessera_exporter — quick start

A ready-to-run Docker Compose stack for [tessera_exporter](https://github.com/sethdotfm/tessera_exporter):
a Prometheus exporter for Brompton Tessera LED processors, with Prometheus, Grafana
and three provisioned dashboards already wired together.

This branch is the deployment stack only. The exporter source, its tests and the
full documentation live on `main`; the container image is pulled from
`ghcr.io/sethdotfm/tessera_exporter:latest`.

Tested against Tessera SX40 processors running firmware **3.5.2**.

---

## Get running

**1. Add your processors** to `targets/tessera.yml`:

```yaml
- targets: ["192.0.2.50"]
  labels:
    site: "STUDIO-1"
    location: "MAIN"
```

Prometheus re-reads this file every 30s — no reload, no restart.

**2. Enable IP control on each processor.** Live Control tile in the Tessera UI,
enabled for the loaded project. Without it `/probe` returns `tessera_up 0` with
`reason="ip_control_disabled"`.

**3. Bring it up:**

```bash
docker compose up -d
```

| Service | URL | Notes |
| --- | --- | --- |
| Grafana | http://localhost:3000 | Dashboards under the Tessera folder |
| Prometheus | http://localhost:9090 | Check Status → Targets if a processor looks missing |
| Exporter | http://localhost:19800/probe?target=192.0.2.50&debug=1 | Human-readable single scrape |
| Loki | http://localhost:3101 | Syslog storage, only used if you set up Alloy below |

Grafana installs the `marcusolsson-dynamictext-panel` plugin on first boot; the
Overview and Detail dashboards need it, so give it a few seconds on a cold start.

---

## Dashboards

**Tessera Overview** shows one repeated row per configured processor. Clicking any
stat opens **Tessera Processor Detail** scoped to that processor. **Tessera Syslog**
covers the log side (see below).

Both metric dashboards key on the `processor` label — `"<ip> - <location>"`, falling
back to bare `"<ip>"` — built by the relabel rules in `prometheus.yml`, not on serial
number. Serial comes from `tessera_info`, which a processor stops publishing the
moment it goes offline, so keying on the target label means an unreachable processor
keeps its row and shows as OFFLINE instead of silently disappearing. **If you edit
`prometheus.yml`, keep those two relabel rules** or the dashboards come up empty.

---

## Syslog (optional)

Tessera processors send their operational log over UDP syslog. Loki (storage) runs in
this compose stack; **Alloy (the receiver) has to run natively on the host.**

Docker Desktop rewrites the source IP of UDP traffic arriving on a published port, so
every log line would look like it came from the same address and the dashboard's
per-processor filtering would not work. Running Alloy on the host avoids that, and is
the configuration this project is actually deployed with.

```bash
# macOS, from this directory:
brew install grafana-alloy

# Install the tessera_exporter config
mkdir -p /opt/homebrew/etc/grafana-alloy
cp te-syslog-alloy/config-native.alloy /opt/homebrew/etc/grafana-alloy/config.alloy

sudo brew services start grafana-alloy   # start
sudo brew services stop grafana-alloy    # stop
```

**Homebrew points Alloy at that whole directory, not at a single file** — the
service runs `alloy run /opt/homebrew/etc/grafana-alloy`, and Alloy combines
*every* `*.alloy` file in it into one config. So:

- The filename doesn't matter. `config.alloy` is just a convention.
- **A second `*.alloy` file in there will be merged in, not ignored.** Two configs
  that both declare `loki.source.syslog "tessera"` is a duplicate component name,
  and Alloy exits immediately rather than starting.
- If you want to keep an old config alongside it, give it an extension that isn't
  `.alloy` (`config.alloy.old` is fine) or move it out of the directory entirely.

Homebrew creates the directory empty — it ships no default config — so on a fresh
install there is nothing to back up and nothing to conflict with.

To **restart** after editing the config, go through launchd directly:

```bash
sudo launchctl kickstart -k system/homebrew.mxcl.grafana-alloy
```

`brew services restart` is unreliable here. Started with `sudo`, Alloy is a root-owned
system daemon in `/Library/LaunchDaemons`, so `brew services` run as your own user
reports `Running: false` and may not restart anything. Confirm the PID changed:

```bash
pgrep -f '/opt/homebrew/opt/grafana-alloy/bin/alloy'
```

Then point your processors' syslog target at this host, port `514`.

Severity is normalized into a `level` label (RFC5424's `informational`/`notice`/`warning`
mapped to Grafana's `debug`/`info`/`warn`/`error`/`critical`) so the Logs panel's level
colouring works instead of showing every line as `UNK`.

Retention is 30 days, with `debug`/`info` lines expiring after 24h. Adjust
`retention_stream` in `te-syslog-loki/loki-config.yaml`.

### Verbatim archive

The stream the dashboard reads is deliberately lossy: routine noise is dropped, and
severity is remapped into Grafana's vocabulary. That is the right trade for monitoring
and the wrong one for a fault report — so the listener also fans out to an unedited
branch under `job="tessera_syslog_raw"`, keeping everything with the original RFC5424
severity name in a `syslog_severity` label. It defaults to 7 days.

To pull it out for the manufacturer as standard RFC3164 wire frames, which any syslog
server can re-ingest:

```bash
./export-raw-syslog.sh 192.0.2.50            # last 24h, one processor
./export-raw-syslog.sh 192.0.2.50 72         # last 72h
./export-raw-syslog.sh all 6 > incident.log  # every processor, last 6h
```

```
<132>Sep  1 14:36:34 192.0.2.50 tessera: Panel 3 reporting cable loop fault
<11>Sep  1 14:36:34 192.0.2.50 kernel: Genlock reference lost
```

Set `LOKI_URL` if Loki isn't on `http://localhost:3101`. A single query is capped at
5000 entries; the script warns on stderr if you hit it.

**Tessera sends a minimal syslog frame** — in testing against 3.5.2, only a priority, a
tag and a message. No timestamp, no hostname, no process ID. Entry timestamps are
Alloy's receipt time. Do not set `use_incoming_timestamp` on the listener to try to
recover one: with nothing to parse, Alloy produces the zero time and Loki rejects the
whole batch with `before 0001-01-01`, silently stopping **both** streams.

---

## Adding to an existing stack

Every block you need is marked in `docker-compose.yml` and `prometheus.yml` between
`▼▼▼` and `▲▲▲`. Copy those into your own files, mount `./targets` at
`/etc/prometheus/tessera_targets`, and copy `grafana/provisioning/` into your Grafana
provisioning directory.

---

## Notes

**`scrape_interval` is 6s.** Brompton's only stated limit is to not poll multiple times
per second. They warn that frequent polling may cause adverse performance on the
hardware, so back off if your processors are heavily loaded. `scrape_timeout` is pinned
to match the interval — setting it *above* the interval is the one thing Prometheus
rejects outright.

**`/metrics` may look empty.** Processor data is on `/probe`; `/metrics` is exporter
self-instrumentation only.

**Per-panel telemetry isn't available.** The Tessera IP Control API (3.5.2) exposes only
`firmware` and `type` per panel. Panel temperature, voltage and per-panel error detail
are visible in the Tessera UI but are not queryable over IP control.

---

## Disclosure

Parts of this code have been created or assisted by generative large language models.
The structure of this project, as well as all READMEs and docs, have been considered
and written by hand.
