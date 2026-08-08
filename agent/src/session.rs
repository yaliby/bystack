//! One connected lifetime with the Controller.
//!
//! The agent **dials out**. Not the other way round: managed hosts open no
//! port, publish no Docker socket, and need no inbound firewall rule or public
//! address — which is what makes the model work behind NAT, where most of the
//! target deployments live (ADR-0008).
//!
//! One long-lived bidirectional stream carries everything. Telemetry flows up
//! and commands flow down over the same connection, framed as protobuf.

use std::collections::HashSet;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use prost::Message;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::{TcpStream, UnixStream};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::Request;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use tokio_tungstenite::{Connector, MaybeTlsStream, WebSocketStream};

use crate::docker::{ActionResult, Engine};
use crate::informer::{self, State, ALL_SLICES};
use crate::trust::{self, Credentials};
use crate::wire::{self, envelope::Payload, Slice};
use crate::{Config, Endpoint};

/// Agent-initiated, so it also keeps the NAT mapping alive.
///
/// Carries **no status payload**. Status changes are events and are sent when
/// they change; piggybacking state onto a timer is how a heartbeat silently
/// becomes a polling loop.
const PING_INTERVAL: Duration = Duration::from_secs(30);

pub enum Ended {
    /// Routine. Reconnect after a backoff.
    Disconnected(String),
    /// The Controller refused us, and reconnecting will not change its mind.
    Refused(String),
}

/// Run until the connection ends.
pub async fn run(config: &Config, engine: &Engine, state: &mut State) -> Ended {
    // The Controller drops its view of us when we disconnect, so anything we
    // remembered about what it already knows is now wrong. Clearing here is
    // what makes the first frame of every connection a genuine full Sync.
    state.clear();

    let socket = match dial(config).await {
        Ok(socket) => socket,
        Err(ended) => return ended,
    };
    let (mut sink, mut stream) = socket.split();

    let info = match engine.info().await {
        Ok(info) => info,
        Err(e) => return Ended::Disconnected(format!("cannot read the engine: {e}")),
    };

    let mut seq: u64 = 0;
    let send = |payload: Payload, seq: &mut u64| {
        *seq += 1;
        wire::envelope(*seq, payload).encode_to_vec()
    };

    // -- Hello ------------------------------------------------------------

    let hello = Payload::Hello(wire::Hello {
        agent_version: wire::AGENT_VERSION.to_string(),
        engine_id: info.id.clone(),
        engine: Some((&info).into()),
        // Advertised, not inferred. We refuse mutations regardless of what
        // arrives on the wire; telling the Controller lets the UI disable the
        // actions rather than offer them and watch every one bounce.
        read_only: config.read_only,
        capabilities: vec!["commands".into(), "resync".into(), "renewal".into(), "logs".into()],
        // Our clock, so the Controller can name a skew as a skew. Certificate
        // validation is time-sensitive and a badly wrong clock otherwise
        // surfaces as a generic TLS error that sends whoever is debugging it
        // to look at the CA (ADR-0011, Consequences).
        unix_time: unix_time(),
    });
    if sink.send(WsMessage::Binary(send(hello, &mut seq))).await.is_err() {
        return Ended::Disconnected("controller closed during Hello".into());
    }

    let ack = match stream.next().await {
        Some(Ok(WsMessage::Binary(bytes))) => match wire::Envelope::decode(&bytes[..]) {
            Ok(envelope) => match envelope.payload {
                Some(Payload::HelloAck(ack)) => ack,
                _ => return Ended::Disconnected("controller did not acknowledge Hello".into()),
            },
            Err(e) => return Ended::Disconnected(format!("undecodable HelloAck: {e}")),
        },
        _ => return Ended::Disconnected("controller closed before HelloAck".into()),
    };

    if !ack.accepted {
        let reason = if ack.reason.is_empty() {
            "controller refused this agent".to_string()
        } else {
            ack.reason
        };
        // The two refusals are not the same shape and treating them alike
        // gets one of them badly wrong. "Awaiting approval" is answered by an
        // operator clicking approve, with this process doing nothing but
        // coming back; "certificate revoked" is answered by a person on this
        // host, and retrying it forever is a log line a minute until someone
        // notices.
        return if ack.retry {
            Ended::Disconnected(reason)
        } else {
            Ended::Refused(reason)
        };
    }

    let resync_interval = Duration::from_secs(if ack.resync_interval == 0 {
        900
    } else {
        ack.resync_interval as u64
    });
    log(&format!(
        "connected to {} (epoch {}, resync {}s)",
        config.controller.describe(),
        ack.controller_epoch,
        resync_interval.as_secs()
    ));

    // -- `since` before the List ------------------------------------------
    //
    // Recorded here, before the first Sync below. The watch then starts from a
    // point that precedes our snapshot, so a change landing during the List is
    // replayed rather than lost. The other order is the classic silent-drift
    // bug and it survives testing because the window is small.
    let since = unix_time();

    let (event_tx, mut event_rx) = mpsc::channel::<String>(256);
    let watch_socket = engine.socket().to_path_buf();
    let watcher = tokio::spawn(async move {
        Engine::new(watch_socket).watch(since, event_tx).await
    });

    // -- first Sync: every slice, every payload ---------------------------

    for slice in ALL_SLICES {
        match informer::scan(engine, state, slice, true).await {
            Ok(Some(payload)) => {
                if sink.send(WsMessage::Binary(send(payload, &mut seq))).await.is_err() {
                    watcher.abort();
                    return Ended::Disconnected("controller closed during Sync".into());
                }
            }
            Ok(None) => {}
            Err(e) => {
                watcher.abort();
                return Ended::Disconnected(format!("initial List failed: {e}"));
            }
        }
    }
    log(&format!("synced {} entities from {}", state.tracked(), info.name));

    // -- steady state -----------------------------------------------------

    let mut resync = tokio::time::interval(resync_interval);
    resync.tick().await; // the first tick is immediate
    let mut ping = tokio::time::interval(PING_INTERVAL);
    ping.tick().await;

    //: The private key for a renewal we have asked for and not yet been
    //: answered. Held here rather than written to disk, because a key with no
    //: certificate is not a credential and a half-written pair is worse than
    //: neither -- the two only become useful together.
    let mut pending_key: Option<String> = None;

    loop {
        tokio::select! {
            // Events from the daemon, coalesced into a set of dirty slices.
            dirty = informer::coalesce(&mut event_rx) => {
                let Some(dirty) = dirty else {
                    watcher.abort();
                    return Ended::Disconnected("event stream ended".into());
                };
                if let Err(reason) = push(engine, state, &mut sink, &mut seq, dirty, false).await {
                    watcher.abort();
                    return Ended::Disconnected(reason);
                }
            }

            // Periodic re-List. Repairs *our* hash map, which after ADR-0009
            // is the only thing that can be wrong -- the Controller's graph is
            // reconciled by every frame. If the hashes agree, this produces
            // nothing and costs nothing.
            _ = resync.tick() => {
                let all: HashSet<Slice> = ALL_SLICES.into_iter().collect();
                if let Err(reason) = push(engine, state, &mut sink, &mut seq, all, false).await {
                    watcher.abort();
                    return Ended::Disconnected(reason);
                }
            }

            _ = ping.tick() => {
                if sink.send(WsMessage::Ping(Vec::new())).await.is_err() {
                    watcher.abort();
                    return Ended::Disconnected("controller stopped answering".into());
                }
            }

            // Commands and resync requests from the Controller.
            incoming = stream.next() => {
                let payload = match incoming {
                    Some(Ok(WsMessage::Binary(bytes))) => {
                        match wire::Envelope::decode(&bytes[..]) {
                            Ok(envelope) => envelope.payload,
                            Err(e) => {
                                watcher.abort();
                                return Ended::Disconnected(format!("undecodable frame: {e}"));
                            }
                        }
                    }
                    Some(Ok(WsMessage::Ping(_) | WsMessage::Pong(_))) => None,
                    Some(Ok(WsMessage::Close(_))) | None => {
                        watcher.abort();
                        return Ended::Disconnected("controller closed the stream".into());
                    }
                    Some(Ok(_)) => None,
                    Some(Err(e)) => {
                        watcher.abort();
                        return Ended::Disconnected(format!("stream error: {e}"));
                    }
                };

                match payload {
                    Some(Payload::Command(command)) => {
                        let result = execute(engine, config, command).await;
                        if sink.send(WsMessage::Binary(
                            send(Payload::CommandResult(result), &mut seq)
                        )).await.is_err() {
                            watcher.abort();
                            return Ended::Disconnected("controller closed during a command".into());
                        }
                    }
                    Some(Payload::LogsRequest(request)) => {
                        // Not gated on `read_only`. That flag refuses
                        // mutations; refusing to show an operator why a
                        // container is failing because the platform is in
                        // its safe mode would have it exactly backwards.
                        let response = fetch_logs(engine, request).await;
                        if sink.send(WsMessage::Binary(
                            send(Payload::LogsResponse(response), &mut seq)
                        )).await.is_err() {
                            watcher.abort();
                            return Ended::Disconnected("controller closed during a logs read".into());
                        }
                    }
                    Some(Payload::ResyncRequest(request)) => {
                        let slices: HashSet<Slice> = if request.slices.is_empty() {
                            ALL_SLICES.into_iter().collect()
                        } else {
                            request.slices.iter().filter_map(|s| slice_from(*s)).collect()
                        };
                        for slice in &slices {
                            state.forget(*slice);
                        }
                        if let Err(reason) =
                            push(engine, state, &mut sink, &mut seq, slices, false).await
                        {
                            watcher.abort();
                            return Ended::Disconnected(reason);
                        }
                    }
                    // Our certificate is two thirds through its life. Answer
                    // with a CSR, over the connection that is already open and
                    // already authenticated -- no cron job, no second channel,
                    // no expiry outage (ADR-0011).
                    Some(Payload::RenewalOffer(offer)) => {
                        match trust::new_request() {
                            Ok(request) => {
                                pending_key = Some(request.key_pem);
                                let frame = send(
                                    Payload::CertificateRequest(wire::CertificateRequest {
                                        csr_pem: request.csr_pem,
                                    }),
                                    &mut seq,
                                );
                                if sink.send(WsMessage::Binary(frame)).await.is_err() {
                                    watcher.abort();
                                    return Ended::Disconnected(
                                        "controller closed during renewal".into(),
                                    );
                                }
                            }
                            // Not fatal. The certificate is still valid for a
                            // third of its life, and the offer comes again on
                            // every connection until one of them works.
                            Err(e) => log(&format!(
                                "cannot renew (certificate expires at {}): {e}",
                                offer.not_after
                            )),
                        }
                    }

                    Some(Payload::CertificateIssued(issued)) => {
                        // The key is the one we generated for the CSR that
                        // asked for this. Without it the certificate is
                        // useless, so a reply we did not ask for is dropped
                        // rather than written over a working identity.
                        match (issued.ok, pending_key.take()) {
                            (true, Some(key_pem)) => {
                                match Credentials::replace(
                                    &config.state_dir,
                                    &issued.certificate_pem,
                                    &key_pem,
                                    &issued.ca_pem,
                                ) {
                                    // Takes effect on the next connection,
                                    // which is where the credentials are read.
                                    // Renegotiating this one would buy nothing:
                                    // the old certificate is valid until it
                                    // is not.
                                    Ok(()) => log(&format!(
                                        "certificate renewed, valid until {}",
                                        issued.not_after
                                    )),
                                    Err(e) => log(&format!(
                                        "renewed certificate could not be stored in {}: {e}",
                                        config.state_dir.display()
                                    )),
                                }
                            }
                            (false, _) => log(&format!("renewal refused: {}", issued.reason)),
                            (true, None) => log("ignoring a certificate we did not ask for"),
                        }
                    }

                    // Unknown frames are ignored, not fatal. A Controller from
                    // a later release may send what this agent predates, and
                    // mixed versions are a normal operating state.
                    _ => {}
                }
            }
        }

        // The watch died. Reconnecting re-Lists, which is necessary: we have
        // no idea what happened while we were not looking.
        if watcher.is_finished() {
            let reason = match watcher.await {
                Ok(Err(e)) => format!("event stream failed: {e}"),
                _ => "event stream ended".to_string(),
            };
            return Ended::Disconnected(reason);
        }
    }
}

// --------------------------------------------------------------------------
// The pipe
// --------------------------------------------------------------------------

/// Anything the framing can run over.
///
/// Boxed rather than made a type parameter on purpose: a generic session would
/// be monomorphised into two complete copies of the informer, the command
/// path and the renewal path, in a binary whose size is a measured budget
/// (ARCHITECTURE §11). One virtual call per frame is not on any path that
/// matters — a busy host produces a handful of frames a minute.
pub trait IoStream: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> IoStream for T {}

type Io = Box<dyn IoStream>;

/// One type for both endpoints, which is what keeps everything below this
/// point ignorant of which one it is talking over.
type Upstream = WebSocketStream<MaybeTlsStream<Io>>;

type Sink = futures_util::stream::SplitSink<Upstream, WsMessage>;

/// The Host header has to be *something*, and nothing reads this one: a unix
/// socket is already the address. `ws://` rather than `wss://` is what selects
/// the plaintext branch of the handshake — there is no TLS to do over a pipe
/// whose only reachable end is a file on this machine.
const LOCAL_URL: &str = "ws://localhost/api/v1/agents/connect";

/// Open the stream, however this Controller is reached.
///
/// The two arms differ in the socket and in the connector, and in nothing
/// else. Both end in the same handshake against the same route, which is the
/// property `docs/MIGRATION.md` §4 is asking for: the local case is a
/// transport, not a second implementation.
async fn dial(config: &Config) -> Result<Upstream, Ended> {
    match &config.controller {
        Endpoint::Local(path) => {
            let socket = UnixStream::connect(path).await.map_err(|e| {
                Ended::Disconnected(format!("cannot reach the controller on {}: {e}", path.display()))
            })?;
            let request = LOCAL_URL
                .into_client_request()
                .map_err(|e| Ended::Refused(format!("unusable local endpoint: {e}")))?;
            open(request, Box::new(socket), None).await.map_err(Ended::Disconnected)
        }

        Endpoint::Remote(base) => {
            // Re-read on every connection, not held across them. A renewal that
            // arrived over the last stream wrote a new certificate, and this is
            // where it takes effect -- without it, automatic renewal would need
            // a restart to do anything, which is most of the point of it gone.
            let credentials = match Credentials::load(&config.state_dir) {
                Ok(Some(credentials)) => credentials,
                Ok(None) => return Err(Ended::Refused("this host's credentials are gone".into())),
                Err(e) => return Err(Ended::Refused(e)),
            };
            let connector = trust::mutual_connector(&credentials).map_err(Ended::Refused)?;

            let request = crate::connect_url(base)
                .into_client_request()
                .map_err(|e| Ended::Refused(format!("unusable controller URL: {e}")))?;
            let socket = tcp(&request).await.map_err(Ended::Disconnected)?;
            open(request, socket, Some(connector)).await.map_err(Ended::Disconnected)
        }
    }
}

/// Connect the TCP socket the TLS handshake will run over.
///
/// Dialled here rather than by `connect_async` so that both endpoints can
/// produce the same stream type. Name resolution and multi-address fallback
/// are `ToSocketAddrs`' job either way.
async fn tcp(request: &Request<()>) -> Result<Io, String> {
    let uri = request.uri();
    let host = uri.host().ok_or_else(|| format!("no host in {uri}"))?;
    let port = uri.port_u16().unwrap_or(443);
    let socket = TcpStream::connect((host, port))
        .await
        .map_err(|e| trust::explain(&format!("cannot reach the controller at {host}:{port}: {e}")))?;
    // Frames are small and infrequent, and latency is what the operator sees.
    // Nagle would hold a delta back waiting for company that is not coming.
    let _ = socket.set_nodelay(true);
    Ok(Box::new(socket))
}

async fn open(request: Request<()>, socket: Io, connector: Option<Connector>) -> Result<Upstream, String> {
    let (upstream, _) =
        tokio_tungstenite::client_async_tls_with_config(request, socket, None, connector)
            .await
            .map_err(|e| trust::explain(&format!("cannot reach the controller: {e}")))?;
    Ok(upstream)
}

/// Re-List the dirty slices and send whatever moved.
async fn push(
    engine: &Engine,
    state: &mut State,
    sink: &mut Sink,
    seq: &mut u64,
    slices: HashSet<Slice>,
    full: bool,
) -> Result<(), String> {
    for slice in ALL_SLICES {
        if !slices.contains(&slice) {
            continue;
        }
        match informer::scan(engine, state, slice, full).await {
            Ok(Some(payload)) => {
                *seq += 1;
                let frame = wire::envelope(*seq, payload).encode_to_vec();
                if sink.send(WsMessage::Binary(frame)).await.is_err() {
                    return Err("controller closed while sending".into());
                }
            }
            // Nothing moved in this slice. Not sending is the point.
            Ok(None) => {}
            Err(e) => {
                // One failed List is not worth dropping the connection over;
                // the resync loop will repair it shortly.
                log(&format!("list of {slice:?} failed: {e}"));
            }
        }
    }
    Ok(())
}

/// Execute one command against the local socket.
///
/// **The second choke point.** The Controller already refused everything it
/// should have, and this refuses again. That is not redundancy for its own
/// sake: this process holds root-equivalent access to a machine, and "the
/// Controller said so" is not an acceptable sole justification for acting on
/// it (ARCHITECTURE §9).
async fn execute(
    engine: &Engine,
    config: &Config,
    command: wire::Command,
) -> wire::CommandResult {
    if config.read_only {
        return wire::CommandResult {
            command_id: command.command_id,
            ok: false,
            detail: "this agent is configured read-only and refuses mutations".into(),
            unchanged: false,
        };
    }

    // An allow-list, not a pass-through. The verb becomes a path segment on an
    // API running as root, and anything outside this set has no business
    // reaching it -- including anything that would traverse out of the path.
    const VERBS: [&str; 6] = ["start", "stop", "restart", "pause", "unpause", "kill"];
    if !VERBS.contains(&command.verb.as_str()) {
        return wire::CommandResult {
            command_id: command.command_id,
            ok: false,
            detail: format!("unsupported verb {:?}", command.verb),
            unchanged: false,
        };
    }
    if command.target_id.is_empty()
        || !command.target_id.chars().all(|c| c.is_ascii_alphanumeric())
    {
        return wire::CommandResult {
            command_id: command.command_id,
            ok: false,
            detail: "target is not a container id".into(),
            unchanged: false,
        };
    }

    match engine.act(&command.target_id, &command.verb, &command.args).await {
        Ok((ActionResult::Applied, _)) => wire::CommandResult {
            command_id: command.command_id,
            ok: true,
            detail: String::new(),
            unchanged: false,
        },
        Ok((ActionResult::Unchanged, _)) => wire::CommandResult {
            command_id: command.command_id,
            ok: true,
            detail: String::new(),
            unchanged: true,
        },
        Ok((ActionResult::Failed, detail)) => wire::CommandResult {
            command_id: command.command_id,
            ok: false,
            detail,
            unchanged: false,
        },
        Err(e) => wire::CommandResult {
            command_id: command.command_id,
            ok: false,
            detail: e.to_string(),
            unchanged: false,
        },
    }
}

/// Answer a logs read, or say why not.
///
/// The same id validation the lifecycle path uses, for the same reason: the
/// target goes into a URL against an API that runs as root, and a container
/// id is hexadecimal. A refusal here is a bug on the Controller's side, so it
/// is reported rather than logged and dropped.
async fn fetch_logs(engine: &Engine, request: wire::LogsRequest) -> wire::LogsResponse {
    if request.target_id.is_empty()
        || !request.target_id.chars().all(|c| c.is_ascii_alphanumeric())
    {
        return wire::LogsResponse {
            request_id: request.request_id,
            ok: false,
            reason: "target is not a container id".into(),
            lines: Vec::new(),
        };
    }

    match engine.logs(&request.target_id, request.tail).await {
        Ok(lines) => wire::LogsResponse {
            request_id: request.request_id,
            ok: true,
            reason: String::new(),
            lines: lines
                .into_iter()
                .map(|line| wire::LogLine { stderr: line.stderr, text: line.text })
                .collect(),
        },
        Err(e) => wire::LogsResponse {
            request_id: request.request_id,
            ok: false,
            // The engine's own words. An operator chasing a failure should
            // not have to guess whether the container or the request was
            // wrong.
            reason: e.to_string(),
            lines: Vec::new(),
        },
    }
}

fn slice_from(value: i32) -> Option<Slice> {
    match value {
        1 => Some(Slice::Container),
        2 => Some(Slice::Network),
        3 => Some(Slice::Volume),
        4 => Some(Slice::Image),
        _ => None,
    }
}

pub fn unix_time() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

pub fn log(message: &str) {
    println!("bystack-agent: {message}");
}
