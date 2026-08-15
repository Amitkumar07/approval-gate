# Security

Practical guidance for running approval-gate somewhere real. This
covers what the library does and doesn't handle for you — read it
before putting any of the network-facing backends (Email, Slack,
Webhook) somewhere reachable from outside your machine.

## Secrets

`EmailBackend.secret`, `SlackBackend.signing_secret`, and any SMTP or
bot-token credentials are plain constructor arguments — approval-gate
does not read environment variables, a secrets manager, or a config
file for you. That's deliberate: reading `os.environ` is one line you
write at the call site, and baking a specific secrets-loading
mechanism into the library would just be a worse version of whatever
your deployment already uses (Docker secrets, AWS Secrets Manager,
Vault, a plain `.env` file loaded by your process manager).

```python
import os
from approval_gate.backends import SlackBackend

backend = SlackBackend(
    bot_token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    channel="#approvals",
)
```

**Never commit a real secret into example code, a demo script, or a
test fixture.** Every `examples/*_demo.py` script in this repo uses a
placeholder value specifically so nobody copy-pastes a working secret
into a public gist.

### What leaks if a secret leaks

- **`EmailBackend.secret`** — every approve/reject link already sent
  becomes forgeable until you rotate it. There's no way to invalidate
  a single outstanding link; rotating the secret invalidates *all* of
  them (including ones a legitimate reviewer hasn't clicked yet).
  Rotate by restarting with a new `secret=` — no other bookkeeping.
- **`SlackBackend.signing_secret`** — an attacker who reaches your
  listener can forge interaction payloads, i.e. approve or reject
  arbitrary pending actions. Rotate it from your Slack app config
  (regenerates the value there too), then restart with the new one.
- **`SlackBackend.bot_token`** — lets someone post messages as your
  bot into whatever channels it has access to. Revoke and reissue from
  the Slack app config; this doesn't affect already-signed
  interactions, only the outbound post.
- **SMTP credentials** — standard mail-account compromise; rotate
  through your mail provider same as any other service account.

None of these secrets are ever logged, printed, or written to the
audit database by approval-gate. Exceptions from a failed send/post
are printed (`[approval_gate] ... failed (ignored): {exception!r}`) —
these come from the underlying library (`smtplib`, `urllib`) and
describe the failure (connection refused, timeout), not the
credential. A `smtplib.SMTPAuthenticationError` specifically can
include text your mail server chose to send back on a rejected login;
that's inherent to how SMTP auth failures are reported, not something
approval-gate adds.

## Running the network-facing backends in production

`EmailBackend`, `SlackBackend`, and `WebhookBackend` each start a
small HTTP listener via Python's stdlib `http.server`
(`ThreadingHTTPServer`). That module's own documentation is explicit
that it implements only basic security checks and isn't hardened for
internet-facing use on its own. All three backends need to be
reachable from outside your machine to work at all (Slack has to
reach your Interactivity Request URL; a reviewer's mail client or
browser has to reach the email/webhook listener) — so "reachable from
the internet" is the normal case for these, not an edge case.

**Put a real reverse proxy in front of it.** nginx, Caddy, or your
cloud provider's load balancer — terminating TLS and forwarding to the
backend's local port. Concretely, that gets you:

- **TLS.** `http.server` speaks plain HTTP. Slack's Interactivity
  Request URL and any link a reviewer clicks should be `https://` —
  terminate TLS at the proxy, forward plaintext to `127.0.0.1:<port>`
  behind it.
- **Basic DoS/abuse protection.** Rate limiting, request size limits,
  slow-request timeouts — things `http.server` doesn't attempt and a
  reverse proxy handles as a matter of course.
- **A stable public hostname** independent of wherever the process
  happens to be running, which `public_base_url` (EmailBackend) and
  the Slack Request URL both need to be configured against.

None of the three backends' own request-verification is weakened by
sitting behind a proxy — `SlackBackend`'s signature check and
`EmailBackend`'s link HMAC both verify the original request content,
which a proxy forwards unchanged.

### What's already handled at the application layer

- Every backend returns `400` on malformed input instead of crashing
  the request thread (see each backend's own tests).
- `SlackBackend` verifies every interaction against Slack's signing
  scheme, including a replay-window check on the timestamp, before
  parsing anything in the payload.
- `EmailBackend` links are HMAC-signed so a link can't be edited into
  approving a different action.
- A double-click or two reviewers racing each other resolves cleanly
  (second request gets 404, or a friendly "already decided" for
  Slack) instead of a race condition in application state.

What a reverse proxy adds is everything below that layer — transport
security and traffic shaping — which is out of scope for a Python
`http.server` instance to provide on its own.

## Multi-process / multi-instance deployments

`AuditLog` uses SQLite in WAL mode with a `busy_timeout`, which is
safe for multiple processes on **one machine** writing to the same
`db_path` concurrently (see `audit.py`'s module docstring). It is not
a distributed database — for a horizontally-scaled deployment across
multiple machines, point `db_path` at a shared filesystem your
deployment already has for this, or don't share one `audit.db` across
machines at all. A Postgres-backed `AuditLog` implementation isn't
built; if you need one, see `BACKEND_TEMPLATE.md`'s spirit (though
that file is about approval *channels*, not the audit store) or open
an issue.

## Reporting a vulnerability

This is a young, unfunded open-source project without a dedicated
security contact yet. If you find something serious, open a GitHub
issue with as much detail as you're comfortable posting publicly, or
mark it clearly as security-sensitive and a maintainer will follow up
on next steps for private disclosure.
