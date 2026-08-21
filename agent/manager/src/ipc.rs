//! The two files the Controller and root say things to each other with.
//!
//! Files rather than a socket, and that is a decision rather than the lazy
//! option. A socket needs a listener, and the listener would have to be the
//! root process — which means a root daemon resident on the box for the
//! 99.99% of its life when nobody is updating anything, accepting connections,
//! parsing frames. What is actually being communicated here is one sentence a
//! few times a year. A file that a `.path` unit watches costs no resident
//! process, no protocol and no parser beyond the twenty lines below.
//!
//! Both documents are spelled the way the manifest is — a magic line, then
//! `key value` — and parsed the same strict way: unknown keys are an error.
//! Not because these are signed (they are not, and nothing here is trusted for
//! being in them), but because the same discipline in the same tree means one
//! set of habits. A field added later arrives as `bystack-intent/2`, and the
//! release that bumps it is the release that teaches both ends to read it.
//!
//! ## What the intent is trusted for, which is almost nothing
//!
//! The Controller writes it, so it is written by the process this feature
//! exists to replace — including the version of it that has already been
//! compromised. It is trusted for exactly one thing: **that somebody asked.**
//!
//! The version in it is not trusted; it is validated as a version and then
//! used to compose a URL under a compiled-in repository, and what comes back
//! is refused unless it is signed by a key compiled into *this* binary and
//! names a version higher than what is installed. The health URL is not
//! trusted either — it is required to be loopback, because a health check that
//! could be pointed at someone else's server is a probation that always
//! passes.
//!
//! So the worst a compromised Controller can do through this channel is cause
//! its own host to install a genuine, current, correctly signed Controller.
//! That is the same property ADR-0017 gives the Controller over the fleet,
//! pointed the other way.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

const INTENT_MAGIC: &str = "bystack-intent/1";
const STATUS_MAGIC: &str = "bystack-status/1";

/// The most either document may be. Both are five short lines.
const MAX_DOCUMENT: u64 = 4 * 1024;

// --------------------------------------------------------------------------
// What the Controller asks for
// --------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Intent {
    /// The release to install. Empty means "whatever is newest", which the
    /// manager resolves for itself — the Controller makes no outbound HTTP
    /// calls and is not about to start for this.
    pub version: String,
    /// Where to ask whether the new Controller came up. Loopback or refused.
    pub health: String,
    pub requested_at: i64,
}

impl Intent {
    pub fn parse(text: &str) -> Result<Self, String> {
        let fields = parse_document(text, INTENT_MAGIC, &["version", "health", "requested_at"])?;
        let version = fields.get("version").cloned().unwrap_or_default();
        if !version.is_empty() && !bystack_release::is_version(&version) {
            return Err(format!(
                "{version:?} is not a version. This is pasted into a release URL, so it is \
                 checked for being one before it goes anywhere near a command line."
            ));
        }
        let health = fields.get("health").cloned().unwrap_or_default();
        if !health.is_empty() {
            check_loopback(&health)?;
        }
        Ok(Self {
            version,
            health,
            requested_at: fields
                .get("requested_at")
                .and_then(|value| value.parse().ok())
                .unwrap_or(0),
        })
    }

    /// The bytes the Controller writes. Here rather than in Python so the two
    /// ends of this file cannot be edited apart; `test_manager.py` reads it
    /// out of this source, the way `test_wire.py` reads the manifest magic.
    pub fn render(&self) -> String {
        format!(
            "{INTENT_MAGIC}\nversion {}\nhealth {}\nrequested_at {}\n",
            self.version, self.health, self.requested_at
        )
    }
}

/// Whether a URL points at this machine, and nothing else.
///
/// The probation check is the only thing standing between a bad release and a
/// Controller that stays broken, and its whole claim is "the process I just
/// started answered". A URL naming any other host turns that into "some
/// server answered", which a compromised Controller could arrange to always be
/// true — so the *one* piece of the intent that decides whether a rollback
/// happens is the one piece that is pinned here.
///
/// Parsed by hand rather than by pulling in a URL crate: what has to be
/// decided is narrow, and the failure mode of a lenient parser here is a check
/// that silently stops checking.
fn check_loopback(url: &str) -> Result<(), String> {
    let rest = url
        .strip_prefix("http://")
        .or_else(|| url.strip_prefix("https://"))
        .ok_or_else(|| format!("{url:?} is not an http(s) URL"))?;
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    // `user@host` would let the host this actually reaches sit after an `@`
    // while a reader's eye stops at what is before it. There is no credential
    // to send to our own health endpoint, so the whole form is refused.
    if authority.contains('@') {
        return Err(format!("{url:?} carries userinfo; that is not a health URL"));
    }
    let host = match authority.rsplit_once(':') {
        // `[::1]:8000` -- strip the port only when what precedes it is not
        // itself part of a bracketed IPv6 literal.
        Some((head, port)) if !head.is_empty() && port.chars().all(|c| c.is_ascii_digit()) => head,
        _ => authority,
    };
    let host = host.trim_start_matches('[').trim_end_matches(']');
    if matches!(host, "127.0.0.1" | "localhost" | "::1") {
        return Ok(());
    }
    Err(format!(
        "the health URL must be on this machine, and {host:?} is not. A probation \
         check that can be pointed elsewhere is one that always passes."
    ))
}

// --------------------------------------------------------------------------
// What root reports back
// --------------------------------------------------------------------------

/// Where a run has got to. Ordered as it happens, and each one is a sentence
/// the dashboard can put under a progress bar.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Fetching,
    Verifying,
    Applying,
    Probation,
    /// The Controller is up and healthy; the fleet's artifacts are being
    /// fetched behind it. Phase two of ADR-0018, and it is reported separately
    /// because the Controller is already usable while it runs.
    Cascading,
    Success,
    Failed,
    /// The new Controller did not come back. The old one did.
    RolledBack,
}

impl Phase {
    pub fn as_str(self) -> &'static str {
        match self {
            Phase::Fetching => "fetching",
            Phase::Verifying => "verifying",
            Phase::Applying => "applying",
            Phase::Probation => "probation",
            Phase::Cascading => "cascading",
            Phase::Success => "success",
            Phase::Failed => "failed",
            Phase::RolledBack => "rolled_back",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Status {
    pub phase: Phase,
    pub version: String,
    pub detail: String,
    /// The version the fleet should now be rolled to, set only once the
    /// artifacts for it are actually on disk. The Controller reads this,
    /// checks it against its own version, and starts the rollout it already
    /// knows how to run.
    pub cascade: String,
    pub updated_at: i64,
}

impl Status {
    pub fn new(phase: Phase, version: &str, detail: &str) -> Self {
        Self {
            phase,
            version: version.to_string(),
            // One line, always. `detail` is the only free text in this file and
            // it comes from error strings that have seen a filesystem path, a
            // curl exit code and whatever systemd had to say -- a newline in
            // one of those would turn the rest of the sentence into a key the
            // reader then refuses the whole document over.
            detail: detail.replace(['\n', '\r'], " ").trim().to_string(),
            cascade: String::new(),
            updated_at: unix_time(),
        }
    }

    pub fn cascading(mut self, version: &str) -> Self {
        self.cascade = version.to_string();
        self
    }

    pub fn render(&self) -> String {
        format!(
            "{STATUS_MAGIC}\nphase {}\nversion {}\ncascade {}\nupdated_at {}\ndetail {}\n",
            self.phase.as_str(),
            self.version,
            self.cascade,
            self.updated_at,
            self.detail
        )
    }

    /// Write it where the Controller will look, atomically.
    ///
    /// Through a temporary file and a rename, because this is polled: a reader
    /// that catches a partial write sees a document with no `phase`, refuses
    /// it, and reports the update as broken at the exact moment it is going
    /// fine.
    pub fn write(&self, path: &Path) -> Result<(), String> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
        }
        let temp = path.with_file_name(format!(".{}.new", crate::layout::STATUS));
        {
            let mut file = OpenOptions::new()
                .create(true)
                .write(true)
                .truncate(true)
                // World-readable on purpose: the Controller runs as `bystack`
                // and has to read it, and there is nothing in here that is not
                // already on the dashboard.
                .mode(0o644)
                .open(&temp)
                .map_err(|e| format!("cannot write {}: {e}", temp.display()))?;
            file.write_all(self.render().as_bytes())
                .map_err(|e| format!("cannot write {}: {e}", temp.display()))?;
            file.sync_all().map_err(|e| e.to_string())?;
        }
        fs::rename(&temp, path).map_err(|e| format!("cannot publish {}: {e}", path.display()))
    }
}

// --------------------------------------------------------------------------

/// Read a document, refusing what it does not fully understand.
pub fn read_document(path: &Path) -> Result<String, String> {
    let size = fs::metadata(path)
        .map_err(|e| format!("cannot read {}: {e}", path.display()))?
        .len();
    if size > MAX_DOCUMENT {
        return Err(format!(
            "{} is {size} bytes; that is not one of these documents",
            path.display()
        ));
    }
    fs::read_to_string(path).map_err(|e| format!("cannot read {}: {e}", path.display()))
}

fn parse_document(
    text: &str,
    magic: &str,
    known: &[&str],
) -> Result<std::collections::HashMap<String, String>, String> {
    let mut lines = text.lines();
    match lines.next().map(str::trim) {
        Some(first) if first == magic => {}
        Some(other) => return Err(format!("unknown format {other:?}; this reads {magic}")),
        None => return Err("the document is empty".into()),
    }
    let mut fields = std::collections::HashMap::new();
    for line in lines {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let (key, value) = match line.split_once(char::is_whitespace) {
            Some((key, value)) => (key, value.trim()),
            // A key with no value: `health` with nothing after it is how the
            // Controller says "I have no opinion", and refusing it would make
            // the empty case the awkward one.
            None => (line, ""),
        };
        if !known.contains(&key) {
            return Err(format!("unknown key {key:?}"));
        }
        if fields.insert(key.to_string(), value.to_string()).is_some() {
            return Err(format!("the document names {key:?} twice"));
        }
    }
    Ok(fields)
}

pub fn unix_time() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_intent_round_trips() {
        let intent = Intent {
            version: "0.5.0".into(),
            health: "http://127.0.0.1:8000/api/v1/healthz".into(),
            requested_at: 1755388800,
        };
        assert_eq!(Intent::parse(&intent.render()).unwrap(), intent);
    }

    #[test]
    fn an_empty_version_means_whatever_is_newest() {
        let intent = Intent::parse("bystack-intent/1\nversion\nhealth\nrequested_at 1\n").unwrap();
        assert_eq!(intent.version, "");
        assert_eq!(intent.health, "");
    }

    #[test]
    fn a_version_that_is_not_one_is_refused_before_it_reaches_a_url() {
        let error = Intent::parse("bystack-intent/1\nversion ../../../etc\n").unwrap_err();
        assert!(error.contains("not a version"), "{error}");
    }

    /// The check that decides whether a rollback can happen at all.
    #[test]
    fn a_health_url_off_this_machine_is_refused() {
        for url in [
            "http://evil.example/healthz",
            "https://127.0.0.1.evil.example/healthz",
            "http://user@evil.example/healthz",
            "http://127.0.0.1@evil.example/healthz",
        ] {
            let document = format!("bystack-intent/1\nhealth {url}\n");
            assert!(Intent::parse(&document).is_err(), "accepted {url}");
        }
        for url in [
            "http://127.0.0.1:8000/api/v1/healthz",
            "http://localhost:8000/api/v1/healthz",
            "http://[::1]:8000/api/v1/healthz",
            "https://127.0.0.1/api/v1/healthz",
        ] {
            let document = format!("bystack-intent/1\nhealth {url}\n");
            assert!(Intent::parse(&document).is_ok(), "refused {url}");
        }
    }

    #[test]
    fn an_unknown_key_is_an_error_rather_than_something_to_skip() {
        assert!(Intent::parse("bystack-intent/1\nversion 0.5.0\nexpires_at 1\n").is_err());
    }

    #[test]
    fn a_detail_never_becomes_a_second_line() {
        let status = Status::new(Phase::Failed, "0.5.0", "cannot read /x\nversion 9.9.9");
        assert_eq!(status.render().lines().count(), 6);
        assert!(!status.detail.contains('\n'));
    }
}
