# Adapter: watercooler (ccgram + tmux)

The prototype runs against a **stock** watercooler install. `WatercoolerSink`
addresses the local target by prefixing the `@handle` that `general-inject`
already looks for, and carries its own quarantine framing in the message body.

```ini
WCFED_SINK=watercooler
WCFED_SINK_TARGET=/home/you/.local/bin/general-inject
```

Outbound uses `fedcast`, which needs no watercooler changes at all — it posts
straight to the gateway.

That is the whole integration. Everything below is optional.

---

## Optional: transparent routing from `broadcast`

The above means agents use two commands: `broadcast` for local, `fedcast` for
cross-org. If you'd rather have one — `broadcast "@docs@zamua ..."` just working —
turn on the log watcher:

```ini
WCFED_WATCH_LOG=/home/you/.ccgram/general.log
```

The gateway tails the feed and forwards any mention carrying a foreign `@org`.
It reads the log rather than wrapping `broadcast`, so the hot path stays
untouched — which matters, because `broadcast` already writes **every** message
to `general.log` before doing anything else.

The watcher starts at the end of the file on first run and persists its offset,
so enabling it never replays history. It skips lines whose role is `remote` or
`bus`, so a delivered federated message is never re-federated.

### Three source changes this needs

Each is a real bug under federation, not a nice-to-have. Without them the
transparent path misroutes and reports false failures — `fedcast` is unaffected.

**1. `wc_mentions` splits qualified handles.** In `bin/watercooler-lib.sh`:

```bash
# Current — turns "@docs@zamua" into TWO mentions, "@docs" and "@zamua".
# A federated ping then misfires into any local session with a colliding name.
wc_mentions() {
  printf '%s' "$1" | grep -oE '@[A-Za-z0-9_.-]+' | sed 's/^@//' || true
}

# Fixed — match the qualified form first, so it wins over the bare prefix.
wc_mentions() {
  printf '%s' "$1" \
    | grep -oE '@[A-Za-z0-9_.-]+(@[a-z0-9][a-z0-9-]*)?' \
    | sed 's/^@//' || true
}
```

**2. A foreign handle is not an undeliverable one.** In `bin/general-inject`,
the loop treats any unresolvable mention as an error — logs `bus/undelivered`
and exits 3. A gateway-routed handle must short-circuit *before* `wc_resolve`,
or every federated message reports failure to its sender:

```bash
for name in $MENTIONS; do
  # handle@org belongs to the federation gateway, which has already taken it
  # from the log. Not ours to resolve, and not a delivery failure.
  case "$name" in
    *@*) continue ;;
  esac
  ...
```

**3. A `remote` role.** `general-inject` accepts any `--role` string, so the
sink already passes `remote` and it lands in the feed correctly. What's missing
is framing: `wc_norm` should emit the external-party wording for `remote`
instead of the "another Claude session in the shared room" text, which is true
locally and wrong across a trust boundary.

Until then, the sink's own quarantine wrapper carries that framing — so this is
a deduplication cleanup, not a safety gap.

## The permission gap

`WatercoolerSink` injects into whatever mode the target session is running, and
this Pi launches every session in YOLO by default. A remote-originated turn
therefore reaches an auto-approving agent with shell access.

The prototype does not fix this. Until it does, point `WCFED_SINK_TARGET`'s
target handle at a session you did not start in YOLO — that is, create one topic
specifically as the federation landing zone and let it run in normal mode.

## Handles

A watercooler handle is a tmux window name, which is derived from the first
message sent to a Telegram topic. Two consequences worth telling the other
operator about:

* Handles are **not stable across a rename**, and a dormant session (idle
  autoclose frees the tmux window but keeps the topic) cannot receive an
  injection at all — `general-inject` reports it and exits 3, which the sink
  surfaces as a delivery failure rather than swallowing.
* Run `roster` for the handles that actually exist. Don't guess, and don't let
  the other side guess either — give them a short list of handles you intend to
  keep.
