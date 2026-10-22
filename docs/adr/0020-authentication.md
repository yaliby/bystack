# ADR-0020 — Accounts and sign-in, with BYheaven single sign-on

**Status:** Accepted, and built.
**Date:** 2026-10-22
**Supersedes in part:** [ADR-0014](0014-no-user-identity.md) — the *"no user identity; the network is the boundary"* half. The other half, that no destructive verb exists, is **unchanged**.
**Depends on:** [ADR-0011](0011-agent-trust-and-enrollment.md), [ADR-0012](0012-operations-and-audit.md)

---

## Context

ADR-0014 priced three identity models against a single-operator LAN control
plane and bought none of them: a login would only say "the one person with the
password did it". Its own *"the one thing an operator must understand"*
paragraph named the cost: anyone who can reach the browser port can start,
stop, restart and kill containers on every managed host, and — with the
optional host grants of [ADR-0016](0016-watched-units-and-processes.md)
installed — stop any systemd unit or signal any process, because the watch
list that bounds those is edited from the same API.

The deployment turned out not to be the one ADR-0014 assumed. The Controller
runs next to other services behind a launcher (BYheaven) and a gateway
(OmniBridge) that already have accounts and already sign people in to each
other. A control plane with root-equivalent reach that is the one thing on that
network without a login is the odd one out, and "put a proxy in front of it"
means every installation has to build the door the product could ship.

## Decision

**The browser-facing API requires a signed-in account, on by default.** The
agent listener is unchanged: it was never reachable by a browser and is
authenticated by mutual TLS ([ADR-0011](0011-agent-trust-and-enrollment.md)).

The mechanism is OmniBridge's, minus the parts a single Controller has no use
for.

| | |
|---|---|
| **Accounts** | `<state_dir>/users.json`: a name, a scrypt hash, `disabled`, a `token_version`. 0600 in the 0700 state directory, rewritten whole by write-and-rename. This is [ADR-0001](../../ARCHITECTURE.md)'s third durable category, *permissions — user intent*, which the table has reserved since the start; ADR-0014 priced it as a fifth, and it is not one. |
| **First run** | With no accounts, the first visit creates the administrator — optionally gated by `auth.setup_token`, because whoever reaches the port first owns the Controller. |
| **Sessions** | `HttpOnly`, `SameSite=Strict` cookie holding `s1.<payload>.<mac>` (HMAC-SHA256 under `<state_dir>/session.key`). Every request re-reads the account, so a changed password or a disabled account ends its sessions on the **next request**, whatever time they had left. Re-enabling does not bring them back. |
| **Brute force** | A per-address rate limit on every route that tries a secret, and a per-account lockout. Unknown names cost the same time as wrong passwords. |
| **Single sign-on** | A BYheaven ticket (`v1.<payload>.<mac>`: HMAC-signed, 30 s, bound to this service by `aud`, single use by `jti`) signs in the **existing, enabled** account of the same name. Off unless `auth.sso_secret` is set. It creates nothing, and a BYheaven user with no account here is refused. |
| **The audit log** | `actor` is the signed-in account's name instead of `anonymous`. |
| **`bystack-ctl`** | Presents `<state_dir>/ctl.key` (or `BYSTACK_CTL_KEY`) as a bearer token. `bystack-ctl user add\|passwd\|enable\|disable\|list` edits accounts in the state directory, which is how a BYheaven user gets an account and how recovery works when nobody can sign in. |

### The gate is one place, and denies by default

`api/gate.py` is a pure ASGI middleware: everything under `/api` and the
generated docs needs a session unless it is named in `PUBLIC_PATHS`. A route
added next year is protected without anyone remembering to protect it. It also
covers the graph WebSocket, which is the most sensitive thing the port serves;
the browser sends its cookie with the handshake, so there is no ticket to mint
and nothing to put in a URL.

The public list is the sign-in routes and **the health probe**, which has to
stay open: the self-updater (ADR-0018) and the container healthcheck call it
from the same machine with no way to sign in, and closing it would make every
update roll itself back.

### Cross-origin writes are refused, not left to the cookie

`SameSite=Strict` is not enough on its own, because "same site" ignores the
port: another web app on this host counts as the same site. The gate refuses a
write, or a WebSocket handshake, whose `Origin` is neither this host nor in
`api.cors_origins` (the dev server). A browser always sends `Origin` on both.

### Why a cookie and not OmniBridge's bearer JWT

OmniBridge keeps a JWT in the page and trades it for short-lived tickets to open
a WebSocket, because a browser cannot attach a header to one. Here the page
makes about a dozen kinds of request and holds a socket open for its whole
life; a cookie the script cannot read needs no plumbing through any of them and
no ticket for the socket. The price is the origin check above, which is
cheap.

### What was deliberately not copied

* **Roles.** Every signed-in account may do whatever the verbs allow. See
  *Consequences*.
* **Two-factor, personal API tokens, and OmniBridge's "no-token access for
  local tools".** The last is an ambient session any local process could
  borrow; `bystack-ctl`'s control key is the explicit, file-permission-gated
  version of the one legitimate use.
* **A user-management page and a change-password form.** Accounts are changed
  with `bystack-ctl user`. A web form is an afternoon of work whenever it is
  wanted; the file format does not change.
* **Login events in the audit log.** That log records operations
  (`AuditEntry.kind` is a `CommandKind`); sign-ins and failures go to the
  Controller's log, with the client address.

## Consequences

**The destructive-verb decision stands.** ADR-0012 made destructive verbs
conditional on answering *who*, and ADR-0014 ruled them out because *who* would
never be answered. It now can be — and that is **not** enough to reopen it.
Accounts answer "who", not "who may": with no roles, adding `prune` would put
unrecoverable data loss one click from every account, including one created for
a colleague to look at the map. `CommandKind` is unchanged, and adding to it
needs an ADR that says what authorizes it.

**The worst case moves from "anyone on the network" to "anyone with an
account".** A session is a password, and the connection carrying it is plain
HTTP unless something terminates TLS. Reaching the Controller over `ssh -L`,
or behind a TLS proxy, is still the right way to leave loopback; the cookie is
marked `Secure` when the proxy says `X-Forwarded-Proto: https`.

**`auth.required: false` is the ADR-0014 deployment, kept.** It is a real
setting for a Controller on loopback that only its operator can reach. With it
off the gate is not installed, `actor` reads `anonymous`, and every statement
in ADR-0014 about the bind being the whole of the authorization is true again.

**Zero-config startup gains one step.** `python -m bystack` still shows a graph
with no configuration, but the first visit asks for an administrator's name and
password.

**A BYheaven user is as strong as BYheaven's own sign-in.** The ticket replaces
the password and there is no second factor here, so anyone who can sign in to
BYheaven can open this Controller as the account of the same name — the same
statement OmniBridge's `SECURITY.md` makes, for the same reason.

**Known limits.** `bystack-ctl user` and a sign-up in the dashboard can race
for the file (no cross-process lock; accounts change a few times a year). A
cookie sign-out forgets *this browser's* session; ending every session of an
account is `bystack-ctl user passwd` or `disable`, which bump the account's
`token_version`.
