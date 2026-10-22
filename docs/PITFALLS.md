# Things that will bite you

Each of these cost real debugging time, and each is defended by a test. Read the
relevant one before changing the agent, the wire protocol, the store's reconcile
path, or the log stream — most of them look like a harmless simplification until
they are not.

Each entry says what goes wrong and what the code does about it. The list is in
the order it was learned, not sorted by subject.

- **Docker's `Status` field is a rendered string** (`"Up 3 hours"`). Putting it
  in a node's content hash re-emits every container on every reconcile and
  turns incremental sync into a full refresh on a timer. **This trap now
  exists twice** — once in the Controller's node hash, once in the Agent's
  change detection, where it would put every payload on the network every
  resync, per host, forever. Both exclusions are pinned by tests, because the
  symptom is not an error: it is a system that works perfectly and costs 100×
  more than it should.
- **A delta must carry the full id set, not just the changes.** The store's
  `reconcile` deletes whatever is absent, so a changes-only frame would delete
  every stable container on the host. Ids are cheap, payloads are not — see
  [ADR-0009](adr/0009-agent-wire-protocol.md).
- **Edges are owned by one endpoint, not both.** A kind-scoped reconcile that
  claims every edge it merely touches will delete relationships it does not
  declare, forever fighting the slice that does declare them.
- **Record the watch's `since` before the initial List**, never after, or
  changes landing during the List are lost with no detectable gap.
- **`asyncio.TaskGroup` wraps errors in an `ExceptionGroup`**, so
  `except SomeError` in a supervisor silently never matches.
- **Engine IDs come in two formats** — a colon-delimited fingerprint before
  Docker 25.0, a UUID after. Both appear in a mixed fleet.
- **Docker answers a redundant start/stop with `304`, not `204`.** It is
  telling you precisely that nothing happened. Folding it into success reports
  a restart that changed nothing as a restart that worked.
- **`request.timeout or DEFAULT` is wrong for a grace period.** `timeout=0` is
  `docker stop -t 0` — "do not wait" — and truthiness turns the one operator
  who explicitly asked for no grace into the one who silently gets ten
  seconds. Read it with `is None`.
- **A command must not update the graph, however tempting.** The one-second
  window where the UI still shows the old state is the price of never
  displaying something the infrastructure did not report. See
  [ADR-0012](adr/0012-operations-and-audit.md) §2.
- **Waiting on a `noop` target for confirmation hangs the UI forever.** Its
  content revision will never change, because nothing changed. Only witness
  targets that actually transitioned.
- **An authoritative delta needs a payload cache on the Controller.** The
  frame carries every id but only the changed payloads, and `reconcile` needs
  a *node* for each id. Reconciling with just the frame's payloads deletes
  every stable container on the host — the exact bug the protocol was designed
  to make impossible, reintroduced in the code that implements it.
- **Clear that cache on disconnect.** Keep it, and a reconnecting agent's
  first frame is applied against a picture from before the gap, resurrecting
  containers that were removed while it was away. Silent, and only visible as
  a container that will not go away.
- **The engine id has two spellings.** `engine_scope()` strips the colons for
  URNs, so a partition keyed by the raw id gives one host two identities:
  `node.source` says one thing and every URN says another, and
  `/graph?sources=` matches neither. Normalize at the registry, once.
- **`repeated` proto fields cannot distinguish empty from unset.** Passing
  `None` to mean "all slices" and `[]` to mean "none" encodes identically.
  Pick the reading that makes an empty list harmless.
- **A removal changes no payload.** Every surviving container hashes exactly as
  before, so an agent that decides to stay silent on "no payload moved" never
  reports a deletion — and since deletion is carried *only* by absence from the
  id set, it is then never reported at all. Membership has to be compared
  separately. Our own agent had this bug; `bystack.conformance` found it.
- **Hash Docker's labels from an ordered map.** A `HashMap` iterates
  differently per process, so hashing one makes every container look changed on
  every List. Same failure as hashing `Status`, by a different route.
- **One URN is legitimately shared by two partitions.** Image identity is the
  content digest and is deliberately *not* engine-scoped — that coincidence is
  the free cross-host correlation. It is therefore the one place a partition
  can reach outside itself, and a reconcile that deletes the node on the first
  claimant's say-so removes an image other hosts are still running and takes
  their `uses_image` edges with it. The trigger is not exotic: any host
  stopping the last container using a shared image re-projects its image slice
  and blanks that image across the entire fleet. Silent, no error anywhere, and
  it repairs itself only at the next resync — and it is a *cross-partition
  write* performed by the mechanism whose whole job is to make those
  impossible. `bystack.conformance.fleet` found it; `test_store.py` pins it.
- **`#[serde(default)]` does not cover `null`.** It covers a field that is
  *absent*; a field that is present and `null` is still a parse error, and one
  of those anywhere in a List rejects the whole List — a host that never syncs
  at all, not a field that goes missing. Docker sends `null` rather than `[]`
  or `{}` in a dozen ordinary places: `Aliases` on an endpoint, `IPAM.Config`
  on a network, `Labels` on a volume, `RepoTags` on an untagged image. Our
  fixtures all said `{}`, so fourteen and twenty-seven checks passed against an
  agent that could not read a real Docker socket at all. `bystack.conformance.
  parity` found it on its first real-daemon run; the fixtures now say `null`,
  which reproduces it with no daemon at all.
- **A content hash must be a function of content, not of container type.**
  `EMPTY` is a `MappingProxyType` and `json` cannot serialize one, so it fell
  to the encoder's fallback rather than its `sort_keys` path and was hashed as
  its *repr*. Two nodes that compare equal then get different revisions, and a
  mapping's hash depends on insertion order — which would re-upsert every node
  on every reconcile, the same failure as hashing `Status`, by a third route.
- **`RestartCount` is not on `GET /containers/json`.** It exists only on the
  inspect endpoint, at every API version, so carrying it is not the addition
  of a field — it is one request per container on every List, in place of one
  per slice. The agent inspects only containers already listed as
  `restarting`: bounded, usually empty, and exactly the set the number means
  anything for. `bystack.conformance` asserts *which* ids were inspected, not
  how many, because an agent that inspects everything answers correctly and
  costs a hundred times more.
- **"Usually empty" is a claim about the median host, and the median host is
  not the one being watched.** The set above is bounded by how bad the deploy
  was, so it is also capped at 32 and issued as one batch of overlapping round
  trips rather than a serial loop. Fifty crash-looping containers used to mean
  fifty sequential inspects *ahead of every frame*, delaying the containers
  that were fine along with the ones that were not — the informer's whole cost
  model inverted, in exactly the situation an operator is staring at it.
  Conformance slows the scripted inspect to 50 ms so the two shapes are
  distinguishable at all: 0.36 s batched against 2.41 s serial.
- **Hashing a counter is only wrong when the counter is a clock.** `Status`
  advances on wall time and must not be hashed; `RestartCount` advances on an
  actual restart and must be, or a crash loop is reported once and never
  again — `restarting` is the same state on the second failure and the four
  hundredth. The cost is a resend per restart of a looping container, which is
  a host that has a problem worth a frame. `FailingStreak` sits on the other
  side of the same line and is deliberately not hashed.
- **Logs are not a command.** `CommandKind` is the closed set of *mutations* a
  read-only Controller refuses. Routing a log read through it would refuse to
  show an operator why a container is failing on the grounds that the platform
  is in its safe mode — exactly backwards. Its own frame, answered regardless
  of `read_only` on both sides.
- **A live tail needs its backpressure answered in three places, not one.**
  A container in a hot loop outruns a browser, and whichever component absorbs
  that is the one that breaks. So: the agent batches per HTTP chunk and blocks
  on a full queue, which stops it reading the daemon; the Controller keeps a
  bounded queue per subscription and drops the *oldest* lines; the browser
  holds a fixed buffer. Dropping oldest is the right loss for a live tail —
  somebody watching output scroll wants the newest lines — and every drop is
  counted and shown, because a gap the UI does not mark is a log an operator
  reads as continuous.
- **A subscription nobody cancels is a `docker logs --follow` running forever
  on someone else's machine.** So the Controller's side is a context manager,
  cancelling on any exit including the browser simply going away, and
  conformance asserts the agent actually drops its connection to the daemon —
  not merely that a cancel frame was sent.
- **Ask an agent's `Hello` what it can do; never its version.** A capability
  absent from the set is the same answer for "too old to know the frame" and
  "compiled out of this build", and the caller's decision is identical.
  Sending the frame anyway is worse than refusing: an older agent ignores it
  silently and the request times out with no diagnosis at all.
