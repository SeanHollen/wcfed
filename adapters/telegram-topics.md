# Adapter: `telegram-topics` (Zamua/claude-plugins)

Two pieces. Both fit the patterns already in the plugin, and neither changes how
Telegram routing works today.

Written against the published architecture: a proxy owns the bot token's
`getUpdates` poll, routes by `message_thread_id`, enqueues `{content, meta}` per
topic, spawns a tmux session per topic (`claude-<slug>-<tid>`), and the
topic-Claude's MCP long-polls `GET /poll?topic=T`. Outbound tools (`reply`,
`react`, `edit_message`, `download_attachment`) POST to the proxy with a `topic`
parameter. If any of that has changed, the shapes below are what need adjusting.

---

## 1. Inbound — one new handler on the proxy

`wcfed`'s `http` sink POSTs this:

```jsonc
{
  "topic": "general",              // envelope to.handle — your topic key
  "text":  "[wcfed: EXTERNAL ...]", // already quarantine-wrapped, deliver verbatim
  "meta": {
    "source": "wcfed",
    "from":   "frontend@seanpi",
    "conv":   "4f2a",
    "depth":  3,
    "kind":   "ping",              // "ping" = wake a session · "post" = ambient
    "id":     "01K2F9C3QW4M8XJ0R5T7YB2NDA",
    "external": true
  }
}
```

The handler reuses the enqueue the Telegram poller already calls — that is the
whole point; federation should not become a second delivery path with its own
bugs.

```ts
// proxy/proxy.ts — alongside the existing routes.
//
// Bind to 127.0.0.1. wcfed's gateway runs on the same host; this endpoint must
// never be reachable from outside, because unlike the relay it is not
// authenticated — the gateway has already verified the HMAC by this point.

if (req.method === "POST" && url.pathname === "/wcfed/inbound") {
  const body = await readJson(req);
  const { topic, text, meta } = body ?? {};

  if (typeof topic !== "string" || typeof text !== "string") {
    return json(res, 400, { ok: false, error: "topic and text are required" });
  }

  // "post" is ambient: it must NOT wake a session. Systems without this
  // distinction tend to map everything to an interrupt, which is how a shared
  // room turns into a pager.
  if (meta?.kind === "post") {
    await appendFeed(topic, `[${meta.from}] ${text}`);
    return json(res, 202, { ok: true, delivered: "feed" });
  }

  // "ping": the same path a Telegram message takes. Your proxy already spawns
  // or resumes the session from registry.json, so nothing extra is needed here.
  await enqueueForTopic(topic, {
    content: text,
    meta: {
      source: "wcfed",
      external: true,          // <- stop-reply-guard keys off this, see below
      from: meta?.from,
      conv: meta?.conv,
      depth: meta?.depth,
      id: meta?.id,
    },
  });

  return json(res, 202, { ok: true, delivered: "topic", topic });
}
```

Then point the gateway at it:

```ini
WCFED_SINK=http
WCFED_SINK_TARGET=http://127.0.0.1:<your-proxy-port>/wcfed/inbound
```

### `stop-reply-guard` needs to know

The hook blocks a turn triggered by an inbound Telegram message that never calls
`reply`. A federated message has **no Telegram message to reply to** — the
correct response is `fedcast`, which the guard has never heard of. Without a
change it will nag on every single cross-org turn.

```python
# hooks/stop-reply-guard.py
if (inbound_meta or {}).get("source") == "wcfed":
    # Federated turns answer with fedcast, not reply(). Nothing to guard.
    sys.exit(0)
```

## 2. Outbound — one more tool

Alongside `reply` / `react` / `edit_message`. Same shape as the others: the
topic-Claude POSTs to a local endpoint, which forwards to the wcfed gateway.

```ts
// The gateway signs and relays. This is a forwarder, not a protocol
// implementation — do not sign here.
if (req.method === "POST" && url.pathname === "/wcfed/send") {
  const { to, text, kind, conv, depth, from } = await readJson(req);
  const gw = process.env.WCFED_GATEWAY_URL ?? "http://127.0.0.1:8799";

  const upstream = await fetch(`${gw}/outbound`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      to,                              // "docs@seanpi"
      text,
      kind: kind ?? "ping",
      conv,                            // thread the reply, or omit to open one
      depth: depth ?? 0,
      from: from ?? "general",         // your session's handle
    }),
  });

  return json(res, upstream.status, await upstream.json());
}
```

MCP tool definition, so a topic-Claude can actually reach it:

```jsonc
{
  "name": "fedcast",
  "description":
    "Message an agent in ANOTHER operator's federation. Address is handle@org. Use kind='ping' only when you need them to act — it interrupts their session. Reply on the same conv id and omit the ping to end an exchange.",
  "input_schema": {
    "type": "object",
    "required": ["to", "text"],
    "properties": {
      "to":    { "type": "string", "description": "handle@org, e.g. docs@seanpi" },
      "text":  { "type": "string" },
      "kind":  { "type": "string", "enum": ["ping", "post"], "default": "ping" },
      "conv":  { "type": "string", "description": "thread id from an inbound message" },
      "depth": { "type": "integer", "default": 0,
                 "description": "carry through +1 from the inbound message" }
    }
  }
}
```

## 3. Don't want to touch the proxy yet?

Use the `command` sink. Anything that reads stdin can join a federation:

```ini
WCFED_SINK=command
WCFED_SINK_TARGET=/path/to/deliver.sh
```

```bash
#!/usr/bin/env bash
# stdin: {"env": {...}, "body": "quarantine-wrapped text"}
python3 -c '
import json, subprocess, sys
d = json.load(sys.stdin)
topic = d["env"]["to"]["handle"]
subprocess.run(["tmux", "send-keys", "-t", f"claude-{topic}", "-l", d["body"]])
subprocess.run(["tmux", "send-keys", "-t", f"claude-{topic}", "Enter"])
'
```

## 4. Handles

An envelope's `to.handle` is your **topic key**, not the full tmux session name.
`general@zamua` should land in the topic your registry calls `general`
(`claude-general`), not in a session literally named `general`. If your topic
keys contain characters outside `[A-Za-z0-9_.-]`, map them at the handler — the
envelope will not carry them.

## 5. What the other side looks like

For symmetry, the watercooler side delivers via `general-inject`, addressing the
target by the tmux window name that is already its bus handle. Neither side sees
the other's mechanism; both see envelopes. That is the design working.
