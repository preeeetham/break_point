"""Fixtures for the Ditto target's tests: core's report factories, and Ditto's own files.

These tests pin the Ditto target to what Faultline did before the core/target split: the same probe
traffic, the same fix library, the same protection, the same predictions on the real compose file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TARGET = Path(__file__).resolve().parents[1]
ROOT = TARGET.parents[1]
sys.path.insert(0, str(ROOT))   # so core's test fixtures import under a bare `pytest` too

from tests.conftest import make_report, policy_raw, thresholds, tmp_store  # noqa: E402,F401  (fixtures)

COMPOSE = TARGET / "deploy" / "docker-compose.yml"
NGINX = TARGET / "deploy" / "nginx" / "nginx.conf"
KNOWLEDGE = TARGET / "ditto_knowledge.yaml"
THRESHOLDS = TARGET / "thresholds.yaml"
PROBE = TARGET / "probe.yaml"
RULES = TARGET / "rules.yaml"
APPS = ["policies", "things", "things-search", "connectivity", "gateway"]


@pytest.fixture(scope="module")
def ditto_topology():
    from faultline.topology.graph import build

    return build(str(COMPOSE), "faultline", str(NGINX), str(KNOWLEDGE), live=False)
