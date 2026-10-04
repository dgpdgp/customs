"""Общие фикстуры. Окружение настраивается до импорта приложения,
чтобы тесты работали с временной БД и не трогали data/."""

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="customs-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production-0123456789"
os.environ["ANTHROPIC_API_KEY"] = "test-key-never-used"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.limits import login_throttle  # noqa: E402
from app.main import app, register_throttle  # noqa: E402
from app.services import llm  # noqa: E402
from tests.fake_llm import fake_extraction_result  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    login_throttle._failures.clear()
    register_throttle._failures.clear()
    yield


@pytest.fixture(autouse=True)
def _restore_runtime_settings():
    """Админ-панель меняет общий объект настроек — возвращаем его после каждого теста."""
    from app import runtime_settings
    from app.config import get_settings

    settings = get_settings()
    snapshot = {d.key: getattr(settings, d.key) for d in runtime_settings.EDITABLE}
    admin_emails = settings.admin_emails
    yield
    for key, value in snapshot.items():
        setattr(settings, key, value)
    settings.admin_emails = admin_emails
    runtime_settings._baseline.clear()
    llm._client.cache_clear()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fake_llm(monkeypatch):
    """Подменяет вызов Claude API детерминированным ответом. Тест может
    изменить `state["result"]`, чтобы смоделировать ошибку модели."""
    state = {"result": fake_extraction_result(), "calls": 0}

    def _extract(reference, commercial):
        state["calls"] += 1
        state["documents"] = [reference.name, *(d.name for d in commercial)]
        return state["result"], llm.LLMCallInfo(model="fake-model", input_tokens=1000, output_tokens=500, duration_ms=5)

    monkeypatch.setattr(llm, "extract_declaration", _extract)
    return state


def register_and_login(client: TestClient, email: str = "broker@example.com", password: str = "s3cret-pass") -> None:
    response = client.post(
        "/register",
        data={"email": email, "password": password, "password_confirm": password, "full_name": "Тест"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


@pytest.fixture
def logged_in(client):
    register_and_login(client)
    return client


def upload_samples(client: TestClient, template: str | None = None, group_by_hs: bool = False):
    files = [
        ("reference_file", ("reference_declaration.xml", (SAMPLES / "reference_declaration.xml").read_bytes(),
                            "application/xml")),
        ("commercial_files", ("invoice_INV-2026-118.xlsx", (SAMPLES / "invoice_INV-2026-118.xlsx").read_bytes(),
                              "application/octet-stream")),
        ("commercial_files", ("packing_list_INV-2026-118.xlsx",
                              (SAMPLES / "packing_list_INV-2026-118.xlsx").read_bytes(), "application/octet-stream")),
    ]
    if template:
        path = SAMPLES / "templates" / template
        files.append(("template_file", (path.name, path.read_bytes(), "application/octet-stream")))
    data = {"group_by_hs": "true"} if group_by_hs else {}
    return client.post("/jobs", files=files, data=data, follow_redirects=False)
