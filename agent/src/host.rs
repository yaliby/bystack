//! What the Controller asked this machine to watch, and what it says back.
//!
//! The four Docker slices need nothing like this file, because an engine's
//! whole inventory *is* the topology and the informer simply Lists it. A
//! machine's is not: it has two thousand units and four hundred processes, an
//! operator cares about six of them, and observing the rest would cost CPU on
//! every managed host forever — the second-rate cAdvisor ARCHITECTURE §12
//! exists to refuse.
//!
//! So the membership of these two slices arrives from the Controller as a
//! [`wire::WatchList`], and this file is the whole of what the agent does with
//! it: look up exactly those things, and report exactly those things.
//!
//! **No policy lives here.** The list is a set of opaque ids, names and
//! patterns; nothing in this file knows that an entry becomes a node, that an
//! id becomes part of a URN, or that `not-found` will be drawn differently
//! from `active`. That is the same line `docker.rs` holds, drawn one subsystem
//! over (ADR-0009 §1).
//!
//! **A host that watches nothing does nothing.** No bus connection, no
//! `/proc` walk, no timer. That is the property that makes this feature
//! affordable on hardware somebody bought for something else, and it is
//! checked rather than assumed: [`Host::watching_units`] and
//! [`Host::watching_processes`] are what the session's select loop arms its
//! work from.

use crate::procfs::{self, Match, Snapshot};
use crate::systemd::{Applied, Systemd, UnitState};
use crate::wire;

/// One thing to watch, as the Controller described it.
pub struct Watch {
    /// The Controller's own id. Opaque here, and echoed back as the entity id
    /// for a process — a process watch has no natural name, because a pid is
    /// recycled by the kernel and changes on exactly the event being watched
    /// for.
    pub id: String,
    /// What the operator calls it. Opaque here too: it is echoed back so a
    /// `Sync` carries everything the Controller needs to draw a card, and
    /// nothing in this agent reads it.
    pub label: String,
    pub target: Target,
}

pub enum Target {
    Unit { name: String },
    Process { kind: Match, pattern: String },
}

/// The host half of one agent: systemd, `/proc`, and the list.
pub struct Host {
    /// Connected on first use and kept, because the connection is the
    /// expensive part and a watched machine uses it every time anything
    /// changes. `None` means "not connected yet or lost"; a machine with no
    /// systemd never leaves that state and says so at `Hello` by not
    /// advertising the capability.
    systemd: Option<Systemd>,
    watch: Vec<Watch>,
}

impl Host {
    pub fn new() -> Self {
        Self { systemd: None, watch: Vec::new() }
    }

    /// Whether this machine can answer for units at all.
    ///
    /// Called once, before `Hello`, and the answer becomes a capability. That
    /// widens what a capability means — "what this build can do" gains "on
    /// this machine" — and it is the honest widening: the Controller's whole
    /// use for the set is deciding whether to send a frame, and a frame this
    /// agent would answer with an error on every host without systemd is one
    /// worth refusing at the point somebody types it, with a reason, rather
    /// than at the point they wonder why the card never appeared.
    pub async fn systemd_available(&mut self) -> bool {
        if self.systemd.is_some() {
            return true;
        }
        match Systemd::connect().await {
            Ok(systemd) => {
                self.systemd = Some(systemd);
                true
            }
            Err(_) => false,
        }
    }

    /// Replace the whole list. Authoritative, exactly like a `Sync`.
    ///
    /// An entry that stops appearing stops being watched, with no separate
    /// removal to go missing. Entries the agent cannot make sense of — an
    /// unknown kind, an unknown match, an empty pattern — are dropped rather
    /// than guessed at: a Controller from a later release may name a kind this
    /// build has never heard of, and inventing an interpretation for it is how
    /// an agent starts watching the wrong thing.
    pub fn set_watchlist(&mut self, list: &wire::WatchList) -> usize {
        self.watch = list
            .entries
            .iter()
            .filter_map(|entry| {
                if entry.id.is_empty() {
                    return None;
                }
                let target = match entry.kind.as_str() {
                    "unit" if !entry.name.is_empty() => {
                        Target::Unit { name: entry.name.clone() }
                    }
                    "process" if !entry.pattern.is_empty() => Target::Process {
                        kind: Match::parse(&entry.match_kind)?,
                        pattern: entry.pattern.clone(),
                    },
                    _ => return None,
                };
                Some(Watch {
                    id: entry.id.clone(),
                    label: entry.label.clone(),
                    target,
                })
            })
            .collect();
        self.watch.len()
    }

    pub fn watching_units(&self) -> bool {
        self.watch.iter().any(|w| matches!(w.target, Target::Unit { .. }))
    }

    pub fn watching_processes(&self) -> bool {
        self.watch.iter().any(|w| matches!(w.target, Target::Process { .. }))
    }

    /// Whether this exact unit is on the list. Compared by name, which is
    /// systemd's own identity for it and the string the Controller sent.
    fn watching_unit(&self, name: &str) -> bool {
        self.watch.iter().any(|watch| match &watch.target {
            Target::Unit { name: watched } => watched == name,
            Target::Process { .. } => false,
        })
    }

    // -- observation -------------------------------------------------------

    /// The unit slice: one entity per watched unit, in list order.
    ///
    /// A unit systemd will not load is still an entity, carrying
    /// `load_state: not-found`. That is the difference between "the service
    /// you asked about is not installed" and a card that silently never
    /// appears — and the second is indistinguishable from having forgotten to
    /// add it.
    pub async fn units(&mut self) -> Vec<wire::Unit> {
        let mut out = Vec::new();
        for watch in &self.watch {
            let Target::Unit { name } = &watch.target else { continue };
            let state = match self.systemd.as_mut() {
                Some(systemd) => systemd.state(name).await.unwrap_or_else(|_| {
                    // The bus went away underneath us -- a systemd restart, a
                    // dbus-daemon restart. Reported as an error state rather
                    // than as a missing entity, and the connection is dropped
                    // so the next scan redials.
                    unavailable(name)
                }),
                None => unavailable(name),
            };
            // One failure is enough to conclude the connection is bad; a
            // second unit would fail identically and slowly.
            if state.load_state == ERROR {
                self.systemd = None;
            }
            out.push(unit_frame(&state));
        }
        out
    }

    /// The process slice: one entity per watched rule, with what matches.
    ///
    /// One walk of `/proc` for every rule rather than one per rule. A host
    /// watching six processes would otherwise read every entry six times, and
    /// the directory walk is the whole cost.
    ///
    /// **And that walk happens off the runtime.** Reading three hundred
    /// processes is a thousand small synchronous file reads — single-digit
    /// milliseconds, or tens of them on a loaded machine — and this runtime is
    /// deliberately current-thread (`main.rs`), so doing it inline would stall
    /// the WebSocket pump for its duration, every interval, forever. It is the
    /// same mistake ADR-0012 §4b records on the Controller, in a process with
    /// far less headroom to hide it.
    pub async fn processes(&mut self) -> Vec<wire::Process> {
        let rules = self.process_rules();
        if rules.is_empty() {
            return Vec::new();
        }
        tokio::task::spawn_blocking(move || scan_processes(rules))
            .await
            // The blocking pool only fails if the runtime is shutting down, in
            // which case nothing is waiting for this answer anyway.
            .unwrap_or_default()
    }

    /// The process watches, as owned data a worker thread can hold.
    fn process_rules(&self) -> Vec<Rule> {
        self.watch
            .iter()
            .filter_map(|watch| match &watch.target {
                Target::Process { kind, pattern } => Some(Rule {
                    id: watch.id.clone(),
                    label: watch.label.clone(),
                    kind: *kind,
                    pattern: pattern.clone(),
                }),
                Target::Unit { .. } => None,
            })
            .collect()
    }

    // -- the picker --------------------------------------------------------

    /// Enumerate this machine, once, because somebody opened the picker.
    ///
    /// The only place in the agent that walks a whole machine, and it runs
    /// exactly when asked. That is the entire difference between this and a
    /// monitoring agent: nobody pays for the walk until somebody wants to see
    /// the list.
    pub async fn inventory(&mut self, request: &wire::InventoryRequest) -> wire::InventoryResponse {
        let limit = (request.limit as usize).clamp(1, procfs::MAX_INVENTORY_ITEMS);
        let mut response = wire::InventoryResponse {
            request_id: request.request_id.clone(),
            ok: true,
            ..Default::default()
        };

        match request.kind.as_str() {
            "unit" => {
                if !self.systemd_available().await {
                    return refuse(request, "this machine has no reachable systemd");
                }
                let systemd = self.systemd.as_mut().expect("connected above");
                match systemd.list_units(&request.filter, limit).await {
                    Ok((units, total)) => {
                        response.total = total as u32;
                        response.items = units
                            .into_iter()
                            .map(|unit| wire::InventoryItem {
                                id: unit.name.clone(),
                                name: unit.name,
                                description: unit.description,
                                state: unit.active_state,
                                detail: unit.sub_state,
                                pid: 0,
                            })
                            .collect();
                    }
                    Err(error) => {
                        self.systemd = None;
                        return refuse(request, &error.to_string());
                    }
                }
            }
            "process" => {
                let filter = request.filter.clone();
                // Off the runtime, like every other walk of `/proc`, and this
                // is the largest of them: a picker enumerates the whole
                // machine rather than stopping at the first field that rules a
                // process out.
                let listed = tokio::task::spawn_blocking(move || {
                    Snapshot::read().map(|mut snapshot| snapshot.listing(&filter, limit))
                })
                .await;
                let (rows, total) = match listed {
                    Ok(Ok(listed)) => listed,
                    Ok(Err(error)) => {
                        return refuse(request, &format!("cannot read /proc: {error}"))
                    }
                    Err(_) => return refuse(request, "this agent is shutting down"),
                };
                response.total = total as u32;
                response.items = rows
                    .into_iter()
                    .map(|(process, exec)| wire::InventoryItem {
                        // What a watch rule would be built from. The
                        // executable path where there is one, because it is
                        // the precise choice; the truncated `comm` otherwise,
                        // which is all the kernel will tell us about a process
                        // we do not own.
                        id: if exec.is_empty() { process.comm.clone() } else { exec },
                        name: process.comm,
                        description: String::new(),
                        state: process.state,
                        detail: process.cmdline,
                        pid: process.pid,
                    })
                    .collect();
            }
            other => return refuse(request, &format!("unknown inventory kind {other:?}")),
        }

        response
    }

    // -- lifecycle ---------------------------------------------------------

    /// Act on one watched unit or process.
    ///
    /// Returns `Ok(Applied)` or an explanation. The verb set is the
    /// Controller's, and every verb here maps onto something that already
    /// existed: this feature added no `CommandKind`, which is what keeps it
    /// clear of ADR-0014 entirely.
    pub async fn act(
        &mut self,
        kind: &str,
        target: &str,
        verb: &str,
        signal: Option<&str>,
    ) -> Result<Applied, String> {
        match kind {
            "unit" => {
                // **Only what this host was asked to watch.** The Controller
                // already refused everything it should have, and this refuses
                // again -- the second choke point of ARCHITECTURE §9, which
                // exists because this process holds privileged access to a
                // machine and "the Controller said so" is not an acceptable
                // sole justification for acting on it.
                //
                // Here it is also what *bounds the polkit rule*. That rule
                // grants `manage-units` to this account for every unit on the
                // machine, because polkit is evaluated per call and cannot
                // know a watch list that changes while it is running
                // (ADR-0016). This check is where the list becomes the bound:
                // a Controller that asked to restart something nobody
                // selected is refused by the agent, not by the policy.
                if !self.watching_unit(target) {
                    return Err(format!(
                        "this agent is not watching {target:?}; add it to this host's \
                         watch list before operating on it"
                    ));
                }
                if !self.systemd_available().await {
                    return Err("this machine has no reachable systemd".into());
                }
                let number = signal
                    .and_then(procfs::signal_number)
                    .unwrap_or(procfs::SIGKILL);
                let systemd = self.systemd.as_mut().expect("connected above");
                let outcome = systemd.act(target, verb, number).await;
                if outcome.is_err() {
                    // A bus failure is a lost connection as often as it is a
                    // refusal, and the next scan must redial rather than
                    // report every unit as broken.
                    self.systemd = None;
                }
                outcome.map_err(|error| error.to_string())
            }
            "process" => self.signal_watch(target, verb, signal).await,
            other => Err(format!("{other:?} is not something this agent can act on")),
        }
    }

    /// Signal every process one rule currently matches.
    ///
    /// The pids are resolved *now*, from a fresh read, and never from whatever
    /// the last scan reported. A stale pid is not a harmless mistake here: the
    /// kernel recycles them, so signalling one we saw a minute ago can deliver
    /// `SIGKILL` to a process that has nothing to do with the rule. This is
    /// the single most dangerous line in the host feature and it is why the
    /// Controller sends a watch id rather than a pid.
    async fn signal_watch(
        &mut self,
        watch_id: &str,
        verb: &str,
        signal: Option<&str>,
    ) -> Result<Applied, String> {
        let number = match verb {
            "stop" => signal.and_then(procfs::signal_number).unwrap_or(procfs::SIGTERM),
            "kill" => signal.and_then(procfs::signal_number).unwrap_or(procfs::SIGKILL),
            "pause" => procfs::SIGSTOP,
            "unpause" => procfs::SIGCONT,
            other => {
                return Err(format!(
                    "{other:?} needs something that knows how to start this process; \
                     a bare process has no recorded way to be launched"
                ))
            }
        };

        let rule = self
            .process_rules()
            .into_iter()
            .find(|rule| rule.id == watch_id)
            .ok_or_else(|| format!("this agent is not watching {watch_id:?}"))?;

        // The read and the signal happen together in one worker, deliberately.
        // Splitting them would put an `await` between resolving a pid and
        // using it, which is the widest possible version of the race this
        // whole design exists to close.
        tokio::task::spawn_blocking(move || {
            let mut snapshot =
                Snapshot::read().map_err(|e| format!("cannot read /proc: {e}"))?;
            let (found, _) = snapshot.matching(rule.kind, &rule.pattern);
            if found.is_empty() {
                // Not a failure. "The daemon you asked me to stop is not
                // running" is an answer to the question, and it is the same
                // answer Docker's 304 gives for starting a running container.
                return Ok(Applied::Unchanged);
            }
            let pids: Vec<u32> = found.iter().map(|p| p.pid).collect();
            match procfs::signal(&pids, number) {
                0 => Err("every matching process refused the signal or had already exited".into()),
                _ => Ok(Applied::Applied),
            }
        })
        .await
        .unwrap_or_else(|_| Err("this agent is shutting down".into()))
    }
}

/// One process watch, owned, so a worker thread can hold it.
#[derive(Clone)]
struct Rule {
    id: String,
    label: String,
    kind: Match,
    pattern: String,
}

/// Match every rule against one walk of `/proc`. Runs in a worker thread.
///
/// A rule that matches nothing still produces an entity — with no instances,
/// which the Controller draws as `absent`. That is an observation ("it is not
/// running"), and dropping it would make a watch on a stopped daemon
/// indistinguishable from never having added it.
fn scan_processes(rules: Vec<Rule>) -> Vec<wire::Process> {
    let mut snapshot = Snapshot::read().ok();
    rules
        .into_iter()
        .map(|rule| {
            let (found, total) = match snapshot.as_mut() {
                Some(snapshot) => snapshot.matching(rule.kind, &rule.pattern),
                // `/proc` unreadable at all. The honest report is that the
                // rule matches nothing we can see — which is also what
                // `ProtectProc=invisible` produces, and the Controller draws
                // both the same way.
                None => (Vec::new(), 0),
            };
            wire::Process {
                watch_id: rule.id,
                label: rule.label,
                match_kind: match_name(rule.kind).to_string(),
                pattern: rule.pattern,
                total: total as u32,
                instances: found.iter().map(instance_frame).collect(),
            }
        })
        .collect()
}

/// `LoadState` for a unit we could not ask about, as opposed to one that does
/// not exist. The Controller draws both as unusable, and an operator can tell
/// "not installed" from "the bus is down" — which are different problems with
/// different fixes.
const ERROR: &str = "error";

fn unavailable(name: &str) -> UnitState {
    UnitState { load_state: ERROR.into(), ..UnitState::not_found(name) }
}

fn unit_frame(state: &UnitState) -> wire::Unit {
    wire::Unit {
        name: state.name.clone(),
        description: state.description.clone(),
        load_state: state.load_state.clone(),
        active_state: state.active_state.clone(),
        sub_state: state.sub_state.clone(),
        unit_file_state: state.unit_file_state.clone(),
        main_pid: state.main_pid,
        active_enter_timestamp: state.active_enter_usec as i64,
        n_restarts: state.n_restarts,
        result: state.result.clone(),
        exec_main_status: state.exec_main_status,
        fragment_path: state.fragment_path.clone(),
    }
}

fn instance_frame(process: &procfs::Process) -> wire::ProcessInstance {
    wire::ProcessInstance {
        pid: process.pid,
        comm: process.comm.clone(),
        cmdline: process.cmdline.clone(),
        state: process.state.clone(),
        started_at: process.started_at,
        uid: process.uid,
        cgroup: process.cgroup.clone(),
    }
}

fn match_name(kind: Match) -> &'static str {
    match kind {
        Match::Name => "name",
        Match::Exec => "exec",
        Match::Cmdline => "cmdline",
    }
}

fn refuse(request: &wire::InventoryRequest, reason: &str) -> wire::InventoryResponse {
    // Refused with a reason rather than answered with an empty list. "This
    // machine has no unit matching `ngin`" and "the bus is unreachable" render
    // identically otherwise, and only one of them is worth acting on.
    wire::InventoryResponse {
        request_id: request.request_id.clone(),
        ok: false,
        reason: reason.to_string(),
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(id: &str, kind: &str, name: &str, match_kind: &str, pattern: &str) -> wire::WatchEntry {
        wire::WatchEntry {
            id: id.into(),
            kind: kind.into(),
            name: name.into(),
            match_kind: match_kind.into(),
            pattern: pattern.into(),
            label: String::new(),
        }
    }

    #[test]
    fn a_watch_list_replaces_rather_than_merges() {
        let mut host = Host::new();
        host.set_watchlist(&wire::WatchList {
            entries: vec![entry("a", "unit", "nginx.service", "", "")],
        });
        assert!(host.watching_units());

        host.set_watchlist(&wire::WatchList { entries: Vec::new() });
        // Authoritative, exactly like a Sync: an entry that stops appearing
        // stops being watched, with no removal to go missing.
        assert!(!host.watching_units());
        assert!(!host.watching_processes());
    }

    #[test]
    fn entries_this_build_cannot_make_sense_of_are_dropped() {
        let mut host = Host::new();
        let kept = host.set_watchlist(&wire::WatchList {
            entries: vec![
                entry("a", "unit", "nginx.service", "", ""),
                // A kind from a later Controller.
                entry("b", "socket-activation", "x", "", ""),
                // A match this build has never heard of.
                entry("c", "process", "", "regex", "^ngin"),
                // A unit entry with no name, and a process entry with no
                // pattern: both would watch everything or nothing, and
                // guessing which is how an agent starts watching the wrong
                // thing.
                entry("d", "unit", "", "", ""),
                entry("e", "process", "", "cmdline", ""),
                entry("", "process", "", "cmdline", "worker"),
            ],
        });
        assert_eq!(kept, 1);
        assert!(host.watching_units());
        assert!(!host.watching_processes());
    }

    /// Both of these refuse *before* anything reads `/proc`, which is why they
    /// can be checks rather than an integration test: no test in this repo may
    /// depend on what happens to be running on the machine it is run on.
    #[tokio::test]
    async fn a_process_verb_that_needs_a_launcher_is_refused_with_a_reason() {
        let mut host = Host::new();
        host.set_watchlist(&wire::WatchList {
            entries: vec![entry("w1", "process", "", "cmdline", "nothing-matches-this")],
        });
        let refusal = host.signal_watch("w1", "start", None).await.unwrap_err();
        assert!(refusal.contains("no recorded way to be launched"), "{refusal}");
        // `restart` is the same refusal for the same reason, and it is the one
        // an operator will actually try.
        assert!(host.signal_watch("w1", "restart", None).await.is_err());
    }

    #[tokio::test]
    async fn a_unit_nobody_selected_cannot_be_operated_on() {
        // The bound on the polkit rule, and the reason it can be granted for
        // every unit: the rule says *this account may manage units*, and this
        // says *only the ones an operator selected*. Refused before the bus is
        // dialled, so a machine with no systemd fails the same way.
        let mut host = Host::new();
        host.set_watchlist(&wire::WatchList {
            entries: vec![entry("a", "unit", "nginx.service", "", "")],
        });

        let refusal = host.act("unit", "sshd.service", "stop", None).await.unwrap_err();
        assert!(refusal.contains("not watching"), "{refusal}");
        assert!(refusal.contains("watch list"), "{refusal}");
    }

    #[tokio::test]
    async fn signalling_an_unknown_watch_is_refused_rather_than_broadcast() {
        let mut host = Host::new();
        // The failure this guards against is the worst one available: a stale
        // or unknown id must never fall through to "signal everything".
        assert!(host.signal_watch("nope", "kill", None).await.is_err());
    }
}
