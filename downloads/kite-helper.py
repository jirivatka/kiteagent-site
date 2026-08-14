#!/usr/bin/env python3
"""Kite Helper for Linux — run your agent on a server, with no Mac involved.

The macOS helper is a menu-bar app built on Network.framework and AppKit. None of
that ports, but almost none of it is the point: the helper's job is to hold the
gateway key, swap a pairing token for it, and proxy. That is what this does.

⚠️ **The iOS app needs no changes to work with this.** It pairs with a host, a
port and a token, then speaks plain HTTP. Nothing in it knows which side is
answering, which is why a second implementation is possible at all.

What it deliberately keeps from the macOS helper, because each was a bug once:

- **`Connection: close` is forwarded upstream.** The proxy rewrites the
  `Authorization` header on the first request head of a connection and then pipes
  bytes. On a keep-alive connection every later request would go upstream still
  carrying the pairing token, and the gateway rejects that as an invalid key. That
  is invisible to curl, which opens a fresh connection per invocation, and
  constant with URLSession, which reuses one.
- **The port is sticky.** There is no Bonjour across a tailnet, so a phone that is
  away from home has only the address and port it paired with. A port that moved
  on restart would strand exactly the case the fallback exists for.
- **Which side of a connection dies first is the push trigger.** With
  `Connection: close`, a completed exchange ends server-side; a phone that iOS
  suspended ends client-side. No cooperation from the app is needed.

Run:
  python3 kite-helper.py --gateway 127.0.0.1:8642 --port 50746
"""
import argparse
import asyncio
import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("KITE_HELPER_HOME", Path.home() / ".kite-helper"))
TOKEN_FILE = STATE / "pairing-token"
PORT_FILE = STATE / "port"
PUSH_FILE = STATE / "push.json"

READ_LIMIT = 64 * 1024


# ---------------------------------------------------------------- pairing token

def load_or_create_token() -> str:
    """The token a paired phone presents.

    It exists so the phone never holds the gateway key. The gateway grants full
    terminal access, so a key on a lost phone is a shell on this server; a
    pairing token can be revoked here without touching the agent's config.

    ⚠️ Only created when genuinely absent. An earlier version of the macOS helper
    regenerated whenever the read failed for any reason, which silently
    invalidated every paired device and presented as "it just stopped working".
    """
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    if TOKEN_FILE.exists():
        value = TOKEN_FILE.read_text().strip()
        if value:
            return value
    # base64url, not base64: the token travels in a URL query, and `+` is the
    # form encoding of a space — a link passing through anything that treats it
    # as form data arrives with a corrupted credential and no visible reason.
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return token


def sticky_port(preferred: int) -> int:
    """Reuse the last port when it is free."""
    if PORT_FILE.exists():
        try:
            remembered = int(PORT_FILE.read_text().strip())
            if remembered and _is_free(remembered):
                return remembered
        except ValueError:
            pass
    port = preferred if _is_free(preferred) else 0
    return port


def _is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


# ------------------------------------------------------------------- addresses

def tailnet_address() -> str | None:
    """A 100.64.0.0/10 address, which is what Tailscale allocates from.

    On a server this is usually the only address a phone can reach, so it is the
    address that goes in the pairing payload.
    """
    try:
        import subprocess
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return None
    import re
    for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", out):
        ip = match.group(1)
        parts = [int(p) for p in ip.split(".")]
        if parts[0] == 100 and 64 <= parts[1] <= 127:
            return ip
    return None


def public_address() -> str | None:
    """First non-loopback IPv4, as a fallback when there is no tailnet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
    except Exception:
        return None


def pairing_link(host: str, port: int, token: str, alt: str | None) -> str:
    from urllib.parse import urlencode
    name = socket.gethostname()
    params = {"n": name, "h": host, "p": str(port), "t": token}
    if alt:
        params["a"] = alt
    return "kite-pair:1?" + urlencode(params)


# ----------------------------------------------------------------------- proxy

class Helper:
    def __init__(self, gateway_host: str, gateway_port: int, gateway_key: str, token: str):
        self.gateway_host = gateway_host
        self.gateway_port = gateway_port
        # Resolved once, not per request. Re-reading a key from disk on every
        # request means any transient failure silently becomes an empty key, and
        # the gateway rejects that as "invalid gateway API key" — intermittently,
        # which is the worst way to fail.
        self.gateway_key = gateway_key
        self.token = token
        self.device_token: str | None = None
        self.activity_tokens: dict[str, str] = {}
        self._load_push()

    # -- push state

    def _load_push(self) -> None:
        if PUSH_FILE.exists():
            try:
                data = json.loads(PUSH_FILE.read_text())
                self.device_token = data.get("device_token")
            except Exception:
                pass

    def _save_push(self) -> None:
        PUSH_FILE.write_text(json.dumps({"device_token": self.device_token}))
        os.chmod(PUSH_FILE, 0o600)

    # -- connection handling

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            head, leftover = await self._read_head(reader)
            if head is None:
                return
            line = head.split("\r\n", 1)[0]

            if not self._token_ok(head):
                # Says what it is and nothing about what is behind it. An empty
                # 401 renders as a blank page, which is indistinguishable from
                # "could not connect" — that ambiguity costs a diagnostic round
                # trip every time.
                body = b"Kite Helper is running here. This request had no valid pairing token.\n"
                writer.write(
                    b"HTTP/1.1 401 Unauthorized\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + body
                )
                await writer.drain()
                return

            if line.startswith("POST /kite/"):
                await self._handle_local(line, leftover, reader, writer)
                return

            await self._proxy(head, leftover, reader, writer, line)
        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"  error from {peer}: {exc}", flush=True)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _read_head(self, reader: asyncio.StreamReader) -> tuple[str | None, bytes]:
        buf = b""
        while len(buf) < READ_LIMIT:
            chunk = await reader.read(4096)
            if not chunk:
                return None, b""
            buf += chunk
            if b"\r\n\r\n" in buf:
                head, _, rest = buf.partition(b"\r\n\r\n")
                return head.decode("latin-1") + "\r\n\r\n", rest
        return None, b""

    def _token_ok(self, head: str) -> bool:
        for line in head.split("\r\n"):
            if line.lower().startswith("authorization:"):
                value = line.split(":", 1)[1].strip()
                if value.lower().startswith("bearer "):
                    value = value[7:]
                return secrets.compare_digest(value, self.token)
        return False

    def _rewrite(self, head: str) -> str:
        """Swap the pairing token for the gateway key, and close the connection.

        Closing after one request is what makes the swap correct, not an
        optimisation: only the first head on a connection is rewritten, so a
        keep-alive connection would send later requests upstream still carrying
        the pairing token.
        """
        out = []
        replaced = False
        for line in head.split("\r\n"):
            low = line.lower()
            if low.startswith(("connection:", "keep-alive:", "proxy-connection:")):
                continue
            if low.startswith("authorization:"):
                out.append(f"Authorization: Bearer {self.gateway_key}")
                replaced = True
                continue
            out.append(line)
        blank = out.index("") if "" in out else len(out)
        if not replaced:
            out.insert(blank, f"Authorization: Bearer {self.gateway_key}")
            blank += 1
        out.insert(blank, "Connection: close")
        return "\r\n".join(out)

    async def _proxy(self, head, leftover, reader, writer, line) -> None:
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(self.gateway_host, self.gateway_port), timeout=10
            )
        except Exception:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        up_writer.write(self._rewrite(head).encode("latin-1") + leftover)
        await up_writer.drain()

        client_first = await self._pipe_both(reader, writer, up_reader, up_writer)

        # The phone went away with a run in flight: notify when it finishes.
        session = self._streaming_session(line)
        if client_first and session:
            asyncio.create_task(self._watch_and_notify(session))

    async def _pipe_both(self, c_reader, c_writer, u_reader, u_writer) -> bool:
        """Returns True when the CLIENT side ended first — a suspended phone."""
        async def pump(src, dst) -> str:
            try:
                while True:
                    data = await src.read(READ_LIMIT)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:
                pass
            return "client" if src is c_reader else "server"

        tasks = {asyncio.create_task(pump(c_reader, u_writer)),
                 asyncio.create_task(pump(u_reader, c_writer))}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        first = done.pop().result()
        for t in pending:
            t.cancel()
        for w in (u_writer, c_writer):
            try:
                w.close()
            except Exception:
                pass
        return first == "client"

    @staticmethod
    def _streaming_session(line: str) -> str | None:
        parts = line.split(" ")
        if len(parts) < 2 or parts[0] != "POST":
            return None
        path = parts[1]
        if not path.endswith("/chat/stream"):
            return None
        bits = [b for b in path.split("/") if b]
        # /api/sessions/{id}/chat/stream
        if len(bits) >= 4 and bits[0] == "api" and bits[1] == "sessions":
            return bits[2]
        return None

    # -- our own endpoints

    async def _handle_local(self, line, leftover, reader, writer) -> None:
        body = leftover
        if not body:
            try:
                body = await asyncio.wait_for(reader.read(READ_LIMIT), timeout=5)
            except asyncio.TimeoutError:
                body = b""
        ok = False
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            data = {}

        if line.startswith("POST /kite/push-token"):
            token = (data.get("device_token") or "").strip().lower()
            if token and all(c in "0123456789abcdef" for c in token):
                self.device_token = token
                self._save_push()
                print("  device token registered", flush=True)
                ok = True
        elif line.startswith("POST /kite/activity-token"):
            tok = (data.get("activity_token") or "").strip()
            sid = (data.get("session_id") or "").strip()
            if tok and sid:
                self.activity_tokens[sid] = tok
                ok = True

        status = b"200 OK" if ok else b"400 Bad Request"
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()

    # -- push

    async def _watch_and_notify(self, session_id: str) -> None:
        """A run continues server-side after the client disconnects — verified —
        so this only has to watch for the transcript to grow."""
        baseline = await self._message_count(session_id)
        if baseline is None:
            return
        deadline = time.time() + 300
        while time.time() < deadline:
            await asyncio.sleep(3)
            now = await self._message_count(session_id)
            if now is not None and now > baseline:
                await self._send_push(session_id)
                return

    async def _message_count(self, session_id: str) -> int | None:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"http://{self.gateway_host}:{self.gateway_port}/api/sessions/{session_id}",
                headers={"Authorization": f"Bearer {self.gateway_key}"},
            )
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=10).read()
            )
            return json.loads(raw).get("session", {}).get("message_count")
        except Exception:
            return None

    async def _send_push(self, session_id: str) -> None:
        if not self.device_token:
            return
        try:
            from apns import send_wakeup  # optional, see linux-helper/README
        except ImportError:
            print("  reply ready, but push is not configured", flush=True)
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: send_wakeup(self.device_token, session_id)
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  push failed: {exc}", flush=True)


# ------------------------------------------------------------------------ main

async def serve(args) -> int:
    token = load_or_create_token()
    gw_host, _, gw_port = args.gateway.partition(":")
    gw_port = int(gw_port or 8642)

    key = os.environ.get("KITE_GATEWAY_KEY") or _key_from_env_file()
    if not key:
        print("No gateway key. Set KITE_GATEWAY_KEY or API_SERVER_KEY in ~/.hermes/.env")
        return 1

    port = sticky_port(args.port)
    helper = Helper(gw_host, gw_port, key, token)
    server = await asyncio.start_server(helper.handle, "0.0.0.0", port)
    actual = server.sockets[0].getsockname()[1]
    PORT_FILE.write_text(str(actual))

    tailnet = tailnet_address()
    host = tailnet or public_address() or "127.0.0.1"
    link = pairing_link(host, actual, token, None)

    # flush=True throughout: under systemd, stdout is a pipe and therefore block
    # buffered, so a service that prints its pairing link at startup and then
    # waits would show an empty log until the buffer filled — which reads as "it
    # did not start". The same buffering hid a working push relay earlier.
    print(f"Kite Helper listening on 0.0.0.0:{actual} -> {gw_host}:{gw_port}", flush=True)
    print(f"Reachable at {host}:{actual}" + ("  (tailnet)" if tailnet else "  ⚠️  not a tailnet address"), flush=True)
    print(flush=True)
    print("Pair the phone with this link:", flush=True)
    print(f"  {link}", flush=True)
    print(flush=True)
    _print_qr(link)
    async with server:
        await server.serve_forever()
    return 0


def _key_from_env_file() -> str | None:
    path = Path.home() / ".hermes/.env"
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _print_qr(text: str) -> None:
    """A QR if qrencode is present, otherwise nothing — the link above is enough,
    and the app accepts a pasted link as well as a scan."""
    import shutil, subprocess
    if not shutil.which("qrencode"):
        print("  (install qrencode to show a scannable code here)", flush=True)
        return
    subprocess.run(["qrencode", "-t", "ANSIUTF8", text], check=False)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--gateway", default="127.0.0.1:8642")
    p.add_argument("--port", type=int, default=50746)
    args = p.parse_args()
    try:
        return asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
