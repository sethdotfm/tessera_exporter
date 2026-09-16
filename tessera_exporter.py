#!/usr/bin/env python3
# tessera_exporter // Version 1.1.1
# https://github.com/sethdotfm/tessera_exporter
"""Prometheus exporter for Brompton Tessera LED processors.

Multi-target exporter (blackbox_exporter pattern).
  /           — landing page
  /metrics    — exporter self-instrumentation only
  /probe      — per-target metrics (?target=<host>[:<port>][&debug=1])
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import math
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import yaml

import schema as _schema

VERSION = "1.1.1"
_DEFAULT_CONFIG = Path(__file__).parent / "tessera.yml"

# Fields folded into tessera_info instead of individual metrics. Provide processor identity for join queries.
_IDENTITY_FIELDS: dict[str, str] = {
    "system/serial-number": "serial",
    "system/processor-name": "processor_name",
    "system/processor-type": "processor_type",
    "system/software-version": "software_version",
    "project/name": "project",
}

# Config

def load_config(path: Optional[str] = None) -> dict:
    p = Path(path) if path else _DEFAULT_CONFIG
    with open(p) as f:
        return yaml.safe_load(f) or {}


# Target parsing

def parse_target(target: str) -> tuple[str, int]:
    """Parse a target string into (host, port).

    Accepts: bare IP, IP:port, [IPv6], [IPv6]:port, with or without http://.
    Default port is 80. Raises ValueError on malformed input.
    """
    # Strip scheme
    if "://" in target:
        target = target.split("://", 1)[1]
    # Strip trailing path / query
    target = target.split("?")[0].split("/")[0].strip()

    if not target:
        raise ValueError("Empty target")

    # IPv6 bracketed address
    if target.startswith("["):
        bracket_end = target.find("]")
        if bracket_end == -1:
            raise ValueError(f"Malformed IPv6 address: {target!r}")
        host = target[1:bracket_end]
        rest = target[bracket_end + 1:]
        if rest == "":
            return host, 80
        if rest.startswith(":"):
            try:
                port = int(rest[1:])
            except ValueError:
                raise ValueError(f"Invalid port in target: {rest[1:]!r}")
            _validate_port(port)
            return host, port
        raise ValueError(f"Unexpected characters after IPv6 address: {rest!r}")

    # host:port or bare host (IPv4 / hostname)
    if ":" in target:
        # rsplit to handle the case where host is a hostname (no colons)
        host, _, port_str = target.rpartition(":")
        if not host:
            raise ValueError(f"Empty hostname in target: {target!r}")
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError(f"Invalid port in target: {port_str!r}")
        _validate_port(port)
        return host, port

    return target, 80


def _validate_port(port: int) -> None:
    if not (1 <= port <= 65535):
        raise ValueError(f"Port {port} out of valid range 1–65535")


# JSON flattening

def flatten_json(data: dict, prefix: str = "") -> dict:
    """Recursively flatten a nested dict to {path: scalar_or_list} pairs.

    List values are kept as-is so classify() can drop Array-type fields.
    None values are kept so callers can skip them explicitly.
    """
    result: dict = {}
    if not isinstance(data, dict):
        return result
    for key, value in data.items():
        path = f"{prefix}/{key}" if prefix else key
        if isinstance(value, dict):
            result.update(flatten_json(value, path))
        else:
            result[path] = value
    return result


# Suffix / scale lookup

def lookup_suffix(path: str, suffix_config: dict) -> tuple[str, float]:
    """Return (name_suffix, scale_factor) for path from the config suffix map.

    First matching glob wins. Suffix entry may be a plain string (scale=1)
    or a dict with 'name' and optional 'scale'.
    """
    for pattern, entry in suffix_config.items():
        if fnmatch.fnmatch(path, pattern):
            if isinstance(entry, str):
                return entry, 1.0
            return entry.get("name", ""), float(entry.get("scale", 1.0))
    return "", 1.0


# Collector / path filtering
# --------------------------------------------------------------------------
def is_enabled(path: str, config: dict) -> bool:
    """Return True if this path should be processed per the config.

    Priority:
      1. include patterns   → True  (override collector-off)
      2. exclude patterns   → False
      3. collector setting  → True/False
      4. default            → True
    """
    includes = config.get("include") or []
    excludes = config.get("exclude") or []

    if any(fnmatch.fnmatch(path, p) for p in includes):
        return True
    if any(fnmatch.fnmatch(path, p) for p in excludes):
        return False

    top = path.split("/")[0]
    collectors = config.get("collectors") or {}
    return bool(collectors.get(top, True))


# Sentinel detection

def is_sentinel(path: str, value: object, sentinel_config: dict) -> bool:
    """Return True if value is a configured sentinel for this path."""
    for pattern, sentinels in sentinel_config.items():
        if fnmatch.fnmatch(path, pattern):
            return value in sentinels
    return False


# Metric name generation

def make_name(schema_parts: list[str], suffix: str = "") -> str:
    """Build a metric name from schema path parts.

    Placeholder segments ({...}) are omitted from the name; their values
    become labels. Dynamic path segments must never appear in metric names.
    """
    static = [p for p in schema_parts if not (p.startswith("{") and p.endswith("}"))]
    slug = "_".join(p.replace("-", "_") for p in static)
    return f"tessera_{slug}{suffix}"


# Prometheus text formatting

def _escape_label_value(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_labels(labels: dict) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{_escape_label_value(str(v))}"' for k, v in sorted(labels.items())]
    return ",".join(parts)


def format_value(v: object) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "NaN"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        # Avoid trailing zeros but keep precision
        formatted = f"{v:.10g}"
        return formatted
    return str(v)


def _emit_family(name: str, samples: list) -> list[str]:
    """Emit HELP, TYPE, and sample lines for one metric family."""
    lines = []
    help_text = samples[0][0].replace("\\", "\\\\").replace("\n", "\\n")
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    for _, labels, value in samples:
        label_str = format_labels(labels)
        val_str = format_value(value)
        if label_str:
            lines.append(f"{name}{{{label_str}}} {val_str}")
        else:
            lines.append(f"{name} {val_str}")
    return lines


# Core metric builder

def build_metrics(
    api_data: dict,
    schema_root: dict,
    config: dict,
) -> tuple[str, dict, list[str]]:
    """Build Prometheus text output from raw API data.

    Returns:
        (prometheus_text, stats, unmatched_paths)

    stats keys:
        exported, dropped_wo_type, dropped_collector, dropped_null,
        sentinel_nan, collisions, unmatched
    """
    stats: dict[str, int] = {
        "exported": 0,
        "dropped_wo_type": 0,
        "dropped_collector": 0,
        "dropped_null": 0,
        "sentinel_nan": 0,
        "collisions": 0,
        "unmatched": 0,
    }

    suffix_config: dict = config.get("suffix") or {}
    sentinel_config: dict = config.get("sentinels") or {}

    # Flatten the full api subtree
    flat = flatten_json(api_data.get("api", api_data))

    # Identity fields -> tessera_info
    identity_labels: dict[str, str] = {}
    for path, label_name in _IDENTITY_FIELDS.items():
        if path in flat and flat[path] is not None:
            identity_labels[label_name] = str(flat[path])

    # families: name -> [(help, labels_dict, numeric_value)]
    families: dict[str, list] = defaultdict(list)

    if identity_labels:
        families["tessera_info"].append((
            "Tessera processor identity (serial, name, type, version, project)",
            identity_labels,
            1,
        ))

    # Collision tracking: (name, frozenset(labels)) -> first path
    seen_keys: dict[tuple, str] = {}

    # Unmatched paths for debug/warning output
    unmatched_paths: list[str] = []

    identity_set = frozenset(_IDENTITY_FIELDS.keys())

    for path, value in flat.items():
        # Skip identity fields
        if path in identity_set:
            continue

        # Collector / include / exclude filter
        if not is_enabled(path, config):
            stats["dropped_collector"] += 1
            continue

        # Skip null values silently
        if value is None:
            stats["dropped_null"] += 1
            continue

        # Schema match
        schema_parts, raw_labels = _schema.match(path, schema_root)
        if schema_parts is None:
            unmatched_paths.append(path)
            stats["unmatched"] += 1
            # Export unmatched paths as info metrics so firmware additions are
            # visible. Only the path goes into a label — the value would create
            # a new series every time it changed.
            um_labels = {"path": path}
            key = ("tessera_unknown_path_info", frozenset(um_labels.items()))
            if key not in seen_keys:
                seen_keys[key] = path
                families["tessera_unknown_path_info"].append((
                    "Value path not found in schema — possible firmware addition",
                    um_labels,
                    1,
                ))
            continue

        meta = _schema.leaf(schema_root, schema_parts)

        # Classify
        kind = _schema.classify(meta)
        if kind == "drop":
            stats["dropped_wo_type"] += 1
            continue

        # Drop list values
        if isinstance(value, list):
            stats["dropped_wo_type"] += 1
            continue

        # Sentinel check — emit as NaN so operators can see the state
        if isinstance(value, (int, float)) and is_sentinel(path, value, sentinel_config):
            numeric_value: object = float("nan")
            stats["sentinel_nan"] += 1
        else:
            numeric_value = value

        # Suffix / scale lookup
        name_suffix, scale = lookup_suffix(path, suffix_config)
        if scale != 1.0 and isinstance(numeric_value, (int, float)) and not math.isnan(numeric_value):
            numeric_value = numeric_value * scale

        # Sanitise labels
        labels = {**identity_labels, **{name: val for name, val in (raw_labels or [])}}

        # Info metrics: move value into label
        metric_name: str
        if kind == "info" or (kind == "gauge" and isinstance(numeric_value, str)):
            labels["value"] = str(value)
            name_suffix = name_suffix + "_info" if name_suffix else "_info"
            metric_name = make_name(schema_parts, name_suffix)
            emit_value: object = 1
        else:
            metric_name = make_name(schema_parts, name_suffix)
            emit_value = numeric_value

        # Collision check
        collision_key = (metric_name, frozenset(labels.items()))
        if collision_key in seen_keys:
            logging.warning(
                "Series collision: %s labels=%s produced by both %s and %s — "
                "check rename/dynamic config",
                metric_name, labels, seen_keys[collision_key], path,
            )
            stats["collisions"] += 1
            continue
        seen_keys[collision_key] = path

        help_text = meta.get("Summary", "")
        families[metric_name].append((help_text, labels, emit_value))
        stats["exported"] += 1

    # Format output
    lines: list[str] = []
    for name in sorted(families):
        lines.extend(_emit_family(name, families[name]))

    return "\n".join(lines) + "\n" if lines else "", stats, unmatched_paths


# Failure / success response builders

_PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _failure_lines(reason: str, duration: float) -> list[str]:
    return [
        "# HELP tessera_up Whether the last scrape of the Tessera processor succeeded",
        "# TYPE tessera_up gauge",
        "tessera_up 0",
        "# HELP tessera_probe_failure Reason the last probe failed",
        "# TYPE tessera_probe_failure gauge",
        f'tessera_probe_failure{{reason="{reason}"}} 1',
        "# HELP tessera_scrape_duration_seconds Duration of the last scrape",
        "# TYPE tessera_scrape_duration_seconds gauge",
        f"tessera_scrape_duration_seconds {format_value(duration)}",
    ]


def _success_prefix_lines(duration: float) -> list[str]:
    return [
        "# HELP tessera_up Whether the last scrape of the Tessera processor succeeded",
        "# TYPE tessera_up gauge",
        "tessera_up 1",
        "# HELP tessera_scrape_duration_seconds Duration of the last scrape",
        "# TYPE tessera_scrape_duration_seconds gauge",
        f"tessera_scrape_duration_seconds {format_value(duration)}",
    ]


def _fail(reason: str, t0: float) -> tuple[str, str]:
    """Build a failure response (Prometheus text, content type)."""
    duration = time.monotonic() - t0
    return "\n".join(_failure_lines(reason, duration)) + "\n", _PROM_CONTENT_TYPE


def _network_reason(exc: BaseException) -> str:
    """Classify a network-level exception into a probe failure reason.

    urllib usually wraps the underlying OSError in a URLError, so callers
    pass exc.reason for those; bare OSErrors are classified directly.
    """
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    return "connection_error"


# Probe

def probe(
    host: str,
    port: int,
    schema_root: dict,
    config: dict,
    timeout: float = 10.0,
    debug: bool = False,
) -> tuple[str, str]:
    """Scrape one processor and return (output_text, content_type).

    output_text is Prometheus text format normally, or plain text if debug=True.
    """
    url = f"http://{host}:{port}/api/"
    t0 = time.monotonic()
    fetch_duration = 0.0
    http_status = 0
    body_bytes = 0

    # urllib applies `timeout` to each socket operation separately, so a
    # processor that accepts the connection and then stalls burns it twice
    # (once on connect, once on read). Halve it so the whole fetch fits in
    # the budget the caller gave us.
    sock_timeout = timeout / 2

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=sock_timeout) as resp:
            http_status = resp.status
            raw = resp.read()
            body_bytes = len(raw)
        fetch_duration = time.monotonic() - t0

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logging.error("Bad JSON from %s: %s", url, exc)
            return _fail("bad_json", t0)

        # IP control disabled: API returns a response-code error body
        if isinstance(data, dict) and "response-code" in data:
            logging.warning(
                "IP control disabled on %s:%s (response-code: %s)",
                host, port, data.get("response-code"),
            )
            return _fail("ip_control_disabled", t0)

    except urllib.error.HTTPError as exc:
        logging.error("HTTP %s from %s: %s", exc.code, url, exc.reason)
        return _fail("http_error", t0)
    except urllib.error.URLError as exc:
        # urllib wraps the underlying OSError; classify by exc.reason
        # (which can also be a plain string for some failure modes)
        cause = exc.reason if isinstance(exc.reason, BaseException) else exc
        logging.error("Network error fetching %s: %s", url, exc.reason)
        return _fail(_network_reason(cause), t0)
    except OSError as exc:
        # Direct socket errors (e.g. read timeout after connect)
        logging.error("Network error fetching %s: %s", url, exc)
        return _fail(_network_reason(exc), t0)

    # Build metrics
    metrics_text, stats, unmatched = build_metrics(data, schema_root, config)
    total_duration = time.monotonic() - t0

    if debug:
        lines = [
            f"URL: {url}",
            f"HTTP status: {http_status}",
            f"Response bytes: {body_bytes}",
            f"Fetch time: {fetch_duration:.3f}s",
            f"Total time: {total_duration:.3f}s",
            "",
            f"Exported: {stats['exported']}",
            f"Dropped (W/O / type): {stats['dropped_wo_type']}",
            f"Dropped (collector off): {stats['dropped_collector']}",
            f"Dropped (null): {stats['dropped_null']}",
            f"Sentinel -> NaN: {stats['sentinel_nan']}",
            f"Collisions: {stats['collisions']}",
            f"Unmatched: {stats['unmatched']}",
        ]
        if unmatched:
            lines.append("")
            lines.append(f"Unmatched paths (first {min(10, len(unmatched))}):")
            for p in unmatched[:10]:
                lines.append(f"  {p}")
        return "\n".join(lines) + "\n", "text/plain; charset=utf-8"

    prefix = "\n".join(_success_prefix_lines(total_duration)) + "\n"
    return prefix + metrics_text, _PROM_CONTENT_TYPE


# HTTP handler

_LANDING_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Tessera Exporter</title></head>
<body>
<h1>Tessera Exporter</h1>
<p>Prometheus exporter for Brompton Tessera LED processors.</p>
<ul>
  <li><a href="/probe?target=192.0.2.50">/probe?target=192.0.2.50</a>
      — per-target metrics</li>
  <li><a href="/probe?target=192.0.2.50&amp;debug=1">/probe?target=…&amp;debug=1</a>
      — human-readable debug output</li>
  <li><a href="/metrics">/metrics</a>
      — exporter self-instrumentation (not processor data)</li>
</ul>
<p><strong>Note:</strong> <code>/metrics</code> shows only exporter process stats,
not processor data. If you curl /metrics and see nothing useful, that is expected —
use /probe with a target.</p>
<p><strong>IP control must be enabled</strong> on the processor (Live Control tile
in the Tessera UI) or /probe will return <code>tessera_up 0</code> with
<code>reason="ip_control_disabled"</code>.</p>
</body>
</html>
"""


def build_self_metrics() -> str:
    """Render the exporter's own /metrics body.

    This is the entire contents of /metrics -- processor data lives on /probe.
    The version label is the hand-maintained VERSION constant above, which CI
    pins to the release tag on publish so it cannot drift from the image it
    ships in (v1.0.0 and v1.0.1 both went out reporting 0.1.0).
    """
    lines = [
        "# HELP tessera_exporter_build_info Exporter version info",
        "# TYPE tessera_exporter_build_info gauge",
        f'tessera_exporter_build_info{{version="{VERSION}"}} 1',
    ]
    return "\n".join(lines) + "\n"


class TesseraHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Tessera exporter."""

    schema_root: dict = {}
    config: dict = {}

    def log_message(self, fmt, *args):  # noqa: N802
        logging.debug(fmt, *args)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path == "/":
            self._send(200, "text/html; charset=utf-8", _LANDING_HTML.encode())
        elif path == "/metrics":
            self._serve_self_metrics()
        elif path == "/probe":
            target_list = params.get("target", [])
            if not target_list:
                self._send(400, "text/plain", b"Missing 'target' query parameter\n")
                return
            target = target_list[0]
            debug = params.get("debug", ["0"])[0] == "1"
            self._serve_probe(target, debug)
        else:
            self._send(404, "text/plain", b"Not found\n")

    def _serve_probe(self, target: str, debug: bool) -> None:
        try:
            host, port = parse_target(target)
        except ValueError as exc:
            self._send(400, "text/plain", f"Bad target: {exc}\n".encode())
            return

        # Honour Prometheus scrape timeout header
        timeout = _scrape_timeout(self.headers)

        body, ctype = probe(
            host, port, self.schema_root, self.config, timeout=timeout, debug=debug
        )
        self._send(200, ctype, body.encode())

    def _serve_self_metrics(self) -> None:
        self._send(200, _PROM_CONTENT_TYPE, build_self_metrics().encode())

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Scraper gave up and closed the connection before we answered.
            logging.warning("Client disconnected before response was sent")


def _scrape_timeout(headers, default: float = 5.0) -> float:
    """Fetch budget for one probe, from X-Prometheus-Scrape-Timeout-Seconds.

    The scraper's deadline covers connecting to us, our probe, and reading
    our response back. If we take the whole deadline for the probe alone it
    hangs up first, and a failed scrape stores no samples at all — so an
    unreachable processor shows up as an absent tessera_up rather than 0,
    which is the opposite of what this exporter exists to report. Reserve
    20% of the deadline (at least 0.5s) so tessera_up 0 makes it back.
    """
    raw = headers.get("X-Prometheus-Scrape-Timeout-Seconds")
    if raw:
        try:
            budget = float(raw)
        except ValueError:
            return default
        # Floor of 0.5s, not 1.0s: at a 1s deadline a 1.0s floor would hand
        # back the whole budget and leave no headroom at all.
        return max(0.5, budget - max(0.5, budget * 0.2))
    return default


# Entry point

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prometheus exporter for Brompton Tessera LED processors"
    )
    parser.add_argument(
        "--web.listen-address",
        dest="listen_address",
        default=":19800",
        metavar="ADDRESS",
        help="Address to listen on (default: :19800)",
    )
    parser.add_argument(
        "--config.file",
        dest="config_file",
        default=str(_DEFAULT_CONFIG),
        metavar="FILE",
        help="Path to config file (default: tessera.yml)",
    )
    parser.add_argument(
        "--log.level",
        dest="log_level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Parse listen address
    addr = args.listen_address
    if addr.startswith(":"):
        host = ""
        port_str = addr[1:]
    elif ":" in addr:
        host, _, port_str = addr.rpartition(":")
    else:
        host = addr
        port_str = "19800"
    try:
        port = int(port_str)
    except ValueError:
        parser.error(f"Invalid listen address: {addr!r}")

    config = load_config(args.config_file)
    schema_root = _schema.load()

    logging.info("Tessera exporter %s starting on %s:%s", VERSION, host or "0.0.0.0", port)
    logging.info("Schema: %d leaf paths loaded", len(_schema.all_paths(schema_root)))

    # Attach schema and config to the handler class
    TesseraHandler.schema_root = schema_root
    TesseraHandler.config = config

    # Threading so one slow/unreachable processor can't stall other targets' scrapes
    server = ThreadingHTTPServer((host, port), TesseraHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Shutting down")


if __name__ == "__main__":
    main()
