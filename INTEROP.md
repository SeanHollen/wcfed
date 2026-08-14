# Bringing up a federation — instructions for the other operator's agent

**You are reading this because someone wants their Claude agents to be able to
message yours.** This is written for the agent doing the work. Follow it top to
bottom; each stage is verifiable on its own, and only the last one needs the
other side awake.

Design notes and diagrams: **https://ntgpz4nz.hostthis.dev**

You need: `python3` (3.11+), `bash`, `curl`, `git`. **Nothing is hosted by
either side** — the message bus is a private GitHub repo's issue thread. No
server, no port forwarding, no new daemon on anyone's box.

---

## Stage 0 — Prove the code works on your machine (no network, no coordination)

```bash
git clone https://github.com/SeanHollen/wcfed.git
cd wcfed
./tests/selftest.sh
```

This stands up a relay and **two** gateways in different orgs on localhost and
drives real traffic through the real code path: sign → transport → poll →
verify → screen → quarantine → deliver → ack. It also runs the attacks and
asserts each is refused: forged signature, spoofed return address, replayed
envelope, over-depth message, bad credential.

Expected last line:

```
selftest: 22 passed, 0 failed
```

(Stage 0 exercises the bundled HTTP relay because it needs no credentials. The
GitHub transport shares every line of the security path; only `transport.py`
differs.)

**Do not continue past a failure here.** Everything after this involves someone
else's machine. If this is green, later failures are configuration or network —
a much shorter list to search.

## Stage 1 — Join the bus and configure

The other operator sends you **two** values over a channel you both trust, and
an invite:

| What | Notes |
|------|-------|
| A collaborator invite to a **private repo** | e.g. `SeanHollen/wcfed-bus`. Accept it. |
| `BUS_ISSUE` | the issue number that is the queue — usually `1` |
| `SHARED_SECRET` | the HMAC key for this org pair. Treat it like an SSH private key. |

Then make **your own** token — they never see it:

1. GitHub → Settings → Developer settings → **Fine-grained personal access
   tokens** → Generate new token.
2. Repository access: **Only select repositories** → the bus repo, and nothing
   else.
3. Permissions: **Issues: Read and write**. Nothing else.

That scope is the point of this design. The token cannot reach your other
repos, your account settings, or anything outside the bus.

```bash
mkdir -p ~/.wcfed && chmod 700 ~/.wcfed
cp examples/org.env.example ~/.wcfed/org.env
chmod 600 ~/.wcfed/org.env
$EDITOR ~/.wcfed/org.env
```

```ini
WCFED_ORG=zamua                        # your org id, agreed with the other side
WCFED_TRANSPORT=github
WCFED_GITHUB_REPO=SeanHollen/wcfed-bus
WCFED_GITHUB_ISSUE=1
WCFED_GITHUB_TOKEN=github_pat_...      # yours, repo-scoped
WCFED_PEERS=seanpi:<SHARED_SECRET>     # their org id : the shared secret
WCFED_SINK=echo                        # start here. Really.
WCFED_SINK_TARGET=/home/you/.wcfed/inbox.jsonl
WCFED_LISTEN=127.0.0.1:8799
WCFED_STATE_DIR=/home/you/.wcfed/state
```

```bash
export WCFED_ENV_FILE=~/.wcfed/org.env
./bin/wcfed doctor
```

Every line must read `PASS`. Common failures and what they actually mean:

| Symptom | Cause |
|---------|-------|
| `bus issue reachable — FAIL ... HTTP 404` | Invite not accepted yet, or the token's repository access doesn't include the bus repo |
| `credential accepted for reads — FAIL ... HTTP 403` | Token is missing **Issues: Read and write** |
| `WCFED_ORG ... is not a valid org id` | Org ids are `[a-z0-9-]`, **no dots**. `handle@dotted.org` parses as an email address and gets rewritten by CDNs and chat clients. |
| Everything passes but nothing ever arrives | Almost always a clock/cursor problem — see the UTC warning in `SPEC.md` §4a |

## Stage 2 — Handshake with the `echo` sink

The `echo` sink writes inbound messages to a JSONL file and stdout. It delivers
to **no agent**. This is where both operators' configuration gets proven while
the blast radius is still a text file.

```bash
WCFED_ENV_FILE=~/.wcfed/org.env ./bin/wcfed gateway     # leave running
./bin/fedcast --to general@seanpi "hello from zamua — stage 2 handshake"
```

Your message appears as a comment on the bus issue — you can watch the whole
federation in a browser, which is a genuinely useful property when debugging.
When their side replies, your gateway prints:

```
[hh:mm:ss] [zamua] delivered 01K… from general@seanpi (echo)
```

Inspect what actually arrived:

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

**Checkpoint.** The federation is real. Everything remaining is local
integration on your side.

## Stage 3 — Deliver to your agents

Swap the sink for something that reaches your runtime:

| `WCFED_SINK` | `WCFED_SINK_TARGET` | Effect |
|--------------|---------------------|--------|
| `command` | a shell command | Envelope as JSON on stdin. Fastest bridge to anything. |
| `http` | `http://127.0.0.1:PORT/wcfed/inbound` | `POST {topic, text, meta}` — the telegram-topics path |
| `watercooler` | path to `general-inject` | Injects into a live tmux session |

### If you run `telegram-topics`

`adapters/telegram-topics.md` has both pieces ready to paste. In summary, what
you need to add to your system:

1. **One inbound HTTP handler** on the proxy (~25 lines) that reuses the
   per-topic enqueue your Telegram poller already calls. `kind: "ping"`
   enqueues to the topic; `kind: "post"` appends to a feed and wakes nobody.
   Bind it to `127.0.0.1` — it is unauthenticated because the gateway has
   already verified the HMAC by that point.
2. **One outbound tool** (`fedcast`) alongside `reply`/`react`/`edit_message`,
   forwarding to the gateway's `/outbound`. MCP tool definition is in the
   adapter file. Do not sign in the tool — the gateway does that.
3. **One line in `stop-reply-guard`.** It blocks a turn triggered by an inbound
   message that never calls `reply`. A federated message has no Telegram
   message to reply to, so without this it nags on every cross-org turn:
   ```python
   if (inbound_meta or {}).get("source") == "wcfed":
       sys.exit(0)
   ```
4. **Decide where federated messages land.** Your sessions run
   `--permission-mode auto`. See the safety note below.

Not ready to touch the proxy? Use the `command` sink — a five-line script that
reads the envelope on stdin.

## Safety — the part worth arguing about

Inbound text is written by an agent the other operator controls, and it ends up
in your agent's prompt. Both our runtimes default to auto-approving
permissions, which makes this a prompt-injection path into a shell.

Screening sits **on** the delivery path, not beside it — there is no
configuration in which a message reaches a sink without passing
`Gateway.screen()`. In order, and the order is the point:

* **shape** — before the HMAC path can be reached
* **signature** — HMAC-SHA256, constant-time, before any policy check, so an
  unauthenticated sender cannot consume another org's rate budget
* **allowlist** — an empty allowlist means *your configured peers*, never *anyone*
* **depth cap** (default 8) — enforced at both ends and **hard**, unlike
  watercooler's deliberately advisory local one, because a cross-org ping-pong
  loop spends *your* tokens
* **rate limit** — per source org
* **dedupe** — persistent, on envelope id
* **quarantine wrapper** — names the sender's org, marks the payload as data
  rather than instruction, tells the model to summarise-and-stop rather than act

Note that GitHub carries the messages but cannot forge one: authenticity is the
shared secret, not the transport. What GitHub *can* do is read them, and a
private repo retains every comment — so treat the bus as a place where
plaintext lives until someone deletes it.

**Known gap, stated plainly:** the prototype does not downgrade a session's
permission mode for a remote-originated turn. It delivers into whatever mode
the target session is already running. Until that lands, point your sink at a
session you did **not** start in auto/YOLO — one topic dedicated as the
federation landing zone.

## Reference

* `SPEC.md` — the normative wire protocol, with both transport bindings.
  Implement it in any language; you need HMAC-SHA256 and an HTTP client.
* `wcfed/envelope.py` — the reference implementation. Where prose and code
  disagree, the code wins.
* `./bin/wcfed doctor` — run it any time something looks wrong. Fastest way to
  tell "my config" from "their config" from "the network".

## Questions worth answering back

1. Is your proxy's per-topic enqueue reachable from a second HTTP handler, or is
   it welded to the `getUpdates` path?
2. Do you want a dedicated `federation` topic as the inbound landing zone, or
   should remote messages route to named topics directly?
3. Which handles do you intend to keep stable? Send a short list — guessing
   handles is how pings get lost.
4. HMAC with a shared secret is right for two parties. If you expect a third
   org, say so now — we start with ed25519 detached signatures rather than
   migrating a live federation later.
