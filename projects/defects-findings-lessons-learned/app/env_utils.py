import os
from pathlib import Path
from urllib.parse import urlparse


_TRUE_VALUES = {"1", "true", "yes", "y", "on"}


def parse_bool_env(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in _TRUE_VALUES


def get_app_env() -> str:
    value = (os.environ.get("APP_ENV") or os.environ.get("FLASK_ENV") or "development").strip().lower()
    return value or "development"


def is_production_env() -> bool:
    return get_app_env() in {"prod", "production"}


def load_local_env_file() -> None:
    if parse_bool_env(os.environ.get("FLASK_SKIP_DOTENV"), default=False):
        return
    if is_production_env():
        return

    env_path = Path(os.environ.get("APP_LOCAL_ENV_FILE") or (Path(__file__).resolve().parent.parent / ".env"))
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key.startswith("#") or key in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def require_https_url(env_name: str, default: str, *, trailing_slash: bool) -> str:
    value = (os.environ.get(env_name) or default).strip()
    if not value:
        raise RuntimeError(f"Brak wymaganej zmiennej srodowiskowej {env_name}.")

    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise RuntimeError(f"{env_name} musi wskazywac na adres HTTPS.")

    normalized = value.rstrip("/")
    return f"{normalized}/" if trailing_slash else normalized


def require_http_or_https_url(env_name: str, default: str, *, trailing_slash: bool) -> str:
    value = (os.environ.get(env_name) or default).strip()
    if not value:
        raise RuntimeError(f"Brak wymaganej zmiennej srodowiskowej {env_name}.")

    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"{env_name} musi wskazywac na adres HTTP albo HTTPS.")

    normalized = value.rstrip("/")
    return f"{normalized}/" if trailing_slash else normalized
