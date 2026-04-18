"""ws_bus.py — Thread-safe WebSocket broadcast registry for RekitGo.

All mobile_events WebSocket connections register here on connect and
unregister on disconnect.  Background threads (analysis, export, download)
call broadcast() to push JSON event strings to every connected client.

Usage:
    import ws_bus
    ws_bus.register(ws)      # call from the WS handler on connect
    ws_bus.unregister(ws)    # call from finally: block on disconnect
    ws_bus.broadcast(msg)    # call from any thread; silently skips dead sockets
"""

import threading

_lock: threading.Lock = threading.Lock()
_clients: set = set()


def register(ws) -> None:
    """Add a connected WebSocket to the broadcast registry."""
    with _lock:
        _clients.add(ws)


def unregister(ws) -> None:
    """Remove a WebSocket from the registry (called on disconnect)."""
    with _lock:
        _clients.discard(ws)


def broadcast(message: str) -> None:
    """Send *message* to every registered client.

    Each send is attempted individually so a stale or closed connection
    cannot block delivery to healthy ones.  Any exception on a particular
    socket is silently ignored — the client's receive loop will notice the
    broken connection and call unregister() itself.
    """
    with _lock:
        targets = set(_clients)          # snapshot so we don't hold the lock during sends

    for ws in targets:
        try:
            ws.send(message)
        except Exception:
            pass                         # stale socket — receive loop will unregister
