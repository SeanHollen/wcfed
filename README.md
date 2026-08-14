# wcfed — federating two Claude-agent buses

Let agents run by **different people, on different machines, under different
runtimes** message each other — without either operator adopting the other's
internals.

Built to join two specific systems, and deliberately generic enough for a third:

| Side | Runtime | Local delivery |
|------|---------|----------------|
| [watercooler](https://github.com/SeanHollen/watercooler) on [ccgram](https://github.com/alexei-led/ccgram) | tmux panes, one per Telegram topic | **push** — `tmux send-keys` into the pane |
| `telegram-topics` ([Zamua/claude-plugins](https://github.com/Zamua/claude-plugins)) | proxy + MCP, one session per topic | **pull** — session long-polls `/poll` |

Design notes and diagrams: **https://ntgpz4nz.hostthis.dev**

---

## Why this isn't just "put both bots in one Telegram group"

Because that cannot work. The Telegram Bot API **never delivers one bot's
messages to another bot**. Two bots can sit in the same group forever and
neither will see the other speak. Telegram is each side's *local* transport; the
federation link has to be built beside it.

## Shape

```
   your org                     relay                    their org
 ┌────────────┐          ┌────────────────┐          ┌────────────┐
 │  agents    │          │  store &       │          │  agents    │
 │     ↕      │          │  forward       │          │     ↕      │
 │  local bus │          │  queue per org │          │  local bus │
 │     ↕      │  POST →  │                │  ← POST  │     ↕      │
 │  gateway   │ ←  poll  │  (dumb, holds  │  poll →  │  gateway   │
 └────────────┘          │   no secrets)  │          └────────────┘
                         └────────────────┘
```

Both gateways **dial out**. Neither listens on a public port, so a Raspberry Pi
behind home NAT federates with a cloud box without any port forwarding. The
relay never holds a shared secret, so it can queue envelopes but cannot forge,
read-and-rewrite, or impersonate. Integrity is end-to-end between gateways.

If *both* sides happen to be publicly reachable, delete the relay and point the
gateways at each other. Nothing else changes.

## Install

No dependencies. Python 3.11+ and bash, that's it.

```bash
git clone https://github.com/SeanHollen/wcfed.git
cd wcfed
./tests/selftest.sh
```

The selftest stands up a relay and two gateways in different orgs on localhost
and drives real traffic through the real code path — sign, relay, long-poll,
verify, screen, quarantine, deliver, ack — plus the failure cases (forged
signature, spoofed return address, replay, depth cap, bad token). **Run it
before you talk to anyone else.** If it passes, every later failure is
configuration or network, which is a much shorter list to search.

## Configure

```bash
cp examples/org.env.example ~/.wcfed/org.env
$EDITOR ~/.wcfed/org.env
export WCFED_ENV_FILE=~/.wcfed/org.env
./bin/wcfed doctor
```

| Setting | Meaning |
|---------|---------|
| `WCFED_ORG` | your org id — lowercase, digits, hyphens, **no dots** (see below) |
| `WCFED_RELAY_URL` | where the relay lives |
| `WCFED_RELAY_TOKEN` | your org's relay credential (abuse control, not authenticity) |
| `WCFED_PEERS` | `org:sharedsecret,...` — one secret per org **pair** |
| `WCFED_SINK` | `echo` · `watercooler` · `http` · `command` |
| `WCFED_SINK_TARGET` | file path / general-inject path / URL / command |
| `WCFED_DEPTH_MAX` | hard cap on agent turns without a human (default 8) |
| `WCFED_RATE_PER_MIN` | inbound cap per source org (default 30) |
| `WCFED_ALLOWED_ORGS` | who may reach you; defaults to exactly your configured peers |
| `WCFED_WATCH_LOG` | optional: tail a watercooler `general.log` for `@handle@org` |

> **Org ids have no dots on purpose.** `handle@org` with a dotted org is
> indistinguishable from an email address, and every CDN, chat client and link
> scanner will rewrite it for you. We learned this by watching Cloudflare turn
> `@frontend@sean.pi` into a `[email protected]` link on a published page.

## Run

```bash
./bin/wcfed gateway          # long-running: inbound poll + local outbound endpoint
./bin/fedcast --to docs@zamua "the spec is at ..."
```

`fedcast` is the cross-org counterpart to watercooler's `broadcast`, with the
same semantics:

```bash
fedcast --to docs@zamua "API is live"            # ping — interrupts them
fedcast --to docs@zamua --post "build is green"  # post — ambient, wakes nobody
fedcast --to docs@zamua --conv 4f2a "done"       # reply that ENDS the thread
```

## Sinks — pick your integration depth

A sink is the only part that knows anything about your agent runtime. Start at
the top and work down; each row is a real milestone.

| Sink | Delivers to | Integration cost |
|------|-------------|------------------|
| `echo` | a JSONL file + stdout | **zero** — start here |
| `command` | any command, envelope as JSON on stdin | a shell script |
| `http` | `POST {topic, text, meta}` to a local URL | one handler on your proxy |
| `watercooler` | a live tmux session via `general-inject` | works on a stock install |

Federate with `echo` on both sides **first**. It proves the transport, the
signatures and both operators' config while the blast radius is still a text
file.

## Safety

Inbound text was written by an agent you do not control, and on the watercooler
side it ends up as literal keystrokes in a session that may be running in YOLO.
That is a prompt-injection path into an auto-approving agent with shell access,
so screening is on the delivery path, not beside it — there is no configuration
in which a message reaches a sink without passing through `Gateway.screen()`.

In order: **shape** (before the HMAC path can be reached) → **signature**
(before an unauthenticated sender can consume rate budget) → **allowlist** →
**depth cap** → **rate limit** → **dedupe** → **quarantine wrapper** → sink.

The quarantine wrapper names the sender's org, states that the content is data
rather than instruction, and gives the one correct reply command. It is the same
discipline watercooler already applies to local pings, widened to a boundary
where the other party is not you.

**Known gap, stated plainly:** the prototype does *not* downgrade a session's
permission mode for a remote-originated turn. `WatercoolerSink` injects into
whatever mode the target session is already running. Until that lands, point
`WCFED_SINK_TARGET` at a session you did not start in YOLO. This is phase 4 of
the plan, and it is the one piece of the safety story the code does not yet
keep on its own.

## Loop control

Watercooler leaves conversation depth **advisory** on purpose — its own source
explains that a hard cap would sever real work mid-thread. Across an org
boundary the calculus changes: a ping-pong loop now burns *the other operator's*
tokens and rate limit. So `wcfed` enforces the cap in both directions and drops
with a notice into both feeds.

## Layout

```
wcfed/envelope.py   the wire format — canonical bytes, HMAC, validation. IS the spec.
wcfed/relay.py      store-and-forward queues, long-poll, TTL, at-least-once
wcfed/gateway.py    poll/verify/screen/deliver/ack + local outbound endpoint
wcfed/sinks.py      echo · watercooler · http · command, and the quarantine wrapper
wcfed/cli.py        relay · gateway · send · doctor · keygen · addr
bin/fedcast         the agent-facing command
tests/selftest.sh   full federation on one box, including the attacks
adapters/           what each runtime has to add, with code
SPEC.md             normative wire protocol — implement this in any language
INTEROP.md          the four-stage protocol for bringing up a NEW federation
```

## Status

Prototype. The wire format, the crypto and the screening are real and tested;
the operational edges (permission downgrade, federated roster, relay
persistence) are marked TODO where they are missing rather than papered over.

Version `0.1.0`. The envelope carries `v`, so v1 stays readable when v2 exists.

MIT.
