"""ws_bus.py — Thread-safe WebSocket broadcast registry for FableGo.

All mobile_events WebSocket connections register here on connect and
unregister on disconnect.  Background threads (analysis, export, download)
call broadcast() to push JSON event strings to every connected client.

Usage:
    import ws_bus
    ws_bus.register(ws)      # call from the WS handler on connect
    ws_bus.unregister(ws)    # call from finally: block on disconnect
    ws_bus.broadcast(msg)    # call from any thread; silently skips dead sockets
"""

import logging
import threading

log = logging.getLogger(__name__)

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
    socket causes the socket to be removed from the registry immediately,
    rather than waiting for the receive loop to notice.
    
    INFO-02 FIX: Added debug logging for WebSocket event debugging.
    """
    with _lock:
        targets = set(_clients)          # snapshot so we don't hold the lock during sends
    
    log.debug("WebSocket broadcast to %d clients: %s", len(targets), message[:100])

    dead: set = set()
    for ws in targets:
        try:
            ws.send(message)
        except Exception:
            dead.add(ws)

    if dead:
        log.debug("Removing %d dead WebSocket connections", len(dead))
        with _lock:
            _clients.difference_update(dead)
