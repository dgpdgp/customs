"""Ограничения для публичного сервера: регистрация, перебор паролей, лимиты LLM."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import get_settings
from tests.conftest import register_and_login, upload_samples

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()

    def set_(**values):
        for key, value in values.items():
            monkeypatch.setattr(s, key, value)

    return set_


def test_registration_can_be_closed(client, settings):
    settings(registration_enabled=False)
    assert "Регистрация закрыта" in client.get("/register").text
    assert "/register" not in client.get("/login").text
    response = client.post("/register", data={"email": "a@example.com", "password": "password1",
                                              "password_confirm": "password1"})
    assert response.status_code == 400 and "Регистрация закрыта" in response.text
    assert client.post("/api/auth/register", json={"email": "a@example.com", "password": "password1"}
                       ).status_code == 403


def test_registration_allowlist(client, settings):
    settings(allowed_emails="boss@example.com, Broker@Example.com")
    assert client.post("/api/auth/register", json={"email": "stranger@example.com", "password": "password1"}
                       ).status_code == 403
    assert client.post("/api/auth/register", json={"email": "broker@example.com", "password": "password1"}
                       ).status_code == 201


def test_login_lockout_after_failures(client, settings):
    settings(login_max_failures=3)
    client.post("/api/auth/register", json={"email": "u@example.com", "password": "right-pass"})
    for _ in range(3):
        assert client.post("/api/auth/login", json={"email": "u@example.com", "password": "wrong"}
                           ).status_code == 401
    # Даже верный пароль не принимается, пока действует блокировка
    blocked = client.post("/api/auth/login", json={"email": "U@example.com", "password": "right-pass"})
    assert blocked.status_code == 429 and "Повторите через" in blocked.json()["detail"]
    form = client.post("/login", data={"email": "u@example.com", "password": "right-pass"})
    assert form.status_code == 429


def test_successful_login_resets_failures(client, settings):
    settings(login_max_failures=3)
    client.post("/api/auth/register", json={"email": "u@example.com", "password": "right-pass"})
    for _ in range(2):
        client.post("/api/auth/login", json={"email": "u@example.com", "password": "wrong"})
    assert client.post("/api/auth/login", json={"email": "u@example.com", "password": "right-pass"}
                       ).status_code == 200
    for _ in range(2):
        client.post("/api/auth/login", json={"email": "u@example.com", "password": "wrong"})
    assert client.post("/api/auth/login", json={"email": "u@example.com", "password": "right-pass"}
                       ).status_code == 200


def test_daily_llm_quota_per_user(client, fake_llm, settings):
    settings(max_llm_requests_per_user_per_day=2)
    register_and_login(client)
    assert upload_samples(client).status_code == 303
    assert upload_samples(client).status_code == 303
    third = upload_samples(client)
    assert third.status_code == 429 and "лимит" in third.text
    assert fake_llm["calls"] == 2


def test_daily_llm_quota_total_and_retry(client, fake_llm, monkeypatch, settings):
    from app.services import llm

    settings(max_llm_requests_per_day=1)
    monkeypatch.setattr(llm, "extract_declaration", lambda r, c: (_ for _ in ()).throw(llm.LLMError("сбой")))
    register_and_login(client)
    upload_samples(client)
    assert client.get("/api/jobs/1").json()["status"] == "failed"
    # Повтор тоже вызывает LLM и упирается в общий лимит
    retry = client.post("/api/jobs/1/retry")
    assert retry.status_code == 429 and "общий суточный лимит" in retry.json()["detail"]


def test_api_docs_flag_and_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200  # по умолчанию включено (локальная разработка)


def test_old_llm_requests_do_not_count(client, fake_llm, settings):
    from app.database import SessionLocal
    from app.limits import LLM_REQUEST_EVENT
    from app.models import DeclarationJob, GenerationLog, User

    settings(max_llm_requests_per_user_per_day=1)
    register_and_login(client)
    with SessionLocal() as db:
        user = db.query(User).one()
        job = DeclarationJob(user_id=user.id, status="failed", files=[], options={}, issues=[])
        db.add(job)
        db.commit()
        db.add(GenerationLog(job_id=job.id, user_id=user.id, event=LLM_REQUEST_EVENT,
                             created_at=datetime.now(UTC) - timedelta(hours=25)))
        db.commit()
    assert upload_samples(client).status_code == 303  # запрос 25-часовой давности не учитывается
    assert upload_samples(client).status_code == 429


def test_create_user_script(client):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "create_user.py"), "admin@example.com", "--name", "Админ"],
        input="admin-password\nadmin-password\n", capture_output=True, text=True, cwd=ROOT,
        env={**os.environ, "DATABASE_URL": get_settings().database_url},
    )
    assert result.returncode == 0, result.stderr
    assert client.post("/api/auth/login", json={"email": "admin@example.com", "password": "admin-password"}
                       ).status_code == 200
