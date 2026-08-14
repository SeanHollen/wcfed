"""The wire envelope: canonical form, signing, verification.

This module IS the spec. SPEC.md describes what happens here in prose, but if
the two ever disagree, believe this file — both sides of a federation run a
byte-identical copy of `canonical()`, and that is the only reason signatures
made on one machine verify on another.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time
from hashlib import sha256

VERSION = 1
KINDS = ("ping", "post")

# A handle is a local session name; an org is a federation participant id.
# Orgs are lowercase and dot-free ON PURPOSE: `handle@org` with a dotted org
# looks exactly like an email address, and every CDN, chat client and link
# scanner in the world will helpfully rewrite it for you.
HANDLE_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$")
ORG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ADDR_RE = re.compile(r"^@?([A-Za-z0-9_-][A-Za-z0-9_.-]{0,63})@([a-z0-9][a-z0-9-]{0,31})$")

MAX_TEXT = 16_384

# Crockford base32, minus I L O U so a human can read an id off a screen.
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class EnvelopeError(ValueError):
    """An envelope that must not be delivered. The message is safe to log."""


def new_id() -> str:
    """A ULID: 48-bit big-endian millisecond timestamp, then 80 random bits.

    Lexicographic order matches creation order, which makes a queue file or a
    log grep-able by time without parsing anything.
    """
    raw = int(time.time() * 1000).to_bytes(6, "big") + os.urandom(10)
    n = int.from_bytes(raw, "big")
    # 128 bits over 26 base32 characters: start at bit 125, not 75. Starting
    # lower silently drops the timestamp and leaves only the random tail, which
    # still collides rarely but is no longer sortable by creation time.
    return "".join(_B32[(n >> shift) & 0x1F] for shift in range(125, -5, -5))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_conv() -> str:
    return secrets.token_hex(2)


def parse_address(addr: str) -> tuple[str, str]:
    """`@docs@zamua` -> ('docs', 'zamua'). Raises on anything else."""
    m = ADDR_RE.match(addr.strip())
    if not m:
        raise EnvelopeError(f"not a federated address: {addr!r} (want handle@org)")
    return m.group(1), m.group(2)


def canonical(env: dict) -> bytes:
    """The exact bytes that get signed.

    Every signed field, sorted by key, no whitespace, UTF-8, `sig` excluded.
    `ensure_ascii=False` matters: it keeps the signature stable over non-ASCII
    text instead of depending on whether one side happened to escape it.
    """
    body = {k: v for k, v in env.items() if k != "sig"}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign(env: dict, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical(env), sha256).hexdigest()


def verify(env: dict, secret: str) -> bool:
    got = env.get("sig")
    if not isinstance(got, str) or not got:
        return False
    return hmac.compare_digest(got, sign(env, secret))


def build(
    *,
    from_org: str,
    from_handle: str,
    to_org: str,
    to_handle: str,
    text: str,
    kind: str = "ping",
    conv: str | None = None,
    depth: int = 0,
    secret: str | None = None,
) -> dict:
    env = {
        "v": VERSION,
        "id": new_id(),
        "conv": conv or new_conv(),
        "depth": int(depth),
        "from": {"org": from_org, "handle": from_handle},
        "to": {"org": to_org, "handle": to_handle},
        "kind": kind,
        "text": text,
        "ts": now_iso(),
    }
    validate(env)
    if secret is not None:
        env["sig"] = sign(env, secret)
    return env


def validate(env: dict) -> None:
    """Structural validation. Says nothing about authenticity — see verify()."""
    if not isinstance(env, dict):
        raise EnvelopeError("envelope is not an object")
    if env.get("v") != VERSION:
        raise EnvelopeError(f"unsupported version {env.get('v')!r} (want {VERSION})")

    for field in ("id", "conv", "kind", "text", "ts"):
        if not isinstance(env.get(field), str) or not env[field]:
            raise EnvelopeError(f"missing or non-string field {field!r}")

    if env["kind"] not in KINDS:
        raise EnvelopeError(f"unknown kind {env['kind']!r} (want one of {KINDS})")

    if not isinstance(env.get("depth"), int) or isinstance(env["depth"], bool):
        raise EnvelopeError("depth must be an integer")
    if env["depth"] < 0:
        raise EnvelopeError("depth must not be negative")

    if len(env["text"]) > MAX_TEXT:
        raise EnvelopeError(f"text is {len(env['text'])} chars, max {MAX_TEXT}")

    for side in ("from", "to"):
        party = env.get(side)
        if not isinstance(party, dict):
            raise EnvelopeError(f"{side!r} is not an object")
        if set(party) != {"org", "handle"}:
            raise EnvelopeError(f"{side!r} must have exactly org and handle")
        if not ORG_RE.match(str(party["org"])):
            raise EnvelopeError(f"{side}.org {party['org']!r} is not a valid org id")
        if not HANDLE_RE.match(str(party["handle"])):
            raise EnvelopeError(f"{side}.handle {party['handle']!r} is not a valid handle")

    # An unexpected key would be dropped from nothing — canonical() signs whatever
    # is present — but it would still reach a sink that might read it. Refuse.
    allowed = {"v", "id", "conv", "depth", "from", "to", "kind", "text", "ts", "sig"}
    extra = set(env) - allowed
    if extra:
        raise EnvelopeError(f"unexpected fields: {sorted(extra)}")


def addr(party: dict) -> str:
    return f"{party['handle']}@{party['org']}"
