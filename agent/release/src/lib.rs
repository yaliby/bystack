//! The signed-release contract, once, for everything that installs one.
//!
//! ADR-0017 gave the agent a way to be handed a binary and decide whether it
//! is worth executing: a signed document naming `{name, version, arch, sha256,
//! released_at}`, checked against a key set compiled into the verifier.
//! ADR-0018 gives the Controller the same thing, and **reuses this contract
//! rather than growing a second one** -- same keys, same manifest format, same
//! signing tool. The only field that differs between the two is `name`.
//!
//! That is why this crate exists. A second implementation of "parse the
//! document, check the signature, compare the versions" would be a second
//! place a rotation has to land, a second parser to keep strict, and a second
//! answer to `0.10.0 > 0.9.0` -- and the two would agree right up until the
//! release where it mattered.
//!
//! What is **not** here is anything about how bytes arrive or where they are
//! installed. The agent is handed chunks over a stream it did not open and
//! installs from a oneshot with no network; the manager pulls over HTTPS and
//! installs in the same process. Those are the parts that genuinely differ,
//! and pushing them in here would produce an abstraction that describes
//! neither.

use std::fs::{File, OpenOptions};
use std::io::Read;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

include!(concat!(env!("OUT_DIR"), "/signing_keys.rs"));

/// The first line of the document, and the only place a format change may be
/// announced.
pub const MAGIC: &str = "bystack-manifest/1";

/// The agent, as `name` spells it. What ADR-0017 distributes.
pub const AGENT: &str = "bystack-agent";

/// The Controller, as `name` spells it. What ADR-0018 pulls.
///
/// A separate value under the same key, and that is the entire difference
/// between the two features at this layer. It is inside the signed document
/// because **a key that signs two artifacts must not let one be presented as
/// the other**: without it, a genuine, current, correctly signed Controller
/// could be handed to a host as its agent, and every check but this one would
/// pass.
pub const CONTROLLER: &str = "bystack-controller";

/// The most a manifest or a signature may be. Both are a few hundred bytes.
///
/// The *artifact* cap is deliberately not here. The agent bounds what a
/// Controller can make a managed host write to a disk that may be a NAS with
/// a gigabyte free; the manager bounds what a compromised release page can
/// make a Controller download. Same mechanism, different question, and one
/// shared number would end up justified by neither.
pub const MAX_DOCUMENT: u64 = 4 * 1024;

// --------------------------------------------------------------------------
// The document
// --------------------------------------------------------------------------

/// What a signature covers: `{name, version, arch, sha256, released_at}`.
///
/// A text document rather than a protobuf message, because **the bytes are the
/// contract**. Protobuf serialization is not canonical — field order and
/// varint width are the encoder's business — so a signer and a verifier
/// building the same message can produce different bytes, and a signature over
/// "the message" would be a signature over whichever encoder ran first. This
/// is parsed from exactly the bytes that were signed and exactly the bytes
/// that arrived.
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

impl Manifest {
    /// Parse, refusing anything not fully understood.
    ///
    /// **Unknown keys are an error, not something to skip.** Skipping is the
    /// forgiving choice and the wrong one here: a field added in a later
    /// format because it carries a constraint — an expiry, a target list — is
    /// a field an older verifier must not quietly ignore while accepting the
    /// signature over it. The way this document grows is `bystack-manifest/2`
    /// and a release, which is the same shape as key rotation and for the same
    /// reason.
    pub fn parse(bytes: &[u8]) -> Result<Self, String> {
        let text = std::str::from_utf8(bytes).map_err(|_| "the manifest is not UTF-8")?;
        let mut lines = text.lines();
        match lines.next().map(str::trim) {
            Some(MAGIC) => {}
            Some(other) => {
                return Err(format!(
                    "unknown manifest format {other:?}; this build reads {MAGIC}"
                ))
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

    /// Whether this artifact is the one asked for, for this machine.
    ///
    /// `expected` is passed rather than compiled in because this crate is
    /// linked by two binaries that install two different things. Neither of
    /// them ever passes a name it was given: the agent passes [`AGENT`] and
    /// the manager passes [`CONTROLLER`], both constants, because a target
    /// name taken from the wire is a check the sender gets to choose the
    /// answer to.
    pub fn check_target(&self, expected: &str) -> Result<(), String> {
        if self.name != expected {
            return Err(format!(
                "this is a signed {:?}, not a {expected}",
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

    /// The digest, as it is spelled in the document and on the command line.
    pub fn sha256_hex(&self) -> String {
        self.sha256.iter().map(|b| format!("{b:02x}")).collect()
    }
}

// --------------------------------------------------------------------------
// The signature
// --------------------------------------------------------------------------

/// Check the signature against every key compiled into this binary.
///
/// Ed25519 through `ring`, which the agent already carries for TLS and for the
/// join token's fingerprint — so the whole of this feature's cryptography
/// costs no dependency and no bytes that binary was not already carrying.
///
/// A build with **no** keys refuses everything and says so in those words.
/// That is a build, not a fault: it is what a checkout produces.
pub fn verify(document: &[u8], signature: &[u8]) -> Result<(), String> {
    verify_with(SIGNING_KEYS, document, signature)
}

/// The check itself, against a key set given rather than compiled in.
///
/// Public for the tests, which have to be able to hold a private key — and a
/// checkout compiles in no public one, so a suite that could only use
/// [`SIGNING_KEYS`] would be a suite that never verifies a real signature. The
/// production callers pass exactly one key set and there is no path that
/// reaches this with another.
pub fn verify_with(keys: &[[u8; 32]], document: &[u8], signature: &[u8]) -> Result<(), String> {
    if keys.is_empty() {
        return Err(
            "this build carries no release signing keys, so it cannot verify an upgrade. \
             Install it from a release instead."
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
        "the manifest is not signed by any key this build trusts. A rotated key arrives \
         in a release, so the build that accepts the new one is the one before it."
            .into(),
    )
}

/// Whether this build can verify a release at all.
///
/// Absence is the answer for "compiled without keys", and it is the answer we
/// want at both ends: the Controller refuses to start a transfer to an agent
/// that was always going to refuse it, and the manager refuses to download
/// forty megabytes it could never check.
pub fn available() -> bool {
    !SIGNING_KEYS.is_empty()
}

// --------------------------------------------------------------------------
// Versions
// --------------------------------------------------------------------------

/// Whether `candidate` is strictly higher than `floor`.
///
/// **The floor is what is installed on the disk, never a stored number.** Any
/// floor written to a file is a floor something can lower, and the obvious
/// place to write it is writable by exactly the process the check exists to
/// survive. It would also be a second source of truth about which version is
/// installed, able to disagree with the file that actually runs.
///
/// So downgrade is not an operation. Rollback is, and it is local and
/// automatic.
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

/// Whether a string is a version and nothing else.
///
/// Used where a version arrives from somewhere other than a signed document —
/// the manager composes a download URL out of one (ADR-0018) — and the answer
/// has to be "there is no path separator, no scheme and no shell in here"
/// before it is pasted into anything. [`newer`] deliberately does not care:
/// it parses leniently so an unexpected suffix orders low rather than
/// crashing, which is the right behaviour for comparing and the wrong one for
/// interpolating.
pub fn is_version(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.starts_with(|c: char| c.is_ascii_digit())
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '-' || c == '+')
}

// --------------------------------------------------------------------------
// Bytes
// --------------------------------------------------------------------------

/// SHA-256 of a file, streamed.
pub fn sha256_file(path: &Path) -> Result<[u8; 32], String> {
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

/// Copy, refusing anything over `cap` without reading it.
///
/// The destination is created `0600` by this process rather than being
/// `chmod`ed afterwards: the window between the two is a window in which the
/// bytes about to be installed as root are readable, and on the install path
/// that window is the whole attack.
pub fn copy_capped(from: &Path, to: &Path, cap: u64) -> Result<(), String> {
    let mut source = File::open(from).map_err(|e| format!("cannot read {}: {e}", from.display()))?;
    let size = source.metadata().map_err(|e| e.to_string())?.len();
    if size > cap {
        return Err(format!(
            "{} is {size} bytes, which is more than this will install",
            from.display()
        ));
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

    fn document(name: &str, version: &str, arch: &str) -> Vec<u8> {
        format!(
            "{MAGIC}\nname {name}\nversion {version}\narch {arch}\n\
             sha256 {}\nreleased_at 1755388800\n",
            "ab".repeat(32)
        )
        .into_bytes()
    }

    #[test]
    fn parses_the_document_it_was_handed() {
        let manifest = Manifest::parse(&document(AGENT, "0.4.0", "x86_64")).unwrap();
        assert_eq!(manifest.name, AGENT);
        assert_eq!(manifest.version, "0.4.0");
        assert_eq!(manifest.arch, "x86_64");
        assert_eq!(manifest.released_at, 1755388800);
        assert_eq!(manifest.sha256_hex(), "ab".repeat(32));
    }

    #[test]
    fn refuses_a_format_it_does_not_know() {
        let text = document(AGENT, "0.4.0", "x86_64");
        let bumped = String::from_utf8(text).unwrap().replace(MAGIC, "bystack-manifest/2");
        assert!(Manifest::parse(bumped.as_bytes()).is_err());
    }

    #[test]
    fn refuses_a_key_it_does_not_know() {
        let mut text = String::from_utf8(document(AGENT, "0.4.0", "x86_64")).unwrap();
        text.push_str("expires_at 1755388800\n");
        let error = Manifest::parse(text.as_bytes()).unwrap_err();
        assert!(error.contains("expires_at"), "{error}");
    }

    #[test]
    fn refuses_a_document_with_two_answers() {
        let mut text = String::from_utf8(document(AGENT, "0.4.0", "x86_64")).unwrap();
        text.push_str("version 9.9.9\n");
        assert!(Manifest::parse(text.as_bytes()).is_err());
    }

    /// The one check that distinguishes ADR-0017's artifact from ADR-0018's.
    /// Both are signed by the same key, so this is the only thing standing
    /// between a fleet and a Controller installed as an agent.
    #[test]
    fn a_controller_is_not_an_agent() {
        let manifest = Manifest::parse(&document(CONTROLLER, "0.5.0", std::env::consts::ARCH)).unwrap();
        assert!(manifest.check_target(CONTROLLER).is_ok());
        let error = manifest.check_target(AGENT).unwrap_err();
        assert!(error.contains("not a bystack-agent"), "{error}");
    }

    #[test]
    fn an_artifact_for_another_architecture_is_refused() {
        let manifest = Manifest::parse(&document(AGENT, "0.4.0", "sparc64")).unwrap();
        assert!(manifest.check_target(AGENT).is_err());
    }

    #[test]
    fn versions_order_numerically_and_releases_outrank_their_candidates() {
        assert!(newer("0.10.0", "0.9.0"));
        assert!(!newer("0.9.0", "0.10.0"));
        assert!(newer("0.4.0", "0.4.0-rc1"));
        assert!(!newer("0.4.0-rc1", "0.4.0"));
        assert!(!newer("0.4.0", "0.4.0"));
    }

    #[test]
    fn a_version_that_would_be_pasted_into_a_url_is_checked_for_being_one() {
        assert!(is_version("0.5.0"));
        assert!(is_version("0.5.0-rc1"));
        assert!(!is_version("../../etc/passwd"));
        assert!(!is_version("0.5.0/../.."));
        assert!(!is_version("v0.5.0"));
        assert!(!is_version(""));
        assert!(!is_version("0.5.0 && curl evil"));
    }

    #[test]
    fn no_keys_is_a_refusal_with_a_sentence() {
        let error = verify_with(&[], b"anything", b"anything").unwrap_err();
        assert!(error.contains("no release signing keys"), "{error}");
    }
}
