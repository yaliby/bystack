//! Content hashing and change detection.
//!
//! The agent's entire reason for existing on the CPU budget it has: hash each
//! entity, compare against the previous value, send only what moved. Hashing
//! is **not** interpretation — we hash a fixed field set and compare integers,
//! and never ask what a field means. That is the line from ADR-0009 §1, and it
//! is checkable: if a change here requires knowing Docker's semantics, it
//! belongs on the Controller.
//!
//! ## The hash does not need to be stable across runs
//!
//! `DefaultHasher` is SipHash and its output is not guaranteed stable between
//! Rust releases. That is fine, and worth stating so nobody "fixes" it by
//! adding a crate: the agent compares a hash only against one it computed
//! itself, in this process, and it persists nothing (ARCHITECTURE §3). A
//! restart re-Lists and re-hashes everything and sends a full Sync anyway.
//!
//! ## The `Status` trap, for the second time
//!
//! Docker's `Status` field is a **rendered string** — `"Up 3 hours"`,
//! `"Exited (137) 3 seconds ago"`. It changes on a wall clock, not on a state
//! change.
//!
//! The Controller already learned this: its node hash excludes it, and it is
//! item one in the README's list of things that will bite you. **The hazard
//! exists a second time here**, and here it does far more damage — hashing it
//! would put the full payload set on the network every resync, per host,
//! forever.
//!
//! The exclusion is safe rather than merely cheap. The only thing the
//! Controller takes from that string is the exit code, and that is only
//! meaningful at the moment `state` changes — which *is* hashed. A container
//! that exits gets re-sent because its state moved, and carries
//! `Exited (137) …` with it. Five minutes later the string reads
//! `5 minutes ago`, nothing else changed, and nothing is sent.
//!
//! `hash_container` is what enforces this, and `tests` pins it. The failure
//! mode is not an error: it is a system that works perfectly and costs 100×
//! more than it should.

use std::collections::hash_map::DefaultHasher;
use std::collections::HashMap;
use std::hash::{Hash, Hasher};

use crate::model::{Container, Image, Network, Volume};
use crate::wire;

pub type Fingerprint = u64;

/// Hash a container over every field **except** `status`.
///
/// Written out field by field rather than deriving `Hash` on the struct.
/// Deriving would silently include `status` the moment someone reorders the
/// declaration, and the resulting failure is invisible — see the module
/// docs.
pub fn hash_container(container: &Container) -> Fingerprint {
    let mut hasher = DefaultHasher::new();
    container.id.hash(&mut hasher);
    container.names.hash(&mut hasher);
    container.image.hash(&mut hasher);
    container.image_id.hash(&mut hasher);
    container.command.hash(&mut hasher);
    container.created.hash(&mut hasher);
    container.state.hash(&mut hasher);
    // container.status is deliberately absent. Do not add it.
    //
    // The healthcheck's verdict, however, must be here. `state` stays
    // `running` while a container fails its probe, so a container that goes
    // unhealthy is otherwise byte-identical to the one we last sent and is
    // never resent -- and the map keeps drawing it green. Only the verdict is
    // hashed, not `failing_streak`, which advances on every failed probe.
    container.health().hash(&mut hasher);
    // And so must the crash-loop depth, for a version of the same reason.
    // `state` stays `restarting` across every restart in a loop, so without
    // this the map would show a container as unstable and never say whether
    // it has failed twice or four hundred times.
    //
    // This one is a decision rather than an obligation, because unlike the
    // health verdict it does advance repeatedly on a bad host. It advances on
    // an *event* -- an actual restart -- which is what separates it from
    // `status`, and the containers it advances for are the ones an operator
    // is watching. A resend per restart of a looping container is the correct
    // price.
    container.restart_count.hash(&mut hasher);
    container.labels.hash(&mut hasher);
    container.ports.hash(&mut hasher);
    container.mounts.hash(&mut hasher);
    container.network_settings.hash(&mut hasher);
    hasher.finish()
}

pub fn hash_network(network: &Network) -> Fingerprint {
    hash_of(network)
}

pub fn hash_volume(volume: &Volume) -> Fingerprint {
    hash_of(volume)
}

pub fn hash_image(image: &Image) -> Fingerprint {
    hash_of(image)
}

/// Hash one watched unit over every field it carries.
///
/// **Including `active_enter_timestamp`, which looks like the `Status` trap
/// above and is its opposite.** That field is a fixed instant that moves when
/// the unit actually restarts; Docker's `Status` is a rendered duration that
/// moves on a wall clock. The distinction is the same one `restart_count`
/// turns on: an event, not a clock.
///
/// Leaving it out has a concrete cost, and it is not a small one. `NRestarts`
/// counts only the restarts *systemd* performed under a `Restart=` policy — a
/// `systemctl restart` by a person does not touch it. So for a manual restart
/// the timestamp is the only field that moves at all: without it in the hash,
/// the unit is byte-identical to the one we last sent, nothing is sent, and
/// the map goes on claiming the service has been up since before the restart
/// that somebody just performed.
///
/// Written out field by field rather than derived, for the reason at the top
/// of this module: a derive would silently pick up whatever the struct gains
/// next, and prost regenerates that struct from the `.proto`.
pub fn hash_unit(unit: &wire::Unit) -> Fingerprint {
    let mut hasher = DefaultHasher::new();
    unit.name.hash(&mut hasher);
    unit.description.hash(&mut hasher);
    unit.load_state.hash(&mut hasher);
    unit.active_state.hash(&mut hasher);
    unit.sub_state.hash(&mut hasher);
    unit.unit_file_state.hash(&mut hasher);
    unit.main_pid.hash(&mut hasher);
    unit.active_enter_timestamp.hash(&mut hasher);
    unit.n_restarts.hash(&mut hasher);
    unit.result.hash(&mut hasher);
    unit.exec_main_status.hash(&mut hasher);
    unit.fragment_path.hash(&mut hasher);
    hasher.finish()
}

/// Hash one process watch over the rule and everything matching it.
///
/// The pids are in the hash, and that is the point rather than an oversight: a
/// daemon that restarted has a new pid and nothing else worth reporting, and a
/// watch that could not see that would be a watch that reports the one event
/// it was added for as no event at all.
///
/// Nothing sampled is in here, because nothing sampled is carried: there is no
/// CPU share and no memory figure anywhere in this slice. Per-process resource
/// series belong to the systems that already do them well (ARCHITECTURE §12),
/// and a number that moves every scan would put the whole slice on the wire
/// every interval — which is how a control plane quietly becomes a metrics
/// agent with a bad sampling rate.
pub fn hash_process(process: &wire::Process) -> Fingerprint {
    let mut hasher = DefaultHasher::new();
    process.watch_id.hash(&mut hasher);
    process.match_kind.hash(&mut hasher);
    process.pattern.hash(&mut hasher);
    process.total.hash(&mut hasher);
    for instance in &process.instances {
        instance.pid.hash(&mut hasher);
        instance.comm.hash(&mut hasher);
        instance.cmdline.hash(&mut hasher);
        instance.state.hash(&mut hasher);
        instance.started_at.hash(&mut hasher);
        instance.uid.hash(&mut hasher);
        instance.cgroup.hash(&mut hasher);
    }
    hasher.finish()
}

fn hash_of<T: Hash>(value: &T) -> Fingerprint {
    let mut hasher = DefaultHasher::new();
    value.hash(&mut hasher);
    hasher.finish()
}

/// What changed in one slice since the last time we looked.
pub struct Change {
    /// Every id currently in the slice — the authoritative membership set.
    ///
    /// Always complete, never just the changes. The Controller reconciles the
    /// slice against this, so a partial set would delete every stable
    /// container on the host. Ids are cheap; payloads are not.
    pub ids: Vec<String>,

    /// Indices into the caller's list whose fingerprint moved.
    pub changed: Vec<usize>,

    /// Whether the id set itself differs from the one we last sent.
    ///
    /// Tracked separately from `changed`, and that distinction is load-bearing.
    /// A container being **removed** changes no payload: every survivor hashes
    /// exactly as before. Deciding to stay silent on `changed.is_empty()`
    /// alone means a deletion is never reported, and because deletion is
    /// carried by absence from the id set, it is never reported by any other
    /// means either. The container simply stays on the operator's canvas
    /// forever.
    pub membership_moved: bool,
}

impl Change {
    /// Nothing to tell the Controller.
    ///
    /// True only when no payload moved *and* the membership is identical.
    /// This is what keeps an idle host at zero bytes; it must not be cheap
    /// enough to also swallow a removal.
    pub fn is_quiet(&self) -> bool {
        self.changed.is_empty() && !self.membership_moved
    }
}

/// One slice's `id -> fingerprint` map.
///
/// The agent's only state, and it is small: roughly 12 KB for a hundred
/// containers. It lives in memory and is rebuilt on every start, because the
/// agent holds no queue, no spool and no cache that survives a restart.
#[derive(Default)]
pub struct SliceState {
    fingerprints: HashMap<String, Fingerprint>,
}

impl SliceState {
    /// Diff a freshly-Listed slice against what we last sent.
    ///
    /// Replaces the stored map wholesale, so an entity that vanished is
    /// forgotten here at the same moment its id stops appearing in `ids` —
    /// which is how the Controller learns it is gone. Deletion needs no
    /// separate signal in either direction.
    pub fn diff<'a, I>(&mut self, entities: I) -> Change
    where
        I: IntoIterator<Item = (&'a str, Fingerprint)>,
    {
        let mut ids = Vec::new();
        let mut changed = Vec::new();
        let mut next = HashMap::new();
        let mut arrivals = false;

        for (index, (id, fingerprint)) in entities.into_iter().enumerate() {
            match self.fingerprints.get(id) {
                Some(previous) if *previous == fingerprint => {}
                Some(_) => changed.push(index),
                None => {
                    arrivals = true;
                    changed.push(index);
                }
            }
            ids.push(id.to_string());
            next.insert(id.to_string(), fingerprint);
        }

        // A departure changes no payload -- every survivor hashes exactly as
        // before -- so it is only visible as the set having shrunk. Comparing
        // sizes is enough given every arrival was already counted above.
        let departures = next.len() < self.fingerprints.len();

        self.fingerprints = next;
        Change { ids, changed, membership_moved: arrivals || departures }
    }

    /// Forget everything, forcing the next diff to report every entity.
    ///
    /// Called when the connection to the Controller drops. The Controller
    /// clears its own view at the same moment, so re-sending everything is
    /// not waste — it is the full Sync that every connection begins with.
    pub fn clear(&mut self) {
        self.fingerprints.clear();
    }

    pub fn len(&self) -> usize {
        self.fingerprints.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn container(state: &str, status: &str) -> Container {
        Container {
            id: "c1".into(),
            state: state.into(),
            status: status.into(),
            ..Default::default()
        }
    }

    #[test]
    fn the_rendered_status_string_is_not_hashed() {
        // The single most expensive mistake available in this file. If this
        // fails, every container on every host is re-sent on every resync and
        // the whole "zero bytes at steady state" property is gone -- with no
        // error anywhere to show for it.
        let before = hash_container(&container("running", "Up 3 hours"));
        let after = hash_container(&container("running", "Up 4 hours"));

        assert_eq!(before, after);
    }

    fn healthy(verdict: &str) -> Container {
        let mut c = container("running", "Up 3 hours");
        c.health = crate::model::Health { status: verdict.into(), failing_streak: 0 };
        c
    }

    #[test]
    fn a_container_that_goes_unhealthy_is_resent() {
        // The state does not move when a healthcheck starts failing, so
        // without the verdict in the hash this container is byte-identical to
        // the one already on the Controller and is never sent again -- and
        // the map keeps drawing it green. There is no error to notice.
        assert_ne!(hash_container(&healthy("healthy")), hash_container(&healthy("unhealthy")));
    }

    #[test]
    fn no_healthcheck_and_a_passing_one_are_not_the_same_container() {
        assert_ne!(hash_container(&healthy("none")), hash_container(&healthy("healthy")));
    }

    #[test]
    fn a_failing_streak_is_a_clock_and_is_not_hashed() {
        // It advances on every failed probe. Hashing it would resend the
        // container every probe interval for as long as it stays broken --
        // which is exactly when the network matters most.
        let mut first = healthy("unhealthy");
        first.health.failing_streak = 3;
        let mut second = healthy("unhealthy");
        second.health.failing_streak = 41;

        assert_eq!(hash_container(&first), hash_container(&second));
    }

    #[test]
    fn a_crash_loop_that_deepens_is_resent() {
        // `state` stays `restarting` across every restart in a loop, so
        // without the count in the hash the Controller learns that a
        // container is unstable exactly once and never how badly. The card
        // would read `Unstable` identically on the second failure and the
        // four-hundredth.
        let mut second = container("restarting", "Restarting (137) 2 seconds ago");
        second.restart_count = 2;
        let mut four_hundredth = container("restarting", "Restarting (137) 2 seconds ago");
        four_hundredth.restart_count = 400;

        assert_ne!(hash_container(&second), hash_container(&four_hundredth));
    }

    #[test]
    fn a_settled_container_is_not_disturbed_by_the_field_existing() {
        // The deliberate cost of hashing this is a resend per restart, for
        // containers that are restarting. Everything else must be untouched,
        // or the field has quietly become a second `status`.
        let running = container("running", "Up 3 hours");
        let same = container("running", "Up 4 hours");

        assert_eq!(hash_container(&running), hash_container(&same));
        assert_eq!(running.restart_count, 0);
    }

    #[test]
    fn an_older_daemon_reports_the_same_verdict_through_the_status_line() {
        // `Health` as a structure is a recent addition to the list endpoint.
        // Falling back to silence would read as "no healthcheck", which is
        // the one wrong answer available here.
        assert_eq!(container("running", "Up 2 hours (unhealthy)").health(), "unhealthy");
        assert_eq!(container("running", "Up 2 hours (healthy)").health(), "healthy");
        assert_eq!(container("running", "Up 1 second (health: starting)").health(), "starting");
        assert_eq!(container("running", "Up 3 hours").health(), "");
        // And the structure wins where both are present.
        assert_eq!(healthy("healthy").health(), "healthy");
    }

    #[test]
    fn the_state_is_hashed() {
        let running = hash_container(&container("running", "Up 3 hours"));
        let exited = hash_container(&container("exited", "Exited (137) 1 second ago"));

        assert_ne!(running, exited);
    }

    #[test]
    fn labels_are_hashed_in_a_stable_order() {
        // BTreeMap, not HashMap. A HashMap would iterate differently per
        // process and make every container look changed on every List.
        let mut first = container("running", "Up 3 hours");
        first.labels.insert("a".into(), "1".into());
        first.labels.insert("b".into(), "2".into());

        let mut second = container("running", "Up 3 hours");
        second.labels.insert("b".into(), "2".into());
        second.labels.insert("a".into(), "1".into());

        assert_eq!(hash_container(&first), hash_container(&second));
    }

    #[test]
    fn a_first_diff_reports_everything() {
        let mut state = SliceState::default();

        let change = state.diff([("a", 1), ("b", 2)]);

        assert_eq!(change.ids, vec!["a", "b"]);
        assert_eq!(change.changed, vec![0, 1]);
    }

    #[test]
    fn an_unchanged_slice_reports_membership_and_nothing_else() {
        let mut state = SliceState::default();
        state.diff([("a", 1), ("b", 2)]);

        let change = state.diff([("a", 1), ("b", 2)]);

        assert!(change.is_quiet());
        assert_eq!(change.ids.len(), 2);
    }

    #[test]
    fn only_the_moved_entity_is_reported() {
        let mut state = SliceState::default();
        state.diff([("a", 1), ("b", 2)]);

        let change = state.diff([("a", 1), ("b", 99)]);

        assert_eq!(change.changed, vec![1]);
        assert_eq!(change.ids, vec!["a", "b"]);
    }

    #[test]
    fn a_removal_is_reported_even_though_no_payload_moved() {
        // The bug the conformance suite caught. Every surviving container
        // hashes exactly as before, so `changed` is empty -- and staying
        // silent on that alone means the removal is never sent. Since
        // deletion is carried *only* by absence from the id set, it would
        // then never be sent by any other means: the container stays on the
        // operator's canvas forever, with nothing anywhere reporting an error.
        let mut state = SliceState::default();
        state.diff([("a", 1), ("b", 2)]);

        let change = state.diff([("a", 1)]);

        assert_eq!(change.ids, vec!["a"]);
        assert!(change.changed.is_empty());
        assert!(!change.is_quiet(), "a removal must be sent");
        assert_eq!(state.len(), 1);
    }

    #[test]
    fn an_arrival_is_reported() {
        let mut state = SliceState::default();
        state.diff([("a", 1)]);

        let change = state.diff([("a", 1), ("b", 2)]);

        assert_eq!(change.changed, vec![1]);
        assert!(!change.is_quiet());
    }

    #[test]
    fn a_swap_of_equal_size_is_still_reported() {
        // One container replaced by another -- the shape of a `compose up`
        // that recreates a service. The set is the same size, so a size
        // comparison alone would miss it; the arrival is what catches it.
        let mut state = SliceState::default();
        state.diff([("a", 1)]);

        let change = state.diff([("b", 1)]);

        assert!(!change.is_quiet());
        assert_eq!(change.ids, vec!["b"]);
    }

    #[test]
    fn clearing_forces_a_full_resend() {
        let mut state = SliceState::default();
        state.diff([("a", 1)]);

        state.clear();
        let change = state.diff([("a", 1)]);

        assert_eq!(change.changed, vec![0]);
    }
}
