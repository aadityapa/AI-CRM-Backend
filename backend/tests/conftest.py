"""Suite-wide test defaults (23 Oct 2026).

The application lists billing data only from the data start work month
(`billing.data_start_month`, default 2026-03 — `services/billing_rules`). Most
fixtures were written with 2025 dates long before that rule existed, so the
suite runs with the window open; tests of the rule itself set their own month
(`monkeypatch.setenv("BILLING_DATA_START_MONTH", ...)`).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _billing_data_window_open(monkeypatch):
    monkeypatch.setenv("BILLING_DATA_START_MONTH", "2000-01")
    yield
