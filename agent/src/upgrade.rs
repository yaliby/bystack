//! Receiving, verifying and installing a signed release (ADR-0017).
//!
//! Three processes upgrade this agent, and **the only one with write access to
//! `/usr/local/bin` has no network**:
//!
//! ```text
//! the daemon      verifies the signature, stages into its own StateDirectory,
//!                 writes a trigger file. `bystack-agent.service` is unchanged
//!                 -- ProtectSystem=strict, empty CapabilityBoundingSet, all
//!                 of it.
//! a .path unit    sees the trigger file. No polkit, no D-Bus method, no
//!                 argument the daemon can parameterise.
//! the updater     `bystack-agent apply-update`, Type=oneshot,
//!                 PrivateNetwork=yes, the one unit in the system with
//!                 ReadWritePaths=/usr/local/bin.
//! ```
//!
//! Both halves are here because they are two ends of one argument and reading
//! either alone gets it wrong. The daemon's verification is **not the one that
//! matters**: it exists so a host refuses a bad artifact at the cheap end,
//! before writing megabytes to a disk that may be someone's root filesystem,
//! and so the refusal is reported over a live connection with a reason. The
//! updater's is the real one, and it is performed on a copy inside a
//! root-owned directory the daemon cannot reach -- because verifying in the
//! staging directory and installing from it would be two reads of a path owned
//! by `bystack-agent`, and a compromised daemon that wins the race between
//! them has just had root install its file.
//!
//! What makes any of it worth executing is a signature over a manifest,
//! checked against keys compiled into this binary (`build.rs`). The Controller
//! holds no key: it can withhold an upgrade, send an old one, or send nothing,
//! and it cannot produce a binary this agent will run.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::session::{log, unix_time};
use crate::wire::{self, AGENT_VERSION};

include!(concat!(env!("OUT_DIR"), "/signing_keys.rs"));

/// Where the daemon stages, under its own `StateDirectory`.
const STAGE_DIR: &str = "upgrade";

/// The partial artifact, while chunks are still arriving. Renamed to
/// [`STAGED`] only when it is complete and its digest matches — so a trigger
/// file can never be next to half a binary.
const INCOMING: &str = "incoming";
const STAGED: &str = "staged";
const MANIFEST: &str = "manifest";
const SIGNATURE: &str = "manifest.sig";

/// What the `.path` unit watches for.
const TRIGGER: &str = "trigger";

/// The agent's own evidence that it reached a Controller, written on every
/// accepted `HelloAck`. The rollback timer reads it; see [`note_connected`].
const CONNECTED: &str = "connected";

/// Root's own directory: the probation flag and the record of what to roll
/// back to. **Not** the daemon's `StateDirectory`, for the same reason the
/// version floor is not stored there — that directory is writable by exactly
/// the process this check exists to survive.
pub const UPDATE_DIR: &str = "/var/lib/bystack-agent-update";
const PROBATION: &str = "probation";

/// Where the updater does its work. systemd supplies it as
/// `RUNTIME_DIRECTORY`; the constant is what a manual `apply-update` gets.
const RUNTIME_DIR: &str = "/run/bystack-agent-updater";

/// What the updater replaces, and the copy it keeps.
const DEFAULT_BIN: &str = "/usr/local/bin/bystack-agent";
const PREVIOUS_SUFFIX: &str = ".prev";

/// Minutes a swapped-in agent has to reach a Controller before it is undone.
///
/// Long enough for a host to boot the new binary, dial out over whatever
/// uplink it has and be admitted; short enough that a fleet is not left on a
/// broken release while somebody is asleep. It is written into the probation
/// file as an absolute deadline rather than read by the rollback script, so
/// changing it here changes the next upgrade rather than every one in flight.
const PROBATION_MINUTES: u64 = 10;

/// The largest artifact this agent will write to disk on a Controller's say-so.
///
/// `dist/` is 2.2 MB for x86_64 and 1.8 MB for aarch64. This is a bound on
/// what a Controller can make a managed host spend, not a size anyone is near:
/// the machine may be a NAS whose root filesystem has a gigabyte free, and
/// "the Controller said so" is not a reason to fill it.
const MAX_ARTIFACT: u64 = 64 * 1024 * 1024;

/// The manifest, small by construction.
const MAX_DOCUMENT: u64 = 4 * 1024;

// --------------------------------------------------------------------------
// The signed document
// --------------------------------------------------------------------------

/// What a signature covers: `{name, version, arch, sha256, released_at}`.
///
/// A text document rather than a protobuf message, because **the bytes are the
/// contract**. Protobuf serialization is not canonical — field order and
/// varint width are the encoder's business — so a signer and a verifier
/// building the same message can produce different bytes, and a signature over
/// "the message" would be a signature over whichever encoder ran first. This
/// is parsed from exactly the bytes that were signed and exactly the bytes
/// that crossed the wire.
///
/// ```text
/// bystack-manifest/1
/// name bystack-agent
/// version 0.4.0
/// arch x86_64
/// sha256 6f1b…
/// released_at 1755388800
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Manifest {
    pub name: String,
    pub version: String,
    pub arch: String,
    pub sha256: [u8; 32],
    pub released_at: i64,
}

/// The first line, and the only place a format change may be announced.
const MAGIC: &str = "bystack-manifest/1";

/// What this agent is. A manifest naming anything else is refused, because a
/// key that ever signs a second artifact must not let one be presented as the
/// other.
const ARTIFACT_NAME: &str = "bystack-agent";

impl Manifest {
    /// Parse, refusing anything not fully understood.
    ///
    /// **Unknown keys are an error, not something to skip.** Skipping is the
    /// forgiving choice and the wrong one here: a field added in a later
    /// format because it carries a constraint — an expiry, a target list — is
    /// a field an older agent must not quietly ignore while accepting the
    /// signature over it. The way this document grows is `bystack-manifest/2`
    /// and a release, which is the same shape as key rotation and for the same
    /// reason.
    pub fn parse(bytes: &[u8]) -> Result<Self, String> {
        let text = std::str::from_utf8(bytes).map_err(|_| "the manifest is not UTF-8")?;
        let mut lines = text.lines();
        match lines.next().map(str::trim) {
            Some(MAGIC) => {}
            Some(other) => {
                return Err(format!("unknown manifest format {other:?}; this agent reads {MAGIC}"))
            }
            None => return Err("the manifest is empty".into()),
        }

        let (mut name, mut version, mut arch, mut sha256, mut released_at) =
            (None, None, None, None, None);

        for line in lines {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            let (key, value) = line
                .split_once(char::is_whitespace)
                .ok_or_else(|| format!("manifest line {line:?} is not `key value`"))?;
            let value = value.trim();
            let seen = match key {
                "name" => name.replace(value.to_string()).is_some(),
                "version" => version.replace(value.to_string()).is_some(),
                "arch" => arch.replace(value.to_string()).is_some(),
                "sha256" => sha256.replace(parse_sha256(value)?).is_some(),
                "released_at" => released_at
                    .replace(
                        value
                            .parse::<i64>()
                            .map_err(|_| format!("released_at {value:?} is not a unix time"))?,
                    )
                    .is_some(),
                other => return Err(format!("unknown manifest key {other:?}")),
            };
            // A repeated key is a document with two answers, and which one
            // wins would be a property of this parser rather than of the
            // signature.
            if seen {
                return Err(format!("manifest names {key:?} twice"));
            }
        }

        let missing = |what: &str| format!("the manifest has no {what}");
        Ok(Self {
            name: name.ok_or_else(|| missing("name"))?,
            version: version.ok_or_else(|| missing("version"))?,
            arch: arch.ok_or_else(|| missing("arch"))?,
            sha256: sha256.ok_or_else(|| missing("sha256"))?,
            released_at: released_at.ok_or_else(|| missing("released_at"))?,
        })
    }

    /// Whether this artifact is for this agent, on this machine.
    pub fn check_target(&self) -> Result<(), String> {
        if self.name != ARTIFACT_NAME {
            return Err(format!(
                "this is a signed {:?}, not a {ARTIFACT_NAME}",
                self.name
            ));
        }
        if self.arch != std::env::consts::ARCH {
            return Err(format!(
                "this host runs {}, and the release is for {}",
                std::env::consts::ARCH,
                self.arch
            ));
        }
        Ok(())
    }
}

/// Check the signature against every key compiled into this binary.
///
/// Ed25519 through `ring`, which is already here for TLS and for the join
/// token's fingerprint — so the whole of this feature's cryptography costs no
/// dependency and no bytes the binary was not already carrying.
///
/// An agent with **no** keys refuses everything and says so in those words.
/// That is a build, not a fault: it is what a checkout produces, and the
/// Controller never sends to one because the `upgrade` capability is not
/// advertised (see [`available`]).
pub fn verify(document: &[u8], signature: &[u8]) -> Result<(), String> {
    verify_with(SIGNING_KEYS, document, signature)
}

/// The check itself, against a key set given rather than compiled in.
///
/// Split out for the tests, which have to be able to hold a private key — and
/// a checkout compiles in no public one, so a suite that could only use
/// [`SIGNING_KEYS`] would be a suite that never verifies a real signature. The
/// production caller passes exactly one key set and there is no way to reach
/// this with another.
fn verify_with(keys: &[[u8; 32]], document: &[u8], signature: &[u8]) -> Result<(), String> {
    if keys.is_empty() {
        return Err(
            "this agent was built with no release signing keys, so it cannot verify an \
             upgrade. Install it with scripts/install-agent.sh instead."
                .into(),
        );
    }
    for key in keys {
        let public = ring::signature::UnparsedPublicKey::new(&ring::signature::ED25519, key);
        if public.verify(document, signature).is_ok() {
            return Ok(());
        }
    }
    Err(
        "the manifest is not signed by any key this agent trusts. A rotated key arrives \
         in a release, so the agent that accepts the new one is the one before it."
            .into(),
    )
}

/// Whether this build can be upgraded over the wire at all.
///
/// Gates the `upgrade` capability. Absence is the answer for both "too old"
/// and "compiled without keys", and it is the answer we want: the Controller
/// refuses to start a transfer with a sentence instead of pushing two
/// megabytes to a host that was always going to refuse them.
pub fn available() -> bool {
    !SIGNING_KEYS.is_empty()
}

// --------------------------------------------------------------------------
// Versions
// --------------------------------------------------------------------------

/// Whether `candidate` is strictly higher than `floor`.
///
/// **The floor is the binary on the disk, never a stored number.** Any floor
/// written to a file is a floor something can lower, and the obvious place to
/// write it — the daemon's own state directory — is writable by exactly the
/// process this check exists to survive. It would also be a second source of
/// truth about which version is installed, able to disagree with the file that
/// actually runs.
///
/// So downgrade is not an operation. Rollback is, and it is local, automatic
/// and root's (see [`apply_update`]).
///
/// Numeric per component, with a pre-release ordering underneath: `0.4.0` is
/// higher than `0.4.0-rc1`, which is higher than `0.3.9`. Compared component
/// by component rather than as strings, because `0.10.0` sorts before `0.9.0`
/// as text and that is a fleet that cannot be upgraded past nine.
pub fn newer(candidate: &str, floor: &str) -> bool {
    order(candidate) > order(floor)
}

/// `(major, minor, patch, release?, pre-release tag)`, ordered.
///
/// The fourth element is what puts `0.4.0` above `0.4.0-rc1`: a release has
/// nothing after the dash and must outrank everything that has.
fn order(version: &str) -> (u64, u64, u64, bool, String) {
    let version = version.trim();
    let (core, pre) = match version.split_once(['-', '+']) {
        Some((core, pre)) => (core, pre.to_string()),
        None => (version, String::new()),
    };
    let mut parts = core.split('.').map(|part| part.parse::<u64>().unwrap_or(0));
    (
        parts.next().unwrap_or(0),
        parts.next().unwrap_or(0),
        parts.next().unwrap_or(0),
        pre.is_empty(),
        pre,
    )
}

// --------------------------------------------------------------------------
// The daemon's half: receive, verify, stage
// --------------------------------------------------------------------------

/// One transfer in flight, on one connection.
///
/// Bound to the connection like every other correlated exchange in this agent.
/// What is *not* bound to it is the partial file on disk: a transfer
/// interrupted by a reconnect is resumed rather than restarted, which is the
/// difference between an upgrade that completes on a flaky uplink and one that
/// never does.
pub struct Transfer {
    id: String,
    manifest: Manifest,
    stage: PathBuf,
    file: File,
    written: u64,
    total: u64,
}

/// Answer an offer: verify it, prepare the staging directory, and say how much
/// of the artifact this host already holds.
///
/// The verification here is the cheap end of the two. It is worth doing anyway
/// for exactly two reasons: it refuses before megabytes are written to a disk
/// we do not own, and it refuses *over a live connection*, so the operator gets
/// a sentence rather than a rollout that stalls.
pub fn offer(
    state_dir: &Path,
    frame: &wire::UpgradeOffer,
    slot: &mut Option<Transfer>,
) -> wire::UpgradeStatus {
    let refuse = |reason: String| wire::UpgradeStatus {
        transfer_id: frame.transfer_id.clone(),
        state: "refused".into(),
        reason,
        resume_from: 0,
    };
    let fail = |reason: String| wire::UpgradeStatus {
        transfer_id: frame.transfer_id.clone(),
        state: "failed".into(),
        reason,
        resume_from: 0,
    };

    if let Err(reason) = verify(&frame.manifest, &frame.signature) {
        return refuse(reason);
    }
    let manifest = match Manifest::parse(&frame.manifest) {
        Ok(manifest) => manifest,
        Err(reason) => return refuse(reason),
    };
    if let Err(reason) = manifest.check_target() {
        return refuse(reason);
    }
    if !newer(&manifest.version, AGENT_VERSION) {
        return refuse(format!(
            "this host runs {AGENT_VERSION} and the release is {}; downgrade is not an \
             operation (ADR-0017)",
            manifest.version
        ));
    }
    if frame.total_bytes == 0 || frame.total_bytes > MAX_ARTIFACT {
        return refuse(format!(
            "an agent binary of {} bytes is not something this host will write to disk",
            frame.total_bytes
        ));
    }

    let stage = state_dir.join(STAGE_DIR);
    if let Err(e) = fs::create_dir_all(&stage) {
        return fail(format!("cannot use {}: {e}", stage.display()));
    }

    // Resume only where the document is byte-identical to the one already on
    // disk. Comparing the *manifest* rather than the version is what makes
    // that safe: two builds of one version, or a re-signed artifact, produce
    // different bytes, and continuing a partial file from the wrong one would
    // splice two binaries into something whose digest fails at the end of a
    // transfer nobody wanted to repeat.
    let same = fs::read(stage.join(MANIFEST)).is_ok_and(|held| held == frame.manifest);
    let partial = stage.join(INCOMING);
    let written = if same {
        fs::metadata(&partial).map(|m| m.len()).unwrap_or(0).min(frame.total_bytes)
    } else {
        let _ = fs::remove_file(&partial);
        0
    };

    if let Err(e) = fs::write(stage.join(MANIFEST), &frame.manifest)
        .and_then(|()| fs::write(stage.join(SIGNATURE), &frame.signature))
    {
        return fail(format!("cannot write the manifest into {}: {e}", stage.display()));
    }

    let file = OpenOptions::new().create(true).write(true).truncate(!same).open(&partial);
    let mut file = match file {
        Ok(file) => file,
        Err(e) => return fail(format!("cannot write {}: {e}", partial.display())),
    };
    if written > 0 {
        if let Err(e) = file.seek(SeekFrom::Start(written)).and_then(|_| file.set_len(written)) {
            return fail(format!("cannot resume {}: {e}", partial.display()));
        }
    }

    log(&format!(
        "accepting {} {} ({} of {} bytes already here)",
        manifest.name, manifest.version, written, frame.total_bytes
    ));
    *slot = Some(Transfer {
        id: frame.transfer_id.clone(),
        manifest,
        stage,
        file,
        written,
        total: frame.total_bytes,
    });

    wire::UpgradeStatus {
        transfer_id: frame.transfer_id.clone(),
        state: "accepted".into(),
        reason: String::new(),
        resume_from: written,
    }
}

/// Take one chunk. `Some` when the transfer has ended, one way or the other.
///
/// Ending it drops the [`Transfer`], which is what makes a refusal final for
/// this connection: the next offer starts a new one, and the partial file it
/// finds is only resumed if the manifest still matches.
pub fn chunk(slot: &mut Option<Transfer>, frame: &wire::UpgradeChunk) -> Option<wire::UpgradeStatus> {
    // Not ours: an in-flight chunk from a transfer that has been replaced,
    // which is ordinary. Answering would be answering about a transfer nobody
    // is waiting on.
    if slot.as_ref()?.id != frame.transfer_id {
        return None;
    }

    let progress = take(slot.as_mut().expect("checked immediately above"), frame);
    let (state, reason) = match progress {
        Progress::More => return None,
        Progress::Staged(version) => {
            log(&format!("staged {version}; the updater installs it and restarts this service"));
            ("staged", String::new())
        }
        Progress::Failed(reason) => ("failed", reason),
    };
    // Dropped either way, which is what makes a failure final for this
    // connection. The next offer starts a new transfer, and the partial file
    // it finds is resumed only if the manifest still matches.
    let id = slot.take().map(|transfer| transfer.id).unwrap_or_default();
    Some(wire::UpgradeStatus {
        transfer_id: id,
        state: state.into(),
        reason,
        resume_from: 0,
    })
}

enum Progress {
    More,
    Staged(String),
    Failed(String),
}

fn take(transfer: &mut Transfer, frame: &wire::UpgradeChunk) -> Progress {
    // The offset is checked rather than seeked to. A chunk that does not
    // continue the file is a bug or an attempt to splice one artifact into
    // another, and both are answered by ending the transfer -- seeking would
    // leave a hole that reads as zeroes and fails the digest much later, with
    // a message about a signature.
    if frame.offset != transfer.written {
        return Progress::Failed(format!(
            "chunk at offset {} does not continue this artifact at {}",
            frame.offset, transfer.written
        ));
    }
    if transfer.written + frame.data.len() as u64 > transfer.total {
        return Progress::Failed("the transfer is longer than it said it would be".into());
    }
    if let Err(e) = transfer.file.write_all(&frame.data) {
        return Progress::Failed(format!("cannot write the staged binary: {e}"));
    }
    transfer.written += frame.data.len() as u64;

    if !frame.last {
        return Progress::More;
    }
    if transfer.written != transfer.total {
        return Progress::Failed(format!(
            "the transfer ended at {} of {} bytes",
            transfer.written, transfer.total
        ));
    }
    match finish(transfer) {
        Ok(version) => Progress::Staged(version),
        Err(reason) => Progress::Failed(reason),
    }
}

/// Flush, digest, promote, and write the trigger file.
///
/// The trigger is written **last**, after the artifact is complete and its
/// digest checked, and after the rename that gives it its final name. A path
/// unit that fired on a half-written file would hand root a truncated binary
/// and the version floor is the only thing that would notice.
fn finish(transfer: &mut Transfer) -> Result<String, String> {
    transfer
        .file
        .flush()
        .and_then(|()| transfer.file.sync_all())
        .map_err(|e| format!("cannot flush the staged binary: {e}"))?;

    let incoming = transfer.stage.join(INCOMING);
    let digest = sha256_file(&incoming)?;
    if digest != transfer.manifest.sha256 {
        // Left on disk deliberately: the next offer compares the manifest, and
        // a mismatching one removes it. Deleting here would make a Controller
        // that keeps sending a corrupt artifact re-send all of it every time.
        return Err(
            "what arrived does not match the digest inside the signed manifest".into()
        );
    }

    fs::rename(&incoming, transfer.stage.join(STAGED))
        .map_err(|e| format!("cannot promote the staged binary: {e}"))?;
    fs::write(
        transfer.stage.join(TRIGGER),
        format!("{}\n", transfer.manifest.version),
    )
    .map_err(|e| format!("cannot write the trigger file: {e}"))?;
    Ok(transfer.manifest.version.clone())
}

/// Record that this agent reached a Controller and was admitted.
///
/// Written on every accepted `HelloAck`, into the `StateDirectory` the daemon
/// already owns — which is the whole reason the flag it answers lives
/// somewhere else. `bystack-agent.service` is unchanged by this feature:
/// `ProtectSystem=strict` makes every other path on the machine read-only to
/// this process, so a daemon that "cleared a flag in a root-owned directory"
/// would need `ReadWritePaths=`, which is the exact cost ADR-0017 exists not to
/// pay.
///
/// So the claim is made here and the *decision* is root's: the rollback
/// service reads this file and asks whether the version it names is the one
/// just installed, and whether it connected after the install. A stale marker
/// from the previous version answers "no" on both counts, which is the answer
/// that matters — "the process is up" is not the claim being tested, "the
/// Controller is talking to it" is.
///
/// Failure is logged and nothing else. A host whose state directory has gone
/// read-only has a larger problem, and refusing to stay connected over it
/// would turn that problem into an outage.
pub fn note_connected(state_dir: &Path) {
    let path = state_dir.join(CONNECTED);
    let body = format!("version={AGENT_VERSION}\nunix={}\n", unix_time());
    if let Err(e) = fs::write(&path, body) {
        log(&format!("cannot record this connection in {}: {e}", path.display()));
    }
}

// --------------------------------------------------------------------------
// Root's half: install
// --------------------------------------------------------------------------

/// Install what the daemon staged. **Runs as root, with no network.**
///
/// Everything it is handed is treated as hostile, because the directory it is
/// handed it in belongs to a network-facing daemon. The first thing it does is
/// copy the three files into a directory of its own, `0700` root:root, and
/// every check after that is performed on the copies. Verifying in the staging
/// directory and installing from it would be two reads of the same path, and a
/// compromised daemon that wins the race between them has bypassed the entire
/// signature chain by a few milliseconds.
///
/// The signature and the version are then checked **again**. Not as ceremony:
/// this is the first check performed on the copy that will actually be
/// installed, and the daemon's earlier check was performed on something else.
pub fn apply_update() -> Result<(), String> {
    let uid = fs::metadata("/proc/self").map(|m| m.uid()).unwrap_or(u32::MAX);
    if uid != 0 {
        return Err(
            "apply-update installs a system binary and must run as root. It is started by \
             bystack-agent-updater.service, not by hand."
                .into(),
        );
    }

    let state_dir = PathBuf::from(
        std::env::var("BYSTACK_STATE_DIR").unwrap_or_else(|_| crate::DEFAULT_STATE_DIR.into()),
    );
    let stage = state_dir.join(STAGE_DIR);
    let target = PathBuf::from(
        std::env::var("BYSTACK_AGENT_BIN").unwrap_or_else(|_| DEFAULT_BIN.into()),
    );

    // Removed first, before anything can fail. `PathExists=` re-triggers as
    // soon as the unit it started goes inactive, so a trigger file that
    // survives a refusal is a oneshot in a loop -- and the refusal that leaves
    // it is exactly the one that repeats: a bad signature does not get better
    // on the second read.
    let trigger = stage.join(TRIGGER);
    if let Err(e) = fs::remove_file(&trigger) {
        if e.kind() != std::io::ErrorKind::NotFound {
            return Err(format!("cannot clear {}: {e}", trigger.display()));
        }
        // Nothing staged. Ordinary: a manual run, or a second trigger for an
        // upgrade this run already installed.
        return Ok(());
    }

    let work = private_workspace()?;
    copy_capped(&stage.join(STAGED), &work.join(STAGED), MAX_ARTIFACT)?;
    copy_capped(&stage.join(MANIFEST), &work.join(MANIFEST), MAX_DOCUMENT)?;
    copy_capped(&stage.join(SIGNATURE), &work.join(SIGNATURE), MAX_DOCUMENT)?;

    let document = fs::read(work.join(MANIFEST)).map_err(|e| e.to_string())?;
    let signature = fs::read(work.join(SIGNATURE)).map_err(|e| e.to_string())?;
    verify(&document, &signature)?;
    let manifest = Manifest::parse(&document)?;
    manifest.check_target()?;

    if sha256_file(&work.join(STAGED))? != manifest.sha256 {
        return Err("the staged binary does not match the digest in its signed manifest".into());
    }

    let floor = installed_version(&target);
    if !newer(&manifest.version, &floor) {
        return Err(format!(
            "{} holds {floor} and the staged release is {}; refusing to install it",
            target.display(),
            manifest.version
        ));
    }

    // **Armed before the swap, and the order is the whole point.** A binary
    // installed with no rollback record is the failure ADR-0017 spends most of
    // its length on: the host comes up unable to reach the Controller, nothing
    // undoes it, and the only way back is ssh. So a probation file that cannot
    // be written stops the upgrade rather than being reported afterwards --
    // never swap a binary you cannot undo.
    //
    // It is removed again if the install fails, so a refusal here leaves the
    // machine exactly as it was found.
    let previous = previous_path(&target);
    arm_probation(&manifest.version, &target, &previous)?;
    if let Err(e) = install(&work.join(STAGED), &target) {
        let _ = fs::remove_file(PathBuf::from(UPDATE_DIR).join(PROBATION));
        return Err(e);
    }

    // Cleared once it is installed. Two megabytes in a daemon's state
    // directory, on every host in a fleet, is not a thing to leave lying
    // around for the sake of a retry that the version floor now refuses.
    for name in [STAGED, MANIFEST, SIGNATURE, INCOMING] {
        let _ = fs::remove_file(stage.join(name));
    }

    log(&format!("installed {} at {}", manifest.version, target.display()));

    // Armed before the restart, not after: an agent that comes up and hangs
    // must still be undone, and a restart that never returns would otherwise
    // take the arming with it.
    run("systemctl", &["start", "bystack-agent-rollback.timer"]);
    // A separate unit in its own cgroup, so restarting the service that
    // triggered this is an ordinary job rather than a process killing its own
    // parent.
    run("systemctl", &["restart", "bystack-agent.service"]);
    Ok(())
}

/// A directory only root can read, and only this run is using.
fn private_workspace() -> Result<PathBuf, String> {
    let dir = PathBuf::from(
        std::env::var("RUNTIME_DIRECTORY").unwrap_or_else(|_| RUNTIME_DIR.into()),
    );
    fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700))
        .map_err(|e| format!("cannot restrict {}: {e}", dir.display()))?;
    // Checked rather than assumed. Under `/run` this can only fail if
    // something already went badly wrong, and proceeding would mean verifying
    // a copy in a directory somebody else can write to -- which is the failure
    // the copy exists to prevent, reintroduced one directory along.
    let owner = fs::metadata(&dir).map_err(|e| e.to_string())?.uid();
    if owner != 0 {
        return Err(format!("{} is not owned by root", dir.display()));
    }
    Ok(dir)
}

/// Copy, refusing anything over `cap` without reading it.
fn copy_capped(from: &Path, to: &Path, cap: u64) -> Result<(), String> {
    let mut source = File::open(from).map_err(|e| format!("cannot read {}: {e}", from.display()))?;
    let size = source.metadata().map_err(|e| e.to_string())?.len();
    if size > cap {
        return Err(format!("{} is {size} bytes, which is more than this will install", from.display()));
    }
    let mut destination = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .mode(0o600)
        .open(to)
        .map_err(|e| format!("cannot write {}: {e}", to.display()))?;
    std::io::copy(&mut source, &mut destination)
        .map_err(|e| format!("cannot copy {}: {e}", from.display()))?;
    destination.sync_all().map_err(|e| e.to_string())?;
    Ok(())
}

/// Put the new binary in place, keeping the old inode beside it.
///
/// The temporary file is created **in the target directory**, and that is not
/// a detail. The tempting version — write in `/var/lib`, rename into place —
/// is broken under enforcing SELinux, and it fails in a way that looks like a
/// bad binary rather than a bad label: a file renamed across directories keeps
/// `var_lib_t`, and the service will not start. Creating it here has it
/// inherit the target directory's default type, and `restorecon` is cheap
/// insurance for the case where it does not.
///
/// `rename` within one directory is atomic and replaces the inode rather than
/// writing through it, so an agent that is running right now keeps the file it
/// started with. That is the same property that makes `install` correct in
/// `scripts/install-agent.sh`, for the same reason.
fn install(staged: &Path, target: &Path) -> Result<(), String> {
    let dir = target.parent().unwrap_or(Path::new("/usr/local/bin"));
    let temp = dir.join(format!(".{}.new", file_name(target)));

    copy_capped(staged, &temp, MAX_ARTIFACT)?;
    fs::set_permissions(&temp, fs::Permissions::from_mode(0o755))
        .map_err(|e| format!("cannot make {} executable: {e}", temp.display()))?;

    // Before the rename, not after: afterwards there is no previous inode
    // left to point at. A hard link rather than a copy, so keeping a rollback
    // costs a directory entry instead of another two megabytes.
    let previous = previous_path(target);
    if target.exists() {
        let _ = fs::remove_file(&previous);
        if let Err(e) = fs::hard_link(target, &previous) {
            log(&format!(
                "cannot keep {} as {}: {e}; installing anyway, with no automatic rollback",
                target.display(),
                previous.display()
            ));
        }
    }

    fs::rename(&temp, target).map_err(|e| {
        let _ = fs::remove_file(&temp);
        format!("cannot install {}: {e}", target.display())
    })?;

    // The rename is atomic; the *directory entry* reaching the disk is not,
    // and a host that loses power in this second should come back with one
    // binary or the other rather than with neither.
    if let Ok(handle) = File::open(dir) {
        let _ = handle.sync_all();
    }

    // Where SELinux is not installed this is a command that does not exist,
    // which is the same non-event as a host where it is permissive.
    run("restorecon", &["-F", &target.display().to_string()]);
    run("restorecon", &["-F", &previous.display().to_string()]);
    Ok(())
}

/// Write the probation flag root reads, and the rollback script acts on.
fn arm_probation(version: &str, target: &Path, previous: &Path) -> Result<(), String> {
    let dir = PathBuf::from(UPDATE_DIR);
    fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o755))
        .map_err(|e| format!("cannot set the mode on {}: {e}", dir.display()))?;

    let now = unix_time();
    let body = format!(
        "# Written by bystack-agent apply-update. Read by bystack-agent-rollback.service.\n\
         version={version}\n\
         installed_at={now}\n\
         deadline={}\n\
         binary={}\n\
         previous={}\n",
        now + (PROBATION_MINUTES * 60) as i64,
        target.display(),
        previous.display(),
    );
    fs::write(dir.join(PROBATION), body)
        .map_err(|e| format!("cannot arm the rollback: {e}"))
}

/// What `target` reports for `--version`, or this binary's own.
///
/// Executed rather than assumed, even though `ExecStart=` is that same path
/// and this process is therefore normally that same file. The case it covers
/// is an operator running `apply-update` from a copy somewhere else, where
/// "our own version" would be a floor of the attacker's — or the operator's —
/// choosing. The **higher of the two** is taken, so neither reading can lower
/// the bar.
fn installed_version(target: &Path) -> String {
    let reported = Command::new(target)
        .arg("--version")
        .output()
        .ok()
        .filter(|out| out.status.success())
        .and_then(|out| {
            String::from_utf8(out.stdout)
                .ok()?
                .split_whitespace()
                .next_back()
                .map(str::to_string)
        });
    match reported {
        Some(version) if newer(&version, AGENT_VERSION) => version,
        _ => AGENT_VERSION.to_string(),
    }
}

fn previous_path(target: &Path) -> PathBuf {
    target.with_file_name(format!("{}{PREVIOUS_SUFFIX}", file_name(target)))
}

fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| "bystack-agent".into())
}

/// Run a command, reporting failure and never propagating it.
///
/// Everything called through here is a best-effort side effect on a host we do
/// not control: `restorecon` may not exist, and a `systemctl` that fails has
/// left an installed binary that the next boot will run anyway. Turning any of
/// those into a failed install would undo work that has already succeeded.
fn run(program: &str, args: &[&str]) {
    match Command::new(program).args(args).status() {
        Ok(status) if status.success() => {}
        Ok(status) => log(&format!("{program} {} exited {status}", args.join(" "))),
        Err(e) => log(&format!("cannot run {program}: {e}")),
    }
}

fn sha256_file(path: &Path) -> Result<[u8; 32], String> {
    let mut file = File::open(path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    let mut context = ring::digest::Context::new(&ring::digest::SHA256);
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|e| format!("cannot read {}: {e}", path.display()))?;
        if read == 0 {
            break;
        }
        context.update(&buffer[..read]);
    }
    let mut digest = [0u8; 32];
    digest.copy_from_slice(context.finish().as_ref());
    Ok(digest)
}

fn parse_sha256(hex: &str) -> Result<[u8; 32], String> {
    if hex.len() != 64 {
        return Err("the manifest's sha256 is not a SHA-256".to_string());
    }
    let mut out = [0u8; 32];
    for (index, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&hex[index * 2..index * 2 + 2], 16)
            .map_err(|_| "the manifest's sha256 is not hexadecimal".to_string())?;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn document(version: &str, arch: &str, digest: &str) -> Vec<u8> {
        format!(
            "bystack-manifest/1\n\
             name bystack-agent\n\
             version {version}\n\
             arch {arch}\n\
             sha256 {digest}\n\
             released_at 1755388800\n"
        )
        .into_bytes()
    }

    #[test]
    fn a_manifest_is_read_field_for_field() {
        let manifest = Manifest::parse(&document("0.4.0", "x86_64", &"ab".repeat(32))).unwrap();
        assert_eq!(manifest.name, "bystack-agent");
        assert_eq!(manifest.version, "0.4.0");
        assert_eq!(manifest.arch, "x86_64");
        assert_eq!(manifest.sha256[0], 0xab);
        assert_eq!(manifest.released_at, 1755388800);
    }

    #[test]
    fn a_manifest_this_agent_does_not_fully_understand_is_refused() {
        // Every one of these would otherwise be *signed* and accepted with a
        // field silently dropped, which is the failure the strict parse
        // exists for: a constraint added in a later format must not be
        // ignorable by an older agent that still checks the signature over it.
        let base = String::from_utf8(document("0.4.0", "x86_64", &"ab".repeat(32))).unwrap();
        for bad in [
            base.replace("bystack-manifest/1", "bystack-manifest/2"),
            format!("{base}expires_at 1755388800\n"),
            format!("{base}version 9.9.9\n"),
            base.replace("sha256 ", "sha256 zz"),
            base.replace("released_at 1755388800\n", ""),
        ] {
            assert!(Manifest::parse(bad.as_bytes()).is_err(), "{bad} should not parse");
        }
    }

    #[test]
    fn an_artifact_for_another_agent_or_another_machine_is_refused() {
        let other = Manifest::parse(&document("0.4.0", "sparc64", &"ab".repeat(32))).unwrap();
        assert!(other.check_target().is_err());

        let renamed = String::from_utf8(document("0.4.0", std::env::consts::ARCH, &"ab".repeat(32)))
            .unwrap()
            .replace("name bystack-agent", "name bystack-agent-experimental");
        assert!(Manifest::parse(renamed.as_bytes()).unwrap().check_target().is_err());
    }

    #[test]
    fn only_a_higher_version_is_an_upgrade() {
        assert!(newer("0.4.0", "0.3.0"));
        assert!(newer("0.3.1", "0.3.0"));
        // The one every string comparison gets wrong, and the reason this is
        // not one: a fleet on 0.9.0 could never be upgraded past it.
        assert!(newer("0.10.0", "0.9.0"));
        assert!(!newer("0.9.0", "0.10.0"));

        assert!(!newer("0.3.0", "0.3.0"), "the same version is not an upgrade");
        assert!(!newer("0.2.0", "0.3.0"), "downgrade is not an operation");

        // A release outranks its own candidates, and a candidate outranks the
        // version before it.
        assert!(newer("0.4.0", "0.4.0-rc1"));
        assert!(newer("0.4.0-rc1", "0.3.9"));
        assert!(!newer("0.4.0-rc1", "0.4.0"));
    }

    #[test]
    fn a_build_with_no_keys_verifies_nothing_and_says_why() {
        // What a checkout produces, and what the `upgrade` capability is
        // gated on. The message has to name the alternative, because the
        // operator reading it is holding a release that will not install.
        if SIGNING_KEYS.is_empty() {
            let refusal = verify(b"anything", &[0u8; 64]).unwrap_err();
            assert!(refusal.contains("install-agent.sh"), "{refusal}");
            assert!(!available());
        }
    }

    #[test]
    fn a_signature_from_a_key_we_do_not_hold_is_refused() {
        // Ed25519 over a document, with a key nobody compiled in. The point is
        // that a *well-formed* signature is not a trusted one -- this is the
        // shape a compromised Controller's own key would arrive in.
        let (public, sign) = keypair(7);
        let signed = sign(b"bystack-manifest/1\n");
        assert!(verify(b"bystack-manifest/1\n", &signed).is_err());
        assert!(verify_with(&[public], b"bystack-manifest/1\n", &signed).is_ok());
    }

    #[test]
    fn a_signature_over_a_different_document_is_not_a_signature_over_this_one() {
        // The whole of the anti-downgrade argument in one assertion: the
        // version is *inside* the signed bytes, so a genuinely signed old
        // release cannot be re-announced as a new one by changing the frame
        // around it.
        let (public, sign) = keypair(9);
        let signed = sign(&document("0.3.0", "x86_64", &"ab".repeat(32)));
        let announced = document("9.9.9", "x86_64", &"ab".repeat(32));
        assert!(verify_with(&[public], &announced, &signed).is_err());
    }

    #[test]
    fn one_key_of_a_set_is_enough_which_is_what_makes_rotation_two_releases() {
        let (old, sign_with_old) = keypair(1);
        let (new, _) = keypair(2);
        let signed = sign_with_old(b"released under the old key");

        // Version N trusts {old, new} and is signed by old; version N+1 is
        // signed by new and trusts {new} alone. Both steps are ordinary
        // upgrades, which is why a leaked key is a release rather than a visit
        // to every machine in the fleet.
        assert!(verify_with(&[old, new], b"released under the old key", &signed).is_ok());
        assert!(verify_with(&[new], b"released under the old key", &signed).is_err());
    }

    #[test]
    fn a_transfer_is_staged_only_when_the_bytes_match_the_signed_digest() {
        let dir = scratch("staged");
        let artifact = b"#!/bin/sh\nnot really an agent\n".to_vec();
        let mut transfer = staging(&dir, &artifact, digest_of(&artifact));

        // Two chunks, because one would not exercise the offset check that
        // stops a second transfer's bytes being spliced into this one.
        assert!(matches!(take(&mut transfer, &part(0, &artifact[..10], false)), Progress::More));
        let end = part(10, &artifact[10..], true);
        assert!(matches!(take(&mut transfer, &end), Progress::Staged(_)));

        // The trigger is what the `.path` unit fires on, and it must not exist
        // until the artifact beside it is complete and verified.
        assert!(dir.join(TRIGGER).exists());
        assert_eq!(fs::read(dir.join(STAGED)).unwrap(), artifact);
        assert!(!dir.join(INCOMING).exists());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn bytes_that_do_not_match_the_manifest_stage_nothing() {
        let dir = scratch("corrupt");
        let artifact = b"the bytes that actually arrived".to_vec();
        // A digest for something else: a corrupted download, or a Controller
        // sending an artifact other than the one it had signed.
        let mut transfer = staging(&dir, &artifact, [0x11; 32]);

        let failure = take(&mut transfer, &part(0, &artifact, true));
        match failure {
            Progress::Failed(reason) => assert!(reason.contains("digest"), "{reason}"),
            _ => panic!("a mismatching digest must not stage"),
        }
        assert!(!dir.join(TRIGGER).exists(), "root must not be woken for this");
        assert!(!dir.join(STAGED).exists());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_chunk_that_does_not_continue_the_artifact_ends_the_transfer() {
        let dir = scratch("spliced");
        let artifact = b"0123456789".to_vec();
        let mut transfer = staging(&dir, &artifact, digest_of(&artifact));

        assert!(matches!(take(&mut transfer, &part(0, b"01234", false)), Progress::More));
        // Seeking here would leave a hole that reads as zeroes and fail the
        // digest much later, with a message about a signature.
        match take(&mut transfer, &part(9, b"56789", true)) {
            Progress::Failed(reason) => assert!(reason.contains("offset"), "{reason}"),
            _ => panic!("an out-of-order chunk must end the transfer"),
        }
        let _ = fs::remove_dir_all(&dir);
    }

    // -- helpers -----------------------------------------------------------

    /// A key pair, and a closure that signs with it. `ring` will not hand back
    /// the seed, so the two travel together.
    fn keypair(seed: u8) -> ([u8; 32], impl Fn(&[u8]) -> Vec<u8>) {
        use ring::signature::KeyPair;
        let pair = ring::signature::Ed25519KeyPair::from_seed_unchecked(&[seed; 32]).unwrap();
        let mut public = [0u8; 32];
        public.copy_from_slice(pair.public_key().as_ref());
        (public, move |message: &[u8]| pair.sign(message).as_ref().to_vec())
    }

    /// A directory of our own. No `tempfile` dependency for four tests.
    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("bystack-upgrade-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A `Transfer` in the state `offer` would have left it in, without
    /// needing a key this build does not have.
    fn staging(dir: &Path, artifact: &[u8], digest: [u8; 32]) -> Transfer {
        Transfer {
            id: "t1".into(),
            manifest: Manifest {
                name: ARTIFACT_NAME.into(),
                version: "9.9.9".into(),
                arch: std::env::consts::ARCH.into(),
                sha256: digest,
                released_at: 1755388800,
            },
            stage: dir.to_path_buf(),
            file: File::create(dir.join(INCOMING)).unwrap(),
            written: 0,
            total: artifact.len() as u64,
        }
    }

    fn part(offset: u64, data: &[u8], last: bool) -> wire::UpgradeChunk {
        wire::UpgradeChunk {
            transfer_id: "t1".into(),
            offset,
            data: data.to_vec(),
            last,
        }
    }

    fn digest_of(bytes: &[u8]) -> [u8; 32] {
        let mut digest = [0u8; 32];
        digest.copy_from_slice(ring::digest::digest(&ring::digest::SHA256, bytes).as_ref());
        digest
    }
}
