# sockets/peer_node.py
# ------------------------------------------------------------
# Peer TCP asíncrono con descubrimiento automático:
#   - Protocolo de texto (tabs) con mensajes:
#       HELLO\t<name>
#       ADDPEER\t<host>\t<port>
#       PEERS\t<host1:port1>,<host2:port2>,...
#       MSG\t<mid>\t<author>\t<text_base64>
#   - Al conectar: envío de HELLO, mi ADDPEER y mi PEERS.
#   - Conjunto known_peers + un loop de reconexión por peer.
#   - Gossip periódico de PEERS.
#   - Flooding con dedupe por mid, buffer local con gid para SSE/UI.
# ------------------------------------------------------------

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Set, Tuple

SEP = "\t"
ENC = "utf-8"

def _b64enc(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode(ENC)).decode(ENC)

def _b64dec(s: str) -> str:
    return base64.urlsafe_b64decode(s.encode(ENC)).decode(ENC)


class PeerNode:
    def __init__(
        self,
        name: str,
        tcp_port: int,
        bootstrap: List[Tuple[str, int]] | None = None,
        advertise: Tuple[str, int] | None = None,
        gossip_interval_sec: int = 20,
    ):
        self.name = name
        self.tcp_port = int(tcp_port)
        self.bootstrap = list(bootstrap or [])
        self.advertise = advertise   # (host,port) público a anunciar
        self.gossip_interval = gossip_interval_sec

        # Conexiones
        self.conns: Set[asyncio.StreamWriter] = set()
        self.conn_lock = asyncio.Lock()

        # Conjunto de peers conocidos
        self.known_peers: Set[Tuple[str, int]] = set()
        self._reconn_tasks: Dict[Tuple[str, int], asyncio.Task] = {}

        # Mensajes
        self._seq = 0         # para mid = "<name>:<seq>"
        self._gid = 0         # id numérico local para UI/SSE
        self._seen: Set[str] = set()
        self._messages: List[Tuple[int, str, str]] = []  # (gid, author, text)

        # Suscriptores SSE
        self._subs: Set[asyncio.Queue] = set()
        self._subs_lock = asyncio.Lock()

        # Tareas y servidor
        self._tasks: List[asyncio.Task] = []
        self._server: Optional[asyncio.base_events.Server] = None

        # Inicializa known_peers con bootstrap + yo mismo (advertise)
        for hp in self.bootstrap:
            if hp and hp[1] > 0:
                self.known_peers.add((hp[0], int(hp[1])))
        if self.advertise and self.advertise[1] > 0:
            self.known_peers.add((self.advertise[0], int(self.advertise[1])))

    # --------------- API pública ---------------

    async def start_background(self):
        """Levanta servidor, reconexiones a peers y gossip."""
        self._server = await asyncio.start_server(self._handle_conn, host="0.0.0.0", port=self.tcp_port)
        self._tasks.append(asyncio.create_task(self._server.serve_forever()))

        # reconectar a todos los peers conocidos (excepto a mí mismo)
        for hp in list(self.known_peers):
            if not self._is_self(hp):
                self._ensure_reconnect(hp)

        # gossip periódico
        self._tasks.append(asyncio.create_task(self._gossip_loop()))

    async def broadcast(self, text: str, author: str):
        """Genera mensaje local y lo difunde por la malla."""
        self._seq += 1
        mid = f"{self.name}:{self._seq}"
        self._seen.add(mid)
        self._new_local_message(author, text)
        await self._send_all(self._make_msg_line(mid, author, text))

    def list_messages_after(self, after_id: int) -> List[Tuple[int, str, str]]:
        if after_id <= 0:
            return list(self._messages)
        return [t for t in self._messages if t[0] > after_id]

    async def sse_stream(self):
        q: asyncio.Queue = asyncio.Queue()
        async with self._subscriber(q):
            while True:
                gid, author, text = await q.get()
                yield f"id: {gid}\n".encode(ENC)
                yield f"data: {author}: {text}\n\n".encode(ENC)

    # --------------- Internos: mensajes/estado ---------------

    def _new_local_message(self, author: str, text: str) -> int:
        self._gid += 1
        gid = self._gid
        self._messages.append((gid, author, text))
        asyncio.create_task(self._notify_subs(gid, author, text))
        return gid

    async def _notify_subs(self, gid: int, author: str, text: str):
        async with self._subs_lock:
            for q in list(self._subs):
                try:
                    q.put_nowait((gid, author, text))
                except Exception:
                    pass

    @asynccontextmanager
    async def _subscriber(self, q: asyncio.Queue):
        async with self._subs_lock:
            self._subs.add(q)
        try:
            yield
        finally:
            async with self._subs_lock:
                self._subs.discard(q)

    # --------------- Internos: util peers ---------------

    def _is_self(self, hp: Tuple[str, int]) -> bool:
        if self.advertise:
            return hp == (self.advertise[0], int(self.advertise[1]))
        return hp[1] == self.tcp_port and hp[0] in {"127.0.0.1", "localhost"}

    def _peers_csv(self) -> str:
        return ",".join(f"{h}:{int(p)}" for (h, p) in sorted(self.known_peers))

    def _add_known_peer(self, hp: Tuple[str, int]) -> bool:
        h, p = hp[0], int(hp[1])
        if p <= 0:
            return False
        tup = (h, p)
        if self._is_self(tup):
            return False
        if tup not in self.known_peers:
            self.known_peers.add(tup)
            self._ensure_reconnect(tup)
            return True
        return False

    def _ensure_reconnect(self, hp: Tuple[str, int]):
        if hp in self._reconn_tasks:
            return
        self._reconn_tasks[hp] = asyncio.create_task(self._reconnect_loop(hp))

    # --------------- Internos: protocolo TCP ---------------

    def _make_msg_line(self, mid: str, author: str, text: str) -> str:
        return f"MSG{SEP}{mid}{SEP}{author}{SEP}{_b64enc(text)}\n"

    def _make_addpeer_line(self, hp: Tuple[str, int]) -> str:
        return f"ADDPEER{SEP}{hp[0]}{SEP}{int(hp[1])}\n"

    def _make_peers_line(self) -> str:
        return f"PEERS{SEP}{self._peers_csv()}\n"

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        async with self.conn_lock:
            self.conns.add(writer)
        try:
            # Handshake: digo quién soy y anuncio mi endpoint + lista de peers
            writer.write(f"HELLO{SEP}{self.name}\n".encode(ENC))
            if self.advertise:
                writer.write(self._make_addpeer_line(self.advertise).encode(ENC))
            writer.write(self._make_peers_line().encode(ENC))
            await writer.drain()

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
                    self._seen.add(mid)
                    self._new_local_message(author, text)
                    await self._send_all(self._make_msg_line(mid, author, text), exclude=writer)
                    continue

                if line.startswith("ADDPEER" + SEP):
                    parts = line.split(SEP)
                    if len(parts) >= 3:
                        host = parts[1].strip()
                        try:
                            port = int(parts[2])
                        except Exception:
                            port = 0
                        if host and port > 0:
                            self._add_known_peer((host, port))
                    continue

                if line.startswith("PEERS" + SEP):
                    csv = line.split(SEP, 1)[1] if SEP in line else ""
                    if csv:
                        for item in csv.split(","):
                            item = item.strip()
                            if not item or ":" not in item:
                                continue
                            h, ps = item.split(":", 1)
                            try:
                                p = int(ps)
                            except Exception:
                                continue
                            self._add_known_peer((h.strip(), p))
                    continue

                # (HELLO u otros mensajes se ignoran por ahora)

        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            async with self.conn_lock:
                self.conns.discard(writer)

    async def _reconnect_loop(self, hp: Tuple[str, int]):
        host, port = hp
        backoff = 1.0
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, port)
                backoff = 1.0
                async with self.conn_lock:
                    self.conns.add(writer)
                await self._handle_conn(reader, writer)
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def _send_all(self, line: str, exclude: Optional[asyncio.StreamWriter] = None):
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
            for w in dead:
                try:
                    w.close()
                    await w.wait_closed()
                except Exception:
                    pass
                self.conns.discard(w)

    async def _gossip_loop(self):
        while True:
            try:
                await asyncio.sleep(self.gossip_interval)
                await self._send_all(self._make_peers_line())
            except Exception:
                await asyncio.sleep(self.gossip_interval)
