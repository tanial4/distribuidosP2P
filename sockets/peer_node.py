# sockets/peer_node.py
# -------------------------------------------------------------------
# Peer TCP asíncrono simple:
#  - Acepta conexiones TCP y se conecta a peers "bootstrap".
#  - Protocolo de flooding con deduplicación por id (mid = "<name>:<seq>").
#  - Cada mensaje entrante/saliente se guarda con un id numérico incremental
#    (gid) para servir a HTTP (/messages) y SSE (id: gid).
#  - Suscriptores SSE se notifican vía colas asyncio.Queue().
# -------------------------------------------------------------------

from __future__ import annotations

import asyncio
import base64
from typing import Dict, List, Optional, Set, Tuple


SEP = "\t"  # separador para el protocolo TCP (evita conflictos con espacios)
ENC = "utf-8"


def _b64enc(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode(ENC)).decode(ENC)


def _b64dec(s: str) -> str:
    return base64.urlsafe_b64decode(s.encode(ENC)).decode(ENC)


class PeerNode:
    def __init__(self, name: str, tcp_port: int, bootstrap: List[Tuple[str, int]] | None = None):
        self.name = name
        self.tcp_port = int(tcp_port)
        self.bootstrap = list(bootstrap or [])

        # Conexiones TCP activas
        self.conns: Set[asyncio.StreamWriter] = set()
        self.conn_lock = asyncio.Lock()

        # Mensajería
        self._seq = 0                     # contador local para mid (id global de red)
        self._gid = 0                     # contador local numérico para UI (/messages y SSE)
        self._seen: Set[str] = set()      # mids vistos para dedupe de flooding
        self._messages: List[Tuple[int, str, str]] = []  # (gid, author, text)

        # Suscriptores SSE -> colas
        self._subscribers: Set[asyncio.Queue] = set()
        self._subs_lock = asyncio.Lock()

        # Tareas de fondo
        self._tasks: List[asyncio.Task] = []
        self._server: Optional[asyncio.base_events.Server] = None

    # ----------------- Público -----------------

    async def start_background(self):
        """Levanta el servidor y comienza a conectar a los bootstrap."""
        self._server = await asyncio.start_server(self._handle_conn, host="0.0.0.0", port=self.tcp_port)

        # Tarea para aceptar conexiones
        self._tasks.append(asyncio.create_task(self._server.serve_forever()))

        # Tareas de reconexión a bootstrap
        for host, port in self.bootstrap:
            self._tasks.append(asyncio.create_task(self._reconnect_loop(host, port)))

    async def broadcast(self, text: str, author: str):
        """Genera un mensaje local y lo inunda a todos los peers."""
        self._seq += 1
        mid = f"{self.name}:{self._seq}"

        # Marca visto y persiste para UI
        self._seen.add(mid)
        gid = self._new_local_message(author, text)

        # Difunde a todos los peers
        line = self._make_msg_line(mid, author, text)
        await self._send_all(line)

    def list_messages_after(self, after_id: int) -> List[Tuple[int, str, str]]:
        """Devuelve [(gid, author, text)] con gid > after_id (para /messages)."""
        if after_id <= 0:
            return list(self._messages)
        # _messages está en orden creciente por gid
        return [t for t in self._messages if t[0] > after_id]

    async def sse_stream(self):
        """
        Generador async para SSE. Emite líneas 'id:' y 'data:'.
        Cada vez que entra un mensaje nuevo, se empuja al subscriber.
        """
        q: asyncio.Queue = asyncio.Queue()
        async with self._subscriber(q):
            # Opcional: podrías emitir backlog inicial aquí si quisieras
            while True:
                gid, author, text = await q.get()
                # Construye paquete SSE
                yield f"id: {gid}\n".encode(ENC)
                yield f"data: {author}: {text}\n\n".encode(ENC)

    # ----------------- Interno: Mensajería -----------------

    def _new_local_message(self, author: str, text: str) -> int:
        """Persiste mensaje y notifica a suscriptores SSE."""
        self._gid += 1
        gid = self._gid
        self._messages.append((gid, author, text))
        # Notificar a los suscriptores
        asyncio.create_task(self._notify_subscribers(gid, author, text))
        return gid

    async def _notify_subscribers(self, gid: int, author: str, text: str):
        async with self._subs_lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait((gid, author, text))
                except Exception:
                    pass

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _subscriber(self, q: asyncio.Queue):
        async with self._subs_lock:
            self._subscribers.add(q)
        try:
            yield
        finally:
            async with self._subs_lock:
                self._subscribers.discard(q)

    # ----------------- Interno: Protocolo TCP -----------------

    def _make_msg_line(self, mid: str, author: str, text: str) -> str:
        # MSG <sep> mid <sep> author <sep> text(base64)
        return f"MSG{SEP}{mid}{SEP}{author}{SEP}{_b64enc(text)}\n"

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        # Guarda la conexión
        async with self.conn_lock:
            self.conns.add(writer)
        peer = None
        try:
            peer = writer.get_extra_info("peername")
            # Handshake mínimo (opcional): enviamos nuestro nombre
            hello = f"HELLO{SEP}{self.name}\n"
            writer.write(hello.encode(ENC))
            await writer.drain()

            # Bucle de lectura
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode(ENC, errors="ignore").rstrip("\r\n")
                if not line:
                    continue

                if line.startswith("MSG" + SEP):
                    try:
                        _, mid, author, b64 = line.split(SEP, 3)
                        if mid in self._seen:
                            continue
                        text = _b64dec(b64)
                    except Exception:
                        continue

                    # Marca visto y persiste
                    self._seen.add(mid)
                    self._new_local_message(author, text)

                    # Reenvía a otros
                    await self._send_all(self._make_msg_line(mid, author, text), exclude=writer)

                # (Opcional) podrías manejar otros comandos como PEERS, PING, etc.

        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            async with self.conn_lock:
                if writer in self.conns:
                    self.conns.remove(writer)

    async def _reconnect_loop(self, host: str, port: int):
        """Intenta mantener una conexión saliente hacia (host,port)."""
        backoff = 1.0
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, port)
                # Resetea backoff al conectar
                backoff = 1.0
                async with self.conn_lock:
                    self.conns.add(writer)

                # Manejamos esa conexión como si fuera entrante (mismo handler)
                await self._handle_conn(reader, writer)
            except Exception:
                # Espera exponencial simple (máx 30s)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def _send_all(self, line: str, exclude: Optional[asyncio.StreamWriter] = None):
        """Envía 'line' a todas las conexiones activas (menos 'exclude')."""
        dead: List[asyncio.StreamWriter] = []
        async with self.conn_lock:
            for w in list(self.conns):
                if exclude is not None and w is exclude:
                    continue
                try:
                    w.write(line.encode(ENC))
                    await w.drain()
                except Exception:
                    dead.append(w)
            # Elimina conexiones muertas
            for w in dead:
                try:
                    w.close()
                    await w.wait_closed()
                except Exception:
                    pass
                self.conns.discard(w)
