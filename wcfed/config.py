"""Gateway/relay configuration, read from an env file or the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .envelope import ORG_RE, EnvelopeError


def load_env_file(path: str | os.PathLike) -> dict[str, str]:
    """Minimal .env reader: KEY=value, `#` comments, optional surrounding quotes.

    Deliberately not dotenv — a federation gateway should not need a dependency
    to read six settings, and the peers file holds shared secrets.
    """
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.split(" #", 1)[0].strip() if " #" in val else val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def _parse_peers(spec: str) -> dict[str, str]:
    """`zamua:hexsecret,other:hexsecret2` -> {org: secret}."""
    peers: dict[str, str] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        org, sep, secret = chunk.partition(":")
        if not sep or not secret:
            raise EnvelopeError(f"bad WCFED_PEERS entry {chunk!r} (want org:secret)")
        org = org.strip()
        if not ORG_RE.match(org):
            raise EnvelopeError(f"bad org id in WCFED_PEERS: {org!r}")
        peers[org] = secret.strip()
    return peers


@dataclass
class Config:
    org: str
    relay_url: str
    relay_token: str
    peers: dict[str, str] = field(default_factory=dict)

    # Safety envelope. These are the knobs the plan's phase 4 is about; they
    # ship on by default because a limit you have to remember to turn on is
    # a limit that is off.
    depth_max: int = 8
    rate_per_min: int = 30
    allowed_orgs: set[str] = field(default_factory=set)

    sink: str = "echo"
    sink_target: str = ""
    default_handle: str = "general"

    listen_host: str = "127.0.0.1"
    listen_port: int = 8799
    state_dir: Path = field(default_factory=lambda: Path.home() / ".wcfed")
    watch_log: str = ""
    poll_wait: int = 25
    verbose: bool = False

    @classmethod
    def from_env(cls, env_file: str | None = None) -> "Config":
        env: dict[str, str] = {}
        if env_file:
            env.update(load_env_file(env_file))
        # Real environment wins, so a test can override one value inline.
        env.update({k: v for k, v in os.environ.items() if k.startswith("WCFED_")})

        def get(key: str, default: str = "") -> str:
            return env.get(f"WCFED_{key}", default).strip()

        org = get("ORG")
        if not ORG_RE.match(org):
            raise EnvelopeError(
                f"WCFED_ORG {org!r} is not a valid org id "
                "(lowercase letters, digits and hyphens; no dots)"
            )

        peers = _parse_peers(get("PEERS"))
        allowed = {o.strip() for o in get("ALLOWED_ORGS").split(",") if o.strip()}
        # Not configuring an allowlist means "exactly the peers I hold a key
        # for", never "anyone". An empty allowlist must not read as open.
        if not allowed:
            allowed = set(peers)

        listen = get("LISTEN", "127.0.0.1:8799")
        host, _, port = listen.rpartition(":")

        return cls(
            org=org,
            relay_url=get("RELAY_URL", "http://127.0.0.1:8787").rstrip("/"),
            relay_token=get("RELAY_TOKEN"),
            peers=peers,
            depth_max=int(get("DEPTH_MAX", "8")),
            rate_per_min=int(get("RATE_PER_MIN", "30")),
            allowed_orgs=allowed,
            sink=get("SINK", "echo"),
            sink_target=get("SINK_TARGET"),
            default_handle=get("DEFAULT_HANDLE", "general"),
            listen_host=host or "127.0.0.1",
            listen_port=int(port or "8799"),
            state_dir=Path(get("STATE_DIR", str(Path.home() / ".wcfed"))).expanduser(),
            watch_log=get("WATCH_LOG"),
            poll_wait=int(get("POLL_WAIT", "25")),
            verbose=get("VERBOSE", "0") not in ("", "0", "false", "no"),
        )

    def secret_for(self, org: str) -> str:
        secret = self.peers.get(org)
        if not secret:
            raise EnvelopeError(f"no shared secret configured for org {org!r}")
        return secret
