//! List → Watch → Resync, against the local socket.
//!
//! The pattern is taken from Kubernetes client-go, because it is the proven
//! answer to "real-time without polling" and there is no reason to invent a
//! worse one. `docs/MIGRATION.md` asks that this docstring move with the code
//! rather than be re-derived, because the bugs it prevents are unchanged by
//! the language:
//!
//! ```text
//! t0  record `since`
//! t1  List      -> full snapshot   -> Sync frames        (authoritative)
//! t2  Watch     -> GET /events?since=t0                  (no gap: t0 < t1)
//! ..  Resync    -> re-List every N minutes                (repairs our hashes)
//! ```
//!
//! **Record `since` before the List, never after.** The event stream then
//! starts from a point that precedes our snapshot, so a change landing during
//! the List is replayed rather than lost. Ordering it the other way is the
//! classic silent-drift bug in every watch implementation, and it is silent
//! precisely because the window is small enough to survive testing.
//!
//! **Why an event triggers a re-List instead of carrying data.** An event
//! tells us *that* something changed and gives an id. We could inspect that
//! id — but then we would also have to reason about what became orphaned, what
//! went stale, and what happens when two events arrive out of order. Instead
//! an event marks its slice dirty and the whole slice is re-Listed. One local
//! call, always correct, no ordering assumptions.
//!
//! That sounds expensive and is not. Bursts are coalesced, so a `compose up`
//! of twenty services costs one List rather than twenty. And the *frame* is
//! still incremental: we hash what we Listed and send payloads only for what
//! moved, so a re-List that finds nothing new produces a frame of ids and
//! costs the Controller nothing to reconcile.
//!
//! **Resync got cheaper and smaller.** It ran every 5 minutes on the
//! Controller because the event stream crossed an SSH tunnel that could drop
//! silently. We read a unix socket on the same kernel; there is no network to
//! lose. Resync now defends only against our own hash map, and the re-List
//! never touches the network at all.

use std::collections::HashSet;
use std::time::Duration;

use futures_util::future::join_all;

use tokio::sync::mpsc;

use crate::docker::{Engine, EngineError};
use crate::hashset::{self, SliceState};
use crate::host::Host;
use crate::model::Container;
use crate::wire::{self, entity, Slice};

/// Burst coalescing window.
///
/// Long enough to collapse a `compose up`, short enough to stay imperceptible
/// in the UI. A hundred events in a fraction of a second become one List and
/// one frame.
pub const COALESCE_WINDOW: Duration = Duration::from_millis(250);

#[derive(Default)]
pub struct State {
    containers: SliceState,
    networks: SliceState,
    volumes: SliceState,
    images: SliceState,
    units: SliceState,
    processes: SliceState,
}

impl State {
    /// Forget every fingerprint, so the next pass re-sends everything.
    pub fn clear(&mut self) {
        self.containers.clear();
        self.networks.clear();
        self.volumes.clear();
        self.images.clear();
        self.units.clear();
        self.processes.clear();
    }

    /// How many entities this agent is currently tracking, across all slices.
    ///
    /// The agent's entire working set, and the number the resource budget is
    /// about: roughly 12 KB of fingerprints for a hundred containers. The two
    /// host slices add at most one entry per thing an operator selected, which
    /// is bounded by the Controller at 64 per host and is usually zero.
    pub fn tracked(&self) -> usize {
        self.containers.len()
            + self.networks.len()
            + self.volumes.len()
            + self.images.len()
            + self.units.len()
            + self.processes.len()
    }

    /// Forget one slice, so the next scan re-sends all of it.
    ///
    /// Used when the Controller asks for a resync: it asked because it
    /// believes we disagree, and answering "nothing changed" out of a hash map
    /// it has just told us to distrust would be useless.
    ///
    /// Also used when a new watch list arrives, for a different reason with the
    /// same shape: the Controller has just changed what it is asking about, and
    /// an entry it has never seen a payload for cannot be reconciled out of a
    /// membership set alone.
    pub fn forget(&mut self, slice: Slice) {
        self.of(slice).clear();
    }

    fn of(&mut self, slice: Slice) -> &mut SliceState {
        match slice {
            Slice::Container => &mut self.containers,
            Slice::Network => &mut self.networks,
            Slice::Volume => &mut self.volumes,
            Slice::Unit => &mut self.units,
            Slice::Process => &mut self.processes,
            _ => &mut self.images,
        }
    }
}

/// Every Docker slice, in the order a first Sync should send them.
///
/// Containers first because they are what the operator is waiting to see; the
/// Controller reconciles each slice independently, so a partially-arrived
/// picture is never inconsistent, only incomplete.
pub const ALL_SLICES: [Slice; 4] =
    [Slice::Container, Slice::Network, Slice::Volume, Slice::Image];

/// The two slices whose membership is the operator's watch list rather than
/// whatever a daemon happens to be running.
///
/// Kept as a separate list rather than appended to [`ALL_SLICES`] because they
/// are driven by something else entirely: `ALL_SLICES` is what a resync
/// re-Lists from the engine, and these come from `host.rs` and are empty on a
/// host nobody has selected anything on.
pub const HOST_SLICES: [Slice; 2] = [Slice::Unit, Slice::Process];

/// Fill in `restart_count` for the containers that are actually looping.
///
/// `RestartCount` is not in `GET /containers/json` at any API version, so
/// carrying it costs an inspect. Inspecting every container would turn one
/// request per List into one per container — the change the informer's whole
/// design avoids — so this asks only about containers whose listed state is
/// `restarting`. That set is bounded, is empty on a healthy host, and is
/// exactly the set for which the number means anything: a container that has
/// never restarted is correctly zero without being asked.
///
/// **Capped, and overlapped.** "Bounded" used to be a claim about the median
/// host rather than a property of the code: the inspects were serialised and
/// there was no ceiling, so fifty containers in a crash loop — a bad deploy,
/// an OOMing node — meant fifty sequential round trips ahead of *every* frame,
/// delaying the containers that are healthy along with the ones that are not.
/// That inverts the informer's cost model in precisely the situation an
/// operator is watching. Now at most [`MAX_CRASH_LOOP_INSPECTS`] are asked
/// about, and they are asked all at once.
///
/// `join_all` rather than a `JoinSet`: the runtime is deliberately
/// current-thread, so spawning buys no parallelism here — the win is having
/// the round trips in flight together, which a joined set of futures gives
/// without requiring `Engine` to be cloneable or the futures to be `'static`.
///
/// A failed inspect is swallowed rather than failing the scan, and silently:
/// the container may have been removed between the List and this call — the
/// same race the informer handles everywhere else — and losing the depth of a
/// crash loop is not a reason to lose the topology it belongs to, nor to
/// print a line per scan for as long as the loop lasts.
async fn deepen_crash_loops(engine: &Engine, listed: &mut [Container]) {
    let looping: Vec<&mut Container> = listed
        .iter_mut()
        .filter(|c| c.state == RESTARTING)
        .take(MAX_CRASH_LOOP_INSPECTS)
        .collect();
    if looping.is_empty() {
        return;
    }

    let counts = join_all(looping.iter().map(|c| engine.restart_count(&c.id))).await;
    for (container, count) in looping.into_iter().zip(counts) {
        if let Ok(count) = count {
            container.restart_count = count;
        }
    }
}

/// Docker's state for a container the engine is restarting under a policy.
/// The one state for which an inspect is worth a round trip.
const RESTARTING: &str = "restarting";

/// How many crash loops one scan will measure the depth of.
///
/// A ceiling on the work, not on the truth: containers past it keep
/// `restart_count` at zero, which is what the field already means for
/// everything that is not looping, and the graph stays honest either way. The
/// alternative — paying for every one of them — is the case this whole
/// function was written to avoid, and a host with more than thirty-two
/// containers crash-looping at once has a problem the exact depth of the
/// thirty-third does nothing to diagnose.
const MAX_CRASH_LOOP_INSPECTS: usize = 32;

/// List one slice and diff it, producing the frame to send.
///
/// `full` forces a `Sync` (every payload) rather than a `Delta` (membership
/// plus what moved). The first pass of every connection is full, because the
/// Controller has just cleared its view of us.
pub async fn scan(
    engine: &Engine,
    state: &mut State,
    slice: Slice,
    full: bool,
) -> Result<Option<wire::envelope::Payload>, EngineError> {
    // Listed first, hashed second, diffed third. Each list is a single local
    // call; the four of them together are what a resync costs.
    let (ids, fingerprints, entities) = match slice {
        Slice::Container => {
            let mut listed = engine.containers().await?;
            // Before hashing, deliberately: `restart_count` is part of the
            // content hash, so filling it in afterwards would diff this List
            // against fingerprints computed from a different field set and
            // resend every crash-looping container on every scan.
            deepen_crash_loops(engine, &mut listed).await;
            let fingerprints: Vec<u64> = listed.iter().map(hashset::hash_container).collect();
            let ids: Vec<String> = listed.iter().map(|c| c.id.clone()).collect();
            let entities: Vec<wire::Entity> = listed
                .iter()
                .map(|c| wire::entity(c.id.clone(), entity::Body::Container(c.into())))
                .collect();
            (ids, fingerprints, entities)
        }
        Slice::Network => {
            let listed = engine.networks().await?;
            let fingerprints: Vec<u64> = listed.iter().map(hashset::hash_network).collect();
            let ids: Vec<String> = listed.iter().map(|n| n.id.clone()).collect();
            let entities: Vec<wire::Entity> = listed
                .iter()
                .map(|n| wire::entity(n.id.clone(), entity::Body::Network(n.into())))
                .collect();
            (ids, fingerprints, entities)
        }
        Slice::Volume => {
            let listed = engine.volumes().await?;
            let fingerprints: Vec<u64> = listed.iter().map(hashset::hash_volume).collect();
            let ids: Vec<String> = listed.iter().map(|v| v.name.clone()).collect();
            let entities: Vec<wire::Entity> = listed
                .iter()
                .map(|v| wire::entity(v.name.clone(), entity::Body::Volume(v.into())))
                .collect();
            (ids, fingerprints, entities)
        }
        _ => {
            let listed = engine.images().await?;
            let fingerprints: Vec<u64> = listed.iter().map(hashset::hash_image).collect();
            let ids: Vec<String> = listed.iter().map(|i| i.id.clone()).collect();
            let entities: Vec<wire::Entity> = listed
                .iter()
                .map(|i| wire::entity(i.id.clone(), entity::Body::Image(i.into())))
                .collect();
            (ids, fingerprints, entities)
        }
    };

    let pairs: Vec<(&str, u64)> = ids
        .iter()
        .map(String::as_str)
        .zip(fingerprints.iter().copied())
        .collect();
    let change = state.of(slice).diff(pairs);

    if full {
        return Ok(Some(wire::envelope::Payload::Sync(wire::Sync {
            slice: slice as i32,
            entities,
        })));
    }

    // Nothing moved. The Controller's picture is already correct, and sending
    // a membership set to tell it so would be the polling loop this design
    // exists to avoid. An idle host puts zero bytes on the wire.
    if change.is_quiet() {
        return Ok(None);
    }

    // Complete membership, payloads only for what moved. This is the frame
    // that lets the Controller run a full authoritative reconcile at delta
    // cost -- see ADR-0009 §2.
    let mut changed = Vec::with_capacity(change.changed.len());
    let mut wanted: HashSet<usize> = change.changed.iter().copied().collect();
    for (index, item) in entities.into_iter().enumerate() {
        if wanted.remove(&index) {
            changed.push(item);
        }
    }

    Ok(Some(wire::envelope::Payload::Delta(wire::Delta {
        slice: slice as i32,
        ids: change.ids,
        changed,
    })))
}

/// Look up one host slice and diff it, producing the frame to send.
///
/// The sibling of [`scan`], and separate from it for the reason
/// [`HOST_SLICES`] is separate from [`ALL_SLICES`]: this one asks the operator's
/// watch list rather than a daemon's inventory, so there is no List, no
/// membership to discover, and nothing at all to do on a host where the list is
/// empty.
///
/// It cannot fail upward. A daemon that will not answer is a reason to drop the
/// connection and re-Sync; a bus that will not answer is a *fact about a
/// watched unit*, and `host.rs` reports it as one — `load_state: error` on the
/// card, rather than silence and a host that looks fine.
pub async fn scan_host(
    host: &mut Host,
    state: &mut State,
    slice: Slice,
    full: bool,
) -> Option<wire::envelope::Payload> {
    let (ids, fingerprints, entities): (Vec<String>, Vec<u64>, Vec<wire::Entity>) = match slice {
        Slice::Unit => {
            let units = host.units().await;
            (
                units.iter().map(|u| u.name.clone()).collect(),
                units.iter().map(hashset::hash_unit).collect(),
                units
                    .into_iter()
                    .map(|u| entity(u.name.clone(), entity::Body::Unit(u)))
                    .collect(),
            )
        }
        _ => {
            let processes = host.processes().await;
            (
                // The watch id, never a pid. A pid is recycled by the kernel
                // and changes on exactly the event being watched for, so an
                // entity keyed by one would be a different entity after every
                // restart -- and the Controller would draw a new card and lose
                // the history attached to the old one.
                processes.iter().map(|p| p.watch_id.clone()).collect(),
                processes.iter().map(hashset::hash_process).collect(),
                processes
                    .into_iter()
                    .map(|p| entity(p.watch_id.clone(), entity::Body::Process(p)))
                    .collect(),
            )
        }
    };

    let pairs: Vec<(&str, u64)> = ids
        .iter()
        .map(String::as_str)
        .zip(fingerprints.iter().copied())
        .collect();
    let change = state.of(slice).diff(pairs);

    if full {
        return Some(wire::envelope::Payload::Sync(wire::Sync {
            slice: slice as i32,
            entities,
        }));
    }
    if change.is_quiet() {
        return None;
    }

    let mut changed = Vec::with_capacity(change.changed.len());
    let mut wanted: HashSet<usize> = change.changed.iter().copied().collect();
    for (index, item) in entities.into_iter().enumerate() {
        if wanted.remove(&index) {
            changed.push(item);
        }
    }

    Some(wire::envelope::Payload::Delta(wire::Delta {
        slice: slice as i32,
        ids: change.ids,
        changed,
    }))
}

/// Collapse a burst of dirty slices into one set.
///
/// Waits for the first event, then drains everything that arrives within the
/// coalescing window. Without this a `compose up` is a hundred round trips and
/// a hundred frames; with it, one of each.
pub async fn coalesce(events: &mut mpsc::Receiver<String>) -> Option<HashSet<Slice>> {
    let first = events.recv().await?;
    let mut dirty = HashSet::new();
    if let Some(slice) = wire::slice_for_event(&first) {
        dirty.insert(slice);
    }

    let deadline = tokio::time::sleep(COALESCE_WINDOW);
    tokio::pin!(deadline);
    loop {
        tokio::select! {
            _ = &mut deadline => break,
            received = events.recv() => match received {
                Some(kind) => {
                    if let Some(slice) = wire::slice_for_event(&kind) {
                        dirty.insert(slice);
                    }
                }
                None => break,
            },
        }
    }

    // A container event invalidates the image slice too: the Controller only
    // draws images something actually runs, so the last container of an image
    // going away is what makes that image stop being topology.
    if dirty.contains(&Slice::Container) {
        dirty.insert(Slice::Image);
    }

    Some(dirty)
}
