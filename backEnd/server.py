import argparse
import asyncio
import os
import uuid
from typing import List, Tuple, Optional

from fastapi import FastAPI, HTTPException, Request, Depends, Query, UploadFile, File
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn


from .auth import (
    issue_token, validate_credentials, require_bearer,
    get_assigned_peer, decode_token
)
from .config import (
    parse_bootstrap, get_cors_origins, get_upload_dir, get_max_upload_mb
)
from sockets.peer_node import PeerNode

app = FastAPI()
node: Optional[PeerNode] = None

# ---- CORS desde .env ----
origins = get_cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Archivos Estáticos ----
UPLOAD_DIR = get_upload_dir() #Asigna el nombre del directorio en el que se subirán
os.makedirs(UPLOAD_DIR, exist_ok=True) ##Comprueba si el directorio existe, si no, lo crea
app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name="files") #Hace pública la liga d elos archivos
MAX_MB = get_max_upload_mb() #Asigna el peso máximo del archivo


# ---- EndPoints ----
# -Comprueba que el servidor esté levantado
@app.get("/health")
async def health():
    return {"ok": True}

# -Descubre qué peer soy (puerto y nombre)
@app.get("/whoami")
async def whoami():
    return {
        "name": getattr(node, "name", None),
        "tcp_port": getattr(node, "tcp_port", None)
    }


# ---- AUTH (LogIn) -----
@app.post("/login")
async def login(body: dict):
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    if not validate_credentials(username, password):
        raise HTTPException(401, "Datos incorrectos, inténtalo nuevamente! (:")

    token = issue_token(username)
    peer_http_base = get_assigned_peer(username)  # viene del .env (USER_PEER_HTTP_JSON)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": username,
        "peer_http_base": peer_http_base,
    }


# ---- ENVIO DE MENSAJES PROTEGIDOS
@app.post("/send")
async def send(body: dict, payload = Depends(require_bearer)):
    if not node:
        raise HTTPException(500, "El nodo no se ha iniciado")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Texto vacío")
    author = payload.get("sub") or "desconocido"
    await node.broadcast(text, author)
    return {"ok": True}

@app.get("/messages", response_class=PlainTextResponse)
async def messages(after_id: int = 0, payload = Depends(require_bearer)):
    if not node:
        raise HTTPException(500, "El nodo no se ha iniciado")
    msgs = node.list_messages_after(after_id)
    return "\n".join(f"{mid}|{author}|{text}" for (mid, author, text) in msgs)

# SSE autenticado (token en query porque EventSource no manda headers)
@app.get("/stream")
async def stream(request: Request, token: str = Query(default="")):
    if not decode_token(token):
        raise HTTPException(401, "Token Invalido")
    if not node:
        raise HTTPException(500, "El nodo no ses ha iniciado")

    async def gen():
        src = node.sse_stream()
        async for chunk in src:
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


# ------ SUBEN ARCHIVOS --------
@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    payload = Depends(require_bearer),
    request: Request = None
):
    if not file.filename:
        raise HTTPException(400, "Archivo inválido")

    data = await file.read()
    size_bytes = len(data)
    if size_bytes > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"Archivo demasiado grande (>{MAX_MB} MB)")

    safe_name = "".join(ch for ch in file.filename if ch.isalnum() or ch in ("-", "_", ".", " ")).strip()
    disk_name = f"{uuid.uuid4().hex}__{safe_name or 'file'}"
    out = os.path.join(UPLOAD_DIR, disk_name)
    with open(out, "wb") as f:
        f.write(data)

    base = str(request.base_url).rstrip("/")
    url = f"{base}/files/{disk_name}"
    return {
        "ok": True,
        "url": url,
        "name": safe_name or "file",
        "size": size_bytes,
        "mime": file.content_type or "application/octet-stream",
    }


# Función Main
def main():
    parser = argparse.ArgumentParser(description="API HTTP + puente P2P")
    parser.add_argument("--name", required=True, help="Nombre del peer")
    parser.add_argument("--tcp-port", type=int, required=True, help="Puerto TCP (P2P)")
    parser.add_argument("--http-port", type=int, required=True, help="Puerto HTTP (API)")
    parser.add_argument("--bootstrap", default="", help="ip:puerto separados por coma (ej. 148.220.211.106:5002)")
    parser.add_argument("--advertise", default="", help="Mi host:puerto público (ej. 148.220.211.106:5002)")
    args = parser.parse_args()

    adv = None
    if args.advertise:
        host, p = args.advertise.split(":", 1)
        adv = (host.strip(), int(p))

    global node
    node = PeerNode(
        args.name,
        args.tcp_port,
        parse_bootstrap(args.bootstrap),
        advertise=adv
    )

    config = uvicorn.Config(app, host="0.0.0.0", port=args.http_port, loop="asyncio")
    server = uvicorn.Server(config)

    async def runner():
        await node.start_background()
        await server.serve()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
