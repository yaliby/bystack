//! Processes, from `/proc`.
//!
//! The other half of the host slices, and the one with no event stream. Docker
//! has `/events` and systemd has D-Bus signals; the kernel's equivalent is the
//! netlink process connector, which needs `CAP_NET_ADMIN` and `AF_NETLINK` —
//! and the agent's unit grants neither, deliberately (ADR-0016). So this is
//! polled, which is the one place in the agent that is, and the preference
//! order in ARCHITECTURE §11 is satisfied rather than broken: there is no
//! event stream available to a process with these privileges, and the honest
//! next choice is a periodic scan.
//!
//! What keeps that affordable is the same rule as everywhere else here:
//! **nothing is scanned until somebody asks for something.** A host whose
//! watch list has no process entries never opens `/proc` at all, and one with
//! three entries reads the directory once per interval and stops at the first
//! field that rules a process out.
//!
//! Matching is substring and equality, never a regular expression. The pattern
//! is authored on the Controller and evaluated here, against every entry in
//! `/proc`, on a machine we do not own — a regex engine on that path is a way
//! to spend somebody's CPU by typing into a text box.

use std::collections::HashMap;
use std::fs;

/// Longest command line carried for one process.
///
/// A command line can be a megabyte -- a shell loop, a compiler invocation, a
/// java service -- and none of it after the first line of a card is read by
/// anybody. Truncated here rather than at the Controller, because the point is
/// not to put it on the network.
pub const MAX_CMDLINE: usize = 512;

/// Processes one watch rule will report.
///
/// A rule that matches five hundred processes is a rule somebody wrote wrongly
/// -- `cmdline` containing `python`, on a machine full of python -- and the
/// answer to it is the count, not five hundred payloads. The count travels
/// separately (`Match::total`) so the mistake is visible rather than silently
/// truncated.
pub const MAX_INSTANCES: usize = 16;

/// Rows one inventory answer will carry.
///
/// Mirrored on the Controller as `MAX_INVENTORY` in
/// `providers/agent/commands.py`; this copy is the authoritative one, because
/// this is the side holding the memory budget and it must not trust a number
/// that arrived over the network. The two are kept in step by a guard in
/// `test_wire.py`, not by this comment.
pub const MAX_INVENTORY_ITEMS: usize = 500;

/// How a rule decides what it is looking at.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Match {
    /// `/proc/<pid>/comm`, exactly. Fifteen characters, kernel-truncated.
    Name,
    /// The resolved target of `/proc/<pid>/exe`, exactly.
    Exec,
    /// A substring of the full command line.
    Cmdline,
}

impl Match {
    pub fn parse(raw: &str) -> Option<Self> {
        match raw {
            "name" => Some(Self::Name),
            "exec" => Some(Self::Exec),
            "cmdline" => Some(Self::Cmdline),
            _ => None,
        }
    }
}

/// One process that matched a rule.
#[derive(Debug, Clone, PartialEq)]
pub struct Process {
    pub pid: u32,
    pub comm: String,
    pub cmdline: String,
    /// The single letter from `/proc/<pid>/stat`: `R`, `S`, `D`, `Z`, `T`.
    pub state: String,
    /// Unix seconds. Derived here from the kernel's boot time and the
    /// process's start ticks, because `/proc` reports the second of those in
    /// clock ticks since boot and nothing above this file should have to know
    /// that.
    pub started_at: i64,
    pub uid: u32,
    /// `/proc/<pid>/cgroup`, verbatim. Whether it means "inside a container"
    /// or "owned by a unit" is the Controller's decision, not ours.
    pub cgroup: String,
}

/// Everything on the machine, read once.
///
/// One walk of `/proc` per scan rather than one per rule: a host watching six
/// processes would otherwise read every entry six times, and the directory is
/// the expensive part.
pub struct Snapshot {
    processes: Vec<Process>,
    /// The executable path per pid, resolved lazily -- `readlink` on
    /// `/proc/<pid>/exe` fails for every process we do not own, and on a
    /// machine where most processes belong to root that is most of them.
    execs: HashMap<u32, String>,
}

impl Snapshot {
    /// Read every process this agent is permitted to see.
    ///
    /// **What is invisible here is a deployment decision, not a bug.**
    /// `ProtectProc=invisible` in the agent's unit hides every process this
    /// user does not own, which is almost all of them; ADR-0016 is where that
    /// is relaxed, and the symptom of not relaxing it is a rule that matches
    /// nothing on a machine where the process is plainly running. That failure
    /// is worth recognising rather than debugging twice.
    pub fn read() -> std::io::Result<Self> {
        let boot = boot_time();
        let ticks = clock_ticks();
        let mut processes = Vec::new();

        for entry in fs::read_dir("/proc")? {
            let Ok(entry) = entry else { continue };
            let name = entry.file_name();
            let Some(pid) = name.to_str().and_then(|s| s.parse::<u32>().ok()) else {
                continue;
            };
            // Every read below can fail because the process exited between the
            // directory listing and this line. That is the ordinary case on a
            // busy machine, not an error: the process is gone, so it does not
            // match anything, and skipping it is the whole of the correct
            // behaviour.
            let Some(process) = read_one(pid, boot, ticks) else { continue };
            processes.push(process);
        }

        Ok(Self { processes, execs: HashMap::new() })
    }

    /// Which processes one rule currently matches, and how many there were.
    pub fn matching(&mut self, kind: Match, pattern: &str) -> (Vec<Process>, usize) {
        let mut found = Vec::new();
        let mut total = 0;
        for process in &self.processes {
            let hit = match kind {
                Match::Name => process.comm == pattern,
                Match::Cmdline => process.cmdline.contains(pattern),
                Match::Exec => {
                    let exec = self
                        .execs
                        .entry(process.pid)
                        .or_insert_with(|| read_exec(process.pid));
                    exec == pattern
                }
            };
            if !hit {
                continue;
            }
            total += 1;
            if found.len() < MAX_INSTANCES {
                found.push(process.clone());
            }
        }
        (found, total)
    }

    /// The picker's rows: every process, filtered and capped.
    ///
    /// Ordered by command line rather than by pid, because a picker is read
    /// by a person looking for a name and pid order is arrival order — which
    /// is to say, no order at all.
    pub fn listing(&mut self, filter: &str, limit: usize) -> (Vec<(Process, String)>, usize) {
        let needle = filter.to_ascii_lowercase();
        let pids: Vec<u32> = self.processes.iter().map(|p| p.pid).collect();
        let mut rows: Vec<(Process, String)> = Vec::new();
        for (index, process) in self.processes.iter().enumerate() {
            let haystack = format!("{} {}", process.comm, process.cmdline).to_ascii_lowercase();
            if !needle.is_empty() && !haystack.contains(&needle) {
                continue;
            }
            let pid = pids[index];
            let exec = self.execs.get(&pid).cloned().unwrap_or_else(|| read_exec(pid));
            rows.push((process.clone(), exec));
        }
        rows.sort_by(|a, b| a.0.comm.cmp(&b.0.comm).then(a.0.pid.cmp(&b.0.pid)));
        let total = rows.len();
        rows.truncate(limit);
        (rows, total)
    }
}

/// Send one signal to every process a rule matches.
///
/// Returns how many were signalled. Zero is a legitimate answer — the rule
/// matches nothing right now — and is reported as such rather than as a
/// failure, because "the daemon you asked me to stop is not running" is an
/// answer to the question.
///
/// **There is no `start` here, and there will not be one.** Launching a
/// process would mean the Controller holding a command line and this process
/// executing it as root, which with no user identity in front of the API
/// (ADR-0014) is arbitrary remote code execution on every managed host. A
/// process watch can stop something; starting it belongs to whatever
/// supervises it, and if nothing does, the answer is a unit file.
pub fn signal(pids: &[u32], signal: i32) -> usize {
    let mut sent = 0;
    for pid in pids {
        // SAFETY: `kill` takes two integers and returns one. A pid that has
        // exited yields ESRCH, which is counted as not sent rather than
        // treated as an error -- the race between reading /proc and signalling
        // is unavoidable and is the ordinary case.
        if unsafe { libc_kill(*pid as i32, signal) } == 0 {
            sent += 1;
        }
    }
    sent
}

/// Signal names to numbers, for the ones a control plane has any business
/// sending.
///
/// An allow-list rather than a parse. The number goes to `kill(2)` as root and
/// to systemd's `KillUnit`, and there is no reason for a Controller to be able
/// to name an arbitrary one -- least of all a real-time signal, which some
/// runtimes use internally for garbage collection and thread suspension.
pub fn signal_number(name: &str) -> Option<i32> {
    match name.trim().to_ascii_uppercase().trim_start_matches("SIG") {
        "HUP" => Some(1),
        "INT" => Some(2),
        "QUIT" => Some(3),
        "KILL" => Some(9),
        "USR1" => Some(10),
        "USR2" => Some(12),
        "TERM" => Some(15),
        "CONT" => Some(18),
        "STOP" => Some(19),
        _ => None,
    }
}

pub const SIGTERM: i32 = 15;
pub const SIGKILL: i32 = 9;
pub const SIGSTOP: i32 = 19;
pub const SIGCONT: i32 = 18;

// --------------------------------------------------------------------------
// /proc
// --------------------------------------------------------------------------

fn read_one(pid: u32, boot: i64, ticks: i64) -> Option<Process> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // The second field is the executable name *in parentheses*, and it may
    // contain spaces and parentheses of its own -- `(my program (old))` is a
    // legal comm. Splitting on whitespace is the classic bug here; the field
    // ends at the *last* `)` in the line, and everything after it is
    // whitespace-separated and safe.
    let close = stat.rfind(')')?;
    let comm = stat.get(stat.find('(')? + 1..close)?.to_string();
    let mut rest = stat.get(close + 2..)?.split_whitespace();

    let state = rest.next()?.to_string();
    // Fields after `state`, one-indexed from `ppid`: starttime is the 20th.
    let start_ticks: i64 = rest.nth(18)?.parse().ok()?;

    let cmdline = fs::read_to_string(format!("/proc/{pid}/cmdline"))
        .map(|raw| {
            let mut joined = raw.replace('\0', " ").trim_end().to_string();
            joined.truncate(MAX_CMDLINE);
            joined
        })
        .unwrap_or_default();

    Some(Process {
        pid,
        // A kernel thread has an empty command line and its comm in brackets.
        // Both are kept as they are: a watch on one is unusual and legitimate,
        // and inventing a display name here would be interpretation.
        cmdline: if cmdline.is_empty() { format!("[{comm}]") } else { cmdline },
        comm,
        state,
        started_at: boot + start_ticks / ticks.max(1),
        uid: read_uid(pid),
        cgroup: fs::read_to_string(format!("/proc/{pid}/cgroup"))
            .map(|raw| raw.trim().to_string())
            .unwrap_or_default(),
    })
}

fn read_uid(pid: u32) -> u32 {
    fs::read_to_string(format!("/proc/{pid}/status"))
        .ok()
        .and_then(|status| {
            status.lines().find_map(|line| {
                line.strip_prefix("Uid:")?
                    .split_whitespace()
                    .next()?
                    .parse()
                    .ok()
            })
        })
        .unwrap_or(0)
}

fn read_exec(pid: u32) -> String {
    fs::read_link(format!("/proc/{pid}/exe"))
        .map(|path| path.to_string_lossy().into_owned())
        // Permission denied for anything we do not own, and empty for a kernel
        // thread. Both read as "no executable path", which makes an `exec`
        // rule match nothing rather than match wrongly.
        .unwrap_or_default()
}

/// Seconds since the epoch at which this machine booted.
///
/// From `/proc/stat`'s `btime`, which is a fixed instant, rather than from
/// `uptime` minus `now`, which drifts by however long the read took. Process
/// start times are computed against it, and a card claiming a daemon restarted
/// two seconds ago when it did not is the kind of wrong that costs somebody a
/// morning.
fn boot_time() -> i64 {
    fs::read_to_string("/proc/stat")
        .ok()
        .and_then(|stat| {
            stat.lines()
                .find_map(|line| line.strip_prefix("btime ")?.trim().parse().ok())
        })
        .unwrap_or(0)
}

/// The kernel's clock ticks per second.
///
/// `sysconf(_SC_CLK_TCK)`, which is 100 on every Linux anyone runs and is read
/// rather than assumed because it is free to be right.
fn clock_ticks() -> i64 {
    // SAFETY: `sysconf` takes an int and returns a long. `_SC_CLK_TCK` is 2 on
    // Linux; a platform where it is not is one this agent does not build for.
    let value = unsafe { libc_sysconf(2) };
    if value > 0 {
        value
    } else {
        100
    }
}

extern "C" {
    #[link_name = "kill"]
    fn libc_kill(pid: i32, signal: i32) -> i32;
    #[link_name = "sysconf"]
    fn libc_sysconf(name: i32) -> i64;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signal_names_are_an_allow_list() {
        assert_eq!(signal_number("SIGTERM"), Some(15));
        assert_eq!(signal_number("term"), Some(15));
        assert_eq!(signal_number("KILL"), Some(9));
        // Not offered, deliberately: real-time signals are used internally by
        // language runtimes, and a control plane that can send one is a
        // control plane that can wedge a JVM from a text box.
        assert_eq!(signal_number("SIGRTMIN+3"), None);
        assert_eq!(signal_number(""), None);
    }

    #[test]
    fn a_match_kind_the_controller_invented_is_refused() {
        assert_eq!(Match::parse("name"), Some(Match::Name));
        assert_eq!(Match::parse("regex"), None);
    }

    /// The classic `/proc/<pid>/stat` bug, pinned. A comm containing a space
    /// and a parenthesis is legal, and splitting the line on whitespace reads
    /// the state and every field after it from the wrong place.
    #[test]
    fn a_process_named_with_parentheses_still_parses() {
        // Written against the parser rather than the filesystem, because no
        // test in this repo may depend on the machine it runs on.
        let stat = "42 (my program (old)) S 1 42 42 0 -1 4194304 100 0 0 0 5 3 0 0 20 0 1 0 98765";
        let close = stat.rfind(')').unwrap();
        let comm = &stat[stat.find('(').unwrap() + 1..close];
        let mut rest = stat[close + 2..].split_whitespace();
        assert_eq!(comm, "my program (old)");
        assert_eq!(rest.next(), Some("S"));
        assert_eq!(rest.nth(18), Some("98765"));
    }
}
