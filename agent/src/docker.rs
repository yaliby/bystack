//! The local Docker Engine, over its unix socket.
//!
//! Never leaves the host. This is the whole reason the agent exists: the
//! socket is root-equivalent, and ADR-0008's bet is that a small process
//! sitting next to it is safer than exposing it to a Controller somewhere
//! else.
//!
//! We speak the Engine HTTP API directly. Paths are unversioned, so the
//! daemon maps them to the newest version it supports and upgrading Docker on
//! a managed host does not require a new agent.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use http_body_util::{BodyExt, Empty};
use hyper::body::Bytes;
use hyper::{Request, StatusCode};
use hyper_util::rt::TokioIo;
use tokio::net::UnixStream;
use tokio::sync::mpsc;

use crate::model::{Container, Event, Image, Info, Network, Volume, VolumeList};

/// Placeholder authority. HTTP requires a Host header; nothing resolves it.
const HOST: &str = "docker";

#[derive(Debug)]
pub enum EngineError {
    Connect(std::io::Error),
    Http(String),
    Decode(String),
    Status(StatusCode, String),
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Connect(e) => write!(f, "cannot reach the docker socket: {e}"),
            Self::Http(e) => write!(f, "http error: {e}"),
            Self::Decode(e) => write!(f, "malformed response: {e}"),
            Self::Status(code, body) => write!(f, "engine returned {code}: {body}"),
        }
    }
}

/// What the engine said about a lifecycle request.
///
/// `Unchanged` is Docker's `304` and is kept distinct all the way to the
/// Controller: starting an already-running container is neither an error nor
/// a change, and folding it into success would report a restart that did
/// nothing as a restart that worked.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ActionResult {
    Applied,
    Unchanged,
    Failed,
}

pub struct Engine {
    socket: PathBuf,
}

impl Engine {
    pub fn new(socket: impl AsRef<Path>) -> Self {
        Self { socket: socket.as_ref().to_path_buf() }
    }

    pub fn socket(&self) -> &Path {
        &self.socket
    }

    // -- reads ------------------------------------------------------------

    pub async fn info(&self) -> Result<Info, EngineError> {
        self.get_json("/info").await
    }

    pub async fn containers(&self) -> Result<Vec<Container>, EngineError> {
        self.get_json("/containers/json?all=1").await
    }

    pub async fn networks(&self) -> Result<Vec<Network>, EngineError> {
        self.get_json("/networks").await
    }

    pub async fn volumes(&self) -> Result<Vec<Volume>, EngineError> {
        let list: VolumeList = self.get_json("/volumes").await?;
        Ok(list.volumes)
    }

    pub async fn images(&self) -> Result<Vec<Image>, EngineError> {
        self.get_json("/images/json").await
    }

    // -- lifecycle --------------------------------------------------------

    pub async fn act(
        &self,
        container_id: &str,
        verb: &str,
        args: &HashMap<String, String>,
    ) -> Result<(ActionResult, String), EngineError> {
        let mut path = format!("/containers/{container_id}/{verb}");
        // Only the arguments each verb actually takes. Forwarding everything
        // would hand the Controller a way to set query parameters we have not
        // reviewed on an API that runs as root.
        match verb {
            "stop" | "restart" => {
                if let Some(t) = args.get("timeout") {
                    path.push_str(&format!("?t={t}"));
                }
            }
            "kill" => {
                if let Some(signal) = args.get("signal") {
                    path.push_str(&format!("?signal={signal}"));
                }
            }
            _ => {}
        }

        let (status, body) = self.request("POST", &path).await?;
        Ok(match status.as_u16() {
            204 => (ActionResult::Applied, String::new()),
            304 => (ActionResult::Unchanged, String::new()),
            _ => (ActionResult::Failed, engine_message(&body, status)),
        })
    }

    // -- event stream -----------------------------------------------------

    /// Stream `GET /events`, sending each decoded event's type to `sink`.
    ///
    /// Filters are applied **server-side**, so `exec_start`, `exec_die` and
    /// health-probe churn are discarded by the daemon and never reach this
    /// process, its CPU, or its JSON parser.
    ///
    /// `since` must have been recorded *before* the initial List. See
    /// [`crate::informer`] — this is the ordering that makes the watch
    /// gapless, and getting it backwards loses changes with no way to detect
    /// that it happened.
    pub async fn watch(&self, since: i64, sink: mpsc::Sender<String>) -> Result<(), EngineError> {
        let filters = r#"{"type":["container","network","volume"]}"#;
        let path = format!(
            "/events?since={since}&filters={}",
            urlencode(filters)
        );

        let stream = UnixStream::connect(&self.socket).await.map_err(EngineError::Connect)?;
        let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
            .await
            .map_err(|e| EngineError::Http(e.to_string()))?;
        tokio::spawn(async move {
            let _ = conn.await;
        });

        let request = Request::builder()
            .method("GET")
            .uri(&path)
            .header("Host", HOST)
            .body(Empty::<Bytes>::new())
            .map_err(|e| EngineError::Http(e.to_string()))?;

        let mut response = sender
            .send_request(request)
            .await
            .map_err(|e| EngineError::Http(e.to_string()))?;

        if !response.status().is_success() {
            return Err(EngineError::Status(response.status(), String::new()));
        }

        // Docker emits newline-delimited JSON. Chunk boundaries land wherever
        // the transport put them, so a line can span two frames and a frame
        // can hold several lines.
        let mut pending = Vec::<u8>::new();
        while let Some(frame) = response.frame().await {
            let frame = frame.map_err(|e| EngineError::Http(e.to_string()))?;
            let Some(chunk) = frame.data_ref() else { continue };
            pending.extend_from_slice(chunk);

            while let Some(newline) = pending.iter().position(|b| *b == b'\n') {
                let line: Vec<u8> = pending.drain(..=newline).collect();
                let Ok(event) = serde_json::from_slice::<Event>(&line) else {
                    // A malformed line is not worth tearing down a healthy
                    // stream over; the periodic resync repairs anything it
                    // caused us to miss.
                    continue;
                };
                if !event.kind.is_empty() && sink.send(event.kind).await.is_err() {
                    return Ok(()); // the informer went away
                }
            }
        }

        // The stream ended without an error, which means the daemon closed
        // it. Reported as an error so the supervisor re-Lists: we have no idea
        // what happened while we were not looking.
        Err(EngineError::Http("event stream closed by the daemon".into()))
    }

    // -- internals --------------------------------------------------------

    async fn get_json<T: serde::de::DeserializeOwned>(&self, path: &str) -> Result<T, EngineError> {
        let (status, body) = self.request("GET", path).await?;
        if !status.is_success() {
            return Err(EngineError::Status(status, engine_message(&body, status)));
        }
        serde_json::from_slice(&body).map_err(|e| EngineError::Decode(e.to_string()))
    }

    /// One request on its own connection.
    ///
    /// Not pooled. The agent makes a handful of requests per burst against a
    /// socket on the same kernel, where a connect is measured in microseconds;
    /// a pool would add state to keep correct across daemon restarts in
    /// exchange for nothing measurable.
    async fn request(&self, method: &str, path: &str) -> Result<(StatusCode, Bytes), EngineError> {
        let stream = UnixStream::connect(&self.socket).await.map_err(EngineError::Connect)?;
        let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
            .await
            .map_err(|e| EngineError::Http(e.to_string()))?;
        tokio::spawn(async move {
            let _ = conn.await;
        });

        let request = Request::builder()
            .method(method)
            .uri(path)
            .header("Host", HOST)
            .body(Empty::<Bytes>::new())
            .map_err(|e| EngineError::Http(e.to_string()))?;

        let response = sender
            .send_request(request)
            .await
            .map_err(|e| EngineError::Http(e.to_string()))?;
        let status = response.status();
        let body = response
            .into_body()
            .collect()
            .await
            .map_err(|e| EngineError::Http(e.to_string()))?
            .to_bytes();
        Ok((status, body))
    }
}

/// The daemon's own explanation, which is better than any we could write.
fn engine_message(body: &Bytes, status: StatusCode) -> String {
    #[derive(serde::Deserialize)]
    struct Message {
        message: String,
    }
    match serde_json::from_slice::<Message>(body) {
        Ok(parsed) if !parsed.message.is_empty() => parsed.message,
        _ => format!("HTTP {status}"),
    }
}

/// Percent-encode a query value.
///
/// Hand-rolled rather than pulling a crate: the only thing we ever encode is
/// our own fixed filter JSON, and a dependency for that would be larger than
/// the function.
fn urlencode(value: &str) -> String {
    let mut out = String::with_capacity(value.len() * 3);
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}
