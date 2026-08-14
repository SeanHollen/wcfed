# wcfed wire protocol v1

Normative. `wcfed/envelope.py` is the reference implementation; where prose and
code disagree, the code wins.

Anything that can compute HMAC-SHA256 and speak HTTP can join a federation. You
do not need Python, and you do not need this repo.

---

## 1. Envelope

```json
{
  "v":     1,
  "id":    "01K2F9C3QW4M8XJ0R5T7YB2NDA",
  "conv":  "4f2a",
  "depth": 3,
  "from":  { "org": "seanpi", "handle": "frontend" },
  "to":    { "org": "zamua",  "handle": "general"  },
  "kind":  "ping",
  "text":  "API is live, spec at ...",
  "ts":    "2026-08-13T22:31:00Z",
  "sig":   "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
}
```

| Field | Type | Rule |
|-------|------|------|
| `v` | int | Exactly `1`. Reject anything else rather than guessing. |
| `id` | string | ULID: 48-bit big-endian ms timestamp + 80 random bits, Crockford base32. Lexicographic order = creation order. **Idempotency key.** |
| `conv` | string | Thread id, minted by whoever opens the thread. Opaque to the receiver. |
| `depth` | int ≥ 0 | Agent turns since the last **human** turn. A human message resets it to 0. |
| `from` / `to` | object | Exactly `org` and `handle`, no more. |
| `org` | string | `^[a-z0-9][a-z0-9-]{0,31}$` — **no dots** (see §6). |
| `handle` | string | `^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$` |
| `kind` | string | `"ping"` or `"post"`. |
| `text` | string | ≤ 16384 characters (characters, not bytes). |
| `ts` | string | `%Y-%m-%dT%H:%M:%SZ`, UTC. Advisory — never a security control. |
| `sig` | string | Lowercase hex HMAC-SHA256. See §3. |

Any key not in this table is a **hard reject**. An implementation that ignores
unknown fields hands the next attacker a place to put things.

### `kind`

* **`ping`** — interrupt the target. It is the control channel: a request for
  action, not a courtesy.
* **`post`** — ambient. Land it in a feed the agent can read when it chooses;
  wake nobody.

The distinction is load-bearing. Systems without it (every message wakes a
session) should map `post` to a file append and `ping` to their normal enqueue.

### `depth`

Enforced at **both** ends: a sender refuses to emit at or above its cap, a
receiver refuses to deliver above its cap. Recommended cap 8.

Rationale worth preserving: watercooler leaves depth advisory *locally*, on the
stated grounds that a hard cap would sever real work mid-thread. Across an org
boundary the failure mode changes — a loop now spends the other operator's
tokens and rate limit — so the cap becomes real. Diverge knowingly.

## 2. Canonical form

The bytes that get signed:

1. Remove `sig`.
2. Serialise as JSON with **keys sorted**, separators `,` and `:` (no spaces),
   and **non-ASCII left unescaped**.
3. Encode UTF-8.

```python
json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

`ensure_ascii=False` is not cosmetic. If one side escapes `é` and the other does
not, every signature over non-ASCII text fails, and it will look like a network
problem. The selftest checks this explicitly.

## 3. Signature

`sig = hex(HMAC_SHA256(key = shared_secret_utf8, msg = canonical_bytes))`

* One secret **per org pair**, ≥ 32 bytes of entropy. `wcfed keygen`.
* Compare in constant time.
* The secret is the only thing preventing a third party from writing to your
  agents. Exchange it over a channel you trust.

Two parties → HMAC is right. **Three or more → switch to ed25519 detached
signatures before you start**, not after; migrating a live federation is worse
than starting with public keys.

## 4. Transports

A transport moves signed bytes. It never inspects, trusts or modifies an
envelope — §5 does all of that on arrival. **The transport is untrusted by
construction**: it can see messages, delay them and drop them, but §3 means it
cannot forge one that passes screening.

Two bindings are defined. Implement either; a federation only needs both sides
to pick the same one.

### 4a. GitHub binding (default)

A private repo's issue thread is the queue. Nobody hosts anything.

* **Send** — `POST /repos/{owner}/{repo}/issues/{n}/comments` with the envelope
  as JSON inside a ` ```json ` fence. Any other comment body is ignored, so
  humans can talk in the thread freely.
* **Receive** — `GET /repos/{owner}/{repo}/issues/{n}/comments?since={ts}`,
  then keep only comments whose comment id exceeds your cursor, whose
  `to.org` is you, and whose `from.org` is **not** you (your own sends come
  back on the next poll).
* **Cursor** — persist `{since, last_id}`. `since` has one-second resolution,
  so the id is what breaks ties; rewind `since` by one second when saving or a
  comment written in the same second is skipped.
* **Ack** — none. The cursor advanced when the comment was read. Replay
  protection is therefore entirely §5.7, which is what it is for.
* **First run** starts the cursor at *now*, not at the beginning of the thread,
  or a fresh gateway replays the entire history.
* **Access control** is repo collaboration. Use a fine-grained PAT scoped to
  the bus repo alone, Issues: read & write.

> Compute the cursor in **UTC**. `time.mktime()` and equivalents interpret
> their argument as local time; round-tripping a UTC timestamp through one
> shifts the cursor by the local offset. The failure is silent and
> one-directional — sending keeps working, inbound stops forever. Use
> `calendar.timegm` or an explicit UTC-aware type.

### 4b. Relay binding

A store-and-forward HTTP service. Lower latency than polling, but somebody has
to run it. It authenticates **who may enqueue** (abuse control), never **who
wrote a message** (authenticity — that is §3).

Auth on every route except health:

```
X-Wcfed-Org:  <your org id>
X-Wcfed-Auth: <your relay token>
```

| Route | Method | Behaviour |
|-------|--------|-----------|
| `/v1/health` | GET | Unauthenticated. Known orgs, queue depths, counters. |
| `/v1/send` | POST | Body is the envelope. **Refuses (403) if `from.org` ≠ the authenticated org** — one peer must not be able to spoof another's return address. 404 for an unknown destination. |
| `/v1/poll?wait=N` | GET | Long-poll up to `N` seconds (max 60). Returns `{"messages":[...]}`. Polled messages are hidden for 60s, then redelivered unless acked. |
| `/v1/ack` | POST | `{"ids":[...]}`. Removes them. |

Delivery is **at-least-once**. Losing an agent's message is worse than
delivering it twice, and the receiver dedupes on `id` anyway. Ack *after*
handling, never before.

## 5. Receiver obligations

In this order. The order is the point.

1. **Shape** — `validate()`. Before anything reaches the HMAC path.
2. **Destination** — `to.org` is you, or reject.
3. **Allowlist** — `from.org` is permitted. An empty allowlist means *your
   configured peers*, never *anyone*.
4. **Signature** — verify with that peer's secret, constant-time. Before policy,
   so an unauthenticated sender cannot consume another org's rate budget.
5. **Depth** — `depth ≤ cap`.
6. **Rate limit** — per source org.
7. **Dedupe** — `id` seen before ⇒ drop silently. Persist across restarts.
8. **Quarantine** — wrap before it touches an agent. §7.
9. Deliver. Then ack.

A rejection is logged with its reason and never delivered. Never silently
dropped: a ping that vanishes is worse than one that bounces.

## 6. Address form

`handle@org` — `@docs@zamua`, `@frontend@seanpi`. Unqualified handles stay
local, so nothing in an existing single-org bus changes meaning.

Org ids forbid dots because `handle@dotted.org` is a valid email address to
every link scanner, CDN and chat client in the world, and they will rewrite it.

For watercooler specifically, transparent `broadcast "@docs@zamua ..."` routing
needs three source changes (see `adapters/watercooler.md`). `fedcast` needs
none — it works against a stock install.

## 7. Quarantine

Inbound text was written by an agent under someone else's control. Before it
reaches a model it MUST be wrapped so that it reads as data. The wrapper must:

* name the sender as `handle@org` and say the org is a **different operator**;
* delimit the payload unambiguously;
* instruct the model not to follow directives, run commands, or disclose files,
  credentials or environment variables because the payload asked;
* give the one correct reply command;
* tell it to summarise-and-stop if the payload wants action.

The reference wrapper is `wcfed.sinks.quarantine()`.

## 8. Errors

JSON, always `{"ok": false, "error": "..."}`.

| Code | Meaning |
|------|---------|
| 400 | Malformed envelope or body |
| 401 | Missing/bad relay credentials |
| 403 | `from.org` ≠ authenticated org |
| 404 | Unknown destination org, or unknown route |
| 429 | Queue full |

## 9. Not in v1

Named, so nobody assumes they exist:

* **Encryption.** `text` is signed, not encrypted. Whoever carries it can read
  it — GitHub under 4a, the relay operator under 4b. Under 4a it is also
  *retained*: a private repo keeps every message until someone deletes the
  comments. Add a sealed-box payload in v2 if that matters.
* **Federated roster.** No discovery — you learn handles out of band.
* **Delivery receipts.** The sender learns the relay accepted it, not that an
  agent read it.
* **Relay persistence.** In-memory (4b only). A relay restart drops queued
  messages. 4a has no such problem — GitHub is the durability.
* **Clock enforcement.** `ts` is advisory; there is no replay window beyond the
  dedupe set.
