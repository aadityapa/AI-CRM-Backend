"""Interview links refuse phones and tablets (12 Oct 2026, user decision)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.device_class import (
    DESKTOP_REQUIRED_CODE, handheld_block, is_handheld_user_agent,
)

ANDROID = ("Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36")
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
IPAD = ("Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
WINDOWS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")


@pytest.mark.parametrize("ua", [ANDROID, IPHONE, IPAD])
def test_phones_and_tablets_are_handheld(ua):
    assert is_handheld_user_agent(ua)


@pytest.mark.parametrize("ua", [WINDOWS, MAC, "", None, "testclient"])
def test_computers_and_blank_agents_pass(ua):
    assert not is_handheld_user_agent(ua)


def test_block_body_and_the_client_hint(monkeypatch):
    monkeypatch.delenv("INTERVIEW_ALLOW_MOBILE", raising=False)
    body = handheld_block({"user-agent": ANDROID})
    assert body and body["code"] == DESKTOP_REQUIRED_CODE
    assert handheld_block({"user-agent": WINDOWS, "sec-ch-ua-mobile": "?1"})
    assert handheld_block({"user-agent": WINDOWS, "sec-ch-ua-mobile": "?0"}) is None


def test_the_rule_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("INTERVIEW_ALLOW_MOBILE", "1")
    assert handheld_block({"user-agent": IPHONE}) is None


def test_verify_and_login_both_check_the_device():
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: ast.get_source_segment(src, n) for n in tree.body
             if isinstance(n, ast.FunctionDef)}
    for name in ("candidate_invite_verify", "candidate_invite_login"):
        assert "_handheld_block(request.headers)" in funcs[name], name
