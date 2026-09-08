"""Timer warning settings resolver."""

from utils.time_warnings import (
    AUDIT_FIELD_BY_KEY,
    resolve_time_warning_settings,
    stamp_time_warning_settings,
)


def test_defaults_enabled():
    cfg = resolve_time_warning_settings({})
    assert cfg["enabled"] is True
    assert cfg["thresholds_sec"]["5min"] == 300
    assert cfg["thresholds_sec"]["30sec"] == 30


def test_custom_thresholds_from_weights():
    cfg = resolve_time_warning_settings(
        {
            "enableTimeWarnings": False,
            "timeWarningSec": {"5min": 240, "2min": 90, "1min": 45, "30sec": 20},
        }
    )
    assert cfg["enabled"] is False
    assert cfg["thresholds_sec"]["5min"] == 240
    assert cfg["thresholds_sec"]["30sec"] == 20


def test_stamp_meta_and_audit_fields():
    meta = {}
    stamp_time_warning_settings(meta, {"enableTimeWarnings": True})
    assert meta["enable_time_warnings"] is True
    assert meta["time_warning_thresholds_sec"]["2min"] == 120
    assert meta["time_warning_audit"] == {}
    assert AUDIT_FIELD_BY_KEY["5min"] == "warning_shown_5min"
