# tessera_exporter — quick start

A ready-to-run Docker Compose stack for [tessera_exporter](https://github.com/sethdotfm/tessera_exporter):
a Prometheus exporter for Brompton Tessera LED processors, with Prometheus, Grafana
and three provisioned dashboards already wired together.

This branch is the deployment stack only. The exporter source, its tests and the
full documentation live on `main`; the container image is pulled from
`ghcr.io/sethdotfm/tessera_exporter:latest`.

Tested against Tessera SX40 processors running firmware **3.5.2**.

Written for **Docker Desktop on macOS and Windows**, which is where most of these
stacks end up. Everything runs in Docker except the optional syslog receiver, which
Docker Desktop cannot host correctly — see [Syslog](#syslog-optional). On Linux the
whole thing runs in Docker; the differences are called out where they matter.

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

Docker Desktop needs to be running first. If port `3000` or `9090` is already taken,
change the left-hand side of the mapping in `docker-compose.yml` — the right-hand side
is inside the container and should stay as it is.

| Service | URL | Notes |
| --- | --- | --- |
| Grafana | http://localhost:3000 | Dashboards under the Tessera folder |
| Prometheus | http://localhost:9090 | Check Status → Targets if a processor looks missing |
| Exporter | http://localhost:19800/probe?target=192.0.2.50&debug=1 | Human-readable single scrape |
| Loki | http://localhost:3101 | Syslog storage, only used once you install Alloy natively |

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

Tessera processors send their operational log over UDP syslog. Point your processors'
syslog target at this machine, port `514`.

Storage (Loki) runs in the compose stack. **The receiver, Alloy, has to run natively on
macOS and Windows** — it is commented out of `docker-compose.yml` for that reason.
Docker Desktop runs containers inside a VM that never sees the processors' real source
addresses, so every log line would arrive looking identical and the per-processor
filtering the dashboards are built around would be useless. `network_mode: host` does
not rescue you there either; it is a Linux feature that Docker Desktop cannot emulate.

Everything else — exporter, Prometheus, Loki, Grafana — stays in Docker. Alloy is the
one piece that moves onto the host.

> **On Linux?** Uncomment the `tessera-exporter-syslog-alloy` service in
> `docker-compose.yml` and skip this entire section. It uses the same `config.alloy`,
> runs with `network_mode: host`, and needs no native install.

### macOS

```bash
# from this directory:
brew install grafana-alloy

# Install the tessera_exporter config
mkdir -p /opt/homebrew/etc/grafana-alloy
cp te-syslog-alloy/config.alloy /opt/homebrew/etc/grafana-alloy/config.alloy

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

**The directory may not exist after `brew install`** — hence the `mkdir -p`. The
formula creates it, but Homebrew does not link empty directories into the prefix,
and there is no default config shipped to populate it. On a fresh machine you get
no directory, and `cp` fails with "No such file or directory".

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

### Windows

> **Windows Firewall blocks inbound UDP 514 by default, and the Alloy installer does
> not open it.** Every other step can be perfect and you will still receive nothing,
> with no error anywhere — Alloy sits waiting on a port the packets never reach. This
> is step 3 below, and it is not optional.

**1. Install Alloy** with [winget or the graphical installer](https://grafana.com/docs/alloy/latest/set-up/install/windows/)
(`alloy-installer-windows-amd64.exe` from the GitHub releases page). It installs to
`%PROGRAMFILES%\GrafanaLabs\Alloy` and registers itself as a Windows service set to
start automatically.

**2. Install the config** — **from an elevated prompt**:

```
copy te-syslog-alloy\config.alloy "%PROGRAMFILES%\GrafanaLabs\Alloy\config.alloy"
```

Writing to `%PROGRAMFILES%` fails from a normal prompt. Depending on your shell that
can pass by quietly, leaving the installer's default config in place and Alloy running
happily while collecting nothing. Confirm it actually landed:

```powershell
Get-Content "$env:PROGRAMFILES\GrafanaLabs\Alloy\config.alloy" | Select-String "tessera_syslog_raw"
```

No output means the copy did not take.

**3. Open UDP 514 inbound** — elevated PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Tessera syslog" -Direction Inbound -Protocol UDP -LocalPort 514 -Action Allow
```

**4. Restart the service** — `services.msc`, right-click **Alloy**, *All Tasks →
Restart*. Do this after every config edit; Alloy reads the file only at startup.

**5. Verify**, before you need it to work. This sends a syslog packet to Alloy over
loopback, testing Alloy and Loki without involving the network or the processors:

```powershell
$u=New-Object System.Net.Sockets.UdpClient
$b=[Text.Encoding]::ASCII.GetBytes("<134>Sep 16 10:00:00 tessera: test message")
$u.Send($b,$b.Length,"127.0.0.1",514)

curl.exe -s "http://localhost:3101/loki/api/v1/label/job/values"
```

`tessera_syslog` in the response means Alloy and Loki are working, and anything still
missing is between the processors and this host — firewall, the processors' syslog
target, or simply nothing having happened yet (Tessera emits on events, not on a
timer; recall a preset or toggle blackout to force lines out). Nothing back means the
problem is local: check `Get-Service Alloy` and `Get-NetUDPEndpoint -LocalPort 514`.

Unlike the macOS install, the service is pointed at that one file rather than a
directory, so a stray second `.alloy` file next to it is ignored. Command-line
arguments live in the registry under `HKEY_LOCAL_MACHINE\SOFTWARE\GrafanaLabs\Alloy`
if you need to change them.

### Either way

`te-syslog-alloy/config.alloy` is the same file the Docker service uses. It listens on
`:514` and pushes to Loki's published port (`127.0.0.1:3101`), which is correct for a
native install and under `network_mode: host` alike — there is no separate config to
keep in sync.

Make sure nothing else on the machine already holds UDP `514`, and that your firewall
allows it inbound — see step 3 above for the Windows rule.

Running several receivers on one host, each on its own port, is a Linux-only
arrangement using `te-syslog-alloy/config-bridge.alloy`; see the
[main branch README](https://github.com/sethdotfm/tessera_exporter#readme).

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

**No syslog arriving, but metrics are fine?** On Windows, check the firewall rule
first — inbound UDP `514` is blocked by default and the Alloy installer does not open
it, which produces exactly this: dashboards populated, syslog panel empty, nothing
logged anywhere. See step 3 under [Windows](#windows). Next most likely is the config
copy silently failing without an elevated prompt, then the processors simply having
nothing to report — Tessera emits on events, not on a timer.

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
