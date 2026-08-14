"""`wcfed` — one entry point for the relay, the gateway and the diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

from .config import Config
from .envelope import EnvelopeError, build, canonical, parse_address, verify
from .gateway import Gateway
from .relay import parse_tokens, serve


def _cfg(args) -> Config:
    return Config.from_env(args.env_file)


def cmd_relay(args) -> int:
    tokens = parse_tokens(args.tokens or os.environ.get("WCFED_RELAY_TOKENS", ""))
    if not tokens:
        print(
            "relay: no org tokens. Pass --tokens 'orgA:tokA,orgB:tokB' or set "
            "WCFED_RELAY_TOKENS.",
            file=sys.stderr,
        )
        return 2
    httpd = serve(args.host, args.port, tokens)
    print(f"wcfed-relay listening on {args.host}:{args.port} for orgs: {', '.join(sorted(tokens))}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("relay down", flush=True)
    return 0


def cmd_gateway(args) -> int:
    Gateway(_cfg(args)).run()
    return 0


def cmd_send(args) -> int:
    """Send without a running gateway — signs and POSTs to the relay directly.

    Useful for a first handshake, when the gateway is not up yet on one side.
    """
    cfg = _cfg(args)
    gw = Gateway(cfg)
    try:
        result = gw.send(
            to=args.to,
            text=args.text,
            kind=args.kind,
            conv=args.conv,
            depth=args.depth,
            from_handle=args.from_handle,
        )
    except (EnvelopeError, Exception) as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result["relay"], indent=2))
    print(f"\nreply with: wcfed send --to {args.to} --conv {result['conv']} \"...\"")
    return 0


def cmd_doctor(args) -> int:
    """Check config, relay reachability and a full sign/verify round trip."""
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        mark = "PASS" if good else "FAIL"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")

    print("wcfed doctor\n")
    try:
        cfg = _cfg(args)
    except Exception as exc:
        print(f"  [FAIL] config — {exc}")
        return 1

    print(f"config: org={cfg.org} sink={cfg.sink} relay={cfg.relay_url}")
    check("org id is valid", True)
    check("at least one peer secret", bool(cfg.peers), f"peers: {sorted(cfg.peers) or 'none'}")
    check("relay token set", bool(cfg.relay_token))
    check(
        "allowlist is not empty",
        bool(cfg.allowed_orgs),
        f"allowed: {sorted(cfg.allowed_orgs)}",
    )
    check("depth cap is finite", cfg.depth_max > 0, f"depth_max={cfg.depth_max}")

    # Sign/verify round trip against every configured peer, offline.
    for org, secret in cfg.peers.items():
        env = build(
            from_org=cfg.org, from_handle="doctor", to_org=org, to_handle="doctor",
            text="round trip", secret=secret,
        )
        check(f"sign/verify round trip with {org}", verify(env, secret))
        tampered = dict(env)
        tampered["text"] = "round trip."
        check(f"tamper is detected for {org}", not verify(tampered, secret))
        check(
            f"canonical form is stable for {org}",
            canonical(env) == canonical(json.loads(json.dumps(env))),
        )

    # Relay reachability
    try:
        with urllib.request.urlopen(f"{cfg.relay_url}/v1/health", timeout=15) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        check("relay reachable", True, f"orgs known: {', '.join(health.get('orgs', []))}")
        check(
            "relay knows our org",
            cfg.org in health.get("orgs", []),
            f"we are {cfg.org!r}",
        )
        for peer in cfg.peers:
            check(f"relay knows peer {peer}", peer in health.get("orgs", []))
    except Exception as exc:
        check("relay reachable", False, str(exc))

    # Relay auth: an unauthenticated poll must be refused, ours must not be.
    try:
        req = urllib.request.Request(
            f"{cfg.relay_url}/v1/poll?wait=0",
            headers={"X-Wcfed-Org": cfg.org, "X-Wcfed-Auth": cfg.relay_token},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            json.loads(resp.read().decode("utf-8"))
        check("relay accepts our token", True)
    except urllib.error.HTTPError as exc:
        check("relay accepts our token", False, f"HTTP {exc.code} — token wrong for this org?")
    except Exception as exc:
        check("relay accepts our token", False, str(exc))

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


def cmd_keygen(args) -> int:
    print("# Shared secret for ONE org pair. Both sides put the same value in")
    print("# WCFED_PEERS. Send it over a channel you trust; it is the only thing")
    print("# stopping a third party from writing to your agents.")
    print(secrets.token_hex(32))
    return 0


def cmd_addr(args) -> int:
    try:
        handle, org = parse_address(args.address)
    except EnvelopeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"handle": handle, "org": org}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wcfed",
        description="Federate two Claude-agent buses over signed envelopes.",
    )
    p.add_argument(
        "--env-file",
        default=os.environ.get("WCFED_ENV_FILE", ""),
        help="env file with WCFED_* settings (default: $WCFED_ENV_FILE)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("relay", help="run the store-and-forward relay")
    r.add_argument("--host", default="0.0.0.0")
    r.add_argument("--port", type=int, default=8787)
    r.add_argument("--tokens", default="", help="orgA:tokenA,orgB:tokenB")
    r.set_defaults(func=cmd_relay)

    g = sub.add_parser("gateway", help="run the gateway for this org")
    g.set_defaults(func=cmd_gateway)

    s = sub.add_parser("send", help="send one federated message")
    s.add_argument("--to", required=True, help="handle@org")
    s.add_argument("--kind", default="ping", choices=("ping", "post"))
    s.add_argument("--conv", default=None)
    s.add_argument("--depth", type=int, default=0)
    s.add_argument("--from", dest="from_handle", default="cli")
    s.add_argument("text")
    s.set_defaults(func=cmd_send)

    d = sub.add_parser("doctor", help="check config, crypto and relay connectivity")
    d.set_defaults(func=cmd_doctor)

    k = sub.add_parser("keygen", help="print a new shared secret")
    k.set_defaults(func=cmd_keygen)

    a = sub.add_parser("addr", help="parse a handle@org address")
    a.add_argument("address")
    a.set_defaults(func=cmd_addr)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except EnvelopeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
