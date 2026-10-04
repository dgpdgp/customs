from app.models import UserSession
from tests.conftest import register_and_login


def test_protected_page_redirects_to_login(client):
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/dashboard"


def test_register_sets_httponly_cookie_and_opens_dashboard(client):
    register_and_login(client)
    cookie_header = client.cookies.get("access_token")
    assert cookie_header
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Новая декларация" in page.text


def test_register_validation_errors(client):
    def register(email, password, confirm):
        return client.post("/register", data={"email": email, "password": password, "password_confirm": confirm})

    assert "Пароли не совпадают" in register("a@example.com", "password1", "password2").text
    assert "не короче 8" in register("a@example.com", "short", "short").text
    assert "Некорректный email" in register("not-an-email", "password1", "password1").text
    register_and_login(client, "dup@example.com")
    client.cookies.clear()
    assert "уже зарегистрирован" in register("DUP@example.com", "password1", "password1").text


def test_password_is_hashed(client):
    from app.database import SessionLocal
    from app.models import User

    register_and_login(client, "hash@example.com", "my-plain-password")
    with SessionLocal() as db:
        user = db.query(User).filter_by(email="hash@example.com").one()
        assert user.password_hash.startswith("$2")  # bcrypt
        assert "my-plain-password" not in user.password_hash


def test_login_form_wrong_password(client):
    register_and_login(client, "u@example.com", "correct-pass")
    client.cookies.clear()
    response = client.post("/login", data={"email": "u@example.com", "password": "wrong-pass"})
    assert response.status_code == 401
    assert "Неверный email или пароль" in response.text


def test_login_rejects_open_redirect(client):
    register_and_login(client, "r@example.com", "correct-pass")
    client.cookies.clear()
    response = client.post("/login", data={"email": "r@example.com", "password": "correct-pass",
                                           "next": "//evil.example.com"}, follow_redirects=False)
    assert response.headers["location"] == "/dashboard"


def test_api_login_bearer_and_logout_revokes_token(client):
    assert client.post("/api/auth/register", json={"email": "api@example.com", "password": "api-password"}
                       ).status_code == 201
    token = client.post("/api/auth/login", json={"email": "api@example.com", "password": "api-password"}
                        ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/auth/me", headers=headers).json()["email"] == "api@example.com"
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    # После выхода тот же JWT больше не принимается: сессия отозвана в БД.
    assert client.get("/api/auth/me", headers=headers).status_code == 401

    from app.database import SessionLocal
    with SessionLocal() as db:
        assert db.query(UserSession).filter(UserSession.revoked_at.isnot(None)).count() == 1


def test_tampered_token_rejected(client):
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer abc.def.ghi"}).status_code == 401
