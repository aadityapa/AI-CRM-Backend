"""HR self-registration must be blocked unless ALLOW_PUBLIC_HR_REGISTRATION=true."""

import importlib

from fastapi.responses import JSONResponse


def test_hr_registration_blocked_by_default(monkeypatch):
    main = importlib.import_module("main")
    monkeypatch.delenv("ALLOW_PUBLIC_HR_REGISTRATION", raising=False)

    out = main.auth_register(
        full_name="HR User",
        email="hr@example.com",
        username="hruser",
        password="secret123",
        role="hr",
    )
    assert isinstance(out, JSONResponse)
    assert out.status_code == 403


def test_hr_registration_allowed_when_flag_true(monkeypatch):
    main = importlib.import_module("main")
    monkeypatch.setenv("ALLOW_PUBLIC_HR_REGISTRATION", "true")
    monkeypatch.setattr(
        main,
        "register_user",
        lambda *a, **k: {"username": "hruser", "role": "hr", "full_name": "HR User"},
    )

    out = main.auth_register(
        full_name="HR User",
        email="hr@example.com",
        username="hruser",
        password="secret123",
        role="hr",
    )
    assert out.get("status") == "ok"
