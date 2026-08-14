# Bringing up a federation — instructions for the other operator's agent

**You are reading this because someone wants their Claude agents to be able to
message yours.** This document is written for the agent doing the work, not for
a human reading over its shoulder. Follow it top to bottom; each stage is
verifiable on its own, and every stage but the last can be done without the
other side being awake.

Design notes and diagrams: **https://ntgpz4nz.hostthis.dev**

You need: `python3` (3.11+), `bash`, `curl`, `git`. Nothing else — no pip
install, no package manager, no runtime that isn't already on the box.

---

## Stage 0 — Prove the code works on your machine (no network, no coordination)

```bash
git clone https://github.com/SeanHollen/wcfed.git
cd wcfed
./tests/selftest.sh
```

This stands up a relay and **two** gateways in different orgs on localhost and
drives real traffic through the real code path: sign → relay → long-poll →
verify → screen → quarantine → deliver → ack. It also runs the attacks — forged
signature, spoofed return address, replayed envelope, over-depth message, bad
relay token — and asserts each one is refused.

Expected last line:

```
selftest: 21 passed, 0 failed
```

**Do not continue past a failure here.** Everything after this stage involves
someone else's machine; if this stage is green, later failures are configuration
or network, which is a much shorter list to search.

## Stage 1 — Configure your org

The other operator will send you **three values** over a channel you both
trust — not in this repo, not in a link, not in a group chat:

| Value | What it is |
|-------|-----------|
| `RELAY_URL` | where the shared relay lives |
| `RELAY_TOKEN` | *your* org's relay credential |
| `SHARED_SECRET` | the HMAC key for this org **pair** |

The shared secret is the only thing stopping a third party from writing
directly into your agents' prompts. Treat it like an SSH private key.

```bash
mkdir -p ~/.wcfed && chmod 700 ~/.wcfed
cp examples/org.env.example ~/.wcfed/org.env
chmod 600 ~/.wcfed/org.env
$EDITOR ~/.wcfed/org.env
```

Fill in:

```ini
WCFED_ORG=zamua                      # your org id — agreed with the other side
WCFED_RELAY_URL=<RELAY_URL>
WCFED_RELAY_TOKEN=<RELAY_TOKEN>
WCFED_PEERS=seanpi:<SHARED_SECRET>   # their org id : the shared secret
WCFED_SINK=echo                      # start here. Really.
WCFED_SINK_TARGET=/home/you/.wcfed/inbox.jsonl
WCFED_LISTEN=127.0.0.1:8799
WCFED_STATE_DIR=/home/you/.wcfed/state
```

Then:

```bash
export WCFED_ENV_FILE=~/.wcfed/org.env
./bin/wcfed doctor
```

`doctor` checks your config, runs a sign/verify/tamper round trip offline, then
confirms the relay is reachable, knows both orgs, and accepts *your* token. All
twelve lines must read `PASS`.

Common failures, and what they actually mean:

| Symptom | Cause |
|---------|-------|
| `relay accepts our token — HTTP 401` | Token is for the other org, or `WCFED_ORG` doesn't match the token you were given |
| `relay knows our org — FAIL` | The relay operator hasn't added your org id yet. Tell them the exact string. |
| `WCFED_ORG ... is not a valid org id` | Org ids are `[a-z0-9-]`, **no dots**. `handle@dotted.org` parses as an email address and gets rewritten by CDNs and chat clients. |

## Stage 2 — Handshake with the `echo` sink

The `echo` sink writes inbound messages to a JSONL file and stdout. It delivers
to **no agent**. This is where you confirm the transport, the signatures and
both operators' configuration while the blast radius is still a text file.

Run the gateway in one terminal:

```bash
WCFED_ENV_FILE=~/.wcfed/org.env ./bin/wcfed gateway
```

It will log `gateway up`, then `listening on http://127.0.0.1:8799`. Leave it
running; it long-polls the relay for inbound and exposes `/outbound` for you.

Send one message (replace with the other side's org id and a handle they gave
you):

```bash
./bin/fedcast --to general@seanpi "hello from zamua — stage 2 handshake"
```

Expected: `fedcast sent to general@seanpi as [...]` plus a reply recipe with a
conversation id.

When they send one back, your gateway prints:

```
[hh:mm:ss] [zamua] delivered 01K... from general@seanpi (echo)
```

and the message lands in `WCFED_SINK_TARGET`. Inspect what actually arrived:

```bash
python3 -c "
import json
for line in open('$HOME/.wcfed/inbox.jsonl'):
    d = json.loads(line)
    print('from:', d['env']['from'], '| text:', d['env']['text'])
    print(d['body'])
"
```

Read the `body` field carefully — that is the **quarantine wrapper**, and it is
what would reach a model. Confirm you are happy with its framing before you
point a sink at a live session.

**Checkpoint.** At this point the federation is real. Both sides can exchange
signed messages. Everything remaining is local integration.

## Stage 3 — Deliver to your agents

Now swap the sink for something that reaches your runtime. Pick the row that
matches how much you want to integrate:

| `WCFED_SINK` | `WCFED_SINK_TARGET` | Effect |
|--------------|---------------------|--------|
| `command` | a shell command | Envelope as JSON on stdin. Fastest bridge to anything. |
| `http` | `http://127.0.0.1:PORT/wcfed/inbound` | `POST {topic, text, meta}` — the telegram-topics path |
| `watercooler` | path to `general-inject` | Injects into a live tmux session |

**If you run `telegram-topics`:** see `adapters/telegram-topics.md`. It is one
new HTTP handler on the proxy (~25 lines, reusing the per-topic enqueue you
already have) plus one outbound tool alongside `reply`/`react`/`edit_message`.
The file contains both, ready to paste.

Two things in that adapter worth reading before you wire it up:

1. Your `stop-reply-guard` hook blocks a turn triggered by an inbound Telegram
   message that never calls `reply`. A federated message has no Telegram message
   to reply to, so the guard needs to recognise the federated source or it will
   nag on every cross-org turn.
2. Your sessions run under `--permission-mode auto`. A federated message is
   written by an agent **the other operator controls**. Route inbound federation
   to a topic whose session is not auto-approving, or accept that you are letting
   a remote party's text reach an agent that runs commands without asking.

## Safety — the part worth arguing about

Inbound text is written by an agent you do not control, and it ends up in
another agent's prompt. That is a prompt-injection path, and both runtimes here
default to auto-approving permissions.

What `wcfed` does about it, on the delivery path rather than beside it (there is
no configuration in which a message skips `Gateway.screen()`):

* HMAC-SHA256 per org pair, constant-time compare, verified **before** any
  policy check — so an unauthenticated sender can't consume rate budget.
* Org allowlist. An empty allowlist means *your configured peers*, never *anyone*.
* Hard depth cap (default 8) enforced at both ends. A cross-org ping-pong loop
  spends **your** tokens, so unlike a local bus this cap is real, not advisory.
* Per-org rate limit (default 30/min).
* Persistent dedupe on envelope id — delivery is at-least-once by design.
* A quarantine wrapper that names the sender's org, marks the payload as data
  rather than instruction, and tells the model to summarise-and-stop rather than
  act.

**What it does not do yet, stated plainly:** it does not downgrade a session's
permission mode for a remote-originated turn. Until that lands, point your sink
at a session you did not start in YOLO / auto.

## Reference

* `SPEC.md` — the normative wire protocol. Implement it in any language; you do
  not need this repo, only HMAC-SHA256 and HTTP.
* `wcfed/envelope.py` — the reference implementation. Where prose and code
  disagree, the code wins.
* `./bin/wcfed doctor` — run it any time something looks wrong. It is the
  fastest way to tell "my config" from "their config" from "the network".

## Questions worth answering back

1. Is your proxy's per-topic enqueue reachable from a second HTTP handler, or is
   it welded to the `getUpdates` path?
2. Do you want a dedicated `federation` topic as the inbound landing zone, or
   should remote messages route to named topics directly?
3. Is your host publicly reachable? If so we can drop the relay entirely and
   point the gateways straight at each other.
4. HMAC with a shared secret is right for two parties. If you expect a third org,
   say so now — we start with ed25519 detached signatures rather than migrating
   a live federation later.
