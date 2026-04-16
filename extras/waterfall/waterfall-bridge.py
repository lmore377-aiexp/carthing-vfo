#!/usr/bin/env python3
"""
waterfall-bridge.py — UDP audio → WebSocket forwarder.

Listens for UDP audio packets (from gqrx's "UDP Controller" or SDR++'s
Network audio sink) and rebroadcasts each datagram verbatim as a binary
WebSocket frame to any connected clients. Pair with waterfall.html, which
computes an FFT on the samples and paints a scrolling waterfall.

Defaults:
    UDP input : 0.0.0.0:7355   (gqrx default)
    WS output : 0.0.0.0:8081

No third-party dependencies.
"""
import base64, hashlib, logging, socket, struct, sys, threading

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s',
                    stream=sys.stdout)
log = logging.getLogger('waterfall')

UDP_PORT = 7355
WS_HOST  = '0.0.0.0'
WS_PORT  = 8081

_WS_MAGIC = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


# ── WebSocket plumbing ───────────────────────────────────────────────────────

def ws_handshake(conn):
    """Complete the RFC-6455 opening handshake. Returns True on success."""
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = conn.recv(2048)
        if not chunk:
            return False
        buf += chunk
    for line in buf.decode('utf-8', errors='replace').split('\r\n'):
        if not line.lower().startswith('sec-websocket-key:'):
            continue
        key = line.split(':', 1)[1].strip()
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_MAGIC).encode()).digest()
        ).decode()
        conn.sendall((
            'HTTP/1.1 101 Switching Protocols\r\n'
            'Upgrade: websocket\r\nConnection: Upgrade\r\n'
            'Sec-WebSocket-Accept: {}\r\n\r\n'
        ).format(accept).encode())
        return True
    return False


def ws_send_binary(conn, data):
    """Send one complete binary frame (opcode 0x2, FIN=1)."""
    n = len(data)
    if n < 126:
        header = bytes([0x82, n])
    elif n < 65536:
        header = struct.pack('>BBH', 0x82, 126, n)
    else:
        header = struct.pack('>BBQ', 0x82, 127, n)
    conn.sendall(header + data)


# ── Client registry ──────────────────────────────────────────────────────────

_clients      = []
_clients_lock = threading.Lock()

def broadcast(data):
    """Send `data` to every connected client, dropping broken ones."""
    dead = []
    with _clients_lock:
        for c in _clients:
            try:
                ws_send_binary(c, data)
            except Exception:
                dead.append(c)
        for c in dead:
            _clients.remove(c)
    for c in dead:
        try: c.close()
        except Exception: pass


def handle_client(conn, addr):
    log.info('WS connect %s', addr)
    try:
        if not ws_handshake(conn):
            return
        with _clients_lock:
            _clients.append(conn)
        # Block until the client goes away. We ignore any frames it sends.
        while conn.recv(4096):
            pass
    except Exception as e:
        log.warning('WS error %s: %s', addr, e)
    finally:
        with _clients_lock:
            if conn in _clients:
                _clients.remove(conn)
        try: conn.close()
        except Exception: pass
        log.info('WS disconnect %s', addr)


# ── Loops ────────────────────────────────────────────────────────────────────

def udp_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', UDP_PORT))
    log.info('UDP audio listener on :%d', UDP_PORT)
    while True:
        data, _addr = s.recvfrom(65536)
        broadcast(data)


def main():
    threading.Thread(target=udp_loop, daemon=True).start()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((WS_HOST, WS_PORT))
    srv.listen(4)
    log.info('WS server on %s:%d', WS_HOST, WS_PORT)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


if __name__ == '__main__':
    main()
