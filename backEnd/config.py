import os, json
from typing import List, Tuple
from dotenv import load_dotenv

load_dotenv()

def get_env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default

def parse_bootstrap(s: str) -> List[Tuple[str, int]]:
    if not s:
        return []
    out = []
    for it in s.split(","):
        ip, port = it.strip().split(":")
        out.append((ip.strip(), int(port)))
    return out

def get_auth_secret() -> str:
    return get_env("AUTH_SECRET", "cambia-este-secreto")

def get_user_peer_map() -> dict:
    raw = get_env("USER_PEER_HTTP_JSON", "{}")
    try:
        m = json.loads(raw)
        if not isinstance(m, dict):
            return {}
        return m
    except Exception:
        return {}

def get_cors_origins() -> list:
    raw = get_env("CORS_ORIGINS", "")
    if not raw:
        return ["*"]
    return [x.strip() for x in raw.split(",") if x.strip()]


def get_upload_dir() -> str:
    return get_env("UPLOAD_DIR", "uploads")

def get_max_upload_mb() -> int:
    try:
        return int(get_env("MAX_UPLOAD_MB", "25"))
    except Exception:
        return 25
