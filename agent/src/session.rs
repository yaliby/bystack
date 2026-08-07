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
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message as WsMessage;

use crate::docker::{ActionResult, Engine};
use crate::informer::{self, State, ALL_SLICES};
use crate::wire::{self, envelope::Payload, Slice};
use crate::Config;

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

    let (socket, _) = match tokio_tungstenite::connect_async(&config.controller_url).await {
        Ok(pair) => pair,
        Err(e) => return Ended::Disconnected(format!("cannot reach the controller: {e}")),
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
        capabilities: vec!["commands".into(), "resync".into()],
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
        // "certificate revoked" does not improve by reconnecting.
        return Ended::Refused(if ack.reason.is_empty() {
            "controller refused this agent".into()
        } else {
            ack.reason
        });
    }

    let resync_interval = Duration::from_secs(if ack.resync_interval == 0 {
        900
    } else {
        ack.resync_interval as u64
    });
    log(&format!(
        "connected to {} (epoch {}, resync {}s)",
        config.controller_url,
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

type Sink = futures_util::stream::SplitSink<
    tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
    WsMessage,
>;

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
