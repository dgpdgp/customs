"""Админ-панель: доступ, настройки и ключи, пользователи, задачи, журналы."""

import re
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import AppSetting, DeclarationJob, User
from app.services import llm
from tests.conftest import register_and_login, upload_samples

ADMIN = "admin@example.com"


def csrf(client, url="/admin/settings") -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', client.get(url).text).group(1)


@pytest.fixture
def admin(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_emails", ADMIN)
    register_and_login(client, ADMIN, "admin-password")
    return client


def test_admin_requires_admin_rights(client):
    assert client.get("/admin", follow_redirects=False).headers["location"].startswith("/login")
    register_and_login(client, "user@example.com")
    assert client.get("/admin").status_code == 404  # раздел не раскрываем
    assert "/admin" not in client.get("/dashboard").text


def test_admin_from_admin_emails_sees_panel(admin):
    assert 'href="/admin"' in admin.get("/dashboard").text
    page = admin.get("/admin")
    assert page.status_code == 200 and "Сервер" in page.text
    for url in ("/admin/settings", "/admin/users", "/admin/jobs", "/admin/logs"):
        assert admin.get(url).status_code == 200, url


def test_forms_require_csrf(admin):
    assert admin.post("/admin/settings/max_upload_mb", data={"value": "5"}).status_code == 422  # нет поля
    assert admin.post("/admin/settings/max_upload_mb", data={"value": "5", "csrf": "forged"}).status_code == 403


def test_setting_applies_immediately_and_resets(admin, fake_llm, monkeypatch):
    token = csrf(admin)
    response = admin.post("/admin/settings/max_llm_requests_per_user_per_day", data={"value": "1", "csrf": token},
                          follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/settings?ok=saved"
    assert get_settings().max_llm_requests_per_user_per_day == 1
    assert upload_samples(admin).status_code == 303
    assert upload_samples(admin).status_code == 429  # новый лимит действует без перезапуска

    admin.post("/admin/settings/max_llm_requests_per_user_per_day/reset", data={"csrf": token})
    assert get_settings().max_llm_requests_per_user_per_day == 30
    with SessionLocal() as db:
        assert db.get(AppSetting, "max_llm_requests_per_user_per_day") is None


def test_invalid_values_are_rejected(admin):
    token = csrf(admin)
    bad = admin.post("/admin/settings/max_upload_mb", data={"value": "0", "csrf": token})
    assert bad.status_code == 400 and "не меньше 1" in bad.text
    bad = admin.post("/admin/settings/llm_effort", data={"value": "ultra", "csrf": token})
    assert bad.status_code == 400 and "Допустимые значения" in bad.text
    bad = admin.post("/admin/settings/registration_invite_code", data={"value": "short", "csrf": token})
    assert bad.status_code == 400
    assert admin.post("/admin/settings/secret_key", data={"value": "x", "csrf": token}).status_code == 404


def test_api_key_is_encrypted_masked_and_used(admin):
    new_key = "sk-ant-api03-NEWKEY-abcdefghijklmnop-WXYZ"
    llm._client()  # клиент со старым ключом уже в кеше
    admin.post("/admin/settings/anthropic_api_key", data={"value": new_key, "csrf": csrf(admin)})

    assert get_settings().anthropic_api_key == new_key
    assert llm._client().api_key == new_key  # кеш клиента сброшен, используется новый ключ
    with SessionLocal() as db:
        stored = db.get(AppSetting, "anthropic_api_key").value
    assert new_key not in stored and "NEWKEY" not in stored  # в базе — только шифротекст
    page = admin.get("/admin/settings").text
    assert new_key not in page and "sk-ant-…WXYZ" in page
    logs = admin.get("/admin/logs").text
    assert "anthropic_api_key = ***" in logs and new_key not in logs


def test_empty_secret_submit_keeps_current_key(admin):
    before = get_settings().anthropic_api_key
    response = admin.post("/admin/settings/anthropic_api_key", data={"value": "", "csrf": csrf(admin)})
    assert response.status_code == 400 and get_settings().anthropic_api_key == before


def test_overrides_are_applied_on_startup(admin):
    from app import runtime_settings

    with SessionLocal() as db:
        db.add(AppSetting(key="max_upload_mb", value="7"))
        db.add(AppSetting(key="anthropic_api_key", value=runtime_settings.encrypt("sk-ant-from-db-key-1234")))
        db.add(AppSetting(key="openai_api_key", value="not-a-valid-token"))  # например, после смены SECRET_KEY
        db.commit()
        failed = runtime_settings.apply_overrides(db)
    assert get_settings().max_upload_mb == 7
    assert get_settings().anthropic_api_key == "sk-ant-from-db-key-1234"
    assert failed == ["openai_api_key"]


def test_user_management(admin):
    token = csrf(admin, "/admin/users")
    admin.post("/admin/users", data={"email": "broker@gmail.com", "password": "broker-pass", "full_name": "Брокер",
                                     "csrf": token})
    with SessionLocal() as db:
        broker = db.query(User).filter_by(email="broker@gmail.com").one()
    assert not broker.is_admin

    admin.post(f"/admin/users/{broker.id}/toggle-admin", data={"csrf": token})
    admin.post(f"/admin/users/{broker.id}/toggle-active", data={"csrf": token})
    with SessionLocal() as db:
        broker = db.get(User, broker.id)
        assert broker.is_admin and not broker.is_active
        me = db.query(User).filter_by(email=ADMIN).one()

    assert admin.post("/api/auth/login", json={"email": "broker@gmail.com", "password": "broker-pass"}
                      ).status_code == 401  # заблокированный не входит
    admin.post(f"/admin/users/{broker.id}/toggle-active", data={"csrf": token})
    admin.post(f"/admin/users/{broker.id}/password", data={"password": "new-broker-pass", "csrf": token})
    assert admin.post("/api/auth/login", json={"email": "broker@gmail.com", "password": "new-broker-pass"}
                      ).status_code == 200

    # Себя заблокировать или разжаловать нельзя
    assert admin.post(f"/admin/users/{me.id}/toggle-active", data={"csrf": token}).status_code == 400
    assert admin.post(f"/admin/users/{me.id}/toggle-admin", data={"csrf": token}).status_code == 400


def test_job_detail_retry_and_delete(admin, fake_llm, monkeypatch):
    from app.services import llm as llm_module

    monkeypatch.setattr(llm_module, "extract_declaration",
                        lambda r, c: (_ for _ in ()).throw(llm_module.LLMError("сбой ИИ")))
    upload_samples(admin)
    page = admin.get("/admin/jobs/1").text
    assert "сбой ИИ" in page and "reference_declaration.xml" in page

    monkeypatch.setattr(llm_module, "extract_declaration",
                        lambda r, c: (fake_llm["result"], llm_module.LLMCallInfo("claude-opus-5-5", 1_000_000, 0, 1)))
    token = csrf(admin, "/admin/jobs/1")
    admin.post("/admin/jobs/1/retry", data={"csrf": token})
    assert admin.get("/api/jobs/1").json()["status"] == "review"
    assert "$4.000" in admin.get("/admin/jobs/1").text  # 1 млн входных токенов Opus 5.5 = $4

    with SessionLocal() as db:
        job_dir = get_settings().upload_dir / str(db.get(DeclarationJob, 1).user_id) / "1"
    assert job_dir.exists()
    admin.post("/admin/jobs/1/delete", data={"csrf": token})
    assert not job_dir.exists()
    with SessionLocal() as db:
        assert db.get(DeclarationJob, 1) is None


def test_overview_cost_estimate(admin, fake_llm, monkeypatch):
    from app.services import llm as llm_module

    call = llm_module.LLMCallInfo("claude-opus-5-5", 500_000, 100_000, 1)
    monkeypatch.setattr(llm_module, "extract_declaration", lambda r, c: (fake_llm["result"], call))
    upload_samples(admin)
    page = admin.get("/admin").text
    assert "$4.00" in page  # 0.5M × $4 + 0.1M × $20 = $4.00


def test_check_key_is_free_and_reports_result(admin, monkeypatch):
    calls = []

    def count_tokens(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(input_tokens=5)

    fake = SimpleNamespace(api_key="k", auth_token=None, credentials=None,
                           messages=SimpleNamespace(count_tokens=count_tokens))
    monkeypatch.setattr(llm, "_client", lambda: fake)
    page = admin.post("/admin/check-key", data={"csrf": csrf(admin)}).text
    assert "Ключ работает" in page and calls[0]["model"] == get_settings().llm_model

    import anthropic
    import httpx2

    def unauthorized(**kwargs):
        response = httpx2.Response(401, request=httpx2.Request("POST", "https://api.anthropic.com"))
        raise anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)

    fake.messages.count_tokens = unauthorized
    assert "Ключ неверный" in admin.post("/admin/check-key", data={"csrf": csrf(admin)}).text


def test_admin_panel_in_georgian(admin):
    admin.cookies.set("lang", "ka")
    assert "ადმინ-პანელი" in admin.get("/admin").text


def test_old_database_gets_is_admin_column(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app import database

    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(255))")
        conn.exec_driver_sql("INSERT INTO users (email) VALUES ('old@example.com')")
    monkeypatch.setattr(database, "engine", old)
    database._add_missing_columns()
    database._add_missing_columns()  # повторный запуск ничего не ломает
    with old.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
        assert "is_admin" in columns
        assert conn.exec_driver_sql("SELECT is_admin FROM users").scalar() == 0
