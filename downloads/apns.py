"""Push notifications from a Linux server, with nothing to install.

`kite-helper.py` imports `send_wakeup` from here and carries on without it if the
import fails, so push is opt-in: drop this file next to the helper, put the key
in `~/.kite/apns.json`, and it starts working.

⚠️ **APNs requires HTTP/2 and the Python standard library cannot speak it.** That
is the whole reason this file looks the way it does. `http.client` is HTTP/1.1
only, and Apple closes the connection on an HTTP/1.1 request without a useful
error — it reads as a network fault rather than a protocol mismatch. Rather than
depend on `httpx[http2]` (which needs `h2`, which a bare server will not have),
the request goes out through `curl`, which is present on every server that could
run the helper and has been built with nghttp2 for years.

Signing is the same story. ES256 means P-256 ECDSA, which the standard library
also lacks. `cryptography` is used when it happens to be importable, and
`openssl` is shelled out to otherwise. Both produce the same bytes.

Config lives at `~/.kite/apns.json`, deliberately the same file the macOS helper
reads, so a key can be moved between them without a second format:

    {
      "key_path": "~/.kite/apns.p8",
      "key_id": "ABCD123456",
      "team_id": "TEAMID1234",
      "topic": "app.kiteagent.kite",
      "sandbox": false
    }
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("KITE_APNS_CONFIG", Path.home() / ".kite/apns.json"))

# APNs provider tokens are valid for an hour, and Apple rate-limits minting them
# per request — repeat offenders start getting rejected outright. Refresh well
# inside the hour instead.
_TOKEN_TTL = 45 * 60
_cached_token: tuple[str, float] | None = None


class APNsError(RuntimeError):
    pass


# ---------------------------------------------------------------------- config

def _config() -> dict:
    if not CONFIG_PATH.exists():
        raise APNsError(f"no APNs config at {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text())
    for required in ("key_path", "key_id", "team_id"):
        if not cfg.get(required):
            raise APNsError(f"{CONFIG_PATH} is missing {required}")
    cfg.setdefault("topic", "app.kiteagent.kite")
    cfg.setdefault("sandbox", False)
    cfg["key_path"] = str(Path(cfg["key_path"]).expanduser())
    return cfg


def _host(cfg: dict) -> str:
    # ⚠️ A production build's device token is meaningless to the sandbox host and
    # vice versa; the mismatch returns 400 BadDeviceToken, which reads like a
    # corrupt token rather than the wrong endpoint.
    return ("https://api.sandbox.push.apple.com" if cfg.get("sandbox")
            else "https://api.push.apple.com")


# --------------------------------------------------------------------- signing

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _der_to_raw(der: bytes) -> bytes:
    """Convert an ECDSA DER signature to the r‖s pair JWS specifies.

    OpenSSL emits DER: SEQUENCE { INTEGER r, INTEGER s }, with each integer
    carrying a leading zero byte when its high bit is set. JOSE wants the two
    values raw and fixed-width. Handing APNs the DER form instead produces a
    403 InvalidProviderToken, which says nothing about the encoding.
    """
    if not der.startswith(b"\x30"):
        raise APNsError("signature is not DER")
    # Skip SEQUENCE tag and its length (short or long form).
    i = 1
    if der[i] & 0x80:
        i += 1 + (der[i] & 0x7F)
    else:
        i += 1

    def _int(pos: int) -> tuple[bytes, int]:
        if der[pos] != 0x02:
            raise APNsError("expected an INTEGER in the signature")
        length = der[pos + 1]
        value = der[pos + 2:pos + 2 + length]
        return value.lstrip(b"\x00").rjust(32, b"\x00"), pos + 2 + length

    r, i = _int(i)
    s, _ = _int(i)
    return r + s


def _sign(signing_input: str, key_path: str) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        key = serialization.load_pem_private_key(
            Path(key_path).read_bytes(), password=None
        )
        der = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")
    except ImportError:
        pass

    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_path, "-binary"],
        input=signing_input.encode(),
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        # stderr may name the key file but never its contents.
        raise APNsError(f"openssl could not sign: {result.stderr.decode()[:200]}")
    return _der_to_raw(result.stdout)


def _provider_token(cfg: dict) -> str:
    global _cached_token
    if _cached_token and time.time() - _cached_token[1] < _TOKEN_TTL:
        return _cached_token[0]

    header = {"alg": "ES256", "kid": cfg["key_id"]}
    claims = {"iss": cfg["team_id"], "iat": int(time.time())}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    token = signing_input + "." + _b64url(_sign(signing_input, cfg["key_path"]))
    _cached_token = (token, time.time())
    return token


# --------------------------------------------------------------------- sending

def _post(cfg: dict, target: str, headers: dict, payload: dict) -> tuple[int, str]:
    """One APNs request, over HTTP/2, via curl.

    `--http2` is not optional: APNs speaks HTTP/2 only, and the failure without
    it looks like a dead network rather than a rejected protocol.
    """
    args = [
        "curl", "--http2", "--silent", "--show-error",
        "--max-time", "20",
        "-X", "POST",
        "-H", f"authorization: bearer {_provider_token(cfg)}",
        "-H", "content-type: application/json",
    ]
    for name, value in headers.items():
        args += ["-H", f"{name}: {value}"]
    args += [
        "--data-binary", "@-",
        "-o", "-",
        "-w", "\n%{http_code}",
        f"{_host(cfg)}/3/device/{target}",
    ]
    result = subprocess.run(
        args, input=json.dumps(payload).encode(), capture_output=True, timeout=30
    )
    if result.returncode != 0:
        raise APNsError(f"curl failed: {result.stderr.decode()[:200]}")
    out = result.stdout.decode(errors="replace")
    body, _, status = out.rpartition("\n")
    try:
        code = int(status.strip())
    except ValueError as exc:
        raise APNsError(f"no status from curl: {out[:200]}") from exc
    return code, body.strip()


def send_wakeup(device_token: str, session_id: str) -> bool:
    """Tell the phone a reply arrived.

    `interruption-level: active`, not `time-sensitive`: an agent finishing a
    reply does not warrant breaking through someone's Focus mode.
    """
    cfg = _config()
    code, body = _post(
        cfg,
        device_token,
        {
            "apns-topic": cfg["topic"],
            "apns-push-type": "alert",
            "apns-priority": "10",
            # Collapsing on the session means a second reply replaces the first
            # notification rather than stacking another one for the same thread.
            "apns-collapse-id": session_id[:64],
        },
        {
            "aps": {
                "alert": {"title": "Kite", "body": "Your agent replied."},
                "sound": "default",
                "interruption-level": "active",
            }
        },
    )
    if code != 200:
        raise APNsError(f"APNs returned {code}: {body[:200]}")
    return True


def end_activity(activity_token: str, session_id: str) -> bool:
    """Stop a Live Activity from outside the app.

    ⚠️ Two things differ from an ordinary push and both are rejections rather
    than silent no-ops: the topic takes a `.push-type.liveactivity` suffix, and
    `apns-push-type` must be `liveactivity`. A suspended app cannot update its
    own activity, so without this the lock screen says "Thinking…" until it
    times out on its own.
    """
    cfg = _config()
    code, body = _post(
        cfg,
        activity_token,
        {
            "apns-topic": f"{cfg['topic']}.push-type.liveactivity",
            "apns-push-type": "liveactivity",
            "apns-priority": "10",
        },
        {
            "aps": {
                "timestamp": int(time.time()),
                "event": "end",
                "content-state": {},
                "dismissal-date": int(time.time()),
            }
        },
    )
    if code != 200:
        raise APNsError(f"APNs returned {code}: {body[:200]}")
    return True


if __name__ == "__main__":
    # `python3 apns.py <device-token>` — a check that the key, the signing and
    # the HTTP/2 path all work, without waiting for an agent to reply.
    import sys

    if len(sys.argv) < 2:
        print("usage: python3 apns.py <device-token>")
        raise SystemExit(2)
    cfg = _config()
    print(f"config   : {CONFIG_PATH}")
    print(f"topic    : {cfg['topic']}")
    print(f"endpoint : {_host(cfg)}")
    try:
        _provider_token(cfg)
        print("signing  : ok")
    except APNsError as exc:
        print(f"signing  : FAILED — {exc}")
        raise SystemExit(1)
    try:
        send_wakeup(sys.argv[1], "manual-test")
        print("push     : accepted by APNs")
    except APNsError as exc:
        print(f"push     : FAILED — {exc}")
        raise SystemExit(1)
