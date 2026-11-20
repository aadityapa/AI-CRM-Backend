"""ONE resolver for the interview-platform store (18 Nov 2026).

Production: AI Costs answered "password authentication failed for user karnex_app"
while logins worked — `ai._db_target()` built the Postgres URL WITHOUT encoding the
password, `main._auth_db_target()` encoded it. A password with `@ # % / :` then
reached Postgres wrong. Every copy now calls `auth_db.default_db_target()`.
"""
from __future__ import annotations

from urllib.parse import unquote, urlparse

import ai
import ats
from auth_db import default_db_target
from services.interview import question_service

_ENV = ("AUTH_DB_URL", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


def _parts(monkeypatch, password: str) -> None:
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DB_HOST", "karnex-db.example.rds.amazonaws.com")
    monkeypatch.setenv("DB_NAME", "karnex")
    monkeypatch.setenv("DB_USER", "karnex_app")
    monkeypatch.setenv("DB_PASSWORD", password)


def test_a_password_with_special_characters_survives_the_url(monkeypatch):
    secret = "Ab@12#x%/y:z"
    _parts(monkeypatch, secret)
    u = urlparse(default_db_target())
    assert u.hostname == "karnex-db.example.rds.amazonaws.com"
    assert unquote(u.username) == "karnex_app"
    assert unquote(u.password) == secret
    assert u.path == "/karnex"


def test_every_module_uses_the_same_target(monkeypatch):
    _parts(monkeypatch, "p@ss#word")
    want = default_db_target()
    assert ai._db_target() == want
    assert ats._db_target() == want
    assert question_service._db_target() == want


def test_auth_db_url_wins_and_is_normalised(monkeypatch):
    _parts(monkeypatch, "x")
    monkeypatch.setenv("AUTH_DB_URL", "postgresql://u:p@db.remote.example:5432/k")
    assert default_db_target() == "postgresql://u:p@db.remote.example:5432/k?sslmode=require"
    assert ai._db_target() == default_db_target()


def test_no_postgres_configured_means_the_local_file(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    assert not default_db_target().startswith("postgres")
