from fastapi import HTTPException, Header
from typing import Optional
import time, jwt  # PyJWT
from .config import get_auth_secret, get_user_peer_map

# Usuarios demo
USERS = {
    "mateo": "123456",
    "liz": "1234567890",
    "sebas": "seabs321",
    "omar": "omarcintilin"
}

# Mapa usuario -> API del peer (desde ENV)
USER_PEER_HTTP = get_user_peer_map()

# JWT
AUTH_SECRET = get_auth_secret()
ALGO = "HS256"
TTL_SECONDS = 60 * 60  # 1 hora

def validate_credentials(username: str, password: str) -> bool:
    return USERS.get(username) == password

def get_assigned_peer(username: str) -> str:
    base = USER_PEER_HTTP.get(username)
    if not base:
        raise HTTPException(500, f"No hay peer HTTP asignado para '{username}' en USER_PEER_HTTP_JSON")
    return base

def issue_token(username: str) -> str:
    now = int(time.time())
    payload = {"sub": username, "iat": now, "exp": now + TTL_SECONDS}
    return jwt.encode(payload, AUTH_SECRET, algorithm=ALGO)

def decode_token(token: str):
    try:
        return jwt.decode(token, AUTH_SECRET, algorithms=[ALGO])
    except jwt.PyJWTError:
        return None

def require_bearer(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401, "Invalid token")
    return payload 
