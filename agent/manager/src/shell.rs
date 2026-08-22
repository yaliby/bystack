//! The two external programs this uses, and why they are external.
//!
//! ## curl is the transport, and the transport is not trusted
//!
//! The manager has to reach GitHub Releases. Doing that in-process means an
//! HTTP client, a TLS stack and a root certificate store inside a binary that
//! runs as root — three dependencies, on the one component in this project
//! whose whole job is to be small enough to reason about.
//!
//! ADR-0017 already established why none of that buys anything. Provenance
//! does not come from the connection; it comes from a signature over a
//! manifest, checked against a key compiled into this binary. A hostile
//! mirror, a mis-issued certificate and an operator with a proxy are all the
//! same event: bytes arrive, and they are refused unless they are signed. So
//! the transport can be the tool every one of these machines already has, and
//! `install-agent.sh` already requires.
//!
//! What is pinned instead is what curl is *allowed to do*: HTTPS only, no
//! plaintext redirect, a size cap, a timeout, and a URL composed here from a
//! compiled-in repository and a string that has been checked for being a
//! version. Nothing that arrives over the connection decides where the next
//! request goes.
//!
//! ## systemctl stops and starts the thing being replaced
//!
//! Unavoidable and uninteresting: the Controller is a systemd service on the
//! installs this feature applies to, and a swap under a running process is how
//! you get a Controller serving a half-replaced zipapp.

use std::path::Path;
use std::process::{Command, Stdio};

/// Where releases come from, unless an operator says otherwise.
///
/// A constant rather than configuration, and read from the environment only as
/// an override an operator sets in the unit file. The version being installed
/// is the one thing about a release that a request can influence; **which
/// repository** is not, because a settable origin would make the intent file a
/// way to name a server, which is exactly what the signature check is arranged
/// so as not to have to care about — and "does not have to care" is a weaker
/// claim than "cannot be pointed there".
const DEFAULT_REPO: &str = "yaliby/bystack";

/// The base a release's files hang off. Overridable for an air-gapped mirror,
/// which is a real deployment and not a hole: whatever is behind it still has
/// to produce artifacts signed by the key this binary was built with.
pub fn release_base(tag: &str) -> String {
    match mirror() {
        Some(base) => format!("{}/{tag}", base.trim_end_matches('/')),
        None => format!("https://github.com/{}/releases/download/{tag}", repo()),
    }
}

/// `BYSTACK_RELEASE_BASE`, if an operator set one.
fn mirror() -> Option<String> {
    std::env::var("BYSTACK_RELEASE_BASE")
        .ok()
        .filter(|value| !value.is_empty())
}

/// Whether a URL may be fetched, and over what.
///
/// The default path is HTTPS and cannot be anything else: it is composed here
/// out of a compiled-in repository, and no input reaches it.
///
/// A **mirror an operator configured** may be plaintext, and that is a
/// deliberate exception rather than an oversight. ADR-0017's argument is that
/// the transport does not establish provenance -- the signature does -- and an
/// operator who has pointed this at a host on their own network has made a
/// decision this binary is in no position to second-guess. What is not
/// negotiable is that plaintext is *only* reachable that way: a `http://` URL
/// that is not under the configured mirror is refused, so no redirect and no
/// composed path can arrive at one.
fn scheme_args(url: &str) -> Result<&'static [&'static str], String> {
    allowed(url, mirror().as_deref())
}

/// [`scheme_args`], with the mirror passed rather than read.
///
/// Split out so the rule can be tested without a test having to set a process
/// environment variable that every other test in the binary would see. It is a
/// security boundary, and "read the code and agree it looks right" is not how
/// one of those should be maintained.
fn allowed(url: &str, mirror: Option<&str>) -> Result<&'static [&'static str], String> {
    if url.starts_with("https://") {
        return Ok(&["--proto", "=https", "--proto-redir", "=https"]);
    }
    match mirror {
        Some(base) if !base.is_empty() && url.starts_with(base.trim_end_matches('/')) => {
            Ok(&["--proto", "=http,https", "--proto-redir", "=http,https"])
        }
        _ => Err(format!(
            "{url} is not https, and is not under the mirror BYSTACK_RELEASE_BASE names. \
             Releases are pulled over TLS unless an operator has pointed this somewhere \
             else on purpose."
        )),
    }
}

pub fn repo() -> String {
    std::env::var("BYSTACK_REPO")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_REPO.into())
}

/// Download one file, refusing anything that is not what was asked for.
pub fn download(url: &str, to: &Path, max_bytes: u64) -> Result<(), String> {
    // `--proto` constrains the first request and `--proto-redir` the ones a
    // server asks for; without the second, a 302 to `http://` is a download
    // this would happily make.
    let status = Command::new("curl")
        .args(scheme_args(url)?)
        .args([
            "--tlsv1.2",
            "--location",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "300",
            "--connect-timeout",
            "20",
            "--max-filesize",
        ])
        .arg(max_bytes.to_string())
        .args(["--output"])
        .arg(to)
        .arg("--")
        .arg(url)
        .stdin(Stdio::null())
        .status()
        .map_err(|e| format!("cannot run curl: {e}. It is how this fetches a release."))?;
    if !status.success() {
        let _ = std::fs::remove_file(to);
        return Err(format!("could not download {url} (curl exited {status})"));
    }
    Ok(())
}

/// Ask GitHub which tag is newest, by following the redirect it publishes for
/// exactly that question.
///
/// `https://github.com/<repo>/releases/latest` redirects to the tag's own
/// page, so the answer is in the final URL. No JSON, no API token, no rate
/// limit that applies to unauthenticated API calls — and no parser in this
/// binary for a document somebody else's server controls. What comes back is
/// still only a *hint*: it names a tag, and everything that tag produces is
/// refused unless it is signed and higher than what is installed.
///
/// **A configured mirror is refused rather than answered.** A mirror is a
/// directory of releases, not an API, so there is nothing on it to ask — and
/// falling back to GitHub would be worse than failing: an air-gapped machine
/// would hang on a host it cannot reach, and a connected one would be told
/// about a version its mirror does not carry, then fail on the download two
/// steps later with a 404 instead of a sentence.
pub fn latest_tag() -> Result<String, String> {
    if let Some(base) = mirror() {
        return Err(format!(
            "this machine installs from the mirror at {base}, and a mirror has nothing \
             to ask which release is newest. Name the version instead -- \
             `bystack-manager request <version>`, or type it into the update box."
        ));
    }

    let output = Command::new("curl")
        .args([
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--tlsv1.2",
            "--location",
            "--fail",
            "--silent",
            "--show-error",
            "--head",
            "--output",
            "/dev/null",
            "--max-time",
            "60",
            "--write-out",
            "%{url_effective}",
            "--",
        ])
        .arg(format!("https://github.com/{}/releases/latest", repo()))
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("cannot run curl: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "cannot ask {} what its newest release is (curl exited {})",
            repo(),
            output.status
        ));
    }
    let url = String::from_utf8_lossy(&output.stdout);
    let tag = url.rsplit('/').next().unwrap_or("").trim();
    // A repository with no releases redirects to the releases index, whose
    // last path segment is `releases` -- which would otherwise be composed
    // into a download URL and reported as a 404 three steps later.
    if tag.is_empty() || !tag.starts_with('v') || !bystack_release::is_version(&tag[1..]) {
        return Err(format!(
            "{} has no release that looks like a version (the newest is {tag:?})",
            repo()
        ));
    }
    Ok(tag.to_string())
}

/// GET a loopback URL and return the body, or the reason there is not one.
///
/// `--insecure` deliberately. This is the Controller asking whether the
/// process it just started on this same machine is answering; a certificate
/// would be authenticating us to ourselves over a socket nobody else can be on
/// the other end of. What the body is checked for is in `apply.rs`, and it is
/// the version — which is the claim that actually matters and which no
/// certificate would have established.
pub fn probe(url: &str) -> Result<String, String> {
    let output = Command::new("curl")
        .args([
            "--fail",
            "--silent",
            "--show-error",
            "--insecure",
            "--max-time",
            "5",
            "--",
        ])
        .arg(url)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("cannot run curl: {e}"))?;
    if !output.status.success() {
        return Err(format!("{url} did not answer (curl exited {})", output.status));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// Run a unit verb and insist it worked.
pub fn systemctl(verb: &str, unit: &str) -> Result<(), String> {
    let status = Command::new("systemctl")
        .args([verb, unit])
        .stdin(Stdio::null())
        .status()
        .map_err(|e| format!("cannot run systemctl: {e}"))?;
    if !status.success() {
        return Err(format!("`systemctl {verb} {unit}` exited {status}"));
    }
    Ok(())
}

/// Best effort, and never a failure.
///
/// Everything called through here is a side effect on a host we do not
/// control: `restorecon` may not exist, which is the same non-event as a host
/// where SELinux is permissive. Turning it into a failed install would undo
/// work that has already succeeded.
pub fn best_effort(program: &str, args: &[&str]) {
    let _ = Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn https_needs_no_permission_and_may_not_be_redirected_off_it() {
        let args = allowed("https://github.com/x/y/releases/download/v1/z", None).unwrap();
        assert!(args.contains(&"--proto-redir"));
        assert!(args.contains(&"=https"));
        assert!(!args.iter().any(|arg| arg.contains("http,")));
    }

    #[test]
    fn plaintext_is_refused_unless_an_operator_named_it() {
        assert!(allowed("http://example.invalid/v1/z", None).is_err());
        assert!(allowed("http://example.invalid/v1/z", Some("")).is_err());
    }

    #[test]
    fn a_configured_mirror_may_be_plaintext_and_only_it_may_be() {
        let mirror = Some("http://releases.internal/bystack");
        assert!(allowed("http://releases.internal/bystack/v1/z", mirror).is_ok());
        // The failure this is really guarding: a URL that merely *starts like*
        // the mirror, or one composed from somewhere else entirely, must not
        // inherit the permission the operator granted to their own host.
        assert!(allowed("http://releases.internal.evil/bystack/v1/z", mirror).is_err());
        assert!(allowed("http://github.com/x/y/v1/z", mirror).is_err());
    }

    #[test]
    fn a_trailing_slash_on_the_mirror_is_not_a_difference() {
        let args = allowed("http://mirror.internal/v1/z", Some("http://mirror.internal/"));
        assert!(args.is_ok());
    }
}
