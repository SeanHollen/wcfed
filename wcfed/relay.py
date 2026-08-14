"""Store-and-forward relay.

The relay exists for exactly one reason: at least one side of a federation is
usually behind NAT (a Raspberry Pi at home, a laptop, a dev box) and cannot
accept an inbound connection. Both gateways therefore *dial out* to the relay —
POST to send, long-poll to receive — and never listen on a public port.

The relay is deliberately dumb and deliberately untrusted:

  * It never has a peer's shared secret, so it cannot forge or read-and-modify
    an envelope. Integrity is end-to-end between gateways.
  * Its own tokens are for abuse control (who may enqueue) rather than for
    authenticity (whose message this is).
  * It holds messages in memory with a TTL. It is a queue, not a store.

If both sides happen to be publicly reachable, delete this file and point the
gateways straight at each other; nothing else changes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_TTL = 24 * 3600
DEFAULT_VISIBILITY = 60  # seconds a polled-but-unacked message stays hidden
MAX_BODY = 256 * 1024
MAX_QUEUE = 1000


class Queues:
    """Per-org message queues with at-least-once delivery.

    Delivery is at-least-once rather than at-most-once on purpose: losing an
    agent's message is worse than delivering it twice, and the receiving
    gateway already dedupes on the envelope id.
    """

    def __init__(self, ttl: int = DEFAULT_TTL, visibility: int = DEFAULT_VISIBILITY):
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._q: dict[str, list[dict]] = {}
        self.ttl = ttl
        self.visibility = visibility
        self.stats = {"enqueued": 0, "delivered": 0, "acked": 0, "expired": 0}

    def _expire(self, now: float) -> None:
        for org, items in self._q.items():
            keep = [it for it in items if it["exp"] > now]
            self.stats["expired"] += len(items) - len(keep)
            self._q[org] = keep

    def put(self, org: str, env: dict) -> None:
        now = time.time()
        with self._cv:
            self._expire(now)
            q = self._q.setdefault(org, [])
            if len(q) >= MAX_QUEUE:
                raise OverflowError(f"queue for {org!r} is full ({MAX_QUEUE})")
            q.append({"env": env, "exp": now + self.ttl, "hidden_until": 0.0})
            self.stats["enqueued"] += 1
            self._cv.notify_all()

    def get(self, org: str, wait: float, limit: int = 32) -> list[dict]:
        deadline = time.time() + wait
        with self._cv:
            while True:
                now = time.time()
                self._expire(now)
                ready = [it for it in self._q.get(org, []) if it["hidden_until"] <= now]
                if ready:
                    batch = ready[:limit]
                    for it in batch:
                        it["hidden_until"] = now + self.visibility
                    self.stats["delivered"] += len(batch)
                    return [it["env"] for it in batch]
                remaining = deadline - now
                if remaining <= 0:
                    return []
                self._cv.wait(timeout=remaining)

    def ack(self, org: str, ids: list[str]) -> int:
        wanted = set(ids)
        with self._cv:
            items = self._q.get(org, [])
            keep = [it for it in items if it["env"].get("id") not in wanted]
            removed = len(items) - len(keep)
            self._q[org] = keep
            self.stats["acked"] += removed
            return removed

    def depth(self) -> dict[str, int]:
        with self._cv:
            return {org: len(items) for org, items in self._q.items() if items}


class RelayState:
    def __init__(self, tokens: dict[str, str], queues: Queues):
        self.tokens = tokens  # org -> relay token
        self.queues = queues
        self.started = time.time()

    def authed_org(self, headers) -> str | None:
        org = (headers.get("X-Wcfed-Org") or "").strip()
        token = (headers.get("X-Wcfed-Auth") or "").strip()
        if not org or not token:
            return None
        expected = self.tokens.get(org)
        if not expected:
            return None
        # compare_digest on str is fine here; both sides are ASCII tokens.
        import hmac as _hmac

        return org if _hmac.compare_digest(token, expected) else None


class Handler(BaseHTTPRequestHandler):
    server_version = "wcfed-relay/1"
    state: RelayState  # injected below

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib hook
        if os.environ.get("WCFED_VERBOSE"):
            super().log_message(fmt, *args)

    # -- helpers ---------------------------------------------------------
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError(f"body too large ({length} > {MAX_BODY})")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    # -- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802 - stdlib hook
        url = urlparse(self.path)
        if url.path == "/v1/health":
            q = self.state.queues
            return self._json(
                200,
                {
                    "ok": True,
                    "service": "wcfed-relay",
                    "uptime_s": round(time.time() - self.state.started),
                    "orgs": sorted(self.state.tokens),
                    "queued": q.depth(),
                    "stats": q.stats,
                },
            )

        if url.path == "/v1/poll":
            org = self.state.authed_org(self.headers)
            if not org:
                return self._json(401, {"ok": False, "error": "bad or missing org auth"})
            params = parse_qs(url.query)
            wait = min(float(params.get("wait", ["25"])[0]), 60.0)
            msgs = self.state.queues.get(org, wait)
            return self._json(200, {"ok": True, "messages": msgs})

        return self._json(404, {"ok": False, "error": "no such route"})

    def do_POST(self):  # noqa: N802 - stdlib hook
        url = urlparse(self.path)
        org = self.state.authed_org(self.headers)
        if not org:
            return self._json(401, {"ok": False, "error": "bad or missing org auth"})

        try:
            payload = self._read_json()
        except Exception as exc:
            return self._json(400, {"ok": False, "error": f"bad body: {exc}"})

        if url.path == "/v1/send":
            env = payload
            # The relay does the least validation it can get away with: it must
            # know where to put the message, and it must not let one org spoof
            # another's return address. Everything else is the receiver's job.
            try:
                dest = env["to"]["org"]
                src = env["from"]["org"]
                mid = env["id"]
            except Exception:
                return self._json(400, {"ok": False, "error": "envelope missing to/from/id"})
            if src != org:
                return self._json(
                    403, {"ok": False, "error": f"authenticated as {org!r}, envelope claims {src!r}"}
                )
            if dest not in self.state.tokens:
                return self._json(404, {"ok": False, "error": f"unknown destination org {dest!r}"})
            try:
                self.state.queues.put(dest, env)
            except OverflowError as exc:
                return self._json(429, {"ok": False, "error": str(exc)})
            return self._json(202, {"ok": True, "id": mid, "queued_for": dest})

        if url.path == "/v1/ack":
            ids = payload.get("ids") or []
            if not isinstance(ids, list):
                return self._json(400, {"ok": False, "error": "ids must be a list"})
            removed = self.state.queues.ack(org, [str(i) for i in ids])
            return self._json(200, {"ok": True, "acked": removed})

        return self._json(404, {"ok": False, "error": "no such route"})


def parse_tokens(spec: str) -> dict[str, str]:
    """`seanpi:tokenA,zamua:tokenB` -> {org: token}."""
    out: dict[str, str] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        org, sep, token = chunk.partition(":")
        if not sep or not token:
            raise ValueError(f"bad token entry {chunk!r} (want org:token)")
        out[org.strip()] = token.strip()
    return out


def serve(host: str, port: int, tokens: dict[str, str], ttl: int = DEFAULT_TTL):
    state = RelayState(tokens, Queues(ttl=ttl))
    handler = type("BoundHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
