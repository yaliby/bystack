//! ByStack agent.
//!
//! Observes one Docker engine and reports to a Controller. It contains **no**
//! business logic, no database, no dashboard, no user model, and no notion of
//! what a "stack" or a "service" is. It does not know the URN scheme. It ships
//! Docker's own vocabulary upward and lets the Controller interpret it.
//!
//! The division is not a gradient (ARCHITECTURE §2): everything that *decides*
//! is in the Controller; everything here *observes and obeys*.
//!
//! ```text
//! bystack-agent --controller wss://controller:8443 --token bst1.<ca>.<secret>
//! bystack-agent --controller wss://controller:8443          # already enrolled
//! bystack-agent --controller unix:/run/bystack/agent.sock   # spawned locally
//! ```
//!
//! Configuration is flags and environment, deliberately — the agent is
//! installed by copying one file onto a machine, and a config file to manage
//! alongside it would be a second thing to deploy for no gain. The one thing
//! it keeps on disk is its own certificate (ADR-0011).

mod dbus;
mod docker;
mod enroll;
mod hashset;
mod host;
mod informer;
mod model;
mod procfs;
mod session;
mod systemd;
mod trust;
mod wire;

use std::path::PathBuf;
use std::time::Duration;

use docker::Engine;
use session::{log, Ended};
use trust::{Credentials, JoinToken};

/// Reconnect backoff bounds.
///
/// A Controller that is down must not be retried in a hot loop, by a hundred
/// agents at once — that is how a control plane outage becomes a thundering
/// herd on the thing trying to recover.
const BACKOFF_MIN: Duration = Duration::from_secs(1);
const BACKOFF_MAX: Duration = Duration::from_secs(60);

/// Where the certificate lives when nobody says otherwise.
///
/// Under `/var/lib` rather than `/etc`: it is state the agent manages and
/// rotates by itself, not configuration a person edits.
const DEFAULT_STATE_DIR: &str = "/var/lib/bystack-agent";

/// Where the Controller is, and therefore what proves who we are to it.
///
/// Two endpoints, **one protocol**. The frames, the framing, the informer and
/// every decision above them are identical on both; what differs is the pipe
/// underneath and what authenticates each end of it. Keeping the local case on
/// this code path rather than giving the Controller a second discovery
/// implementation is the whole argument of `docs/MIGRATION.md` §4.
#[derive(Debug)]
pub enum Endpoint {
    /// A Controller reached over the network — the ordinary case. Mutually
    /// authenticated, and enrolled before the first connection (ADR-0011).
    ///
    /// The base URL, `wss://host:8443`. The paths underneath it are the
    /// contract's, not the operator's, so they are not configurable.
    Remote(String),
    /// The Controller that spawned this process, over a unix socket on this
    /// machine.
    ///
    /// No enrollment and no certificate, because there is nothing for them to
    /// establish: the socket's filesystem permissions are the authentication,
    /// and anyone who can open it is already on this host with access to the
    /// Docker socket we would be protecting. Adding a CA round trip between a
    /// parent process and the child it just forked would be ceremony, not
    /// security.
    Local(PathBuf),
}

impl Endpoint {
    /// How it should read in a log line.
    pub fn describe(&self) -> String {
        match self {
            Endpoint::Remote(url) => url.clone(),
            Endpoint::Local(path) => format!("unix:{}", path.display()),
        }
    }
}

pub struct Config {
    pub controller: Endpoint,
    pub socket: String,
    pub state_dir: PathBuf,
    pub token: Option<String>,
    /// Refuse every mutation regardless of what arrives on the wire.
    ///
    /// Advertised to the Controller at `Hello` so the UI can disable the
    /// actions rather than offer them and fail.
    pub read_only: bool,
}

impl Config {
    fn from_args() -> Result<Self, String> {
        let mut controller = std::env::var("BYSTACK_CONTROLLER").ok();
        let mut token = std::env::var("BYSTACK_TOKEN").ok();
        let mut state_dir = std::env::var("BYSTACK_STATE_DIR")
            .unwrap_or_else(|_| DEFAULT_STATE_DIR.into());
        let mut socket = std::env::var("BYSTACK_DOCKER_SOCKET")
            .unwrap_or_else(|_| "/var/run/docker.sock".into());
        let mut read_only = std::env::var("BYSTACK_READ_ONLY")
            .map(|v| v != "false" && v != "0")
            .unwrap_or(false);

        let mut args = std::env::args().skip(1);
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--controller" => controller = args.next(),
                "--token" => token = args.next(),
                "--state-dir" => state_dir = args.next().unwrap_or(state_dir),
                "--socket" => socket = args.next().unwrap_or(socket),
                "--read-only" => read_only = true,
                "--version" => {
                    println!("bystack-agent {}", wire::AGENT_VERSION);
                    std::process::exit(0);
                }
                "--help" | "-h" => {
                    println!("{USAGE}");
                    std::process::exit(0);
                }
                other => return Err(format!("unknown argument {other:?}\n\n{USAGE}")),
            }
        }

        let controller = controller.ok_or_else(|| {
            format!("--controller is required (or set BYSTACK_CONTROLLER)\n\n{USAGE}")
        })?;
        let controller = parse_endpoint(&controller)?;

        Ok(Self {
            controller,
            socket,
            state_dir: PathBuf::from(state_dir),
            token,
            read_only,
        })
    }
}

/// Accept the base URL, and say something useful about everything else.
///
/// **`ws://` is refused, not downgraded to.** There is no insecure mode: no
/// flag, no environment variable, no scheme. The SSH transport had one and it
/// was already a documented footgun; this connection hands a remote party
/// commands to run as root and is strictly more dangerous (ADR-0011).
///
/// `unix:` is not an exception to that. Plaintext over a *network* is refused
/// because anyone on the path can read and rewrite it; a unix socket has no
/// path, and its reachability is a file mode rather than a routing table.
fn parse_endpoint(url: &str) -> Result<Endpoint, String> {
    let trimmed = url.trim().trim_end_matches('/');

    if let Some(path) = trimmed
        .strip_prefix("unix://")
        .or_else(|| trimmed.strip_prefix("unix:"))
    {
        if path.is_empty() {
            return Err(format!("unix: needs a socket path, got {url:?}"));
        }
        return Ok(Endpoint::Local(PathBuf::from(path)));
    }

    if let Some(rest) = trimmed.strip_prefix("ws://") {
        return Err(format!(
            "refusing ws://{rest}: agent connections are mutually authenticated and there is no \
             insecure mode (ADR-0011). Use wss://."
        ));
    }
    if !trimmed.starts_with("wss://") {
        return Err(format!("--controller must be a wss:// or unix: URL, got {url:?}"));
    }
    // The old spelling passed the full endpoint path. Accepted and trimmed
    // rather than refused, because it is in scripts and its meaning is
    // unambiguous.
    Ok(Endpoint::Remote(
        trimmed
            .trim_end_matches("/api/v1/agents/connect")
            .trim_end_matches('/')
            .to_string(),
    ))
}

pub fn connect_url(controller: &str) -> String {
    format!("{controller}/api/v1/agents/connect")
}

pub fn enroll_url(controller: &str) -> String {
    format!("{controller}/api/v1/agents/enroll")
}

const USAGE: &str = "\
usage: bystack-agent --controller <wss url> [--token <token>] [options]

  --controller  Controller base URL, e.g. wss://controller:8443, or
                unix:/path/to.sock for a Controller on this machine
  --token       Join token, for the first run only. Mint one with
                POST /api/v1/agents/tokens on the Controller.
  --state-dir   Where the client certificate lives
                (default /var/lib/bystack-agent)
  --socket      Docker socket (default /var/run/docker.sock)
  --read-only   Refuse every mutation, whatever the Controller sends

Environment: BYSTACK_CONTROLLER, BYSTACK_TOKEN, BYSTACK_STATE_DIR,
             BYSTACK_DOCKER_SOCKET, BYSTACK_READ_ONLY";

/// A single-threaded runtime.
///
/// The whole workload is one connection, one socket stream and a timer, and it
/// is blocked in `epoll` almost all of the time. A work-stealing scheduler
/// would add a thread per core and the synchronisation to go with it, to run a
/// program that does nothing most seconds of its life.
#[tokio::main(flavor = "current_thread")]
async fn main() {
    let config = match Config::from_args() {
        Ok(config) => config,
        Err(message) => {
            eprintln!("bystack-agent: {message}");
            std::process::exit(2);
        }
    };

    let engine = Engine::new(&config.socket);

    // Checked once, up front, because the failure is extremely common -- the
    // user is simply not in the `docker` group -- and the alternative is an
    // opaque connection error on the first request, after the agent has
    // already reported itself healthy.
    //
    // It also has to come first now: the Engine ID is the agent's identity,
    // so there is nothing to enrol *as* until the socket answers.
    let info = match engine.info().await {
        Ok(info) => info,
        Err(e) => {
            eprintln!("bystack-agent: {e}");
            eprintln!("  the socket is owned by root:docker. If you are not in that group:");
            eprintln!("  sudo usermod -aG docker \"$USER\"   # then log out and back in");
            std::process::exit(1);
        }
    };

    if let Err(message) = ensure_enrolled(&config, &info.id).await {
        eprintln!("bystack-agent: {message}");
        std::process::exit(1);
    }

    let mut state = informer::State::default();
    // Outlives the connection, exactly as `state` does, and for a related
    // reason: the bus connection it holds is expensive to establish and says
    // nothing about the Controller. The *watch list* inside it does not
    // survive -- every connection begins by being told what to watch, because
    // the Controller may have changed it while this host was away.
    let mut host = host::Host::new();
    let mut backoff = BACKOFF_MIN;
    // Registered once, before the first connection, and kept for the life of
    // the process. See `Shutdown` for why that is not a style choice.
    let mut stopping = match Shutdown::listen() {
        Ok(stopping) => stopping,
        Err(e) => {
            // Refusing to start beats starting unstoppably. A daemon a service
            // manager cannot stop is one it can only kill, which turns every
            // restart into an unclean one.
            eprintln!("bystack-agent: cannot listen for stop signals: {e}");
            std::process::exit(1);
        }
    };

    loop {
        tokio::select! {
            ended = session::run(&config, &engine, &mut state, &mut host) => match ended {
                Ended::Refused(reason) => {
                    // Not a condition that improves by reconnecting: a revoked
                    // certificate, or an identity we cannot prove.
                    eprintln!("bystack-agent: refused by the controller: {reason}");
                    std::process::exit(1);
                }
                Ended::Disconnected(reason) => {
                    log(&format!("{reason}; reconnecting in {}s", backoff.as_secs()));
                    // The backoff is a wait, not a commitment. Sleeping through
                    // it plainly is what made a disconnected agent unstoppable
                    // for up to a minute -- and a Controller shutting down
                    // *closes the stream first*, so "disconnected" is precisely
                    // the state every ordinary stop finds this process in.
                    tokio::select! {
                        _ = tokio::time::sleep(backoff) => {}
                        _ = stopping.recv() => {
                            log("shutting down");
                            return;
                        }
                    }
                    backoff = (backoff * 2).min(BACKOFF_MAX);
                }
            },
            _ = stopping.recv() => {
                log("shutting down");
                return;
            }
        }
    }
}

/// Someone asking this process to stop.
///
/// **Both signals, not just the interactive one.** `ctrl_c` is SIGINT, which is
/// what a terminal sends and what almost nothing else does. Every way this
/// agent is actually deployed stops it with SIGTERM: `systemctl stop` sends it,
/// `docker stop` sends it, and so does the Controller's own supervisor when it
/// spawns one locally (`runtime/localagent.py`, `_terminate`).
///
/// **And registered once, for the life of the process**, which is the part that
/// is not a style preference. A `Signal` stream only receives what arrives
/// while it exists; one built inside the future a `select!` polls is dropped
/// every time some *other* branch of that select completes first, and a signal
/// delivered in the gap is delivered to nobody. In this agent the gap is not
/// theoretical -- it is the reconnect backoff, which is exactly where an
/// ordinary shutdown finds the process, because a Controller closes the stream
/// on its way out and the agent's next move is to wait and redial.
///
/// The failure was quiet in the way these always are: the process did not
/// react, and every caller escalates to SIGKILL eventually -- five seconds for
/// the Controller, ninety for systemd's default. Nothing was ever reported as
/// broken. Stopping simply took a suspiciously round number of seconds.
struct Shutdown {
    interrupt: tokio::signal::unix::Signal,
    terminate: tokio::signal::unix::Signal,
}

impl Shutdown {
    fn listen() -> std::io::Result<Self> {
        use tokio::signal::unix::{signal, SignalKind};
        Ok(Self {
            interrupt: signal(SignalKind::interrupt())?,
            terminate: signal(SignalKind::terminate())?,
        })
    }

    /// Cancel-safe, which is what lets it sit in a `select!` that some other
    /// branch may win. Both `Signal::recv` calls are, and this adds nothing
    /// that is not.
    async fn recv(&mut self) {
        tokio::select! {
            _ = self.interrupt.recv() => {}
            _ = self.terminate.recv() => {}
        }
    }
}

/// Make sure a usable certificate exists on disk, enrolling if one does not.
///
/// It checks rather than returns, because every connection re-reads the
/// credentials: a renewal that arrives over the stream writes new ones, and
/// the next connection is where they take effect. Holding a copy here would
/// mean the agent kept presenting the certificate it started with until it
/// was restarted, which is the one thing automatic renewal exists to avoid.
///
/// A token present alongside a stored certificate re-enrols. An operator who
/// went to the trouble of minting one and putting it on the host is saying the
/// stored credential should be replaced, which is exactly the case where the
/// key was lost or the host was rebuilt.
///
/// A local endpoint has none of this. It touches no state directory, which is
/// why a Controller can spawn one on a first run with nothing configured and
/// nothing to clean up afterwards.
async fn ensure_enrolled(config: &Config, engine_id: &str) -> Result<(), String> {
    let controller = match &config.controller {
        Endpoint::Remote(url) => url,
        Endpoint::Local(_) => {
            return match config.token {
                // Refused rather than ignored. A token is a credential someone
                // minted deliberately, and silently dropping it would leave
                // them believing this agent had enrolled with a Controller it
                // never dialled.
                Some(_) => Err("--token is for a wss:// Controller; a local socket does not \
                                enrol (docs/MIGRATION.md section 4)"
                    .to_string()),
                None => Ok(()),
            }
        }
    };

    if let Some(raw) = &config.token {
        let token = JoinToken::parse(raw)?;
        let pending = enroll::enroll(controller, &token, engine_id, &config.state_dir).await?;
        if pending {
            log(
                "enrolled, and waiting for an operator to approve this host. \
                 Reconnecting until they do.",
            );
        }
    }

    // Loaded once here purely to fail at startup rather than on the first
    // handshake, where an unusable certificate and an unreachable Controller
    // look the same from a log file.
    match Credentials::load(&config.state_dir)? {
        Some(credentials) => trust::mutual_connector(&credentials).map(drop),
        None => Err(format!(
            "this host has not enrolled. Mint a join token on the Controller \
             (POST /api/v1/agents/tokens) and pass it as --token.\n  \
             Looked in {}.",
            config.state_dir.display()
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn remote(url: &str) -> String {
        match parse_endpoint(url).unwrap() {
            Endpoint::Remote(base) => base,
            Endpoint::Local(path) => panic!("{url} parsed as a local socket at {path:?}"),
        }
    }

    fn local(url: &str) -> PathBuf {
        match parse_endpoint(url).unwrap() {
            Endpoint::Local(path) => path,
            Endpoint::Remote(base) => panic!("{url} parsed as a remote controller at {base}"),
        }
    }

    #[test]
    fn the_base_url_is_what_the_paths_are_built_from() {
        let base = remote("wss://controller:8443/");
        assert_eq!(connect_url(&base), "wss://controller:8443/api/v1/agents/connect");
        assert_eq!(enroll_url(&base), "wss://controller:8443/api/v1/agents/enroll");
    }

    #[test]
    fn the_old_full_endpoint_spelling_still_works() {
        assert_eq!(remote("wss://controller:8443/api/v1/agents/connect"), "wss://controller:8443");
    }

    #[test]
    fn plaintext_is_refused_and_says_why() {
        let error = parse_endpoint("ws://controller:8000").unwrap_err();
        assert!(error.contains("ADR-0011"), "{error}");
        assert!(error.contains("wss://"), "{error}");
    }

    #[test]
    fn both_spellings_of_a_local_socket_are_the_same_path() {
        // `unix:///run/x.sock` is the URL-correct form and `unix:/run/x.sock`
        // is what anyone types. Accepting one and refusing the other would be
        // a pedantry tax paid by whoever is reading the error at the time.
        assert_eq!(local("unix:///run/bystack/agent.sock"), PathBuf::from("/run/bystack/agent.sock"));
        assert_eq!(local("unix:/run/bystack/agent.sock"), PathBuf::from("/run/bystack/agent.sock"));
    }

    #[test]
    fn a_unix_endpoint_with_no_path_is_refused() {
        assert!(parse_endpoint("unix:").is_err());
        assert!(parse_endpoint("unix://").is_err());
    }
}
