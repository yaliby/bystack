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
//! bystack-agent --controller ws://controller:8000/api/v1/agents/connect
//! ```
//!
//! Configuration is flags and environment, deliberately — the agent is
//! installed by copying one file onto a machine, and a config file to manage
//! alongside it would be a second thing to deploy for no gain.

mod docker;
mod hashset;
mod informer;
mod model;
mod session;
mod wire;

use std::time::Duration;

use docker::Engine;
use session::{log, Ended};

/// Reconnect backoff bounds.
///
/// A Controller that is down must not be retried in a hot loop, by a hundred
/// agents at once — that is how a control plane outage becomes a thundering
/// herd on the thing trying to recover.
const BACKOFF_MIN: Duration = Duration::from_secs(1);
const BACKOFF_MAX: Duration = Duration::from_secs(60);

pub struct Config {
    pub controller_url: String,
    pub socket: String,
    /// Refuse every mutation regardless of what arrives on the wire.
    ///
    /// Advertised to the Controller at `Hello` so the UI can disable the
    /// actions rather than offer them and fail.
    pub read_only: bool,
}

impl Config {
    fn from_args() -> Result<Self, String> {
        let mut controller = std::env::var("BYSTACK_CONTROLLER").ok();
        let mut socket = std::env::var("BYSTACK_DOCKER_SOCKET")
            .unwrap_or_else(|_| "/var/run/docker.sock".into());
        let mut read_only = std::env::var("BYSTACK_READ_ONLY")
            .map(|v| v != "false" && v != "0")
            .unwrap_or(false);

        let mut args = std::env::args().skip(1);
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--controller" => controller = args.next(),
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

        let controller_url = controller.ok_or_else(|| {
            format!("--controller is required (or set BYSTACK_CONTROLLER)\n\n{USAGE}")
        })?;

        Ok(Self { controller_url, socket, read_only })
    }
}

const USAGE: &str = "\
usage: bystack-agent --controller <url> [--socket <path>] [--read-only]

  --controller  Controller WebSocket URL, e.g.
                ws://controller:8000/api/v1/agents/connect
  --socket      Docker socket (default /var/run/docker.sock)
  --read-only   Refuse every mutation, whatever the Controller sends

Environment: BYSTACK_CONTROLLER, BYSTACK_DOCKER_SOCKET, BYSTACK_READ_ONLY";

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
    if let Err(e) = engine.info().await {
        eprintln!("bystack-agent: {e}");
        eprintln!("  the socket is owned by root:docker. If you are not in that group:");
        eprintln!("  sudo usermod -aG docker \"$USER\"   # then log out and back in");
        std::process::exit(1);
    }

    let mut state = informer::State::default();
    let mut backoff = BACKOFF_MIN;

    loop {
        tokio::select! {
            ended = session::run(&config, &engine, &mut state) => match ended {
                Ended::Refused(reason) => {
                    // Not a condition that improves by reconnecting.
                    eprintln!("bystack-agent: refused by the controller: {reason}");
                    std::process::exit(1);
                }
                Ended::Disconnected(reason) => {
                    log(&format!("{reason}; reconnecting in {}s", backoff.as_secs()));
                    tokio::time::sleep(backoff).await;
                    backoff = (backoff * 2).min(BACKOFF_MAX);
                }
            },
            _ = tokio::signal::ctrl_c() => {
                log("shutting down");
                return;
            }
        }
    }
}
