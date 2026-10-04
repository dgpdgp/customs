"""Трёхъязычный интерфейс: русский (по умолчанию), английский, грузинский."""

import re

import pytest

from app import i18n
from tests.conftest import register_and_login, upload_samples


def test_every_message_has_three_languages_with_same_placeholders():
    for key, texts in i18n.MESSAGES.items():
        assert set(texts) == {"ru", "en", "ka"}, key
        placeholders = {lang: set(re.findall(r"\{(\w+)\}", text)) for lang, text in texts.items()}
        assert all(text.strip() for text in texts.values()), key
        assert placeholders["ru"] == placeholders["en"] == placeholders["ka"], key


def test_default_language_is_russian(client):
    page = client.get("/login").text
    assert '<html lang="ru">' in page and "Вход" in page


def test_switch_language_remembers_choice(client):
    response = client.get("/lang/ka?next=/register", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/register"
    assert response.cookies.get("lang") == "ka"

    page = client.get("/login").text
    assert '<html lang="ka">' in page and "შესვლა" in page
    assert 'class="is-active"' in page or "is-active" in page


@pytest.mark.parametrize(("code", "expected"), [("en", "Sign in"), ("ka", "შესვლა"), ("xx", "Вход")])
def test_login_page_languages(client, code, expected):
    client.cookies.set("lang", code)
    assert expected in client.get("/login").text


def test_switch_rejects_external_redirect(client):
    response = client.get("/lang/en?next=//evil.example.com", follow_redirects=False)
    assert response.headers["location"] == "/dashboard"


def test_server_messages_follow_language(client):
    client.cookies.set("lang", "en")
    form = client.post("/register", data={"email": "a@gmail.com", "password": "password1",
                                          "password_confirm": "password2"})
    assert "Passwords do not match" in form.text
    api = client.post("/api/auth/login", json={"email": "nobody@gmail.com", "password": "whatever1"})
    assert api.json()["detail"] == "Wrong email or password"


def test_js_translations_are_embedded(logged_in, fake_llm):
    logged_in.cookies.set("lang", "en")
    upload_samples(logged_in)
    page = logged_in.get("/jobs/1").text
    assert "window.I18N" in page and '"js.col.field": "Field"' in page
    assert '"field.hs_code": "HS code"' in page


def test_job_issues_written_in_language_chosen_at_upload(client, fake_llm):
    register_and_login(client)
    client.cookies.set("lang", "ka")
    upload_samples(client)
    client.cookies.set("lang", "ru")  # смена языка после загрузки не переписывает замечания задачи
    issues = client.get("/api/jobs/1").json()["issues"]
    validator = [i["message"] for i in issues if i["source"] == "validator"]
    assert any("სასაქონლო კოდი ვერ დადგინდა" in m for m in validator)


def test_approve_issues_in_current_language(logged_in, fake_llm):
    upload_samples(logged_in)
    data = logged_in.get("/api/jobs/1").json()["proposed"]
    logged_in.cookies.set("lang", "en")
    issues = logged_in.post("/api/jobs/1/approve", json={"data": data}).json()["issues"]
    assert any("HS code" in i["message"] for i in issues)


def test_llm_is_asked_to_write_issues_in_user_language():
    from app.services import llm
    from app.services.parsers import ParsedDocument

    token = i18n.set_lang("en")
    try:
        text = llm.build_user_content(ParsedDocument("r.xml", "xml", "<a/>"), [])[-1]["text"]
    finally:
        i18n.reset_lang(token)
    assert "английском" in text


def test_switcher_order_is_ka_en_ru(client):
    page = client.get("/login").text
    assert page.index('lang="ka"') < page.index('lang="en"', page.index("lang-switch")) < page.index(
        'lang="ru"', page.index("lang-switch"))


def test_default_language_is_configurable(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "default_language", "ka")
    assert '<html lang="ka">' in client.get("/login").text
    client.cookies.set("lang", "ru")  # явный выбор пользователя важнее настройки
    assert '<html lang="ru">' in client.get("/login").text
