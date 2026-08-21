//! Fetch it, check it, swap it, and undo it if the Controller does not come
//! back (ADR-0018).
//!
//! The order of the last two is the whole of the safety argument and it is the
//! same one ADR-0017 makes for the agent: **never swap a binary you cannot
//! undo.** The probation record is written before the rename, so a record that
//! cannot be written stops the upgrade instead of being reported after it.
//!
//! Where this departs from the agent is probation itself, and the reason is
//! worth stating because it removes a whole unit from the packaging. The agent
//! is on probation until *a Controller somewhere else* accepts its `Hello`,
//! which is evidence that arrives on a connection the updater does not have —
//! so ADR-0017 needs an independent timer to come along later and read a flag.
//! The Controller's evidence is a GET to loopback. The process that made the
//! change can simply make the request, which means probation collapses into
//! this function and the rollback is not a separate schedule that has to
//! survive.
//!
//! What that does *not* cover is this process dying mid-probation — a reboot,
//! an OOM kill, an operator's `Ctrl-C`. That leaves an armed probation record
//! and a Controller nobody is watching, so [`rollback`] exists and
//! `bystack-manager-rollback.service` runs it at boot. One unit, conditioned
//! on the file, rather than a timer that ticks forever.

use std::fs;
use std::io::Write;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use bystack_release::{
    copy_capped, newer, sha256_file, verify, Manifest, AGENT, CONTROLLER, MAX_DOCUMENT,
};

use crate::ipc::{self, Intent, Phase, Status};
use crate::layout::{Layout, SERVICE};
use crate::shell;

/// The most this will download on a release page's say-so.
///
/// The Controller's zipapp carries its dependencies, so it is tens of
/// megabytes rather than the agent's two. This is a bound on what a hostile or
/// broken release can make a host spend, not a size anyone is near.
const MAX_ARTIFACT: u64 = 256 * 1024 * 1024;

/// How long a swapped-in Controller has to answer for itself.
///
/// Generous: this covers a cold start of a Python process that unpacks a
/// zipapp on first run, on hardware that may be a small VPS. The cost of
/// waiting is an operator watching a progress bar; the cost of being impatient
/// is rolling back a Controller that was going to be fine.
const PROBATION_SECONDS: u64 = 180;

/// How often to ask during that.
const PROBE_INTERVAL: Duration = Duration::from_secs(2);

/// Where to look when the intent named nowhere.
///
/// The Controller always writes its own address, so this is only reached by a
/// hand-written intent — and it is the Controller's own default bind address
/// (`ApiConfig` in `backend/.../config.py`), not a guess.
const DEFAULT_HEALTH: &str = "http://127.0.0.1:8000/api/v1/healthz";

/// The architectures a cascade fetches for the fleet.
///
/// Both, always, and not just this host's. A Controller upgrading a mixed
/// fleet has to hold artifacts for architectures it is not — ADR-0017 priced
/// that and concluded it is an operational cost rather than a trust decision,
/// because the host verifies. This is where that cost is actually paid.
const FLEET_ARCHES: [&str; 2] = ["x86_64", "aarch64"];

// --------------------------------------------------------------------------

/// The whole of an update, from an intent file to a Controller that answered.
///
/// Started by `bystack-manager.path` seeing the intent file appear. Returns
/// `Ok(())` when there was nothing to do, which is the ordinary case for a
/// re-trigger: `PathExists=` fires again as soon as the unit it started goes
/// inactive, so an intent that survived a run would be a oneshot in a loop.
pub fn run(layout: &Layout) -> Result<(), String> {
    require_root("apply")?;

    let intent_path = layout.intent();
    let text = match ipc::read_document(&intent_path) {
        Ok(text) => text,
        Err(_) if !intent_path.exists() => return Ok(()),
        Err(e) => return Err(e),
    };
    // Removed before anything that can fail, and the failure to remove it is
    // itself fatal: a refusal that leaves the trigger in place is a refusal
    // that repeats, and the refusals this makes -- a bad signature, a version
    // that is not higher -- do not get better on the second read.
    fs::remove_file(&intent_path)
        .map_err(|e| format!("cannot clear {}: {e}", intent_path.display()))?;

    let intent = match Intent::parse(&text) {
        Ok(intent) => intent,
        Err(e) => return report(layout, Status::new(Phase::Failed, "", &e), e),
    };

    match update(layout, &intent) {
        Ok(status) => {
            status.write(&layout.status())?;
            Ok(())
        }
        Err(e) => report(layout, Status::new(Phase::Failed, &intent.version, &e), e),
    }
}

fn update(layout: &Layout, intent: &Intent) -> Result<Status, String> {
    // Before a byte is fetched. A build with no keys can never install
    // anything, and forty megabytes downloaded to be refused is forty
    // megabytes of somebody's transfer allowance.
    if !bystack_release::available() {
        return Err("this manager was built with no release signing keys, so it cannot \
                    verify a Controller release. Install one from a release build \
                    (agent/keys/README.md)."
            .into());
    }

    Status::new(Phase::Fetching, &intent.version, "Finding the release.")
        .write(&layout.status())?;

    let tag = if intent.version.is_empty() {
        shell::latest_tag()?
    } else {
        format!("v{}", intent.version)
    };
    let version = tag.trim_start_matches('v').to_string();

    // The cheap refusal, before the download. The same check runs again below
    // against the *signed* version, which is the one that counts; this one is
    // here so an operator who clicks the button twice gets a sentence in a
    // second rather than after a download.
    let floor = installed_version(&layout.controller());
    if !newer(&version, &floor) {
        return Err(format!(
            "this machine runs {floor} and {tag} is {version}; that is not an upgrade. \
             Downgrading is not an operation -- rollback is, and it is automatic."
        ));
    }

    let work = prepare_work(layout)?;
    let base = shell::release_base(&tag);
    let artifact = format!("bystack-controller-{}", std::env::consts::ARCH);

    Status::new(Phase::Fetching, &version, &format!("Downloading {artifact} {version}."))
        .write(&layout.status())?;
    let binary = work.join(&artifact);
    shell::download(&format!("{base}/{artifact}"), &binary, MAX_ARTIFACT)?;
    let document_path = work.join(format!("{artifact}.manifest"));
    let signature_path = work.join(format!("{artifact}.manifest.sig"));
    shell::download(&format!("{base}/{artifact}.manifest"), &document_path, MAX_DOCUMENT)?;
    shell::download(
        &format!("{base}/{artifact}.manifest.sig"),
        &signature_path,
        MAX_DOCUMENT,
    )?;

    Status::new(Phase::Verifying, &version, "Checking the signature.")
        .write(&layout.status())?;
    let manifest = check(&binary, &document_path, &signature_path, CONTROLLER)?;

    // The tag said one thing and the signed document says another. Both are
    // claims about the same release and one of them is wrong, so neither is a
    // safe guess -- and installing the document's version under the tag's name
    // would put a number on the dashboard that no file on disk agrees with.
    if manifest.version != version {
        return Err(format!(
            "{tag} publishes an artifact whose signed manifest says {}. \
             One of the two is wrong and this will not guess which.",
            manifest.version
        ));
    }
    if !newer(&manifest.version, &floor) {
        return Err(format!(
            "this machine runs {floor} and the signed release is {}; refusing to install it",
            manifest.version
        ));
    }

    // The last check that costs nothing, and the one that catches the failure
    // this artifact is actually shaped to have.
    Status::new(Phase::Verifying, &version, "Checking that it runs at all.")
        .write(&layout.status())?;
    smoke_test(&binary, &manifest.version, &work)?;

    // ------------------------------------------------------------------
    // The swap
    // ------------------------------------------------------------------

    Status::new(Phase::Applying, &version, "Stopping the Controller and swapping it.")
        .write(&layout.status())?;

    let health = if intent.health.is_empty() { DEFAULT_HEALTH } else { &intent.health };

    // Armed first. A binary installed with no rollback record is the failure
    // this whole feature is arranged around: the Controller comes up unable to
    // serve, nothing undoes it, and the only way back is ssh on the one machine
    // that was supposed to make ssh unnecessary.
    arm(layout, &version, health)?;

    // Stopped rather than swapped underneath. A zipapp is opened and read as it
    // runs; replacing the inode is atomic for anyone who opens it *after*, and
    // says nothing about a process that is part-way through importing out of
    // the one before.
    shell::systemctl("stop", SERVICE)?;

    if let Err(e) = install(&binary, &layout.controller(), &layout.previous()) {
        // Nothing was replaced, so there is nothing to roll back -- but the
        // Controller is stopped and this run is over, and leaving it that way
        // would turn a refused upgrade into an outage.
        let _ = fs::remove_file(layout.probation());
        let _ = shell::systemctl("start", SERVICE);
        return Err(e);
    }

    // The new binary is in place and will not start. That is exactly what
    // probation is for, so it goes down the same path as a Controller that
    // starts and does not work.
    if let Err(e) = shell::systemctl("start", SERVICE) {
        undo(layout, &e);
        return Err(e);
    }

    // ------------------------------------------------------------------
    // Probation
    // ------------------------------------------------------------------

    Status::new(Phase::Probation, &version, "Waiting for the new Controller to answer.")
        .write(&layout.status())?;

    if let Err(reason) = wait_healthy(health, &version) {
        undo(layout, &reason);
        return Ok(Status::new(
            Phase::RolledBack,
            &floor,
            &format!("{reason} The previous Controller ({floor}) has been put back."),
        ));
    }

    // Disarmed only now. Between the rename and this line the boot-time
    // rollback would have undone the swap, which is the correct answer for
    // every way this process can die in between.
    let _ = fs::remove_file(layout.probation());

    // ------------------------------------------------------------------
    // Phase two: the fleet
    // ------------------------------------------------------------------

    Status::new(Phase::Cascading, &version, "Fetching the fleet's agents.")
        .write(&layout.status())?;

    let detail = match cascade(layout, &work, &base, &version) {
        Ok(count) => {
            return Ok(Status::new(
                Phase::Success,
                &version,
                &format!(
                    "The Controller is {version} and answering. {count} signed agent \
                     artifact(s) are ready for the fleet."
                ),
            )
            .cascading(&version))
        }
        // Not a failure of the update. The Controller is up, healthy and the
        // version that was asked for; what did not happen is the *fleet's*
        // half, which an operator can do by hand or by running this again.
        Err(e) => format!(
            "The Controller is {version} and answering. The fleet's agents were not \
             fetched: {e}"
        ),
    };
    Ok(Status::new(Phase::Success, &version, &detail))
}

// --------------------------------------------------------------------------
// Rollback
// --------------------------------------------------------------------------

/// Undo an armed swap. Idempotent, and a no-op when nothing is armed.
///
/// Run at boot by `bystack-manager-rollback.service`, which is conditioned on
/// the probation file existing. That covers the case [`run`] cannot: this
/// process dying between the rename and the health check.
pub fn rollback(layout: &Layout) -> Result<(), String> {
    require_root("rollback")?;
    let record = match read_probation(layout) {
        Some(record) => record,
        None => return Ok(()),
    };
    let reason = format!(
        "The upgrade to {} was interrupted before the new Controller answered.",
        record.version
    );
    undo(layout, &reason);
    Status::new(
        Phase::RolledBack,
        &record.previous_version,
        &format!("{reason} The previous Controller has been put back."),
    )
    .write(&layout.status())
}

/// Put the previous inode back and start the service again.
///
/// Never returns a failure. By the time this is called something has already
/// gone wrong, and there is no caller with a better answer than "report what
/// happened and leave the machine as recoverable as it can be made".
fn undo(layout: &Layout, reason: &str) {
    eprintln!("bystack-manager: rolling back: {reason}");
    let _ = shell::systemctl("stop", SERVICE);

    let target = layout.controller();
    let previous = layout.previous();
    if previous.is_file() {
        match install(&previous, &target, &target.with_extension("failed")) {
            Ok(()) => eprintln!("bystack-manager: restored {}", target.display()),
            Err(e) => eprintln!(
                "bystack-manager: cannot restore {}: {e}. The previous Controller is still \
                 at {} and can be copied over by hand.",
                target.display(),
                previous.display()
            ),
        }
    } else {
        eprintln!(
            "bystack-manager: there is no {} to restore. This machine needs its Controller \
             reinstalled by hand.",
            previous.display()
        );
    }

    // Disarmed after the restore, not before: a process killed in the middle
    // of this has to be picked up by the next boot, and a cleared flag is a
    // boot that does not look.
    let _ = fs::remove_file(layout.probation());
    let _ = shell::systemctl("start", SERVICE);
}

struct Probation {
    version: String,
    previous_version: String,
}

fn read_probation(layout: &Layout) -> Option<Probation> {
    let text = fs::read_to_string(layout.probation()).ok()?;
    let mut version = String::new();
    let mut previous_version = String::new();
    for line in text.lines() {
        match line.split_once('=') {
            Some(("version", value)) => version = value.trim().to_string(),
            Some(("previous_version", value)) => previous_version = value.trim().to_string(),
            _ => {}
        }
    }
    Some(Probation { version, previous_version })
}

/// Write the record that says what to undo, and by when.
fn arm(layout: &Layout, version: &str, health: &str) -> Result<(), String> {
    let state = layout.state();
    fs::create_dir_all(&state).map_err(|e| format!("cannot create {}: {e}", state.display()))?;
    // Root's, and only root's. The Controller is the process this record
    // exists to be able to undo; a directory it could write to is a rollback
    // it could cancel.
    fs::set_permissions(&state, fs::Permissions::from_mode(0o700))
        .map_err(|e| format!("cannot restrict {}: {e}", state.display()))?;

    let now = ipc::unix_time();
    let body = format!(
        "# Written by bystack-manager. Read by bystack-manager-rollback.service.\n\
         version={version}\n\
         previous_version={}\n\
         installed_at={now}\n\
         deadline={}\n\
         health={health}\n\
         binary={}\n\
         previous={}\n",
        installed_version(&layout.controller()),
        now + PROBATION_SECONDS as i64,
        layout.controller().display(),
        layout.previous().display(),
    );
    fs::write(layout.probation(), body).map_err(|e| format!("cannot arm the rollback: {e}"))
}

// --------------------------------------------------------------------------
// The pieces
// --------------------------------------------------------------------------

/// Verify a downloaded artifact the way the agent verifies a pushed one.
///
/// `expected` is a constant at every call site. The signature is checked
/// first, so the name and the digest are not defending against a forgery —
/// they defend against a *genuine* signature over the wrong thing, which is
/// the only attack left once the key holds and the whole reason what gets
/// signed is a document rather than a hash.
fn check(
    binary: &Path,
    document_path: &Path,
    signature_path: &Path,
    expected: &str,
) -> Result<Manifest, String> {
    let document = fs::read(document_path).map_err(|e| e.to_string())?;
    let signature = fs::read(signature_path).map_err(|e| e.to_string())?;
    verify(&document, &signature)?;
    let manifest = Manifest::parse(&document)?;
    if manifest.name != expected {
        return Err(format!("this is a signed {:?}, not a {expected}", manifest.name));
    }
    if sha256_file(binary)? != manifest.sha256 {
        return Err(format!(
            "{} does not match the digest in its own signed manifest",
            binary.display()
        ));
    }
    Ok(manifest)
}

/// Make the downloaded artifact say what it is, before anything is stopped.
///
/// The Controller is a zipapp assembled from wheels for one CPython ABI
/// (`scripts/build-controller.sh`), so "this host has no `python3.12`" is a
/// real and ordinary way for a correctly signed release to be unrunnable
/// here. Every other check above passes for it.
///
/// Without this, that host finds out by having its Controller stopped,
/// replaced, failing to start, and being rolled back -- a minute of downtime
/// and an alarming status line, for something answerable in two seconds while
/// everything is still running. **The rollback is for the failures that cannot
/// be found in advance; this is one that can.**
///
/// `SHIV_ROOT` into the work directory, because a zipapp unpacks itself on
/// first run and doing that as root into the service account's cache would
/// leave files the Controller then cannot write.
fn smoke_test(binary: &Path, version: &str, work: &Path) -> Result<(), String> {
    fs::set_permissions(binary, fs::Permissions::from_mode(0o755))
        .map_err(|e| format!("cannot make {} executable: {e}", binary.display()))?;
    let output = Command::new(binary)
        .arg("--version")
        .env("SHIV_ROOT", work.join("shiv"))
        .output()
        .map_err(|e| format!("cannot run the downloaded Controller: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "the downloaded Controller will not run on this host (exit {}): {}. \
             It is a zipapp built for one CPython version; check that this machine \
             has the interpreter its first line names.",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim().replace('\n', " ")
        ));
    }
    let reported = String::from_utf8_lossy(&output.stdout)
        .split_whitespace()
        .next_back()
        .unwrap_or_default()
        .to_string();
    if reported != version {
        return Err(format!(
            "the downloaded Controller reports {reported:?} and its signed manifest says \
             {version:?}. Nothing will be installed on the strength of two answers."
        ));
    }
    Ok(())
}

/// Put the new file in place, keeping the old inode beside it.
///
/// A temporary file created **in the target directory**, `fsync`, `rename`.
/// The tempting version — write in the work directory, rename into place — is
/// broken under enforcing SELinux, and it fails in a way that looks like a bad
/// binary rather than a bad label: a file renamed across directories keeps the
/// type it was created with. Creating it here has it inherit the target
/// directory's default, and `restorecon` is cheap insurance for the rest.
fn install(source: &Path, target: &Path, previous: &Path) -> Result<(), String> {
    let dir = target
        .parent()
        .ok_or_else(|| format!("{} has no directory", target.display()))?;
    fs::create_dir_all(dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    let temp = dir.join(format!(
        ".{}.new",
        target.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
    ));

    copy_capped(source, &temp, MAX_ARTIFACT)?;
    fs::set_permissions(&temp, fs::Permissions::from_mode(0o755))
        .map_err(|e| format!("cannot make {} executable: {e}", temp.display()))?;

    // Before the rename, not after: afterwards there is no previous inode left
    // to point at. A hard link rather than a copy, so keeping a rollback costs
    // a directory entry instead of another forty megabytes.
    if target.exists() {
        let _ = fs::remove_file(previous);
        if let Err(e) = fs::hard_link(target, previous) {
            eprintln!(
                "bystack-manager: cannot keep {} as {}: {e}",
                target.display(),
                previous.display()
            );
        }
    }

    fs::rename(&temp, target).map_err(|e| {
        let _ = fs::remove_file(&temp);
        format!("cannot install {}: {e}", target.display())
    })?;

    // The rename is atomic; the *directory entry* reaching the disk is not,
    // and a host that loses power in this second should come back with one
    // file or the other rather than with neither.
    if let Ok(handle) = fs::File::open(dir) {
        let _ = handle.sync_all();
    }
    shell::best_effort("restorecon", &["-F", &target.display().to_string()]);
    Ok(())
}

/// Ask the Controller what it is, until it says the right thing or the clock
/// runs out.
///
/// **The version is part of the question.** "The port answers" is satisfied by
/// the old Controller that never actually stopped, by a proxy in front of it,
/// and by a process that came up on the previous zipapp because the rename
/// went somewhere unexpected. The claim being tested is "the thing I installed
/// is the thing serving", and only the version establishes it.
fn wait_healthy(url: &str, version: &str) -> Result<(), String> {
    let deadline = std::time::Instant::now() + Duration::from_secs(PROBATION_SECONDS);
    let mut last = String::from("it never answered");
    while std::time::Instant::now() < deadline {
        match shell::probe(url) {
            Ok(body) => {
                if body.contains(&format!("\"version\":\"{version}\""))
                    || body.contains(&format!("\"version\": \"{version}\""))
                {
                    return Ok(());
                }
                last = format!("it answered, and did not report {version}");
            }
            Err(e) => last = e,
        }
        std::thread::sleep(PROBE_INTERVAL);
    }
    Err(format!(
        "The new Controller did not come up within {PROBATION_SECONDS}s: {last}."
    ))
}

/// Phase two: put the fleet's signed agents where the Controller hands them out.
///
/// The Controller's `ReleaseStore` rescans its release directory on every read
/// (`backend/.../infra/releases.py`), so this is the entire mechanism — three
/// files per architecture, and the rollout the operator already had is
/// available for the version that was just installed.
///
/// Verified here even though the agents verify for themselves. Not as a trust
/// control: it is so a mispaired or truncated artifact is refused on the one
/// machine that can report it in a sentence, rather than on a fleet, one host
/// at a time, as a puzzling refusal.
fn cascade(layout: &Layout, work: &Path, base: &str, version: &str) -> Result<usize, String> {
    let releases = layout.releases();
    fs::create_dir_all(&releases)
        .map_err(|e| format!("cannot create {}: {e}", releases.display()))?;

    let mut installed = 0;
    let mut failures = Vec::new();
    for arch in FLEET_ARCHES {
        let artifact = format!("bystack-agent-{arch}");
        match fetch_agent(work, base, &artifact, version) {
            Ok(()) => {
                // The binary and the signature first, the manifest last. The
                // Controller indexes by `*.manifest` and needs the other two
                // beside it, so a scan that lands mid-copy sees a directory
                // that has not changed yet rather than one that is broken.
                for suffix in ["", ".manifest.sig", ".manifest"] {
                    let name = format!("{artifact}{suffix}");
                    publish(&work.join(&name), &releases.join(&name))?;
                }
                installed += 1;
            }
            Err(e) => failures.push(format!("{artifact}: {e}")),
        }
    }

    if installed == 0 {
        return Err(failures.join("; "));
    }
    if !failures.is_empty() {
        // Half a release is worse than none for a mixed fleet, and it is worth
        // a sentence -- but the half that is here is genuinely usable, and
        // deleting it to be tidy would take away the upgrade that works.
        eprintln!("bystack-manager: {}", failures.join("; "));
    }
    Ok(installed)
}

fn fetch_agent(work: &Path, base: &str, artifact: &str, version: &str) -> Result<(), String> {
    let binary = work.join(artifact);
    shell::download(&format!("{base}/{artifact}"), &binary, MAX_ARTIFACT)?;
    let document = work.join(format!("{artifact}.manifest"));
    let signature = work.join(format!("{artifact}.manifest.sig"));
    shell::download(&format!("{base}/{artifact}.manifest"), &document, MAX_DOCUMENT)?;
    shell::download(&format!("{base}/{artifact}.manifest.sig"), &signature, MAX_DOCUMENT)?;

    // `check` and not `check_target`: one of these is for an architecture this
    // machine is not, which is the normal case for a Controller holding a
    // mixed fleet's releases and the exact thing `check_target` refuses.
    let manifest = check(&binary, &document, &signature, AGENT)?;
    if manifest.version != version {
        return Err(format!(
            "its signed manifest says {}, and the Controller was just installed at {version}",
            manifest.version
        ));
    }
    Ok(())
}

/// Move a verified file into the release directory, world-readable.
fn publish(from: &Path, to: &Path) -> Result<(), String> {
    let temp = to.with_file_name(format!(
        ".{}.new",
        to.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
    ));
    copy_capped(from, &temp, MAX_ARTIFACT)?;
    // The Controller reads this directory as `bystack` and sends the bytes to
    // the fleet. It does not execute them, so `0644` -- and the agents that
    // receive them install them, which is where the executable bit belongs.
    fs::set_permissions(&temp, fs::Permissions::from_mode(0o644))
        .map_err(|e| format!("cannot set the mode on {}: {e}", temp.display()))?;
    fs::rename(&temp, to).map_err(|e| format!("cannot publish {}: {e}", to.display()))
}

/// A directory only root can read, emptied before it is used.
///
/// Emptied at the *start* of a run rather than the end of one: a run that died
/// holding a partial artifact must not leave bytes behind that the next run's
/// digest check is the only thing standing between.
fn prepare_work(layout: &Layout) -> Result<PathBuf, String> {
    let work = layout.work();
    let _ = fs::remove_dir_all(&work);
    fs::create_dir_all(&work).map_err(|e| format!("cannot create {}: {e}", work.display()))?;
    fs::set_permissions(&work, fs::Permissions::from_mode(0o700))
        .map_err(|e| format!("cannot restrict {}: {e}", work.display()))?;
    Ok(work)
}

/// What the installed Controller reports, or `0.0.0`.
///
/// Executed rather than read from anywhere, for the reason ADR-0017 gives for
/// the agent's version floor: any number written to a file is a number
/// something can lower, and the file that actually runs is the only source of
/// truth that cannot disagree with itself.
///
/// A binary that will not run at all answers `0.0.0`, which lets any release
/// install over it. That is deliberate and it is not a downgrade path: getting
/// there requires already being able to break a root-owned file in
/// `/opt/bystack/bin`, and the alternative -- refusing to install over a
/// Controller that is already broken -- is a machine that can only be fixed by
/// hand.
fn installed_version(target: &Path) -> String {
    Command::new(target)
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
        })
        .unwrap_or_else(|| "0.0.0".into())
}

fn require_root(verb: &str) -> Result<(), String> {
    let uid = fs::metadata("/proc/self").map(|m| m.uid()).unwrap_or(u32::MAX);
    if uid != 0 {
        return Err(format!(
            "`{verb}` replaces the Controller and must run as root. It is started by \
             bystack-manager.service, not by hand."
        ));
    }
    Ok(())
}

/// Report a failure where the dashboard will see it, and still fail.
///
/// The status file is the only thing an operator watching a progress bar has.
/// A run that dies without writing one leaves that bar spinning until somebody
/// reads a journal, which is the state this feature exists to get people out
/// of.
fn report(layout: &Layout, status: Status, error: String) -> Result<(), String> {
    if let Err(e) = status.write(&layout.status()) {
        let mut stderr = std::io::stderr();
        let _ = writeln!(stderr, "bystack-manager: cannot write the status file: {e}");
    }
    Err(error)
}
