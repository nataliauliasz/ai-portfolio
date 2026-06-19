import os
from pathlib import Path

try:
    from app.env_utils import load_local_env_file
except ModuleNotFoundError:
    from env_utils import load_local_env_file

load_local_env_file()


def normalize_windows_path(path: str) -> str:
    # Keep Windows template/data paths compatible with libraries that dislike \\?\ prefixes.
    if os.name == "nt" and path.startswith("\\\\?\\"):
        return path[4:]
    return path


APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
DB_PATH = Path(os.environ.get("DB_PATH") or str(PROJECT_DIR / "people.sqlite3"))
LESSONS_LEARNED_DB_PATH = normalize_windows_path(
    str(os.environ.get("LESSONS_LEARNED_DB_PATH") or (DB_PATH.parent / "lessons_learned.sqlite3"))
)
AUTH_SESSION_DB_PATH = normalize_windows_path(
    str(os.environ.get("AUTH_SESSION_DB_PATH") or (DB_PATH.parent / "auth_sessions.sqlite3"))
)
PEOPLE_DB_PATH = normalize_windows_path(str(DB_PATH))
PEOPLE_SEED_PATH = normalize_windows_path(str(APP_DIR / "people_seed.json"))
TEMPLATES_DIR = normalize_windows_path(str(APP_DIR / "templates"))
