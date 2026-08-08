# Open work

Written for whoever picks this up next, and written to be *executed* rather
than admired. [MIGRATION](MIGRATION.md) is the record of the agent pivot and
is essentially closed; this is what is left after it, with the decisions that
have already been made attached to each item so they are not re-litigated.

**Do not trust this file over the tree.** Every claim below was true at the
commit that last touched it. Section 1 is how you check in ninety seconds.

---

## 1. Establishing the baseline

Run these before changing anything. If one of them is not green, that is the
task, and the rest of this document is out of date.

```
cd agent   && cargo build --release          # do this first; the suites below drive it

cd ../backend
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest -q                                     # 289 passed
.venv/bin/python -m bystack.conformance       ../agent/target/release/bystack-agent   # 26/26
.venv/bin/python -m bystack.conformance.fleet ../agent/target/release/bystack-agent   # 27/27
.venv/bin/python -m bystack.conformance.local ../agent/target/release/bystack-agent   # 12/12

cd ../agent    && cargo clippy --release --all-targets -- -D warnings && cargo test --release  # 31 passed
cd ../frontend && npx tsc --noEmit && npm test -- --run                                        # 148 passed
```

Agent binary: **1.82 MiB** against a 12 MiB budget, RSS **~3.95 MiB** against
20. Both are measured by `bystack.conformance`, not asserted from memory.

Three things that look like failures and are not:

- **`ruff format --check` reports 29 files and `cargo fmt --check` reports 8.**
  Pre-existing, on files that predate this work: the code is hand-wrapped and
  neither formatter is a gate. Do not "fix" it — the diff would be the whole
  repository and it would destroy deliberate comment alignment.
- **`cargo clippy` alone does not link the binary.** The conformance suites run
  `agent/target/release/bystack-agent`, so an agent change that is only
  clippy-checked is tested against the *previous* build — which looks exactly
  like a Controller-side bug. `cargo build --release` first, every time.
- **`git` has no configured identity.** Existing commits use
  `yaliby <yaliby4@gmail.com>` via environment variables. Match it:
  `GIT_AUTHOR_NAME=… GIT_AUTHOR_EMAIL=… GIT_COMMITTER_NAME=… GIT_COMMITTER_EMAIL=… git commit`.
  Do not write to their git config. There is no remote; nothing is pushed.

---

## 2. What was closed, and where to look

Three items that were the bulk of this file are done. They are listed here
rather than deleted because the *reasons* are still the interesting part, and
because the next person will be tempted by exactly the choices that were
rejected.

### Container logs — closed, end to end

`GET /api/v1/graph/node/logs?urn=…&tail=…`. Agent (`docker.rs::logs`,
`session.rs::fetch_logs`), Controller (`providers/agent/commands.py::LogsChannel`,
`AgentProvider.logs`, `api/routes/graph.py`), UI (`frontend/src/features/logs/`,
a collapsed section in the node inspector).

Decisions that are settled — **do not reopen**:

- **Logs are not a `CommandKind`** and the route does not touch
  `CommandService`. That enum is the closed set of *mutations* a read-only
  Controller refuses; refusing to show an operator why a container is failing
  because the platform is in its safe mode is exactly backwards. Both sides
  answer regardless of `read_only`, and `test_agent_logs.py` pins it.
- **One shot, not a stream.** `tail` is clamped by the daemon, by the agent
  (`MAX_LOG_LINES = 2000`) and by the route's schema. Follow/streaming is a
  separate feature with a different backpressure problem; do not shape this as
  its first half.
- **The stream tag is kept per line.** `stderr` is most of the diagnostic
  value. Do not flatten it into one blob on the way through.
- **A disconnected host is answered, not 404'd.** The reason travels in the
  body (`ok: false`), because the container is on screen and the operator
  clicked it. A host with no agent-backed provider *at all* is a 404, because
  that cannot become true by waiting.
- **The panel does not fetch on selection.** Clicking through a canvas would
  issue one agent round trip per card. Opening it is the ask; `useLogs.ts`
  says so at the top.

### Crash-loop depth — closed

`RestartCount` reaches the graph as `restart_count` on a container node.

- **The agent inspects only containers listed as `restarting`.** The field is
  not on `GET /containers/json` at any API version, so carrying it costs an
  inspect; inspecting everything would turn one request per List into one per
  container, which is the cost the informer's whole design avoids.
  `bystack.conformance` asserts *which ids* were inspected rather than how
  many, because an agent that inspects everything answers correctly.
- **It is hashed, deliberately.** `restarting` is the same state on the second
  failure and the four-hundredth, so a count outside the hash is a loop
  reported once and never again. It advances on an *event*, unlike `Status`
  which advances on a clock — that is the whole line, and `FailingStreak` sits
  on the other side of it.
- Surfaced as an ordinary attribute row, like `exit_code`. If you want it on
  the card itself, that is a `theme.ts`/`render.ts` change and a new decision.

### Durable audit — closed. **RBAC is not** — see §4

`infra/audit/durable.py`, on by default, JSON Lines in `agents.state_dir`,
bounded by count and compacted write-and-rename. ADR-0012 §4a is the record.

The in-memory ring survives as the fallback for a Controller with nowhere to
write, because "cannot persist" and "will not start" are different answers and
only one of them is acceptable for a control plane.

---

## 3. Known gaps in the three features above

**Start here.** These are defects and missing verification in the work that
just landed, not roadmap items. They are ranked: the first three are worth
doing before anything new, the last three are hygiene you can pick up when you
are next in the file.

Every one of them names the exact site and the cheapest fix that closes it.
Nothing here is a redesign.

### 3.1 The audit log `fsync`s on the event loop — **fix this first**

**What.** `DurableAuditLog._append` does a blocking write + `os.fsync` and is
called from `CommandService.execute`, which is a coroutine. Two records per
command, so two fsyncs, on the thread that also runs every agent's WebSocket
pump and every browser's delta stream.

**Why it matters.** Measured: **0.01 ms on tmpfs, 0.78 ms on NVMe/btrfs.** On
a spinning disk, a busy host or network storage it is 10–40 ms — per command,
with the whole Controller stopped. And the test suite cannot see it, because
`tmp_path` is tmpfs: this is a defect that is green by construction.

**Where.** `backend/src/bystack/infra/audit/durable.py`, `_append`.

**The efficient fix.** Do not build a write-behind queue; it needs a flush on
shutdown, an ordering guarantee and a test for a crash mid-queue, and buys
nothing here. Make `record`/`finalize` `async` and `await asyncio.to_thread(...)`
around the file work, or keep them sync and hand the write to a single
serialised worker task. The port (`core/ports/command.AuditLog`) is sync
today, so making it async is a three-caller change in `runtime/commands.py`.

**How you know it worked.** Add a check that records N entries against a path
whose write is artificially slow (monkeypatch `os.fsync` to `time.sleep`) and
asserts the event loop stayed responsive — e.g. a concurrent
`asyncio.sleep(0)` counter keeps advancing. Then revert the fix and watch it
fail, per §6.

### 3.2 The crash-loop inspect is sequential and uncapped

**What.** `deepen_crash_loops` awaits one `GET /containers/{id}/json` per
restarting container, in a loop, inside the List path.

**Why it matters.** The design claims the set is "bounded, usually empty".
Statistically true; there is no hard bound. A host where 50 containers are
crash-looping — a bad deploy, an OOMing node — pays 50 **serialised** unix
round trips before any frame is sent, delaying the containers that are fine
along with the ones that are not. That is the informer's whole cost model
inverted, in exactly the situation an operator is watching.

**Where.** `agent/src/informer.rs`, `deepen_crash_loops`.

**The efficient fix.** Two lines of policy, not a redesign: a hard cap
(inspect at most N, say 32, and leave the rest at zero — the graph is honest
either way), and `futures::future::join_all` over the capped set so the round
trips overlap. `futures` is not yet a dependency; `tokio::task::JoinSet` is
already available and does the same job.

**How you know it worked.** Extend `scenario_crash_loop_depth` in
`conformance/runner.py` — it already asserts *which ids* were inspected, so
add a host with more restarting containers than the cap and assert the count
stops there.

### 3.3 `.fleet` and `.local` conformance do not cover logs or `restart_count`

**What.** 27/27 and 12/12 are unchanged by this work. Only the single-agent
suite exercises either feature.

**Why it matters.** `.fleet` exists *because* a single-agent run is blind to
everything ADR-0008 claims about a fleet — and "a logs request routed to the
wrong host's agent" is precisely that class of bug. Nothing currently rules it
out. `.local` is the only suite that drives the real supervisor over a unix
socket, so nothing proves logs work on the zero-config path at all.

**Where.** `backend/src/bystack/conformance/fleet.py` and `local.py`.

**The efficient fix.** `fleet.py` already has three engines with distinct
container sets. One check: put a distinguishable line in host B's container,
ask host A's provider for host B's container id, and assert it is refused
rather than answered. `local.py`: one check that a logs read over the unix
socket returns the scripted lines — the plumbing is identical to the
single-agent scenario, so it is a copy of ~15 lines.

### 3.4 Three constants are mirrored across languages with no guard

**What.** `MAX_LOG_TAIL = 2000` (Python) mirrors `MAX_LOG_LINES = 2000`
(Rust); `DEFAULT_LOG_TAIL = 200` mirrors `TAIL = 200` in the frontend. All
three are kept in step by a comment.

**Why it matters.** If the agent lowers its clamp, the route's schema goes on
accepting a number the agent silently truncates, and the UI goes on saying
"Last 200 lines" over 50. This repo already decided that cross-language drift
gets a guard rather than a comment — that is what
`test_wire.py::test_every_docker_field_the_mapper_reads_is_carried_on_the_wire`
is.

**Where.** `providers/agent/commands.py:70`, `agent/src/docker.rs:320`,
`frontend/src/features/logs/model/useLogs.ts:31`.

**The efficient fix.** One test in `test_wire.py`, beside the existing drift
guard: read `agent/src/docker.rs` and assert the `MAX_LOG_LINES` literal
equals `MAX_LOG_TAIL`. It is a regex over a file, which is exactly what
`_keys_read_by_mapper` already does. The frontend constant is better solved by
deleting it — let the client omit `tail` and take the Controller's default.

### 3.5 `DurableAuditLog.finalize` is O(retain) in the common case

**What.** It scans the deque from the oldest end to find an id that is almost
always the newest, because the entry being finalized is the one just
dispatched. Measured **0.50 ms** with the ring full.

**Why it matters.** Not much, today: a few commands a minute. It is here
because the shape was inherited from `memory.py`, where the ring is 2,000 —
and raising retention to 20,000 made it 10× worse without anyone deciding to.

**Where.** `backend/src/bystack/infra/audit/durable.py`, `finalize`.

**The efficient fix.** `_index` already maps id → entry. Make it map id →
position, or hold the entries in a `dict` and derive `recent()` from
`reversed(dict)` — Python dicts are insertion-ordered, and eviction is
`next(iter(...))`. The second removes the scan from `memory.py` too.

### 3.6 `LogsChannel` is a near-verbatim copy of `CommandChannel`

**What.** ~60 lines duplicated in `providers/agent/commands.py`.

**Why it matters.** The duplication was deliberate — the two genuinely differ
in what a failure *means*, and the shared docstring says so — but nothing
stops one being fixed and the other not. The bug class the class exists to
prevent (a leaked future) would then exist in exactly one of them.

**The efficient fix.** Do not generalise into a base class with a type
parameter; the differences are in the failure mapping and would end up as
hooks. Extract only the parked-future mechanics — `dispatch/resolve/abandon`
over a `Future[T]` — and let each channel keep its own envelope construction
and its own outcome mapping. If that reads worse than the duplication, leave
it and add a comment saying the copy was measured and kept.

---

## 4. Authentication, and the destructive operations behind it

**This is the largest open item that is not packaging, and it is a design
question before it is a coding one.**

`CommandKind` has no `remove`, `prune`, volume deletion or image cleanup, and
its docstring says why: a destructive operation is only defensible once the
platform can answer *who deleted the database volume*. That needed three
things. Two are now done — the audit is durable, and it records refusals and
attempts as well as completions. The third is missing entirely:

**Nothing authenticates a browser to this API.** There is no login, no
session, no token, no user model. `CommandRequest.actor` defaults to the
literal string `"anonymous"` and the client is explicitly forbidden from
setting it (`test_the_client_cannot_choose_who_the_audit_log_blames` — an
attacker-chosen name beside a real operation is worse than none, because it
looks like evidence). The API binds to loopback by default, and that is the
whole of the current answer.

So the work is not "add RBAC to the command service". It is, in order:

1. **Decide the identity model.** Local accounts? OIDC? A reverse proxy that
   asserts a header, which is what most homelab deployments already have in
   front of everything else? These have very different consequences for the
   two-listener split in ADR-0011 and for the "may sit behind an ordinary
   reverse proxy" property the browser port was given.
2. **Write the ADR before the code.** Every comparable decision in this tree
   has one, and this one changes the shape of the API surface permanently.
3. Then `actor` becomes real, RBAC has something to be a function of, and
   `CommandKind` can grow — in that order, and not before.

Do not add a destructive verb to unblock a demo. Adding one before the "who"
exists is the single sequencing mistake this codebase has been careful to
avoid, and it is the one that cannot be quietly walked back.

---

## 5. Step 7 — packaging

The last unchecked step in [MIGRATION §6](MIGRATION.md). Nothing exists yet:
no `.github/workflows`, no Dockerfile, no systemd unit, no `bystack-ctl`.

The one hook already in place is `bystack/_bundled/bystack-agent`
(`runtime/localagent.py:48`), first in the local agent's search order and
absent in a source checkout, where the search falls through to
`agent/target/release/`.

**Why this blocks the product, not just the release:** "add a host" in the
dashboard mints a token and composes
`bystack-agent --controller wss://… --token …` for the operator to paste — a
command that assumes the binary is *already on the target machine*. There is
no package, no installer and no bootstrap script, so today the button hands
you a command you cannot yet run on a new server. Until step 7 lands, the
fleet story is complete for hosts that already have the binary and for no
others.

Note for whoever writes the systemd unit: `serve()` owns SIGINT and SIGTERM
itself (`main.py`, commit `4143b5f`) precisely so `systemctl stop` finishes
cleanly. `bystack.conformance.local`'s `stopped_by_signal` is the check that
holds it — it spawns the real entry point and signals it, and it is the only
check in the suite that does. Do not let a refactor quietly hand the signals
back to uvicorn: `Server.serve()` re-raises the signal it caught, which
terminates the process before any cleanup its caller arranged.

Two things the packaging will now have to place that it would not have before:

- `agents.state_dir` holds a third file (`operations.jsonl`) and is created
  0700. A container image needs it on a volume or the audit log is durable
  only until the container is replaced, which is worse than honest.
- The frontend bundle is **1.72 MB (530 KB gzipped)**, over vite's warning
  threshold. Untouched, and a code-splitting question that belongs here.

---

## 6. Smaller known items

- **`status_text` is carried but never surfaced.** Deliberate: it is a
  rendered relative timestamp, and it is excluded from the agent's content
  hash, so a displayed copy would be stale by an unbounded amount. The durable
  facts inside it — exit code, health verdict — are extracted instead. Leave
  it alone unless you also solve the staleness.
- **Docker healthchecks** are carried and hashed as of `9f56763`;
  `FailingStreak` deliberately is not, because it advances on every failed
  probe. `statusOf` in `frontend/.../theme.ts` draws `running` + `unhealthy`
  as a warning, and `starting` deliberately as good.
- **`GET /agents` merges a durable enrollment row with the live provider set**,
  so a host that once enrolled over mTLS and now runs as the Controller's local
  agent shows `status: approved` with `local: true`. That is correct and was
  verified; a clean machine shows `status: local` with no registry row at all.
- **The agent advertises capabilities and the Controller reads them.**
  `AgentSession.capabilities`, populated from `Hello`. Anything you add to the
  wire that an older agent would not recognise must be gated on one, or the
  request times out with no diagnosis instead of refusing with one.

---

## 7. Conventions that will bite you

- **The `.proto` is the contract.** Never renumber, never reuse a tag; retire
  with `reserved`. Mixed-version fleets are a normal operating state under
  ADR-0008, not a migration window. Highest tag in use: `restart_count = 14`
  on `Container`, `logs_response = 24` on the envelope.
- **Python bindings are generated and checked in.** After editing the schema
  run `backend/scripts/generate_proto.py`, which writes both the Python
  bindings and `agent/proto/agent.bin`. `test_wire.py` fails if the committed
  copy drifts.
- **Adding a field the mapper reads means editing three files** — the
  `.proto`, the agent, and `ingest.py`'s translation back into Docker's
  vocabulary. `test_wire.py::test_every_docker_field_the_mapper_reads_is_
  carried_on_the_wire` is what stops you forgetting the third.
- **No test anywhere may require a Docker daemon or a network.**
- **No test may write outside `tmp_path` either.** `conftest.py`'s autouse
  `_state_dir_is_disposable` redirects the default state directory; without it
  every test that builds an app from a bare `Settings()` mints a CA and
  appends to an audit log in the home directory of whoever ran the suite.
- **A check that cannot fail is decoration.** Every check added in this round
  was validated by reverting the fix and watching it fail. Do the same — and
  delete `__pycache__` between mutation runs, or you will spend twenty minutes
  debugging bytecode.
- **`PartitionWriter` never gets a `source` argument.** It is what stops a
  compromised agent writing to another host's partition.
