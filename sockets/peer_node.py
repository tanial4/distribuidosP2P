# dentro de class PeerNode, reemplaza el broadcast por este:
async def broadcast(self, text: str, author: str):
    """Emite message con autor (usuario), flooding + dedupe, y lo registra localmente."""
    self._seq += 1
    mid = f"{self.name}:{self._seq}"   # id único por emisor (peer)
    self._seen.add(mid)
    self._new_local_message(author, text)   # autor real (usuario)
    for w in list(self.conns.values()):
        try:
            await self._send_line(w, f"MSG {mid} {author} {text}")
        except Exception:
            pass
