"""Backward compatibility tests for interview mode mapping."""

from utils.interview_mode_mapper import (
    api_interview_mode_from_storage,
    normalize_interview_mode,
    storage_interview_mode_from_api,
    to_display_label,
)


def test_legacy_mock_maps_to_technical():
    assert normalize_interview_mode("mock") == "technical"
    assert to_display_label("mock") == "Technical Interview"


def test_legacy_standard_maps_to_hr():
    assert normalize_interview_mode("standard") == "hr"
    assert to_display_label("standard") == "HR Interview"


def test_canonical_values_round_trip():
    assert normalize_interview_mode("technical") == "technical"
    assert normalize_interview_mode("hr") == "hr"
    assert storage_interview_mode_from_api("technical") == "mock"
    assert storage_interview_mode_from_api("hr") == "standard"


def test_api_read_from_storage():
    assert api_interview_mode_from_storage("mock") == "technical"
    assert api_interview_mode_from_storage("standard") == "hr"


def test_label_strings_for_old_records():
    assert to_display_label("Mock Technical Interview") == "Technical Interview"
    assert to_display_label("Standard Interview") == "HR Interview"
