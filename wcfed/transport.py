"""How envelopes get from one gateway to another.

A transport moves signed bytes and nothing else. It never inspects, trusts or
modifies an envelope — `Gateway.screen()` does all of that on arrival, which is
why swapping transports changes no part of the security story.

Two ship today:

  http    a store-and-forward relay you run. Lowest latency, but it is a
          service someone has to host and keep alive.

  github  a private repo's issue thread as the queue. Nobody hosts anything:
          both sides already have GitHub, and a fine-grained PAT scoped to that
          ONE repo is a far smaller credential than a server's or a Telegram
          account's. Latency is a poll interval rather than a long-poll, which
          for agent handoffs is a fine trade.

Adding a third (Matrix, ntfy, S3, a shared drive) means implementing three
methods. It does not mean touching screening, signing or delivery.
"""

from __future__ import annotations

import calendar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .envelope import EnvelopeError

_FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
MARKER = "wcfed-envelope-v1"


class TransportError(RuntimeError):
    pass


class Transport:
    name = "transport"

    def send(self, env: dict) -> dict:
        raise NotImplementedError

    def poll(self, wait: int) -> list[dict]:
        """Return zero or more envelopes, blocking up to `wait` seconds."""
        raise NotImplementedError

    def ack(self, ids: list[str]) -> None:
        """Confirm delivery. May be a no-op for cursor-based transports."""

    def health(self) -> dict:
        return {"ok": True, "transport": self.name}


def _request(url: str, *, method: str = "GET", headers: dict | None = None,
             payload: dict | None = None, timeout: float = 30) -> tuple[int, dict, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            body = json.loads(raw) if raw.strip() else {}
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace")
        raise TransportError(f"HTTP {exc.code} from {url}: {detail}") from exc


# ---------------------------------------------------------------- http relay
class HttpTransport(Transport):
    name = "http"

    def __init__(self, org: str, relay_url: str, relay_token: str):
        if not relay_url:
            raise EnvelopeError("http transport needs WCFED_RELAY_URL")
        self.org = org
        self.url = relay_url.rstrip("/")
        self.token = relay_token

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "X-Wcfed-Org": self.org,
            "X-Wcfed-Auth": self.token,
        }

    def send(self, env: dict) -> dict:
        _, body, _ = _request(
            f"{self.url}/v1/send", method="POST", headers=self._headers(), payload=env
        )
        return body

    def poll(self, wait: int) -> list[dict]:
        _, body, _ = _request(
            f"{self.url}/v1/poll?wait={wait}", headers=self._headers(), timeout=wait + 15
        )
        return body.get("messages") or []

    def ack(self, ids: list[str]) -> None:
        if ids:
            _request(f"{self.url}/v1/ack", method="POST", headers=self._headers(),
                     payload={"ids": ids})

    def health(self) -> dict:
        _, body, _ = _request(f"{self.url}/v1/health", timeout=15)
        return body


# -------------------------------------------------------------------- github
class GithubTransport(Transport):
    """A private repo's issue thread as the message queue.

    One comment per envelope, the JSON in a fenced block so the thread stays
    readable to a human scrolling it. Ordering, durability, access control and
    audit come free from GitHub; we add nothing but the cursor.

    Access control is repo collaboration: a peer who is not on the repo cannot
    post at all, and a fine-grained PAT scoped to this repo cannot reach
    anything else in either account. That is the whole reason to prefer this
    over a Telegram user session.
    """

    name = "github"
    API = "https://api.github.com"

    def __init__(self, org: str, repo: str, issue: int, token: str,
                 state_dir: Path, interval: int = 10):
        if not repo or "/" not in repo:
            raise EnvelopeError("github transport needs WCFED_GITHUB_REPO=owner/name")
        if not token:
            raise EnvelopeError("github transport needs WCFED_GITHUB_TOKEN")
        self.org = org
        self.repo = repo
        self.issue = int(issue)
        self.token = token
        self.interval = max(3, int(interval))
        self.cursor_file = state_dir / f"gh-cursor-{org}.json"
        state_dir.mkdir(parents=True, exist_ok=True)

    def _headers(self) -> dict:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "wcfed-gateway",
        }

    # -- cursor ----------------------------------------------------------
    def _load_cursor(self) -> dict:
        if self.cursor_file.exists():
            try:
                return json.loads(self.cursor_file.read_text())
            except Exception:
                pass
        # First run starts from now, not from the beginning of the thread: an
        # existing bus would otherwise replay its entire history into a fresh
        # gateway the moment it starts.
        return {"since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "last_id": 0}

    def _save_cursor(self, since: str, last_id: int) -> None:
        self.cursor_file.write_text(json.dumps({"since": since, "last_id": last_id}))

    # -- wire ------------------------------------------------------------
    def send(self, env: dict) -> dict:
        body = (
            f"<!-- {MARKER} to={env['to']['org']} from={env['from']['org']} -->\n"
            f"**{env['from']['handle']}@{env['from']['org']}** → "
            f"**{env['to']['handle']}@{env['to']['org']}** "
            f"· `{env['kind']}` · conv `{env['conv']}` · depth {env['depth']}\n\n"
            "```json\n" + json.dumps(env, indent=2, ensure_ascii=False) + "\n```"
        )
        status, resp, _ = _request(
            f"{self.API}/repos/{self.repo}/issues/{self.issue}/comments",
            method="POST", headers=self._headers(), payload={"body": body},
        )
        return {"ok": True, "id": env["id"], "comment": resp.get("html_url"), "status": status}

    def _fetch(self) -> list[dict]:
        cursor = self._load_cursor()
        url = (
            f"{self.API}/repos/{self.repo}/issues/{self.issue}/comments"
            f"?since={urllib.parse.quote(cursor['since'])}&per_page=100"
        )
        _, comments, _ = _request(url, headers=self._headers(), timeout=30)
        if not isinstance(comments, list):
            return []

        out: list[dict] = []
        last_id = cursor["last_id"]
        newest = cursor["since"]
        for c in comments:
            cid = int(c.get("id", 0))
            if cid <= cursor["last_id"]:
                continue  # `since` has second resolution; the id breaks ties
            last_id = max(last_id, cid)
            newest = max(newest, c.get("created_at") or newest)

            m = _FENCE_RE.search(c.get("body") or "")
            if not m:
                continue  # a human commented in the thread; not our business
            try:
                env = json.loads(m.group(1))
            except Exception:
                continue
            if not isinstance(env, dict):
                continue
            # Our own sent messages come back on the next poll. Skipping by org
            # rather than by comment author means a peer cannot suppress our
            # delivery by impersonating an author.
            if (env.get("from") or {}).get("org") == self.org:
                continue
            if (env.get("to") or {}).get("org") != self.org:
                continue
            out.append(env)

        # Rewind one second: `since` is inclusive to the second, and a comment
        # written in the same second as the cursor would otherwise be skipped.
        if last_id != cursor["last_id"]:
            self._save_cursor(_minus_one_second(newest), last_id)
        return out

    def poll(self, wait: int) -> list[dict]:
        deadline = time.time() + max(0, wait)
        while True:
            found = self._fetch()
            if found:
                return found
            if time.time() >= deadline:
                return []
            time.sleep(min(self.interval, max(1, deadline - time.time())))

    def ack(self, ids: list[str]) -> None:
        """No-op: the cursor advanced when the comment was read.

        Redelivery therefore cannot happen from the transport's side, so the
        gateway's dedupe set is what protects against a peer re-posting an
        envelope id — which is exactly what it is for.
        """

    def health(self) -> dict:
        _, issue, _ = _request(
            f"{self.API}/repos/{self.repo}/issues/{self.issue}",
            headers=self._headers(), timeout=20,
        )
        return {
            "ok": True,
            "transport": "github",
            "repo": self.repo,
            "issue": self.issue,
            "title": issue.get("title"),
            "state": issue.get("state"),
            "comments": issue.get("comments"),
        }


def _minus_one_second(ts: str) -> str:
    """Rewind a UTC timestamp by one second.

    Uses calendar.timegm, NOT time.mktime: mktime interprets its argument as
    LOCAL time, so on any box that is not UTC it shifts the cursor by the
    local offset. That failure is silent and one-directional — the cursor
    lands in the future, every subsequent comment sorts before it, and inbound
    messages simply stop arriving while sending still works perfectly.
    """
    try:
        parsed = time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(calendar.timegm(parsed) - 1))
    except Exception:
        return ts


def make_transport(cfg) -> Transport:
    if cfg.transport == "http":
        return HttpTransport(cfg.org, cfg.relay_url, cfg.relay_token)
    if cfg.transport == "github":
        return GithubTransport(
            cfg.org, cfg.github_repo, cfg.github_issue, cfg.github_token,
            cfg.state_dir, cfg.github_interval,
        )
    raise EnvelopeError(f"unknown transport {cfg.transport!r} (have: http, github)")
