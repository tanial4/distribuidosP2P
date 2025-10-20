import argparse
import asyncio
import os
import uuid
from typing import List, Tuple

from fastapi import FastAPI, HTTPException, Request, Depends, Query, UploadFile, File
from fastapi.responses import PlainTextResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

# Imports locales
from .auth import (
    issue_token, validate_credentials, require_bearer,
    get_assigned_peer, decode_token
)
from .config import (
    parse_bootstrap, get_cors_origins, get_upload_dir, get_max_upload_mb
)
from sockets.peer_node import PeerNode

app = FastAPI()
node: PeerNode | None = None  # se setea en main()

# ----- CORS (desde .env) -----
origins = get_cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----- Archivos estáticos (uploads) -----
UPLOAD_DIR = get_upload_dir()
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name="files")
MAX_MB = get_max_upload_mb()

# ========== Endpoints básicos ==========
@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/whoami")
async def whoami():
    return {
        "name": getattr(node, "name", None),
        "tcp_port": getattr(node, "tcp_port", None)
    }

# ========== AUTH ==========
@app.post("/login")
async def login(body: dict):
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    if not validate_credentials(username, password):
        raise HTTPException(401, "Credenciales inválidas")

    token = issue_token(username)
    # El peer HTTP asignado por usuario viene de .env (USER_PEER_HTTP_JSON)
    peer_http_base = get_assigned_peer(username)

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": username,
        "peer_http_base": peer_http_base,
    }

# ========== CHAT PROTEGIDO ==========
@app.post("/send")
async def send(body: dict, payload = Depends(require_bearer)):
    if not node:
        raise HTTPException(500, "Node not started")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Text vacío")
    author = payload.get("sub") or "desconocido"  # usuario del JWT
    await node.broadcast(text, author)
    return {"ok": True}

@app.get("/messages", response_class=PlainTextResponse)
async def messages(after_id: int = 0, payload = Depends(require_bearer)):
    if not node:
        raise HTTPException(500, "Node not started")
    msgs = node.list_messages_after(after_id)
    # Formato: id|author|text por línea
    return "\n".join(f"{mid}|{author}|{text}" for (mid, author, text) in msgs)

# SSE autenticado (token en query porque SSE no manda headers custom)
@app.get("/stream")
async def stream(request: Request, token: str = Query(default="")):
    if not decode_token(token):
        raise HTTPException(401, "Invalid token")

    if not node:
        raise HTTPException(500, "Node not started")

    async def gen():
        gen_inner = node.sse_stream()
        try:
            async for chunk in gen_inner:
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            pass

    return StreamingResponse(gen(), media_type="text/event-stream")

# ========== UPLOAD DE ARCHIVOS ==========
@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    payload = Depends(require_bearer),
    request: Request = None
):
    if not file.filename:
        raise HTTPException(400, "Archivo inválido")

    # Límite de tamaño (simple). Para archivos grandes, escribir por chunks.
    data = await file.read()
    size_bytes = len(data)
    if size_bytes > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"Archivo demasiado grande (>{MAX_MB} MB)")

    # Nombre seguro
    safe_name = "".join(ch for ch in file.filename if ch.isalnum() or ch in ("-", "_", ".", " ")).strip()
    uid = uuid.uuid4().hex
    disk_name = f"{uid}__{safe_name or 'file'}"

    out_path = os.path.join(UPLOAD_DIR, disk_name)
    with open(out_path, "wb") as f:
        f.write(data)

    base = str(request.base_url).rstrip("/")  # p.ej. http://192.168.1.11:8002
    url = f"{base}/files/{disk_name}"

    return {
        "ok": True,
        "url": url,
        "name": safe_name or "file",
        "size": size_bytes,
        "mime": file.content_type or "application/octet-stream",
    }

# ========== ARRANQUE DEL SERVIDOR ==========
def main():
    parser = argparse.ArgumentParser(description="API HTTP puente para red P2P TCP (multi-dispositivo + uploads)")
    parser.add_argument("--name", required=True, help="Nombre del peer")
    parser.add_argument("--tcp-port", type=int, required=True, help="Puerto TCP del peer (P2P)")
    parser.add_argument("--http-port", type=int, required=True, help="Puerto HTTP de esta API")
    parser.add_argument("--bootstrap", default="", help="Lista ip:puerto separados por coma, ej. 192.168.1.11:5002")
    args = parser.parse_args()

    global node
    node = PeerNode(args.name, args.tcp_port, parse_bootstrap(args.bootstrap))

    config = uvicorn.Config(app, host="0.0.0.0", port=args.http_port, loop="asyncio")
    server = uvicorn.Server(config)

    async def runner():
        await node.start_background()  # inicia peer TCP
        await server.serve()           # inicia API HTTP

    asyncio.run(runner())

if __name__ == "__main__":
    main()
