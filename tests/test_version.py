"""Tests for the exporter's own version reporting.

tessera_exporter_build_info is the entire contents of /metrics, and its version
label comes from a hand-maintained constant. That constant has drifted before:
the v1.0.0 and v1.0.1 images were both published reporting version="0.1.0".

CI pins VERSION to the release tag in the publish job, so the binding check
lives there. These tests cover the parts that don't need a tag to verify.
"""

from __future__ import annotations

import re

from tessera_exporter import VERSION, build_self_metrics

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_version_is_semver():
    """The publish workflow compares VERSION against a v*.*.* tag."""
    assert SEMVER.match(VERSION), f"VERSION must be MAJOR.MINOR.PATCH, got {VERSION!r}"


def test_self_metrics_exposition_format():
    """/metrics must stay parseable: HELP, TYPE, then the sample."""
    body = build_self_metrics()

    assert body.endswith("\n"), "exposition format requires a trailing newline"

    lines = body.strip().split("\n")
    assert lines[0] == "# HELP tessera_exporter_build_info Exporter version info"
    assert lines[1] == "# TYPE tessera_exporter_build_info gauge"
    assert lines[2] == f'tessera_exporter_build_info{{version="{VERSION}"}} 1'


def test_self_metrics_is_not_empty():
    """The promtool CI job scrapes this endpoint.

    An empty body would pass `promtool check metrics` silently, so removing the
    only metric here would quietly reduce that job to a no-op.
    """
    assert build_self_metrics().strip(), "/metrics must expose at least one sample"
