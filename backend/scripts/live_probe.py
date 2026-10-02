#!/usr/bin/env python3
"""Security probe against a REAL uvicorn (temp SQLite, no secrets, no provider keys).

Starts the app on a free port and replays the attacks the Phase 0 hardening closed: spoofed Host,
Host path injection, proxy headers, public-webhook edge cases, an oversized chunked body, static
files, CORS. Probes that need a non-loopback caller use this machine's LAN address; if there is none
those probes are reported as SKIPPED (never silently passed).

  python scripts/live_probe.py
Exit code 0 = every probe behaved as expected.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lan_address() -> str | None:
    """This machine's outward-facing IPv4 address (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("203.0.113.1", 9))
            address = s.getsockname()[0]
        return None if address.startswith("127.") else address
    except OSError:
        return None


def main() -> int:
    port = free_port()
    lan = lan_address()
    tmp = tempfile.mkdtemp(prefix="liveprobe_")
    env = dict(
        os.environ, MORAA_ENV_FILE="", DATABASE_URL=f"sqlite:///{tmp}/p.db", UPLOAD_DIR=f"{tmp}/up",
        REPORT_DIR=f"{tmp}/rep", LOG_DIR=f"{tmp}/logs", ENVIRONMENT="development",
        IMAGE_PROVIDER_STARTUP_DIAGNOSTICS="false", WHATSAPP_PAY_ENABLED="false", GEMINI_API_KEY="",
        OPENAI_API_KEY="", META_APP_SECRET="", RAZORPAY_WEBHOOK_SECRET="", ALLOW_UNSIGNED_WEBHOOKS="false",
    )
    log = open(os.path.join(tmp, "server.log"), "wb")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)],
        cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    results: list[bool] = []

    def raw(peer: str, request: bytes, body: bytes = b"") -> str:
        with socket.create_connection((peer, port), timeout=10) as s:
            s.sendall(request + body)
            s.settimeout(10)
            data = b""
            try:
                while chunk := s.recv(4096):
                    data += chunk
            except OSError:
                pass
        return data.split(b"\r\n", 1)[0].decode(errors="replace") or "(no response)"

    def get(peer, path, host, extra=""):
        return raw(peer, f"GET {path} HTTP/1.1\r\nHost: {host}\r\n{extra}Connection: close\r\n\r\n".encode())

    def post(peer, path, host, body=b"{}"):
        head = (f"POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
        return raw(peer, head.encode(), body)

    def check(label: str, got: str, expect: str, needs_lan: bool = False) -> None:
        if needs_lan and lan is None:
            print(f"SKIP {label:62} (no non-loopback address on this machine)")
            return
        ok = expect in got
        results.append(ok)
        print(("PASS " if ok else "FAIL ") + f"{label:62} -> {got}  (expected {expect})")

    try:
        for _ in range(120):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if server.poll() is not None:
                    print("FAIL server exited during startup:\n" + open(os.path.join(tmp, "server.log"), errors="replace").read()[-1500:])
                    return 2
                time.sleep(0.5)

        local = f"localhost:{port}"
        peer = lan or "127.0.0.1"
        check("local browser, /api/history", get("127.0.0.1", "/api/history", local), "200")
        check("remote peer, Host: localhost, /api/history", get(peer, "/api/history", local), "404", needs_lan=True)
        check("remote peer, Host: localhost, /docs", get(peer, "/docs", local), "404", needs_lan=True)
        check("remote peer, Host path-injection '#'", get(peer, "/api/history", "x/api/meta/webhook#"), "400", needs_lan=True)
        check("remote peer, Host path-injection '?'", get(peer, "/openapi.json", "x/api/meta/webhook?"), "400", needs_lan=True)
        check("tunnel style (loopback + X-Forwarded-For)", get("127.0.0.1", "/api/history", local, "X-Forwarded-For: 203.0.113.9\r\n"), "404")
        check("empty X-Forwarded-For header", get("127.0.0.1", "/api/history", local, "X-Forwarded-For: \r\n"), "404")
        check("X-Real-IP header", get("127.0.0.1", "/api/history", local, "X-Real-IP: 203.0.113.9\r\n"), "404")
        check("webhook GET with a bad verify token", get(peer, "/api/meta/webhook?hub.mode=subscribe&hub.verify_token=x&hub.challenge=1", "abc.ngrok-free.app"), "403")
        check("webhook POST, unsigned, no secret configured", post(peer, "/api/meta/webhook", "abc.ngrok-free.app"), "200")
        check("razorpay POST, unsigned, no secret configured", post(peer, "/api/payments/razorpay/webhook", "abc.ngrok-free.app"), "400")
        check("'//api' path trick", get(peer, "//api/history", "abc.ngrok-free.app"), "404")
        check("/uploads from a remote host name", get(peer, "/uploads/x/original.jpg", "abc.ngrok-free.app"), "404")

        # 3.9 MB chunked body (no Content-Length) to a public webhook path
        with socket.create_connection((peer, port), timeout=10) as s:
            s.sendall(b"POST /api/meta/webhook HTTP/1.1\r\nHost: abc.ngrok-free.app\r\n"
                      b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n")
            piece = b" " * 65536
            try:
                for _ in range(60):
                    s.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                s.sendall(b"0\r\n\r\n")
            except OSError:
                pass
            s.settimeout(10)
            try:
                status = s.recv(200).split(b"\r\n", 1)[0].decode()
            except OSError:
                status = "(connection closed)"
        check("chunked 3.9 MB body to a public webhook", status, "413")

        os.makedirs(f"{tmp}/up/abc", exist_ok=True)
        Path(f"{tmp}/up/abc/original.jpg").write_bytes(b"x" * 5000)
        check("static file, local, Range request", get("127.0.0.1", "/uploads/abc/original.jpg", local, "Range: bytes=0-99\r\n"), "206")
        check("static file, local, plain", get("127.0.0.1", "/uploads/abc/original.jpg", local), "200")
        check("static path traversal", get("127.0.0.1", "/uploads/%2e%2e/%2e%2e/p.db", local), "404")
        check("local CORS preflight from the dashboard origin", raw("127.0.0.1", (
            f"OPTIONS /api/history HTTP/1.1\r\nHost: {local}\r\nOrigin: http://localhost:3000\r\n"
            "Access-Control-Request-Method: GET\r\nConnection: close\r\n\r\n").encode()), "200")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{sum(results)}/{len(results)} probes as expected" + ("" if lan else "  (remote-peer probes skipped)"))
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
