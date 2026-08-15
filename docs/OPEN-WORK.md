# Open work

Written for whoever picks this up next, and written to be *executed* rather
than admired. [MIGRATION](MIGRATION.md) is the record of the agent pivot and
is essentially closed; this is what is left after it, with the decisions that
have already been made attached to each item so they are not re-litigated.

**One thing is actually open: §5, packaging.** §2, §3, §3b and §8 are closed
work kept for their reasoning, §4 is a decision *not* to build something, and
§6–§7 are standing notes. If you are looking for the next task, it is §5.

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
.venv/bin/python -m pytest -q                                     # 398 passed
.venv/bin/python -m bystack.conformance       ../agent/target/release/bystack-agent   # 32/32
.venv/bin/python -m bystack.conformance.fleet ../agent/target/release/bystack-agent   # 35/35
.venv/bin/python -m bystack.conformance.local ../agent/target/release/bystack-agent   # 17/17

cd ../agent    && cargo clippy --release --all-targets -- -D warnings && cargo test --release  # 49 passed
cd ../frontend && npx tsc --noEmit && npm test -- --run                                        # 243 passed
```

Agent binary: **1.96 MiB** against a 12 MiB budget, RSS **~5.00 MiB** against
20. Both are measured by `bystack.conformance`, not asserted from memory. The
movement from 1.82/3.95 is the hand-rolled D-Bus client and the host slices
(ADR-0016) — recorded rather than smoothed, because the honest version of
"under budget" is the one that shows what moved it.

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
- ~~**One shot, not a stream.**~~ **Both, now.** The one-shot read survives as
  the API and the fallback; opening the panel uses the *live* path
  (`LogsSubscribe`/`LogsChunk`/`LogsCancel`, SSE at
  `GET /graph/node/logs/stream`). `tail` is still clamped by the daemon, the
  agent (`MAX_LOG_LINES = 2000`) and the route's schema — the first two kept
  in step by a guard, see §3.4 — and is now the *backfill* before the tail
  begins. The UI does not send it at all.

  The backpressure question the one-shot design deferred got answered rather
  than avoided, and in three places because it has to be: the agent batches per
  HTTP chunk and blocks on a full queue (which stops it reading the daemon),
  the Controller keeps a bounded queue per subscription and **drops oldest**,
  and the browser holds `LIVE_BUFFER` lines. Every drop is counted and shown —
  a gap the UI does not mark is a log an operator reads as continuous.
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
  many, because an agent that inspects everything answers correctly. The set
  is also **capped at 32 and issued concurrently** — see §3.2, which is where
  "bounded, usually empty" stopped being a claim and became a property.
- **It is hashed, deliberately.** `restarting` is the same state on the second
  failure and the four-hundredth, so a count outside the hash is a loop
  reported once and never again. It advances on an *event*, unlike `Status`
  which advances on a clock — that is the whole line, and `FailingStreak` sits
  on the other side of it.
- Surfaced as an ordinary attribute row, like `exit_code`. If you want it on
  the card itself, that is a `theme.ts`/`render.ts` change and a new decision.

### Durable audit — closed. **RBAC is not coming** — see §4

`infra/audit/durable.py`, on by default, JSON Lines in `agents.state_dir`,
bounded by count and compacted write-and-rename. ADR-0012 §4a is the record,
and §4b is the write path (fsynced, off the event loop — see §3.1).

The in-memory ring survives as the fallback for a Controller with nowhere to
write, because "cannot persist" and "will not start" are different answers and
only one of them is acceptable for a control plane.

`actor` reads `anonymous` and always will. That is ADR-0014 rather than an
unfinished edge, and it is why nothing in `CommandKind` destroys anything.

---

## 3. The gaps in those three features — all closed

They were six, ranked, and they are done. Kept here rather than deleted for
the same reason §2 is: the next person will be tempted by the options that
were rejected, and two of these were defects that were **green by
construction** — which is the part worth remembering.

Every fix was validated the way §7 requires: revert it, watch the new check
fail, put it back. The numbers below are from those runs.

### 3.1 The audit log fsynced on the event loop — fixed

`AuditLog.record` and `finalize` are now `async`, and `DurableAuditLog` awaits
`asyncio.to_thread` around the file work. ADR-0012 **§4b** is the record.

- **Async on the port, not just the implementation.** The in-memory ring has
  nothing to await and is a coroutine anyway: a `record` whose sync-ness
  depended on which log the composition root chose would put that choice into
  the shape of every caller.
- **Still fsynced per record.** Durability at the moment of return is the
  property; only the thread changed. A write-behind queue was rejected — it
  needs a flush on shutdown, an ordering guarantee and a crash-mid-queue test,
  and buys latency nobody was short of.
- **One lock**, so concurrent writers land in order, and **compaction takes
  its snapshot on the loop** — a worker must not iterate a mapping `record`
  can still add to. Both have their own check; both were mutation-tested.
- The check that matters: `test_a_slow_disk_does_not_stop_the_event_loop`
  monkeypatches `os.fsync` to sleep and asserts the longest gap between two
  turns of the loop. Reverted, it reports a **253 ms** stall. This is the
  shape to copy for anything else `tmp_path` hides — tmpfs fsync is 0.01 ms,
  so the suite could not otherwise see this at all.

### 3.2 The crash-loop inspect was sequential and uncapped — fixed

`agent/src/informer.rs`: at most `MAX_CRASH_LOOP_INSPECTS` (32), issued
through `join_all` so the round trips overlap.

- **`join_all`, not a `JoinSet`.** OPEN-WORK previously suggested the latter on
  the grounds that `futures` was not a dependency — but `futures-util` already
  was, for the WebSocket sink. `join_all` needs no spawn, no `'static` bound
  and no `Clone` on `Engine`, none of which a deliberately current-thread
  runtime should be made to grow. The `alloc` feature is now named explicitly
  in `Cargo.toml` rather than arriving through tokio-tungstenite's unification.
- **The cap is on the work, not the truth.** Containers past it keep
  `restart_count` at zero, which is what the field already means for anything
  not looping.
- Conformance grew two checks. Asserting the cap needed a fixture with more
  crash loops than the cap; asserting the *overlap* needed
  `ScriptedEngine.inspect_delay`, because a scripted inspect answers instantly
  and both shapes look identical at that speed. At 50 ms: **0.36 s batched,
  2.41 s serial**, with the old code inspecting all 41.

### 3.3 `.fleet` and `.local` now cover logs

- **`.fleet`** has `scenario_logs_routing`: each host's containers get
  distinguishable lines, a read through beta's provider must return beta's
  text and touch **only** beta's daemon, and asking *alpha's* provider for
  beta's container id must be refused. That last one is the partition rule
  drawn from the read side — `PartitionWriter` stops an agent writing another
  host's entities; nothing had stopped one being used as a window onto them.
- **`.local`** now reads a log over the unix socket and checks the tail is
  bounded there too. It is the only suite that drives the real supervisor, and
  the zero-config path is the one a first-run user is on.
- A misrouted log read is worse than a misrouted command: the command fails
  loudly on a daemon that does not have the container, while the read hands
  the operator another machine's output under the container they clicked.

### 3.4 The mirrored constants have a guard — fixed

`test_wire.py::test_the_constants_mirrored_across_languages_still_agree` reads
the literal out of the Rust source, exactly as `_keys_read_by_mapper` does. It
covers `MAX_LOG_LINES` / `MAX_LOG_TAIL` and, now, `MAX_CRASH_LOOP_INSPECTS`
against the copy `conformance/runner.py` asserts with. **Add the next
cross-language number to that test, not to a comment.**

The frontend's copy was deleted rather than guarded: `useLogs.ts` omits `tail`
and takes the Controller's default, which is the side that owns the bound and
already publishes it in the OpenAPI schema. The panel's footer counts the
lines that arrived, so it was never repeating the constant anyway.

### 3.5 `finalize` is O(1) — fixed

Both audit logs are now one insertion-ordered `dict` keyed by operation id,
which is a ring, an index and an eviction policy at once: re-assigning a key
keeps its position, so `finalize` is an assignment, and the oldest entry is
`next(iter(...))`. `recent` is `islice` over `reversed(...)`, so answering
costs the limit rather than the whole retention. The deque-plus-side-index it
replaces had to be kept in step by hand.

### 3.6 The channels share their parked future — fixed

`ParkedRequests[T]` in `providers/agent/commands.py` holds the correlation map
and the three invariants that leak if broken; `CommandChannel` and
`LogsChannel` keep their own envelope construction and outcome mapping,
because what a failure *means* is where they genuinely differ.

Not a base class with a type parameter — that would have turned the real
difference into a pair of overridden hooks. The check that this was worth
doing: deleting the `finally` that drops the future now fails **three** tests
across **both** channels, which is precisely the coupling that was missing.

---

## 3b. The gaps in live log streaming — all closed

§3 audited the three features that landed together. Live streaming landed
*after* that audit, in the same commit that wrote it up, so it went in with
nobody having done to it what §3 did to the others. Six things, and the
pattern §3 warns about repeated exactly: **two of the six were checks that
could not fail**, and one of those was hiding a fixture that could not observe
the thing it was fixturing.

### 3b.1 The cancellation check was decoration — fixed

`scenario_live_logs` asserted "closing the panel stops the follow on the host"
*after* calling `end_log_stream`, so the daemon had already released the
follow because the log had ended. Proven rather than argued: with the agent's
entire `LogsCancel` handler deleted, the suite passed **32/32**. The check is
now taken on the way out of a tail whose log is still open, and nothing but
the cancel can end that one — same mutation, and it fails.

The ending moved to a second tail, because the two checks want opposite states
of the same log: one must still be open when the reader leaves, the other must
close underneath a reader who stays.

### 3b.2 The scripted engine could not see a reader leave — fixed

Underneath the above. `_follow_logs` claimed it "runs until the reader goes
away", but it only ever noticed on the *next write* — so on a quiet container
it followed forever. That is why §3b.1 could not have been written without
this: the cancellation it wanted to observe was invisible unless something
happened to be writing. It now watches for EOF on the request side explicitly.

A follow left open by a check also hung the whole run at teardown, because
`Server.wait_closed()` waits for handler tasks. That is how this was found.

### 3b.3 A disconnect racing the subscribe was a 500 — fixed

`AgentProvider.follow` checked the session was live, then `await`ed a send that
can raise `AgentDisconnected` — which is not `LogsUnavailable`, so it escaped
`__aenter__` and the route's `except` missed it. The window is small and it is
the *likeliest* moment for it to close: the host an operator opens a log panel
on is often the host that just went away. Translated at the site; the route
answers 409 like every other refusal.

### 3b.4 An idle stream had no heartbeat — fixed

A quiet container is the normal case, and the stream sent no bytes at all
between lines. The reverse proxy this listener was deliberately kept able to
sit behind closes that on its own read timeout (nginx: 60s), after which
`EventSource` reconnects, re-subscribes and replays the backfill — the same
screenful reappearing every minute, and a new `docker logs --follow` on the
managed host each time. `STREAM_KEEPALIVE` is 15s and the comment is `: keepalive`.

The check is bounded on purpose: without the heartbeat `anext` never returns,
and an unbounded wait would hang CI rather than fail it.

### 3b.5 The streaming path had no Python tests at all — fixed

Not one file in `tests/` mentioned `LogsStream`, `LogsSubscriptions`,
`logs_chunk` or the SSE route. The bounded queue, its drop policy, the
subscription registry, `abandon`, and the event encoder were covered by four
conformance checks and nothing else. `test_agent_logs.py` now has 25 more,
including the one that matters most for the encoder: a log line containing
`\n\nevent: end\n\n` must not become a frame of its own.

Writing them turned up dead code — `offer` had a forcing loop for the terminal
event that discarded the same single event the ordinary bound below it
discarded, and could never fire twice. Removed; the guarantee lives in one
place now, and deleting *that* fails three checks.

### 3b.6 `.fleet` and `.local` did not cover streaming — fixed

Exactly the §3.3 gap, one feature later. `.fleet` has
`scenario_live_logs_routing`: a tail is answered by the host holding the
container, no other host's daemon is following, lines written while watching
arrive from the right host, the tail is released on cancel, and **alpha's
agent cannot follow beta's container**. That last one is the partition rule
drawn for a subscription — and a misrouted *tail* is worse than a misrouted
read, because it attaches another machine's output to a panel that is already
open and already trusted, line by line, while the operator watches.

`.local` follows a log over the unix socket and checks the cancel releases it
there too, on the machine the Controller is already sharing.

---

## 4. Authentication — **decided against**. See ADR-0014

This was the largest open item that is not packaging, and it was a design
question before it was a coding one. The design question has been answered:
**there is no user identity model, and there will not be one at this scale.**
[ADR-0014](adr/0014-no-user-identity.md) is the record; this is the summary.

The question underneath the three candidate shapes (local accounts, OIDC, a
trusted proxy header) was never "which is best" — it was *who is this for*.
The answer is one operator, one Controller, on a LAN they own, managing their
own machines. There is no second user to distinguish from the first, so a
login screen answers "the one person with the password did it", which is what
the absence of a login screen already says. Every candidate was priced against
that and none earned its cost.

**Two consequences, and they are the decision rather than side-effects:**

1. **`actor` is `"anonymous"` permanently.** Not a placeholder. It records
   that this platform does not know who asked and has decided not to find out,
   which is true and therefore a better audit record than an invented name.
   The client still cannot set it — an attacker-chosen name beside a real
   operation looks like evidence, and that test stays.
2. **`CommandKind` stays reversible-lifecycle-only permanently.** ADR-0012
   made destructive verbs conditional on answering *who*; that condition is
   now never met, so the conclusion is settled rather than pending. **Do not
   add `remove`, `prune` or volume deletion.** Doing so requires superseding
   ADR-0014 first, and the whole of §4's old warning still applies: it is the
   one sequencing mistake this codebase has avoided and cannot be quietly
   walked back.

The second is what makes the first safe, and the pairing is the load-bearing
part. No auth **and** no destructive verb means the worst case is "someone who
could reach the port restarted a container" — recoverable, audited, visible in
the topology within a second. No auth **with** `prune` means unrecoverable
data loss with no attribution. Only one of those is acceptable, so the two
decisions travel together or not at all.

**The boundary that does exist**, and it is not nothing: `api.host` is
`127.0.0.1` by default, the agent listener is mTLS with an internal CA and
per-host approval (ADR-0011), and the local agent socket is 0600 in a 0700
directory. `read_only` is **not** part of that boundary any more — ADR-0014
flipped it to `false`, on the grounds that the verb set is what bounds the
damage and a control plane that refuses every action on first run is a
support burden rather than a secure default. The one
thing an operator must understand is that setting `api.host` to `0.0.0.0`
puts the whole control surface on the LAN with no second gate — which is the
deployment ADR-0014 assumes and the reason it is not the default.

**What would reopen this:** a second operator whose actions must be told
apart, the Controller reachable from an untrusted network, or a destructive
operation becoming genuinely necessary. Those change the deployment, which is
what ADR-0014 is scoped to — a feature request does not.

---

## 5. Step 7 — packaging

~~The last unchecked step in [MIGRATION §6](MIGRATION.md).~~ **Landed**, except
for one clause. What exists now: `scripts/build-agent.sh` (static musl, both
architectures, checksummed), `scripts/install-agent.sh`, `packaging/`
(Dockerfile, compose, two systemd units, the image's config),
`backend/hatch_build.py`, `bystack-ctl`, and two workflows.

**The thing that blocked the product is closed.** "Add a host" composed
`bystack-agent --controller … --token …` — a command that assumed the binary
was already on the target machine, so the button handed you something you
could not run on a new server. It now composes a `curl … | sudo sh` over the
installer, pinned to the Controller's own version, with the direct form kept
alongside for a host that already has the agent.

### What is left: Controller-driven upgrade

**Decided and recorded in [ADR-0015](adr/0015-agent-upgrade.md).** The
reporting half is built: `/healthz` carries the Controller's version, and both
`bystack-ctl hosts` and the Hosts panel mark the agents that differ. Applying
an upgrade is `install-agent.sh` on that host, which is idempotent and never
touches the certificate.

Automating it is deliberately not done, and the obvious design does not work:

**The agent has no root store.** `agent/Cargo.toml` takes rustls's
underscore-prefixed `__rustls-tls` feature precisely to avoid the public CA
sets the two ordinary spellings bring, because the agent trusts exactly one
CA — the Controller's — and a public CA that mis-issues for the Controller's
hostname must not be a way in. So an agent cannot fetch a release from GitHub
without acquiring the trust surface it was built to refuse, plus the binary
size that comes with it.

The shape that fits ADR-0008 is the Controller pushing the binary down the
stream that is already open and already mutually authenticated. That needs:

- a wire message and a capability to gate it on, so an older agent refuses
  with a diagnosis rather than timing out (the rule in §7 below);
- a Controller that holds binaries for architectures other than its own —
  the wheel carries exactly one, on purpose;
- an agent that can replace its own executable and exit for the service
  manager to restart it, which is where `install`-not-`cp` matters: replacing
  the inode rather than writing through it is what stops a running process
  reading a half-written file.

And a cost that is not the transport: `ProtectSystem=strict` in the agent's
unit makes `/usr/local/bin` read-only to the service, so self-replacement needs
`ReadWritePaths=` there — write access to a directory of executables, granted
to a network-facing daemon. **Superseding ADR-0015 is the way to do that**, not
adding a flag.

Nothing in the current tree pre-empts any of it: no wire message, no tag spent,
no capability invented, and the `.proto` is unchanged.

### Two notes that were live during this work, and still are

`serve()` owns SIGINT and SIGTERM itself (`main.py`, commit `4143b5f`)
precisely so `systemctl stop` finishes cleanly, and
`bystack.conformance.local`'s `stopped_by_signal` is what holds it. Do not let
a refactor hand the signals back to uvicorn: `Server.serve()` re-raises the
signal it caught, which terminates the process before any cleanup its caller
arranged. That check now also measures *how long* the stop took — the agent
ignored SIGTERM entirely and every shutdown was a kill five seconds late,
which a ten-second budget passed happily.

`agents.state_dir` holds `operations.jsonl` and is created 0700. The image
declares it a volume; a container that keeps its audit trail only until it is
replaced is worse than one with none, because it looks like a record.

~~The frontend bundle is 1.72 MB (530 KB gzipped), over vite's warning
threshold.~~ **Split.** elkjs is fetched on first layout, so the initial
payload is 293 kB / 94 kB gzipped. The warning threshold moved to just above
elkjs, because a warning that fires on every build is one nobody reads.

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
- **A number written down in two languages gets a guard, not a comment.** Add
  it to `test_wire.py::test_the_constants_mirrored_across_languages_still_agree`,
  which reads the literal out of the Rust source. Two are guarded today; the
  third is one line.
- **`AuditLog.record` and `finalize` are coroutines.** Not an accident of the
  durable implementation — see ADR-0012 §4b. Anything doing I/O on the event
  loop inside `CommandService.execute` stops every agent pump and every
  browser stream, and `tmp_path` is tmpfs so the suite will not tell you.
- **No test anywhere may require a Docker daemon or a network.**
- **No test may write outside `tmp_path` either.** `conftest.py`'s autouse
  `_state_dir_is_disposable` redirects the default state directory; without it
  every test that builds an app from a bare `Settings()` mints a CA and
  appends to an audit log in the home directory of whoever ran the suite.
- **A check that cannot fail is decoration.** Every check added in this round
  was validated by reverting the fix and watching it fail. One of them passed
  under mutation on the first attempt and had to be rewritten to open the race
  it claimed to cover, which is the whole reason the rule exists. Sometimes
  seeing the failure means slowing something down on purpose — an fsync, an
  inspect, a serialization — because the real thing is too fast for the defect
  to show. Do the same, and delete `__pycache__` between mutation runs, or you
  will spend twenty minutes
  debugging bytecode.
- **`PartitionWriter` never gets a `source` argument.** It is what stops a
  compromised agent writing to another host's partition.


---

## 8. Watched units and processes — closed, and where the edges are

`ADR-0016`. A per-host watch list on the Controller
(`core/ports/watch.py`, `infra/watch/`), two slices on the wire
(`SLICE_UNIT`, `SLICE_PROCESS`), a D-Bus client and a `/proc` reader in the
agent (`agent/src/dbus.rs`, `systemd.rs`, `procfs.rs`, `host.rs`), a mapper on
the Controller (`providers/host/`), five routes (`api/routes/watch.py`), a
scoped command (`api/routes/commands.py`) and a panel
(`frontend/src/features/watch/`).

Decisions that are settled — **do not reopen without the ADR**:

- **Membership is the operator's selection.** Nothing enumerates a machine on
  a timer. The picker (`GET /agents/{id}/inventory`) is the only thing that
  walks a whole host and it runs when somebody opens the dialog — the same
  rule `useLogs.ts` follows, for the same reason.
- **A pid is not an identity.** The node is the *rule*; pids are attributes.
  A command names a watch id and the agent resolves it to pids in the same
  worker that signals them, with no `await` in between. Do not "optimise" that
  by caching the pids from the last scan: the kernel recycles them.
- **A watched thing that is not there is still a node** (`not-found`,
  `absent`). Dropping the entity is what makes a watch indistinguishable from
  having forgotten to add one.
- **No new `CommandKind`, and no `enable`/`disable`/`mask`.** Every verb maps
  onto the existing six. Unit-file changes are not lifecycle — they change what
  the machine does after the next reboot — and adding one means reopening
  ADR-0014 first, exactly as `prune` does.
- **`active_enter_timestamp` is hashed**, and it looks like the `status_text`
  trap and is its opposite. A `systemctl restart` by a person moves nothing
  else: `NRestarts` counts only policy restarts and `ActiveState` is `active`
  before and after. Verified against a live machine — the graph saw a manual
  restart in under two seconds, and would have seen nothing without it.
- **The polkit rule is broad and the bound is in the agent.** `host.rs` refuses
  a lifecycle command for a unit that is not on this host's list. That is what
  makes `manage-units` grantable at all; see the ADR and the comment in
  `packaging/polkit/49-bystack-agent.rules`.
- **A fan-out is N adds and a label, never a fleet rule.**
  `POST /agents/watch` takes one draft and a list of hosts, mints one
  `group_id`, and stores an ordinary independent entry on each. Nothing is
  stored about the group itself: removing one member leaves the rest, and
  there is no operation that edits "the group". The id exists so a panel can
  say *also chosen on 3 other hosts* (`WatchEntryOut.group_hosts`, counted
  fleet-wide by the route because a host's own list cannot know it).

  The reason it stops there is the same reason membership is a selection at
  all. A group that owned its members is a second source of truth about what a
  host watches, and the reconciliation between it and the per-host list ends
  with a machine holding entries nobody chose for it. Bounded by `MAX_FANOUT`,
  which is the promise to the fleet that `MAX_ENTRIES` is to one machine.
  One host's refusal — a duplicate, a bad pattern — does not unwind the
  others; every host is reported in its own words, which is why the route
  answers `200` with a per-host list rather than `201`.
- **The scope is chosen at the button, and sent as a list.**
  `POST /commands/group` takes a `group_id` and the hosts to act on, resolves
  each to that host's node, and runs each through `CommandService` exactly as
  a click on that card would — same read-only choke point, same expansion,
  same audit. The route decides nothing, which is why it exists without
  reopening ADR-0014.

  Three UX modes (this host, these four, all nine) and **one mechanism**: the
  client sends the list, never the word. A server-side "all" would act on a
  host enrolled between the operator reading the screen and pressing the
  button. `ActionBar` defaults the scope to this host on every selection and
  resets it when the selection moves, because a scope that persisted is how
  somebody restarts nine machines meaning to restart one.

  **One audit entry per host, deliberately** — these are N operations on N
  machines, and a single record claiming "restarted the group" would hide
  which machine actually took it. Worst-wins across hosts, the same ordering
  `CommandResult.status` uses across targets within one, so eight successes
  and one sleeping machine reads as a failure. Bounded by `MAX_TARGETS`, not
  `MAX_FANOUT`: the first governs how many machines one click may restart, the
  second how much intent may be stored.

  `GET /agents/watch` (fleet-wide) exists for this and only this: a host's own
  list cannot say which *other* machines hold the selection, and the
  operations bar needs that before it can offer a scope.

Two things left deliberately undone, both cheap and neither obviously right:

- **The process poll is a fixed ten seconds.** It could be adaptive, or driven
  by the same coalescing window as everything else. Nothing has asked for it.
- **`runs_in` is drawn only to nodes already in the graph.** A process inside
  an unwatched unit carries the unit *name* as an attribute and no edge. The
  alternative — auto-watching the unit — would add entries an operator did not
  choose, which is the whole thing this feature refuses to do.
