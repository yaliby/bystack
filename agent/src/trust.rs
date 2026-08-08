//! What the agent knows about who it is and who it is talking to.
//!
//! Four things live here: the join token, the CSR the agent generates, the
//! credentials it stores, and the two TLS configurations it uses — one for
//! enrolling, one for everything after.
//!
//! The Agent stores nothing at all beyond its own client certificate
//! (ARCHITECTURE §3). No queue, no spool, no cache that survives a restart.
//! This module is the entire exception, and it is three files in one
//! directory.
//!
//! **There is no insecure mode.** Not a flag, not an environment variable, not
//! a URL scheme. The SSH transport had one (`insecure_skip_host_key_check`)
//! and it was already a documented footgun; this interface hands a remote
//! party commands to run as root, and is strictly more dangerous (ADR-0011).

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::client::WebPkiServerVerifier;
use rustls::crypto::{ring as provider, verify_tls12_signature, verify_tls13_signature};
use rustls::{ClientConfig, DigitallySignedStruct, Error as TlsError, RootCertStore, SignatureScheme};
use rustls_pki_types::{CertificateDer, ServerName, UnixTime};
use tokio_tungstenite::Connector;

const CERT_FILE: &str = "agent.crt";
const KEY_FILE: &str = "agent.key";
const CA_FILE: &str = "ca.crt";

const TOKEN_PREFIX: &str = "bst1";

/// A join token, already split into the two credentials it carries.
///
/// It is two, not one, and that is the whole design: the secret authenticates
/// the agent to the Controller, and the fingerprint authenticates the
/// Controller to the agent. Without the second half enrollment would be
/// trust-on-first-use, and the token would be handed to whatever answered the
/// address.
pub struct JoinToken {
    pub raw: String,
    pub ca_fingerprint: [u8; 32],
}

impl JoinToken {
    pub fn parse(raw: &str) -> Result<Self, String> {
        let parts: Vec<&str> = raw.trim().split('.').collect();
        if parts.len() != 3 || parts[0] != TOKEN_PREFIX {
            return Err(format!(
                "not a join token: expected {TOKEN_PREFIX}.<fingerprint>.<secret>"
            ));
        }
        Ok(Self { raw: raw.trim().to_string(), ca_fingerprint: parse_sha256(parts[1])? })
    }
}

/// Everything the agent needs to open an authenticated connection.
pub struct Credentials {
    pub certificate_pem: String,
    pub key_pem: String,
    pub ca_pem: String,
}

impl Credentials {
    /// Read them back, or `None` if this host has not enrolled.
    ///
    /// `None` rather than an error for a missing directory, because "has not
    /// enrolled yet" is the ordinary first-run state and not a failure. A
    /// *partial* set is different and is reported: a certificate without its
    /// key means something went wrong on the way to disk, and starting an
    /// enrollment over it would silently discard a working identity.
    pub fn load(dir: &Path) -> Result<Option<Self>, String> {
        let paths = [dir.join(CERT_FILE), dir.join(KEY_FILE), dir.join(CA_FILE)];
        let present = paths.iter().filter(|p| p.exists()).count();
        if present == 0 {
            return Ok(None);
        }
        if present != paths.len() {
            return Err(format!(
                "{} holds an incomplete credential set; \
                 remove it and enroll again with a fresh --token",
                dir.display()
            ));
        }
        let read = |path: &PathBuf| {
            fs::read_to_string(path).map_err(|e| format!("cannot read {}: {e}", path.display()))
        };
        Ok(Some(Self {
            certificate_pem: read(&paths[0])?,
            key_pem: read(&paths[1])?,
            ca_pem: read(&paths[2])?,
        }))
    }

    /// Write them, with the key readable only by this user.
    ///
    /// The mode is set as the file is created rather than after: `open` then
    /// `chmod` leaves a window in which the host's identity is on disk under
    /// whatever umask happened to be in force.
    pub fn store(&self, dir: &Path) -> io::Result<()> {
        fs::create_dir_all(dir)?;
        restrict(dir, 0o700)?;
        write_secret(&dir.join(KEY_FILE), &self.key_pem)?;
        fs::write(dir.join(CERT_FILE), &self.certificate_pem)?;
        fs::write(dir.join(CA_FILE), &self.ca_pem)?;
        Ok(())
    }

    /// Replace the certificate, keeping the key that matches the new one.
    ///
    /// Renewal generates a fresh key pair, so both move together. Writing the
    /// key first means a crash between the two leaves a key with no
    /// certificate — recoverable by re-enrolling — rather than a certificate
    /// whose key we no longer hold, which looks valid and is not.
    pub fn replace(dir: &Path, certificate_pem: &str, key_pem: &str, ca_pem: &str) -> io::Result<()> {
        Self {
            certificate_pem: certificate_pem.to_string(),
            key_pem: key_pem.to_string(),
            ca_pem: ca_pem.to_string(),
        }
        .store(dir)
    }
}

/// A certificate request and the key that will answer for it.
pub struct KeyedRequest {
    pub csr_pem: String,
    pub key_pem: String,
}

/// Generate a key pair and a PKCS#10 request for it.
///
/// The subject is left empty on purpose. The Controller sets it from the
/// engine id it decided to believe and ignores anything in here, so putting a
/// name in would be stating a preference nobody reads (ADR-0011).
pub fn new_request() -> Result<KeyedRequest, String> {
    let key = rcgen::KeyPair::generate().map_err(|e| format!("cannot generate a key: {e}"))?;
    let params =
        rcgen::CertificateParams::new(Vec::<String>::new()).map_err(|e| e.to_string())?;
    let csr = params
        .serialize_request(&key)
        .map_err(|e| format!("cannot build a certificate request: {e}"))?;
    Ok(KeyedRequest { csr_pem: csr.pem().map_err(|e| e.to_string())?, key_pem: key.serialize_pem() })
}

// --------------------------------------------------------------------------
// TLS
// --------------------------------------------------------------------------

/// For enrolling: the server is authenticated by the token's fingerprint.
///
/// The agent has no CA certificate yet — that is what it is about to be
/// given. What it does have is a hash of one, out of band, in the token an
/// operator pasted. That is enough to refuse everything else.
pub fn enrollment_connector(token: &JoinToken) -> Result<Connector, String> {
    let config = ClientConfig::builder()
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(PinnedCa::new(token.ca_fingerprint)))
        .with_no_client_auth();
    Ok(Connector::Rustls(Arc::new(config)))
}

/// For everything after: our certificate, and their CA.
pub fn mutual_connector(credentials: &Credentials) -> Result<Connector, String> {
    let mut roots = RootCertStore::empty();
    for certificate in read_certificates(&credentials.ca_pem, "CA")? {
        roots.add(certificate).map_err(|e| format!("unusable CA certificate: {e}"))?;
    }

    let chain = read_certificates(&credentials.certificate_pem, "client")?;
    let key = rustls_pemfile::private_key(&mut credentials.key_pem.as_bytes())
        .map_err(|e| format!("unreadable private key: {e}"))?
        .ok_or_else(|| "the stored private key is empty".to_string())?;

    let config = ClientConfig::builder()
        .with_root_certificates(roots)
        .with_client_auth_cert(chain, key)
        .map_err(|e| format!("certificate and key do not match: {e}"))?;
    Ok(Connector::Rustls(Arc::new(config)))
}

/// Verifies a server chain against one SHA-256, then against webpki.
///
/// Deliberately not "accept anything". The fingerprint check decides *which*
/// CA is acceptable; everything after it — signature, validity dates, name
/// matching — is `WebPkiServerVerifier` doing its ordinary job with that CA
/// as the only root. Skipping the second half would turn a pinned CA into a
/// pinned nothing, because a fingerprint says who signed, not what.
#[derive(Debug)]
struct PinnedCa {
    fingerprint: [u8; 32],
}

impl PinnedCa {
    fn new(fingerprint: [u8; 32]) -> Self {
        Self { fingerprint }
    }
}

impl ServerCertVerifier for PinnedCa {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        ocsp: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, TlsError> {
        // The last certificate the server sent is the one it offers as its
        // root. The Controller sends the chain for exactly this reason.
        let (root, chain) = match intermediates.split_last() {
            Some((root, rest)) => (root, rest),
            None => (end_entity, &[][..]),
        };

        let seen = ring::digest::digest(&ring::digest::SHA256, root);
        if seen.as_ref() != self.fingerprint {
            return Err(TlsError::General(
                "the controller's CA does not match the fingerprint in the join token"
                    .to_string(),
            ));
        }

        let mut roots = RootCertStore::empty();
        roots
            .add(root.clone())
            .map_err(|e| TlsError::General(format!("unusable CA certificate: {e}")))?;
        WebPkiServerVerifier::builder_with_provider(
            Arc::new(roots),
            Arc::new(provider::default_provider()),
        )
        .build()
        .map_err(|e| TlsError::General(e.to_string()))?
        .verify_server_cert(end_entity, chain, server_name, ocsp, now)
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        verify_tls12_signature(message, cert, dss, &provider::default_provider().signature_verification_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        verify_tls13_signature(message, cert, dss, &provider::default_provider().signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        provider::default_provider().signature_verification_algorithms.supported_schemes()
    }
}

/// Turn a TLS failure into something an operator can act on.
///
/// Clock skew is the case worth the effort. Certificate validation is
/// time-sensitive, and a host whose clock is badly wrong fails the handshake
/// with a message about certificate validity that reads as "the certificate
/// is broken" — sending whoever is holding the pager to look at the CA
/// instead of at `timedatectl` (ADR-0011, Consequences).
pub fn explain(error: &str) -> String {
    let lowered = error.to_lowercase();
    if lowered.contains("expired") || lowered.contains("not valid yet") {
        return format!(
            "{error}\n  \
             this is what a wrong clock looks like: certificate validity is checked against it.\n  \
             check `timedatectl` on this host before suspecting the certificate."
        );
    }
    if lowered.contains("fingerprint in the join token") {
        return format!(
            "{error}\n  \
             the token was minted by a different Controller, or something else answered."
        );
    }
    if lowered.contains("notvalidforname") || lowered.contains("not valid for name") {
        return format!(
            "{error}\n  \
             the Controller's certificate does not cover the name you dialled.\n  \
             add it to `agents.server_names` in the Controller's config and restart it."
        );
    }
    error.to_string()
}

// --------------------------------------------------------------------------
// Internals
// --------------------------------------------------------------------------

fn read_certificates(pem: &str, what: &str) -> Result<Vec<CertificateDer<'static>>, String> {
    let certificates: Result<Vec<_>, _> = rustls_pemfile::certs(&mut pem.as_bytes()).collect();
    let certificates =
        certificates.map_err(|e| format!("unreadable {what} certificate: {e}"))?;
    if certificates.is_empty() {
        return Err(format!("no {what} certificate found"));
    }
    Ok(certificates)
}

fn parse_sha256(hex: &str) -> Result<[u8; 32], String> {
    if hex.len() != 64 {
        return Err("the token's fingerprint is not a SHA-256".to_string());
    }
    let mut out = [0u8; 32];
    for (index, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&hex[index * 2..index * 2 + 2], 16)
            .map_err(|_| "the token's fingerprint is not hexadecimal".to_string())?;
    }
    Ok(out)
}

#[cfg(unix)]
fn restrict(path: &Path, mode: u32) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
}

#[cfg(not(unix))]
fn restrict(_path: &Path, _mode: u32) -> io::Result<()> {
    Ok(())
}

#[cfg(unix)]
fn write_secret(path: &Path, contents: &str) -> io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(contents.as_bytes())
}

#[cfg(not(unix))]
fn write_secret(path: &Path, contents: &str) -> io::Result<()> {
    fs::write(path, contents)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_token_carries_a_fingerprint_and_a_secret() {
        let token = JoinToken::parse(&format!("bst1.{}.secret", "ab".repeat(32))).unwrap();
        assert_eq!(token.ca_fingerprint[0], 0xab);
    }

    #[test]
    fn a_token_without_a_usable_fingerprint_is_refused() {
        // Every one of these would otherwise reach the handshake and be
        // refused there, with a message about TLS rather than about the thing
        // the operator actually mistyped.
        for bad in [
            "not-a-token",
            "bst1.short.secret",
            &format!("bst1.{}.secret", "zz".repeat(32)),
            &format!("bst9.{}.secret", "ab".repeat(32)),
        ] {
            assert!(JoinToken::parse(bad).is_err(), "{bad} should not parse");
        }
    }

    #[test]
    fn a_generated_request_is_a_pem_csr_with_its_own_key() {
        let request = new_request().unwrap();
        assert!(request.csr_pem.contains("BEGIN CERTIFICATE REQUEST"));
        assert!(request.key_pem.contains("PRIVATE KEY"));
    }

    #[test]
    fn a_wrong_clock_is_named_rather_than_left_as_a_tls_error() {
        let explained = explain("invalid peer certificate: Expired");
        assert!(explained.contains("timedatectl"), "{explained}");
    }
}
