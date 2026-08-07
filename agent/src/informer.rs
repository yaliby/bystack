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

use tokio::sync::mpsc;

use crate::docker::{Engine, EngineError};
use crate::hashset::{self, SliceState};
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
}

impl State {
    /// Forget every fingerprint, so the next pass re-sends everything.
    pub fn clear(&mut self) {
        self.containers.clear();
        self.networks.clear();
        self.volumes.clear();
        self.images.clear();
    }

    /// How many entities this agent is currently tracking, across all slices.
    ///
    /// The agent's entire working set, and the number the resource budget is
    /// about: roughly 12 KB of fingerprints for a hundred containers.
    pub fn tracked(&self) -> usize {
        self.containers.len() + self.networks.len() + self.volumes.len() + self.images.len()
    }

    /// Forget one slice, so the next scan re-sends all of it.
    ///
    /// Used when the Controller asks for a resync: it asked because it
    /// believes we disagree, and answering "nothing changed" out of a hash map
    /// it has just told us to distrust would be useless.
    pub fn forget(&mut self, slice: Slice) {
        self.of(slice).clear();
    }

    fn of(&mut self, slice: Slice) -> &mut SliceState {
        match slice {
            Slice::Container => &mut self.containers,
            Slice::Network => &mut self.networks,
            Slice::Volume => &mut self.volumes,
            _ => &mut self.images,
        }
    }
}

/// Every slice, in the order a first Sync should send them.
///
/// Containers first because they are what the operator is waiting to see; the
/// Controller reconciles each slice independently, so a partially-arrived
/// picture is never inconsistent, only incomplete.
pub const ALL_SLICES: [Slice; 4] =
    [Slice::Container, Slice::Network, Slice::Volume, Slice::Image];

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
            let listed = engine.containers().await?;
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
