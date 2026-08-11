//! systemd's manager, over D-Bus.
//!
//! The unit half of the host slices. Everything here is a thin translation of
//! `org.freedesktop.systemd1` into the structs `wire.rs` puts on the network —
//! it decides nothing, exactly as `docker.rs` decides nothing, because the
//! Controller is where a `LoadState` becomes a `not-found` card and a name
//! becomes a URN (ADR-0009 §1).
//!
//! **Only what is watched is ever asked about.** There is no periodic
//! enumeration of a machine's units anywhere in this file: [`Systemd::state`]
//! is called once per entry in the Controller's watch list, and
//! [`Systemd::list_units`] runs only when an operator has opened the picker.
//! A host that watches nothing pays for nothing, which is the property
//! ARCHITECTURE §12 asks of anything that lives next to a socket like this.

use crate::dbus::{Arg, Bus, DbusError, Value};

const MANAGER: &str = "org.freedesktop.systemd1";
const MANAGER_PATH: &str = "/org/freedesktop/systemd1";
const MANAGER_IFACE: &str = "org.freedesktop.systemd1.Manager";
const UNIT_IFACE: &str = "org.freedesktop.systemd1.Unit";
const SERVICE_IFACE: &str = "org.freedesktop.systemd1.Service";
const PROPERTIES: &str = "org.freedesktop.DBus.Properties";

/// How a job is queued. `replace` is what `systemctl` sends and what an
/// operator means: do this now, cancelling whatever conflicting job is
/// pending. `fail` would refuse when something else was already in flight,
/// which turns a busy moment into an error nobody can act on.
const MODE: &str = "replace";

/// One watched unit, as systemd describes it.
///
/// Field for field what the wire carries, and in systemd's own vocabulary. The
/// three-state split (`load`, `active`, `sub`) is kept rather than flattened
/// for the reason the `.proto` records: `active`/`exited` and `active`/
/// `running` are a finished oneshot and a live daemon, and an operator
/// diagnosing one needs to know which they are looking at.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct UnitState {
    pub name: String,
    pub description: String,
    pub load_state: String,
    pub active_state: String,
    pub sub_state: String,
    pub unit_file_state: String,
    pub main_pid: u32,
    /// `ActiveEnterTimestamp`, in systemd's microseconds. Converted by the
    /// Controller, which is the side that knows what anything means.
    pub active_enter_usec: u64,
    pub n_restarts: u32,
    pub result: String,
    pub exec_main_status: u32,
    pub fragment_path: String,
}

impl UnitState {
    /// What to report for a unit systemd will not load.
    ///
    /// A watch on a unit that is not installed is legitimate and common — an
    /// operator adds `nginx.service` before installing nginx, or mistypes it —
    /// and the answer has to be a *state*, not an absence. A missing entity
    /// would leave a card that silently never appears, which reads as "I never
    /// added it" rather than as "it is not installed".
    pub fn not_found(name: &str) -> Self {
        Self {
            name: name.to_string(),
            load_state: "not-found".into(),
            active_state: "inactive".into(),
            sub_state: "dead".into(),
            ..Self::default()
        }
    }
}

/// One row of the picker.
#[derive(Debug, Clone, PartialEq)]
pub struct UnitListing {
    pub name: String,
    pub description: String,
    pub active_state: String,
    pub sub_state: String,
}

/// What systemd said about a lifecycle request.
///
/// `Unchanged` has no equivalent on the bus — systemd answers a `StartUnit` on
/// an active unit with a job that completes immediately, not with Docker's
/// `304`. It is *derived*, by reading the state before acting, because the
/// distinction is worth keeping end to end: a restart storm that changed
/// nothing must not report as a restart storm that worked.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Applied {
    Applied,
    Unchanged,
}

pub struct Systemd {
    bus: Bus,
}

impl Systemd {
    pub async fn connect() -> Result<Self, DbusError> {
        Ok(Self { bus: Bus::connect().await? })
    }

    /// Ask systemd to emit state changes, and ask the bus to deliver them.
    ///
    /// Two calls because they answer different questions. `Subscribe` tells
    /// *systemd* to bother emitting signals at all — it does not, otherwise,
    /// unless somebody is listening. `AddMatch` tells the *bus daemon* to
    /// route them to us, and without it the daemon filters out every broadcast
    /// as uninteresting to this connection.
    ///
    /// The match is deliberately broad: every property change from systemd,
    /// not one rule per watched unit. A narrow match would have to be torn
    /// down and rebuilt on every watch-list edit, and would miss a unit that
    /// did not exist when the rule was written — which is exactly the unit
    /// somebody is watching *for*. The cost of the broad rule is wakeups on a
    /// busy machine, and it is bounded where it matters: the burst is
    /// coalesced, and what follows is a re-read of the watched units only.
    pub async fn subscribe(&mut self) -> Result<(), DbusError> {
        self.bus
            .call(MANAGER, MANAGER_PATH, MANAGER_IFACE, "Subscribe", &[])
            .await?;
        let matched = self
            .bus
            .call(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "AddMatch",
                &[Arg::Str(
                    "type='signal',sender='org.freedesktop.systemd1',\
                     interface='org.freedesktop.DBus.Properties',member='PropertiesChanged'",
                )],
            )
            .await;
        // A direct connection to PID 1 has no bus daemon to add a match to,
        // and refusing the whole subscription over that would disable the
        // watch on precisely the minimal images the private socket exists for.
        // Signals arrive there unfiltered anyway, so the refusal is ignored
        // and anything worse than a refusal is not.
        if let Err(error) = matched {
            if !matches!(error, DbusError::Call { .. }) {
                return Err(error);
            }
        }
        Ok(())
    }

    /// Block until systemd says something changed.
    pub async fn next_change(&mut self) -> Result<(), DbusError> {
        self.bus.next_signal().await.map(drop)
    }

    // -- reads -------------------------------------------------------------

    /// Everything about one unit, whether or not it exists.
    ///
    /// `LoadUnit` rather than `GetUnit`, and the difference is the whole
    /// feature: `GetUnit` fails for a unit that is installed but has never
    /// been started, and a watch that could only see units systemd already
    /// had in memory would go blind on exactly the service somebody is waiting
    /// to come up.
    pub async fn state(&mut self, name: &str) -> Result<UnitState, DbusError> {
        let path = match self
            .bus
            .call(MANAGER, MANAGER_PATH, MANAGER_IFACE, "LoadUnit", &[Arg::Str(name)])
            .await
        {
            Ok(message) => message
                .body
                .first()
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            // `NoSuchUnit`, or a name systemd will not even parse. An answer
            // about a unit, not about the machine, so it is reported as one.
            Err(DbusError::Call { .. }) => return Ok(UnitState::not_found(name)),
            Err(other) => return Err(other),
        };
        if path.is_empty() {
            return Ok(UnitState::not_found(name));
        }

        let properties = self
            .bus
            .call(MANAGER, &path, PROPERTIES, "GetAll", &[Arg::Str(UNIT_IFACE)])
            .await?;
        let all = properties.body.first().cloned().unwrap_or(Value::Array(Vec::new()));

        let mut state = UnitState {
            // systemd's own `Id` rather than the name we asked with, so an
            // alias resolves to the thing it points at instead of producing
            // two cards for one unit.
            name: text(&all, "Id").unwrap_or_else(|| name.to_string()),
            description: text(&all, "Description").unwrap_or_default(),
            load_state: text(&all, "LoadState").unwrap_or_default(),
            active_state: text(&all, "ActiveState").unwrap_or_default(),
            sub_state: text(&all, "SubState").unwrap_or_default(),
            unit_file_state: text(&all, "UnitFileState").unwrap_or_default(),
            active_enter_usec: number(&all, "ActiveEnterTimestamp"),
            fragment_path: text(&all, "FragmentPath").unwrap_or_default(),
            ..UnitState::default()
        };

        // The failure counters live on the type-specific interface, and only
        // services have one. Asked for individually rather than with a second
        // `GetAll`: a service's property set includes every `ExecStart` line
        // with its full argument vector, which is kilobytes of nested structs
        // to carry four numbers.
        if state.name.ends_with(".service") {
            state.main_pid = self.property(&path, SERVICE_IFACE, "MainPID").await;
            state.n_restarts = self.property(&path, SERVICE_IFACE, "NRestarts").await;
            state.exec_main_status = self.property(&path, SERVICE_IFACE, "ExecMainStatus").await;
            state.result = self
                .text_property(&path, SERVICE_IFACE, "Result")
                .await
                .unwrap_or_default();
        }
        Ok(state)
    }

    /// Every loaded unit, plus every installed one that is not.
    ///
    /// Both, because either alone is a picker somebody cannot find their
    /// service in. `ListUnits` knows about units systemd currently holds in
    /// memory — which excludes an installed service that has never run — and
    /// `ListUnitFiles` knows about files on disk, which excludes everything
    /// generated at runtime (`.mount` units, `.scope`s, anything from a
    /// generator). The union is the answer to "what could I watch here".
    pub async fn list_units(&mut self, filter: &str, limit: usize) -> Result<(Vec<UnitListing>, usize), DbusError> {
        let mut found: Vec<UnitListing> = Vec::new();
        let mut seen: Vec<String> = Vec::new();

        let loaded = self
            .bus
            .call(MANAGER, MANAGER_PATH, MANAGER_IFACE, "ListUnits", &[])
            .await?;
        if let Some(Value::Array(rows)) = loaded.body.first() {
            for row in rows {
                let Value::Struct(fields) = row else { continue };
                let name = fields.first().and_then(Value::as_str).unwrap_or_default();
                if name.is_empty() || !matches(name, filter) {
                    continue;
                }
                seen.push(name.to_string());
                found.push(UnitListing {
                    name: name.to_string(),
                    description: field_text(fields, 1),
                    active_state: field_text(fields, 3),
                    sub_state: field_text(fields, 4),
                });
            }
        }

        let files = self
            .bus
            .call(MANAGER, MANAGER_PATH, MANAGER_IFACE, "ListUnitFiles", &[])
            .await?;
        if let Some(Value::Array(rows)) = files.body.first() {
            for row in rows {
                let Value::Struct(fields) = row else { continue };
                let path = fields.first().and_then(Value::as_str).unwrap_or_default();
                let name = path.rsplit('/').next().unwrap_or_default();
                if name.is_empty() || !matches(name, filter) || seen.iter().any(|s| s == name) {
                    continue;
                }
                found.push(UnitListing {
                    name: name.to_string(),
                    description: String::new(),
                    // Not loaded, so it has no runtime state at all. Reported
                    // as `inactive` rather than empty, because that is what it
                    // is: a unit systemd knows how to start and has not.
                    active_state: "inactive".into(),
                    sub_state: field_text(fields, 1),
                });
            }
        }

        found.sort_by(|a, b| a.name.cmp(&b.name));
        let total = found.len();
        found.truncate(limit);
        Ok((found, total))
    }

    // -- lifecycle ---------------------------------------------------------

    /// Start, stop, restart or kill one unit.
    ///
    /// The verb set is the Controller's `CommandKind`, minus the two that mean
    /// nothing here: systemd has no notion of pausing a unit, and offering one
    /// would be a button whose only outcome is an error. The mapping is
    /// otherwise one to one, which is why this feature needed no new verb and
    /// therefore does not touch ADR-0014.
    pub async fn act(
        &mut self,
        name: &str,
        verb: &str,
        signal: i32,
    ) -> Result<Applied, DbusError> {
        // Read before acting, so "already running" can be reported as the
        // no-op it is. One extra round trip against a local socket, for a
        // distinction the audit log keeps forever.
        let before = self.state(name).await?.active_state;
        let method = match verb {
            "start" if before == "active" => return Ok(Applied::Unchanged),
            "stop" if before == "inactive" || before == "failed" => return Ok(Applied::Unchanged),
            "start" => "StartUnit",
            "stop" => "StopUnit",
            "restart" => "RestartUnit",
            "kill" => "KillUnit",
            other => {
                return Err(DbusError::Call {
                    name: "bystack.UnsupportedVerb".into(),
                    message: format!("{other:?} means nothing to a unit"),
                })
            }
        };

        if method == "KillUnit" {
            self.bus
                .call(
                    MANAGER,
                    MANAGER_PATH,
                    MANAGER_IFACE,
                    "KillUnit",
                    // `all`: the whole cgroup, not just the main process. A
                    // service whose main pid is dead but whose children are
                    // not is the state an operator reaches for `kill` in.
                    &[Arg::Str(name), Arg::Str("all"), Arg::I32(signal)],
                )
                .await?;
        } else {
            self.bus
                .call(
                    MANAGER,
                    MANAGER_PATH,
                    MANAGER_IFACE,
                    method,
                    &[Arg::Str(name), Arg::Str(MODE)],
                )
                .await?;
        }
        Ok(Applied::Applied)
    }

    // -- internals ---------------------------------------------------------

    async fn property(&mut self, path: &str, interface: &str, name: &str) -> u32 {
        // A property that is missing reads as zero rather than as a failure.
        // `NRestarts` did not exist before systemd 235, and a host running one
        // of those should report a unit with no restart count -- not no unit.
        self.bus
            .call(MANAGER, path, PROPERTIES, "Get", &[Arg::Str(interface), Arg::Str(name)])
            .await
            .ok()
            .and_then(|message| message.body.first().and_then(Value::as_u64))
            .unwrap_or(0) as u32
    }

    async fn text_property(&mut self, path: &str, interface: &str, name: &str) -> Option<String> {
        self.bus
            .call(MANAGER, path, PROPERTIES, "Get", &[Arg::Str(interface), Arg::Str(name)])
            .await
            .ok()
            .and_then(|message| {
                message.body.first().and_then(Value::as_str).map(str::to_string)
            })
    }
}

fn text(all: &Value, key: &str) -> Option<String> {
    all.field(key).and_then(Value::as_str).map(str::to_string)
}

fn number(all: &Value, key: &str) -> u64 {
    all.field(key).and_then(Value::as_u64).unwrap_or(0)
}

fn field_text(fields: &[Value], index: usize) -> String {
    fields.get(index).and_then(Value::as_str).unwrap_or_default().to_string()
}

/// The picker's filter, applied here rather than in the browser.
///
/// A 2,000-unit host must not put its whole unit table on somebody's home
/// uplink so that a text box can discard 1,990 of them.
///
/// **Both sides are folded here**, rather than the needle being folded once by
/// the caller. That is two allocations per unit on a path that runs when a
/// person opens a dialog, against a contract — "pass me something already
/// lowercase" — that nothing enforces and that was already wrong once: the
/// check below caught it.
fn matches(name: &str, needle: &str) -> bool {
    needle.is_empty()
        || name
            .to_ascii_lowercase()
            .contains(&needle.to_ascii_lowercase())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_unit_that_will_not_load_is_a_state_rather_than_an_absence() {
        let state = UnitState::not_found("nginx.service");
        assert_eq!(state.name, "nginx.service");
        assert_eq!(state.load_state, "not-found");
        // Reported as inactive as well, because it is: the Controller
        // substitutes `not-found` for the display state, and a card that read
        // `active` here would be the worst of both.
        assert_eq!(state.active_state, "inactive");
    }

    #[test]
    fn the_filter_is_case_insensitive_and_empty_means_everything() {
        assert!(matches("NetworkManager.service", "networkmanager"));
        assert!(matches("nginx.service", "NGINX"));
        assert!(matches("anything.service", ""));
        assert!(!matches("nginx.service", "apache"));
    }
}
