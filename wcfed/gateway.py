"""The gateway: local agent runtime on one side, signed envelopes on the other.

One process per org. It runs three things concurrently:

  inbound   long-poll the relay -> verify -> screen -> quarantine -> sink -> ack
  outbound  a local HTTP endpoint (`fedcast` posts here) -> sign -> relay
  watch     optional: tail a watercooler general.log and route `@handle@org`

Everything the plan calls "phase 4" lives in `screen()`, and it is on the
delivery path rather than beside it, so there is no configuration in which a
message reaches a sink without passing through it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config
from .envelope import EnvelopeError, addr, build, parse_address, validate, verify
from .sinks import make_sink, quarantine


class Rejected(Exception):
    """Screening refused the message. Never delivered, always logged."""


def log(cfg: Config, *parts: object) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{cfg.org}]", *parts, flush=True)


class SeenIds:
    """Bounded, persistent dedupe set.

    The relay delivers at-least-once, so redelivery after a crash or a missed
    ack is normal traffic, not an error. Without this an agent gets pinged
    twice for one message and answers twice.
    """

    def __init__(self, path: Path, cap: int = 5000):
        self.path = path
        self.cap = cap
        self._order: deque[str] = deque()
        self._set: set[str] = set()
        self._lock = threading.Lock()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines()[-cap:]:
                line = line.strip()
                if line:
                    self._order.append(line)
                    self._set.add(line)

    def add_if_new(self, mid: str) -> bool:
        with self._lock:
            if mid in self._set:
                return False
            self._set.add(mid)
            self._order.append(mid)
            while len(self._order) > self.cap:
                self._set.discard(self._order.popleft())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(mid + "\n")
            return True


class RateLimiter:
    """Token bucket per source org."""

    def __init__(self, per_min: int):
        self.per_min = per_min
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, org: str) -> bool:
        if self.per_min <= 0:
            return True
        now = time.time()
        with self._lock:
            hits = self._hits.setdefault(org, deque())
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self.per_min:
                return False
            hits.append(now)
            return True


class Gateway:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sink = make_sink(cfg.sink, cfg.sink_target)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.seen = SeenIds(cfg.state_dir / f"seen-{cfg.org}.txt")
        self.rate = RateLimiter(cfg.rate_per_min)
        self.feed = cfg.state_dir / f"feed-{cfg.org}.log"
        self._stop = threading.Event()
        self.stats = {"sent": 0, "received": 0, "rejected": 0, "delivered": 0}

    # -- relay plumbing --------------------------------------------------
    def _relay(self, path: str, payload: dict | None, method: str = "POST", timeout: float = 40):
        url = f"{self.cfg.relay_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Wcfed-Org": self.cfg.org,
                "X-Wcfed-Auth": self.cfg.relay_token,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # -- outbound --------------------------------------------------------
    def send(
        self,
        to: str,
        text: str,
        kind: str = "ping",
        conv: str | None = None,
        depth: int = 0,
        from_handle: str = "gateway",
    ) -> dict:
        handle, org = parse_address(to)
        if org == self.cfg.org:
            raise EnvelopeError(f"{to} is this org; use your local bus, not the federation")
        secret = self.cfg.secret_for(org)
        if depth >= self.cfg.depth_max:
            raise Rejected(
                f"outbound depth {depth} >= cap {self.cfg.depth_max}; "
                "close the thread instead of extending it"
            )
        env = build(
            from_org=self.cfg.org,
            from_handle=from_handle,
            to_org=org,
            to_handle=handle,
            text=text,
            kind=kind,
            conv=conv,
            depth=depth,
            secret=secret,
        )
        resp = self._relay("/v1/send", env)
        self.stats["sent"] += 1
        self._append_feed("->", env)
        log(self.cfg, f"sent {env['id']} -> {to} ({kind}, #{env['conv']} d{env['depth']})")
        return {"ok": True, "id": env["id"], "conv": env["conv"], "relay": resp}

    # -- inbound ---------------------------------------------------------
    def screen(self, env: dict) -> None:
        """Every reason an inbound message must not reach an agent.

        Order matters: shape before signature (so a malformed envelope cannot
        reach the HMAC path), signature before policy (so an unauthenticated
        sender cannot consume another org's rate budget).
        """
        validate(env)
        src_org = env["from"]["org"]

        if env["to"]["org"] != self.cfg.org:
            raise Rejected(f"addressed to org {env['to']['org']!r}, we are {self.cfg.org!r}")

        if src_org not in self.cfg.allowed_orgs:
            raise Rejected(f"org {src_org!r} is not in the allowlist")

        secret = self.cfg.peers.get(src_org)
        if not secret:
            raise Rejected(f"no shared secret for org {src_org!r}")
        if not verify(env, secret):
            raise Rejected(f"bad signature from {src_org!r}")

        if env["depth"] > self.cfg.depth_max:
            raise Rejected(f"depth {env['depth']} exceeds cap {self.cfg.depth_max}")

        if not self.rate.allow(src_org):
            raise Rejected(f"rate limit exceeded for {src_org!r} ({self.cfg.rate_per_min}/min)")

    def handle_inbound(self, env: dict) -> None:
        mid = env.get("id", "?")
        try:
            self.screen(env)
        except (Rejected, EnvelopeError) as exc:
            self.stats["rejected"] += 1
            log(self.cfg, f"REJECTED {mid}: {exc}")
            self._append_feed("!!", env, note=str(exc))
            return

        if not self.seen.add_if_new(mid):
            log(self.cfg, f"duplicate {mid}, already delivered — dropping")
            return

        body = quarantine(env)
        try:
            result = self.sink.deliver(env, body)
        except Exception as exc:
            log(self.cfg, f"SINK FAILED for {mid}: {exc}")
            self._append_feed("xx", env, note=f"sink failed: {exc}")
            return

        self.stats["delivered"] += 1
        self._append_feed("<-", env)
        log(self.cfg, f"delivered {mid} from {addr(env['from'])} ({result})")

    def _append_feed(self, arrow: str, env: dict, note: str = "") -> None:
        """The readable local record. Mirrors watercooler's 'the log IS the
        registry' idea: no separate state to corrupt or migrate."""
        self.feed.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H:%M")
        who = addr(env["from"]) if arrow != "->" else addr(env["to"])
        line = (
            f"[{stamp}] {arrow} #{env.get('conv','-')} d{env.get('depth','?')} "
            f"{who}: {env.get('text','')}"
        )
        if note:
            line += f"   ({note})"
        with self.feed.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def poll_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                resp = self._relay(
                    f"/v1/poll?wait={self.cfg.poll_wait}",
                    None,
                    method="GET",
                    timeout=self.cfg.poll_wait + 15,
                )
                backoff = 1.0
                msgs = resp.get("messages") or []
                if msgs:
                    self.stats["received"] += len(msgs)
                    for env in msgs:
                        self.handle_inbound(env)
                    # Ack after handling, not before: a crash mid-delivery
                    # should redeliver, and dedupe makes that cheap.
                    ids = [m.get("id") for m in msgs if m.get("id")]
                    try:
                        self._relay("/v1/ack", {"ids": ids})
                    except Exception as exc:
                        log(self.cfg, f"ack failed (will redeliver): {exc}")
            except urllib.error.HTTPError as exc:
                log(self.cfg, f"poll HTTP {exc.code}: {exc.read()[:200]!r}")
                self._stop.wait(min(backoff, 30))
                backoff = min(backoff * 2, 30)
            except Exception as exc:
                if not self._stop.is_set():
                    log(self.cfg, f"poll error: {type(exc).__name__}: {exc}")
                    self._stop.wait(min(backoff, 30))
                    backoff = min(backoff * 2, 30)

    # -- optional: transparent routing from a watercooler log ------------
    def watch_log_loop(self) -> None:
        """Tail general.log and forward any `@handle@org` mention.

        This is the transparent path: an agent types the `broadcast` it already
        knows and a federated handle just works. It reads the log rather than
        wrapping `broadcast`, so the hot path stays untouched.
        """
        path = Path(self.cfg.watch_log).expanduser()
        offset_file = self.cfg.state_dir / f"offset-{self.cfg.org}"
        offset = int(offset_file.read_text()) if offset_file.exists() else None
        log(self.cfg, f"watching {path}")

        import re

        line_re = re.compile(r"^\[[^\]]*\]\s+#(\S+)\s+d(\d+)\s+([^/]+)/([^:]+):\s*(.*)$")
        fed_re = re.compile(r"@([A-Za-z0-9_-][A-Za-z0-9_.-]{0,63})@([a-z0-9][a-z0-9-]{0,31})")

        while not self._stop.is_set():
            try:
                if not path.exists():
                    self._stop.wait(2)
                    continue
                size = path.stat().st_size
                if offset is None or offset > size:
                    offset = size  # first run or truncation: start at the end
                if offset == size:
                    self._stop.wait(1)
                    continue
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    lines = fh.readlines()
                    offset = fh.tell()
                offset_file.write_text(str(offset))

                for line in lines:
                    m = line_re.match(line.strip())
                    if not m:
                        continue
                    conv, depth, role, sender, text = m.groups()
                    if role in ("remote", "bus"):
                        continue  # never re-federate what federation delivered
                    for handle, org in fed_re.findall(text):
                        if org == self.cfg.org:
                            continue
                        try:
                            self.send(
                                f"{handle}@{org}",
                                text,
                                kind="ping",
                                conv=None if conv == "-" else conv,
                                depth=int(depth),
                                from_handle=sender.strip(),
                            )
                        except Exception as exc:
                            log(self.cfg, f"watch-log forward failed: {exc}")
            except Exception as exc:
                log(self.cfg, f"watch-log error: {exc}")
                self._stop.wait(2)

    # -- local control endpoint -----------------------------------------
    def serve_local(self) -> ThreadingHTTPServer:
        gw = self

        class LocalHandler(BaseHTTPRequestHandler):
            server_version = "wcfed-gateway/1"

            def log_message(self, fmt, *args):  # noqa: A003
                if gw.cfg.verbose:
                    super().log_message(fmt, *args)

            def _json(self, code, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/health"):
                    return self._json(
                        200,
                        {
                            "ok": True,
                            "org": gw.cfg.org,
                            "sink": gw.cfg.sink,
                            "peers": sorted(gw.cfg.peers),
                            "allowed_orgs": sorted(gw.cfg.allowed_orgs),
                            "depth_max": gw.cfg.depth_max,
                            "stats": gw.stats,
                        },
                    )
                return self._json(404, {"ok": False, "error": "no such route"})

            def do_POST(self):  # noqa: N802
                if not self.path.startswith("/outbound"):
                    return self._json(404, {"ok": False, "error": "no such route"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception as exc:
                    return self._json(400, {"ok": False, "error": f"bad body: {exc}"})
                try:
                    result = gw.send(
                        to=payload["to"],
                        text=payload["text"],
                        kind=payload.get("kind", "ping"),
                        conv=payload.get("conv"),
                        depth=int(payload.get("depth", 0)),
                        from_handle=payload.get("from", "gateway"),
                    )
                    return self._json(200, result)
                except KeyError as exc:
                    return self._json(400, {"ok": False, "error": f"missing field {exc}"})
                except (EnvelopeError, Rejected) as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    return self._json(502, {"ok": False, "error": f"relay: {exc}"})

        httpd = ThreadingHTTPServer((self.cfg.listen_host, self.cfg.listen_port), LocalHandler)
        httpd.daemon_threads = True
        return httpd

    # -- lifecycle -------------------------------------------------------
    def run(self) -> None:
        cfg = self.cfg
        log(cfg, f"gateway up · sink={cfg.sink} · relay={cfg.relay_url}")
        log(cfg, f"peers={sorted(cfg.peers)} depth_max={cfg.depth_max} rate={cfg.rate_per_min}/min")

        httpd = self.serve_local()
        threads = [
            threading.Thread(target=httpd.serve_forever, daemon=True, name="local"),
            threading.Thread(target=self.poll_loop, daemon=True, name="poll"),
        ]
        if cfg.watch_log:
            threads.append(
                threading.Thread(target=self.watch_log_loop, daemon=True, name="watch")
            )
        for t in threads:
            t.start()
        log(cfg, f"listening on http://{cfg.listen_host}:{cfg.listen_port} (fedcast target)")
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            httpd.shutdown()
            log(cfg, "gateway down")

    def stop(self) -> None:
        self._stop.set()
