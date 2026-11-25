#!/usr/bin/env python3
"""PairChat local hub: offline pairing over your own Wi-Fi or hotspot.

Run this on one computer (e.g. your laptop):

    python serve.py

It does two jobs, using only the Python standard library:
  1. Serves the PairChat web app to every device on the same network.
  2. Acts as the pairing server (a minimal PeerJS-compatible signalling server),
     so the 6-digit room code works with NO internet connection.

Phones/laptops join by scanning the invite QR code shown in the app, so nobody
has to type an IP address. Chat messages still travel directly device-to-device
over encrypted WebRTC; this hub only introduces the devices to each other.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import struct
import sys
import threading
import time
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 4 << 20          # 4 MB: signalling is a few KB, relayed file chunks ~0.5 MB
IDLE_TIMEOUT = 60            # seconds; PeerJS clients send a heartbeat every 5 s
APP_PREFIX = "pairchat-v1-"
# Browsers hide their local IP behind random "<uuid>.local" names (mDNS). Phones/hotspots often
# can't resolve those, so the direct link never opens. We swap in the address we actually see.
MDNS_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.local")

clients: dict[str, "Client"] = {}
relay_links: dict[str, set[str]] = {}      # who has relayed chat with whom (to announce departures)
clients_lock = threading.Lock()


def log(text: str) -> None:
    print(f"  [hub] {text}", flush=True)


def label(peer_id: str) -> str:
    return f"room {peer_id[len(APP_PREFIX):]}" if peer_id.startswith(APP_PREFIX) else f"device {peer_id[:6]}"


def other_hubs(my_port: int) -> list[int]:
    """Ports of other PairChat hubs already running on this computer (rooms are NOT shared between hubs)."""
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    def probe(port: int):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/pairchat-local.json", timeout=0.5) as r:
                return port if json.loads(r.read() or b"{}").get("local") else None
        except Exception:
            return None

    ports = [p for p in range(8760, 8781) if p != my_port]
    with ThreadPoolExecutor(len(ports)) as pool:          # all at once: Windows is slow to refuse closed ports
        return [p for p in pool.map(probe, ports) if p]


def browser_name(ua: str) -> str:
    for token, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("SamsungBrowser/", "Samsung Internet"),
                        ("Chrome/", "Chrome"), ("Safari/", "Safari")):
        if token in ua:
            os_name = next((o for o in ("Android", "iPhone", "Windows", "Linux", "Mac") if o in ua), "")
            return f"{name}{' on ' + os_name if os_name else ''}"
    return "unknown browser"


def is_loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip in ("::1", "localhost")


# --------------------------------------------------------------------------- network helpers
def lan_ips() -> list[str]:
    """Best guess at this machine's addresses on the local network (primary first)."""
    found: list[str] = []
    try:  # the interface that holds the default route (sends no packets)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    ips: list[str] = []
    for ip in found:
        if ip.startswith("127.") or ip in ips:
            continue
        ips.append(ip)
    ips.sort(key=lambda ip: ip.startswith("169.254."))  # link-local last
    return ips


# --------------------------------------------------------------------------- websocket plumbing
class Client:
    def __init__(self, peer_id: str, token: str, handler: "Handler"):
        self.id, self.token, self.handler = peer_id, token, handler
        self.lock = threading.Lock()
        self.ip = handler.client_address[0]
        try:
            self.local_ip = handler.connection.getsockname()[0]   # our address as this device sees it
        except OSError:
            self.local_ip = ""

    def send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        n = len(data)
        if n < 126:
            header = struct.pack("!BB", 0x81, n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x81, 126, n)
        else:
            header = struct.pack("!BBQ", 0x81, 127, n)
        try:
            with self.lock:
                self.handler.wfile.write(header + data)
                self.handler.wfile.flush()
        except OSError:
            pass

    def close(self) -> None:
        try:
            with self.lock:
                self.handler.wfile.write(b"\x88\x00")
                self.handler.wfile.flush()
            self.handler.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def read_exact(rfile, n: int) -> bytes:
    data = rfile.read(n)
    if data is None or len(data) < n:
        raise ConnectionError("socket closed")
    return data


def read_message(rfile) -> tuple[int, bytes]:
    """Read one (possibly fragmented) client frame. Returns (opcode, payload)."""
    chunks: list[bytes] = []
    first_opcode = None
    while True:
        b1, b2 = read_exact(rfile, 2)
        fin, opcode = b1 & 0x80, b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", read_exact(rfile, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", read_exact(rfile, 8))[0]
        if length > MAX_FRAME:
            raise ConnectionError("frame too large")
        mask = read_exact(rfile, 4) if b2 & 0x80 else b""
        payload = read_exact(rfile, length)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode >= 0x8:                      # control frames are never fragmented
            return opcode, payload
        if first_opcode is None:
            first_opcode = opcode
        chunks.append(payload)
        if fin:
            return first_opcode, b"".join(chunks)


# --------------------------------------------------------------------------- HTTP handler
class Handler(SimpleHTTPRequestHandler):
    server_version = "PairChatHub/1.0"
    port = 8765

    def log_message(self, fmt, *args):  # keep the console readable
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/pairchat-local.json":
            if not is_loopback(self.client_address[0]):
                log(f"device {self.client_address[0]} opened the app")
            return self.send_json({
                "local": True,
                "lanUrls": [f"http://{ip}:{self.port}/" for ip in lan_ips()],
            })
        if url.path == "/peerjs/id":                     # PeerJS asks for a random ID
            return self.send_text(secrets.token_hex(8))
        if url.path == "/peerjs":
            if "websocket" in self.headers.get("Upgrade", "").lower():
                return self.websocket(parse_qs(url.query))
            log(f"pairing request from {self.client_address[0]} without WebSocket upgrade "
                f"(a proxy or browser extension may be interfering)")
            return self.send_error(426, "WebSocket upgrade required")
        return super().do_GET()

    def do_POST(self):
        """check.html posts its self-test results here so they show up in this console."""
        if urlsplit(self.path).path != "/check-report":
            return self.send_error(404)
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 64 * 1024)
            report = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return self.send_error(400)
        print(f"\n  ===== Connection check from {self.client_address[0]} =====", flush=True)
        for step in report.get("steps", [])[:20]:
            mark = {"ok": "PASS", "fail": "FAIL", "warn": "WARN"}.get(step.get("status"), "INFO")
            print(f"  [{mark}] {str(step.get('name'))[:60]}: {str(step.get('detail'))[:300]}", flush=True)
        print(f"  browser: {str(report.get('ua'))[:200]}\n", flush=True)
        self.send_json({"ok": True})

    def send_json(self, obj):
        self.send_text(json.dumps(obj), "application/json")

    def send_text(self, text, ctype="text/plain"):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    # ----- PeerJS signalling over WebSocket
    def websocket(self, qs):
        peer_id = (qs.get("id") or [""])[0][:64]
        token = (qs.get("token") or [""])[0][:64]
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or not peer_id:
            log(f"bad pairing request from {self.client_address[0]} (missing key or id)")
            self.send_error(400)
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        # Written by hand: BaseHTTPRequestHandler would say "HTTP/1.0 101", which Chrome
        # tolerates but Firefox rejects (RFC 6455 requires HTTP/1.1).
        self.wfile.write((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n").encode())
        self.wfile.flush()
        self.close_connection = True
        self.connection.settimeout(IDLE_TIMEOUT)

        me = Client(peer_id, token, self)
        if not peer_id.startswith(APP_PREFIX):
            log(f"device {me.ip} connected for pairing ({browser_name(self.headers.get('User-Agent', ''))})")
        with clients_lock:
            existing = clients.get(peer_id)
            if existing and existing.token != token:
                taken = True
            else:
                taken = False
                clients[peer_id] = me
        if taken:                                    # someone already owns this room code
            me.send({"type": "ID-TAKEN", "payload": {"msg": "ID is taken"}})
            me.close()
            return
        if existing:
            existing.close()
        me.send({"type": "OPEN"})
        if peer_id.startswith(APP_PREFIX):
            log(f"{label(peer_id)} created on {me.ip}")

        started = time.monotonic()
        reason = "closed by the browser"
        try:
            while True:
                opcode, payload = read_message(self.rfile)
                if opcode == 0x8:                    # close
                    code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                    reason = f"browser closed it (code {code})" if code else "browser closed it"
                    break
                if opcode == 0x9:                    # ping -> pong
                    with me.lock:
                        self.wfile.write(struct.pack("!BB", 0x8A, len(payload)) + payload)
                        self.wfile.flush()
                    continue
                if opcode != 0x1:
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    self.route(me, msg)
                except Exception as e:               # never let one bad message kill the connection
                    log(f"error routing {msg.get('type')} from {me.ip}: {e!r}")
        except socket.timeout:
            reason = f"no heartbeat for {IDLE_TIMEOUT}s"
        except (ConnectionError, OSError) as e:
            reason = f"connection dropped ({e.__class__.__name__}: {e})"
        finally:
            if not peer_id.startswith(APP_PREFIX) and not peer_id.startswith("check-"):
                log(f"device {me.ip} pairing connection ended after {time.monotonic() - started:.0f}s: {reason}")
            gone_to: list[Client] = []
            with clients_lock:
                if clients.get(peer_id) is me:
                    del clients[peer_id]
                    for other in relay_links.pop(peer_id, set()):
                        relay_links.get(other, set()).discard(peer_id)
                        if other in clients:
                            gone_to.append(clients[other])
            for c in gone_to:                        # tell relay partners this device left
                c.send({"type": "RELAY-GONE", "src": peer_id})
            if peer_id.startswith(APP_PREFIX):
                log(f"{label(peer_id)} closed")
            me.close()

    @staticmethod
    def rewrite_mdns(payload, sender: Client, target: Client):
        """Replace '<uuid>.local' ICE candidates with the sender's real LAN address."""
        ip = sender.ip
        if is_loopback(ip):
            if is_loopback(target.ip):
                return payload                       # both on this computer: mDNS works locally
            ip = target.local_ip                     # sender is this laptop: use the address the other device reached us on
        if not ip or ":" in ip:
            return payload
        text = json.dumps(payload)
        new = MDNS_RE.sub(ip, text)
        return payload if new == text else json.loads(new)

    @classmethod
    def route(cls, me: Client, msg: dict):
        mtype = msg.get("type")
        if mtype == "HEARTBEAT":
            return
        dst = str(msg.get("dst", ""))
        with clients_lock:
            target = clients.get(dst)

        if mtype == "RELAY":                         # fallback: chat carried through the hub
            if not target:
                me.send({"type": "RELAY-FAIL", "src": dst})
                return
            with clients_lock:
                new_link = dst not in relay_links.get(me.id, set())
                relay_links.setdefault(me.id, set()).add(dst)
                relay_links.setdefault(dst, set()).add(me.id)
            if new_link and not me.id.startswith("check-"):
                log(f"{label(me.id)} <-> {label(dst)}: direct link unavailable, relaying chat through this laptop")
            target.send({"type": "RELAY", "src": me.id, "payload": msg.get("payload")})
            return

        if mtype not in ("OFFER", "ANSWER", "CANDIDATE", "LEAVE"):
            return
        if target:
            payload = cls.rewrite_mdns(msg.get("payload"), me, target)
            if mtype == "OFFER" and dst.startswith(APP_PREFIX):
                log(f"join request for {label(dst)} from {me.ip}")
            target.send({"type": mtype, "src": me.id, "dst": dst, "payload": payload})
        elif mtype == "OFFER":                       # wrong room code -> "no room found"
            log(f"join attempt for unknown {label(dst)} from {me.ip}")
            me.send({"type": "EXPIRE", "src": dst, "dst": me.id})


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="PairChat offline hub")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the app automatically")
    ap.add_argument("--skip-hub-check", action="store_true", help="don't look for other hubs on this computer (tests/demo)")
    args = ap.parse_args()

    Handler.port = args.port
    handler = partial(Handler, directory=APP_DIR)
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    except OSError as e:
        sys.exit(f"Could not start on port {args.port}: {e}. Try --port 8766")
    httpd.daemon_threads = True

    ips = lan_ips()
    print("\n  PairChat hub is running - no internet needed.\n")
    others = [] if args.skip_hub_check else other_hubs(args.port)
    if others:
        print("  !! WARNING: another PairChat hub is already running on port "
              + ", ".join(map(str, others)) + ".")
        print("  !! Rooms are not shared between hubs - all devices must use the SAME one.")
        print("  !! Close the other window (Ctrl+C), or press Ctrl+C here and use that one.\n")
    print(f"  On this computer : http://localhost:{args.port}/")
    for ip in ips:
        print(f"  Other devices    : http://{ip}:{args.port}/")
    if not ips:
        print("  (No network found - connect this computer to Wi-Fi or a phone hotspot.)")
    print("\n  Create a room, tap 'Invite' and scan the QR code with the other devices.")
    print("  All devices must be on the same Wi-Fi / hotspot. Press Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, [f"http://localhost:{args.port}/"]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
