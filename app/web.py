"""Общие объекты для HTML-страниц: шаблоны Jinja2 с переводами."""

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import i18n
from app.config import BASE_DIR, get_settings
from app.security import csrf_token

APP_DIR = BASE_DIR / "app"
settings = get_settings()


def _i18n_context(request: Request) -> dict:
    """Переводы для каждого шаблона: язык берётся из cookie запроса."""
    lang = i18n.normalize(request.cookies.get(i18n.COOKIE_NAME))
    return {
        "lang": lang,
        "languages": i18n.LANGUAGES,
        "language_short": i18n.LANGUAGE_SHORT,
        "t": lambda key, **params: i18n.t(key, lang, **params),
        "js_i18n": i18n.js_messages(lang),
        "csrf_token": csrf_token(request),
    }


templates = Jinja2Templates(directory=APP_DIR / "templates", context_processors=[_i18n_context])
templates.env.globals["registration_enabled"] = lambda: settings.registration_enabled
templates.env.globals["invite_required"] = lambda: settings.invite_required
