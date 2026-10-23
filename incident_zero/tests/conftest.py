from __future__ import annotations

import pytest

from incident_zero.config import Settings
from incident_zero.events import EventBus
from incident_zero.store import IncidentStore


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.data_dir = str(tmp_path / "data")
    s.practice_dir = str(tmp_path / "practice")
    s.llm_enabled = False
    s.sentinel_interval_s = 0.01
    s.verify_settle_s = 0
    s.verify_window_s = 0.2
    s.approval_timeout_s = 5
    return s


@pytest.fixture
def store(settings):
    return IncidentStore(settings.data_dir)


@pytest.fixture
def bus(store):
    return EventBus(store)
