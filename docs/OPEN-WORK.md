# Open work

Written for whoever picks this up next, and written to be *executed* rather
than admired. [MIGRATION](MIGRATION.md) is the record of the agent pivot and
is essentially closed; this is what is left after it, with the decisions that
have already been made attached to each item so they are not re-litigated.

**Do not trust this file over the tree.** Every claim below was true at commit
`78e4df8`. Section 1 is how you check in ninety seconds.

---

## 1. Establishing the baseline

Run these before changing anything. If one of them is not green, that is the
task, and the rest of this document is out of date.

```
cd backend
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest -q                                     # 248 passed
.venv/bin/python -m bystack.conformance       ../agent/target/release/bystack-agent   # 18/18
.venv/bin/python -m bystack.conformance.fleet ../agent/target/release/bystack-agent   # 27/27
.venv/bin/python -m bystack.conformance.local ../agent/target/release/bystack-agent   # 12/12

cd ../agent    && cargo clippy --release --all-targets -- -D warnings && cargo test --release  # 29 passed
cd ../frontend && npx tsc --noEmit && npm test -- --run                                        # 136 passed
```

Agent binary: **1.81 MiB** against a 12 MiB budget, RSS **~4.0 MiB** against
20. Both are measured by `bystack.conformance`, not asserted from memory.

Two things that look like failures and are not:

- **`ruff format --check` reports 29 files and `cargo fmt --check` reports 8.**
  Pre-existing, on files that predate this work: the code is hand-wrapped and
  neither formatter is a gate. Do not "fix" it — the diff would be the whole
  repository and it would destroy deliberate comment alignment.
- **`git` has no configured identity.** Existing commits use
  `yaliby <yaliby4@gmail.com>` via environment variables. Match it:
  `GIT_AUTHOR_NAME=… GIT_AUTHOR_EMAIL=… GIT_COMMITTER_NAME=… GIT_COMMITTER_EMAIL=… git commit`.
  Do not write to their git config. There is no remote; nothing is pushed.

---

## 2. Container logs — the Controller half

**The largest open hole, and the one nearest to done.** The contract and the
agent are committed and tested (`78e4df8`). Nothing calls any of it yet.

### What exists

`proto/bystack/agent/v1/agent.proto`, envelope tags 23 and 24:

```
LogsRequest  { string request_id = 1; string target_id = 2; uint32 tail = 3; }
LogsResponse { string request_id = 1; bool ok = 2; string reason = 3;
               repeated LogLine lines = 4; }
LogLine      { bool stderr = 1; string text = 2; }
```

Agent side, all done: `Engine::logs` and `demultiplex` in `agent/src/docker.rs`
(six tests in `mod log_tests`), dispatch in `agent/src/session.rs` via
`fetch_logs`, and `"logs"` advertised in the `Hello` capability list.

### Decisions already taken — do not reopen

- **Logs are not a `CommandKind`.** That enum is the closed set of *mutations*
  a read-only Controller refuses. Refusing to show an operator why a container
  is failing because the platform is in its safe mode is exactly backwards, so
  logs are their own frame and the agent answers them regardless of
  `read_only`. **The Controller route must not go through `CommandService`.**
- **One shot, not a stream.** `tail` is clamped by the daemon and again by the
  agent (`MAX_LOG_LINES = 2000`). Follow/streaming is a separate feature with a
  different backpressure problem; do not shape this as its first half.
- **The stream tag is kept per line.** `stderr` is most of the diagnostic
  value. Do not flatten it into one blob on the way through.

### What to build, in this order

**Step 1 — `capabilities` on the session port.** *Do this first; it is the
one piece that is not a copy of an existing pattern, and discovering it
half-way through step 2 costs a refactor.*

`core/ports/agent.py`'s `AgentSession` protocol exposes `engine_id`,
`read_only`, `agent_version`, `local` and `send` — and **no capabilities**.
Add a `capabilities: frozenset[str]` property, populate it from `Hello` where
the concrete session is built (`api/routes/agents.py`), and mirror it in the
test doubles under `tests/`. Absence of a capability is the answer for both
"too old" and "compiled out", which is why this is a set and not a version
check.

**Step 2 — `LogsChannel`, beside `CommandChannel`.** In
`providers/agent/commands.py`, which is already the correlation module and
whose docstring explains the whole approach. Copy `CommandChannel` exactly —
it is ~60 lines and every line of it is load-bearing:

- id from `uuid.uuid4().hex[:16]`, future parked in `self._pending`
- `await session.send(envelope)` then `async with asyncio.timeout(deadline)`
- `except AgentDisconnected` and `except TimeoutError` handled separately
- **`finally: self._pending.pop(request_id, None)`, unconditionally** — this
  is the leak the class exists to prevent
- an `abandon(reason)` that fails every parked waiter on disconnect
- a `resolve()` that discards an unknown `request_id` loudly rather than
  raising inside the receive loop

**Step 3 — wire it into `AgentProvider`** (`providers/agent/provider.py`).
Four touch points, all next to their `CommandChannel` equivalents:

| Line (at `78e4df8`) | What is there | What to add |
|---|---|---|
| 94 | `self._channel = CommandChannel()` | `self._logs = LogsChannel()` |
| 164 | `"pending_commands": len(self._channel)` | a `pending_logs` metric |
| 186 | channel re-created on `bind` | same for `_logs` |
| 199 | `self._channel.abandon(reason)` | same for `_logs` |
| ~246 | `case "command_result":` | `case "logs_response":` → `self._logs.resolve(...)` |

Then an `async def logs(self, container_id: str, tail: int) -> …` that refuses
early — with a reason, not an empty list — when the session is absent or does
not advertise `logs`. `GET /commands/actions` already sets the precedent that
an empty answer carries a reason.

**Step 4 — the route.** `GET /graph/node/logs` in `api/routes/graph.py`
(`router` is already `prefix="/graph"`). It needs the provider set, so use
`Collectors` from `api/deps.py` (`Annotated[Collector, Depends(get_collector)]`)
alongside the existing `Store`.

The URN is engine-scoped — `bystack:container:<engine_id>/<container_id>` —
so split on the first `/` after the kind to get the partition and the Docker
id. `core/identity.URN` and `URNError` are already imported in that file;
reuse them and return 400 on `URNError`, 404 for a URN that is not in the
graph or names a host with no connected agent.

**Step 5 — tests.** `tests/test_agent_api.py` and `tests/test_agent_ingest.py`
show the synthetic-agent pattern; drive `LogsResponse` frames through it. The
cases that matter are the ones the channel exists for: a response for an
unknown `request_id`, a disconnect with a request in flight, a timeout, and an
agent that does not advertise `logs`. **No test may require a Docker daemon or
a network** — that rule is absolute in this repo.

**Step 6 — a conformance check.** `bystack.conformance` is where a claim
about *an* agent in any language belongs. `conformance/engine.py` is the
scripted engine; it will need a `/containers/{id}/logs` handler that returns a
framed body. One check that a request comes back with both streams tagged.

**Step 7 — the UI.** `features/activity/` is the closest sibling and is a
timeline, not a log viewer; logs belong in the node inspector
(`features/topology/ui/NodeInspector.tsx`), reached from the container that is
failing. Follow `useFleet.ts` for the fetch-and-error shape.

### Estimated shape

Steps 1–5 are one focused session. Step 6 is small. Step 7 is its own.

---

## 3. Crash-loop depth (`RestartCount`)

A crash loop currently renders as `Unstable` with `exit_code` (as of
`d5f17b0`, `Restarting (137)` is parsed as well as `Exited (137)`), and
`capability.py` correctly offers `stop`/`kill` to break it. What is missing is
*how many times*.

**The obstacle, verified against Docker API 1.55:** `RestartCount` is not in
`GET /containers/json` at all. It only exists on `GET /containers/{id}/json`,
so carrying it costs an inspect per container on every List — a change to the
informer's shape, not the addition of a field.

**The cheap way, if it is wanted:** inspect *only* containers whose listed
state is `restarting`. That set is bounded and usually empty, so the steady
state costs nothing, and it is exactly the set for which the number is
interesting. Hash it like `health` (see §4 of this file's sibling reasoning in
`agent/src/hashset.rs`) — but note it advances on a clock while a container is
looping, so hashing it means resending that container each restart. That is
probably correct here and is a real decision to make explicitly.

Do not compute it Controller-side by counting observed transitions: the graph
is ephemeral by ADR-0001, so the count would reset on Controller restart and
read as zero for a host that has been looping for a day.

---

## 4. Durable audit and RBAC — the gate for destructive operations

`CommandKind` has no `remove`, `prune`, volume deletion or image cleanup, and
its docstring says exactly why: destructive operations need a durable audit
trail and RBAC to answer "who deleted the database volume", and
`infra/audit/memory.py` is an in-memory ring of 2000 entries that a restart
erases. Its own module docstring calls itself "the honest v0".

**These three arrive together or not at all.** Adding destructive verbs before
the audit is durable is the one sequencing mistake this codebase has been
careful to avoid; ADR-0012 is the record. Anyone tempted should read
`core/ports/command.py` first.

The enrollment registry (`infra/agentca/registry.py`, JSON at
`agents.state_dir`) is the only durable store in the system today and is the
obvious model for how a durable audit would be introduced — including its
retention question, which ADR-0006 clause 4 says every table must answer.

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
- **Frontend bundle is 1.72 MB (530 KB gzipped)**, over vite's warning
  threshold. Untouched; a code-splitting question for step 7.
- **`GET /agents` merges a durable enrollment row with the live provider set**,
  so a host that once enrolled over mTLS and now runs as the Controller's local
  agent shows `status: approved` with `local: true`. That is correct and was
  verified; a clean machine shows `status: local` with no registry row at all.

---

## 7. Conventions that will bite you

- **The `.proto` is the contract.** Never renumber, never reuse a tag; retire
  with `reserved`. Mixed-version fleets are a normal operating state under
  ADR-0008, not a migration window.
- **Python bindings are generated and checked in.** After editing the schema
  run `backend/scripts/generate_proto.py`, which writes both the Python
  bindings and `agent/proto/agent.bin`. `test_wire.py` fails if the committed
  copy drifts.
- **Adding a field the mapper reads means editing three files** — the
  `.proto`, the agent, and `ingest.py`'s translation back into Docker's
  vocabulary. `test_wire.py::test_every_docker_field_the_mapper_reads_is_
  carried_on_the_wire` is what stops you forgetting the third.
- **No test anywhere may require a Docker daemon or a network.**
- **A check that cannot fail is decoration.** Both checks added in this round
  were validated by reverting the fix and watching them fail. Do the same.
- **`PartitionWriter` never gets a `source` argument.** It is what stops a
  compromised agent writing to another host's partition.
