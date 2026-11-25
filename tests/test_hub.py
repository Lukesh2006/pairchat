"""End-to-end tests for the PairChat offline hub (app/serve.py).

Starts a real hub on a free port and talks to it like a browser would.
Standard library only:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))
import serve  # noqa: E402  (imported for unit-testing helpers)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WS:
    """Tiny masked WebSocket client, enough to speak the PeerJS signalling protocol."""

    def __init__(self, port: int, peer_id: str, token: str = "tok", ua: str = "Firefox/128.0 (X11; Linux)"):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /peerjs?key=peerjs&id={peer_id}&token={token}&version=1.5.4 HTTP/1.1\r\n"
               f"Host: 127.0.0.1:{port}\r\nUser-Agent: {ua}\r\nSec-WebSocket-Version: 13\r\n"
               f"Sec-WebSocket-Key: {key}\r\nConnection: keep-alive, Upgrade\r\nUpgrade: websocket\r\n\r\n")
        self.sock.sendall(req.encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        self.status_line = head.split(b"\r\n")[0].decode()

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> dict:
        b1, b2 = self._read(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read(8))[0]
        data = self._read(n)
        if b1 & 0x0F == 0x8:
            raise ConnectionError("server closed")
        return json.loads(data)

    def send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        head = struct.pack("!B", 0x81)
        if n < 126:
            head += struct.pack("!B", 0x80 | n)
        elif n < 65536:
            head += struct.pack("!BH", 0x80 | 126, n)
        else:
            head += struct.pack("!BQ", 0x80 | 127, n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def close(self) -> None:
        try:
            self.sock.sendall(b"\x88\x80" + os.urandom(4))
            self.sock.close()
        except OSError:
            pass


class HubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.proc = subprocess.Popen([sys.executable, "-u", str(APP / "serve.py"), "--port", str(cls.port), "--no-browser", "--skip-hub-check"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/pairchat-local.json", timeout=0.5)
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("hub did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)

    def get(self, path: str) -> bytes:
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=3).read()

    # ---------- HTTP
    def test_serves_app(self):
        self.assertIn(b"<title>PairChat</title>", self.get("/"))

    def test_hub_discovery(self):
        info = json.loads(self.get("/pairchat-local.json"))
        self.assertTrue(info["local"])
        self.assertIsInstance(info["lanUrls"], list)

    def test_random_id(self):
        a, b = self.get("/peerjs/id"), self.get("/peerjs/id")
        self.assertEqual(len(a), 16)
        self.assertNotEqual(a, b)

    def test_does_not_serve_repo_files(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/../tests/test_hub.py")

    # ---------- WebSocket signalling
    def test_handshake_is_http11_and_says_open(self):
        ws = WS(self.port, "pairchat-v1-100001")
        self.assertEqual(ws.status_line, "HTTP/1.1 101 Switching Protocols")   # Firefox requires 1.1
        self.assertEqual(ws.recv()["type"], "OPEN")
        ws.close()

    def test_room_code_taken(self):
        host = WS(self.port, "pairchat-v1-100002", token="a"); host.recv()
        dup = WS(self.port, "pairchat-v1-100002", token="b")
        self.assertEqual(dup.recv()["type"], "ID-TAKEN")
        dup.close(); host.close()

    def test_unknown_room_expires(self):
        guest = WS(self.port, "guest-unknown"); guest.recv()
        guest.send({"type": "OFFER", "dst": "pairchat-v1-999999", "payload": {}})
        msg = guest.recv()
        self.assertEqual((msg["type"], msg["src"]), ("EXPIRE", "pairchat-v1-999999"))
        guest.close()

    def test_offer_is_forwarded_with_sender(self):
        host = WS(self.port, "pairchat-v1-100003"); host.recv()
        guest = WS(self.port, "guest-a"); guest.recv()
        guest.send({"type": "OFFER", "dst": "pairchat-v1-100003", "payload": {"sdp": "x"}})
        msg = host.recv()
        self.assertEqual((msg["type"], msg["src"], msg["payload"]), ("OFFER", "guest-a", {"sdp": "x"}))
        host.close(); guest.close()

    def test_relay_and_departure(self):
        host = WS(self.port, "pairchat-v1-100004"); host.recv()
        guest = WS(self.port, "guest-b"); guest.recv()
        guest.send({"type": "RELAY", "dst": "pairchat-v1-100004", "payload": {"type": "hello", "name": "Phone"}})
        msg = host.recv()
        self.assertEqual((msg["type"], msg["src"], msg["payload"]["name"]), ("RELAY", "guest-b", "Phone"))
        guest.close()
        msg = host.recv()
        self.assertEqual((msg["type"], msg["src"]), ("RELAY-GONE", "guest-b"))
        host.close()

    def test_relay_to_missing_device_fails(self):
        guest = WS(self.port, "guest-c"); guest.recv()
        guest.send({"type": "RELAY", "dst": "nobody", "payload": {}})
        self.assertEqual(guest.recv()["type"], "RELAY-FAIL")
        guest.close()

    def test_large_relay_message(self):
        host = WS(self.port, "pairchat-v1-100005"); host.recv()
        guest = WS(self.port, "guest-d"); guest.recv()
        blob = "A" * 600_000                                   # a relayed file chunk
        guest.send({"type": "RELAY", "dst": "pairchat-v1-100005", "payload": {"type": "chunk", "b64": blob}})
        self.assertEqual(len(host.recv()["payload"]["b64"]), 600_000)
        host.close(); guest.close()


class HelperTests(unittest.TestCase):
    class Fake:
        def __init__(self, ip, local_ip="10.0.0.5"):
            self.ip, self.local_ip = ip, local_ip

    CAND = {"candidate": {"candidate": "candidate:1 1 udp 2122260223 6f1c2d3e-aaaa-bbbb-cccc-0123456789ab.local 54321 typ host"}}

    def test_mdns_replaced_with_sender_ip(self):
        out = serve.Handler.rewrite_mdns(self.CAND, self.Fake("10.0.0.9"), self.Fake("10.0.0.7"))
        self.assertIn(" 10.0.0.9 54321 typ host", out["candidate"]["candidate"])

    def test_laptop_uses_address_other_device_reached(self):
        out = serve.Handler.rewrite_mdns(self.CAND, self.Fake("127.0.0.1"), self.Fake("10.0.0.7", local_ip="10.0.0.5"))
        self.assertIn(" 10.0.0.5 54321 ", out["candidate"]["candidate"])

    def test_both_on_this_computer_untouched(self):
        out = serve.Handler.rewrite_mdns(self.CAND, self.Fake("127.0.0.1"), self.Fake("127.0.0.1"))
        self.assertEqual(out, self.CAND)

    def test_browser_names(self):
        self.assertEqual(serve.browser_name("Mozilla/5.0 (X11; Linux x86_64) Firefox/154.0"), "Firefox on Linux")
        self.assertEqual(serve.browser_name("Mozilla/5.0 (Linux; Android 14) Chrome/130 Mobile Safari/537.36"), "Chrome on Android")


if __name__ == "__main__":
    unittest.main()
