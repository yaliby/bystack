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

use crate::model::{Container, Event, Image, Info, Inspect, Network, Volume, VolumeList};

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

/// Cloneable, and cheaply: it is a socket path and nothing else. No pool, no
/// shared connection, no state to keep consistent between copies -- every
/// request already opens its own connection (see `request`). A log
/// subscription needs an owned `Engine` to hand to a spawned task, and this is
/// the whole of what that costs.
#[derive(Clone)]
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

    /// How many times the engine has restarted one container.
    ///
    /// One inspect, for one integer. `RestartCount` is not in
    /// `GET /containers/json` at any API version, so this is the only place
    /// it can come from — and the caller ([`crate::informer`]) is expected to
    /// ask only for containers whose listed state is `restarting`, which is a
    /// bounded and usually empty set. Calling it per container on every List
    /// would turn one request per slice into one per container, which is the
    /// change this design exists to avoid.
    pub async fn restart_count(&self, container_id: &str) -> Result<u32, EngineError> {
        let path = format!("/containers/{}/json", urlencode(container_id));
        let inspect: Inspect = self.get_json(&path).await?;
        Ok(inspect.restart_count)
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

    /// The last `tail` lines a container wrote, both streams, demultiplexed.
    ///
    /// `tail` is applied by the daemon, which is what makes this answerable
    /// from a process with a measured memory budget on a container that has
    /// been logging for a month. It is clamped here as well: the Controller
    /// asking for everything is still the agent's problem to survive.
    pub async fn logs(&self, container_id: &str, tail: u32) -> Result<Vec<LogLine>, EngineError> {
        let tail = tail.clamp(1, MAX_LOG_LINES);
        let path = format!(
            "/containers/{}/logs?stdout=1&stderr=1&timestamps=1&tail={tail}",
            urlencode(container_id)
        );
        let (status, body) = self.request("GET", &path).await?;
        if !status.is_success() {
            return Err(EngineError::Status(status, engine_message(&body, status)));
        }
        Ok(demultiplex(&body, tail as usize))
    }

    /// Follow a container's log, sending each batch of lines to `sink`.
    ///
    /// The streaming sibling of [`Engine::logs`], and the same demultiplexing
    /// on a stream that never ends rather than a body that does. `tail` is the
    /// backfill the daemon sends before the live tail begins, so opening a
    /// panel on a container that died a minute ago shows why.
    ///
    /// **Batching is deliberate and is the backpressure story.** A container
    /// in a hot loop can write thousands of lines a second, and one frame per
    /// line would put that rate on the Controller's WebSocket and on the
    /// browser. Everything decoded from one HTTP chunk is sent as one batch;
    /// the daemon's own chunking is the natural rate limit, and a chatty
    /// container costs frames proportional to its *output*, not to its line
    /// count.
    ///
    /// Returns when the daemon closes the stream -- the container exited, or
    /// was removed. That is a normal end and not an error: the caller reports
    /// it as `done` so the operator knows the silence is the end.
    ///
    /// Cancellation is the caller's job, by dropping the future. Every await
    /// here is cancel-safe in the only sense that matters: nothing outside
    /// this function is left half-written, because the only thing it mutates
    /// is `sink`.
    pub async fn follow_logs(
        &self,
        container_id: &str,
        tail: u32,
        sink: mpsc::Sender<Vec<LogLine>>,
    ) -> Result<(), EngineError> {
        let tail = tail.clamp(1, MAX_LOG_LINES);
        let path = format!(
            "/containers/{}/logs?stdout=1&stderr=1&timestamps=1&follow=1&tail={tail}",
            urlencode(container_id)
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
            let status = response.status();
            let mut body = Vec::new();
            while let Some(frame) = response.frame().await {
                let Ok(frame) = frame else { break };
                if let Some(chunk) = frame.data_ref() {
                    body.extend_from_slice(chunk);
                }
            }
            return Err(EngineError::Status(
                status,
                engine_message(&Bytes::from(body), status),
            ));
        }

        // Docker's 8-byte frame header can be split across HTTP chunks, and a
        // line can span two of them. `pending` holds whatever is not yet a
        // whole log frame, exactly as the event stream holds a partial line.
        let mut pending = Vec::<u8>::new();
        while let Some(frame) = response.frame().await {
            let frame = frame.map_err(|e| EngineError::Http(e.to_string()))?;
            let Some(chunk) = frame.data_ref() else { continue };
            pending.extend_from_slice(chunk);

            // `usize::MAX` because tailing is the daemon's job on the backfill
            // and meaningless on the live portion -- every line that arrives
            // here is one the operator asked to see.
            let (lines, consumed) = demultiplex_prefix(&pending, usize::MAX);
            if consumed > 0 {
                pending.drain(..consumed);
            }
            if !lines.is_empty() && sink.send(lines).await.is_err() {
                return Ok(()); // nobody is reading any more
            }
        }

        Ok(())
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

/// One line a container wrote, and which stream it wrote it on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogLine {
    pub stderr: bool,
    pub text: String,
}

/// Ceiling on a single logs answer, regardless of what was asked for.
pub const MAX_LOG_LINES: u32 = 2000;

/// Split Docker's log body into lines, tagged with the stream they came from.
///
/// Two wire formats, and the daemon does not tell you which one you are
/// getting -- it depends on whether the container was created with a TTY.
///
/// * **Without a TTY** the body is framed: an 8-byte header per chunk, where
///   byte 0 is the stream (1 = stdout, 2 = stderr), bytes 1..4 are zero and
///   bytes 4..8 are the payload length, big-endian.
/// * **With a TTY** both streams are already merged by the daemon and the
///   body is the raw bytes.
///
/// The discriminator is the header shape itself, which is why the check is
/// what it is: a first byte of 0/1/2 followed by three zero bytes is not a
/// thing a plausible log line starts with, and a TTY container's output would
/// have to begin with a control character and two NULs to be mistaken for a
/// frame. Guessing wrong on a TTY container renders the header as mojibake at
/// the start of every line, which is the failure this comment exists to make
/// findable.
pub fn demultiplex(body: &[u8], tail: usize) -> Vec<LogLine> {
    let mut lines = Vec::new();
    if framed(body) {
        let mut at = 0usize;
        while at + 8 <= body.len() {
            let stderr = body[at] == 2;
            let length = u32::from_be_bytes([body[at + 4], body[at + 5], body[at + 6], body[at + 7]])
                as usize;
            at += 8;
            let end = (at + length).min(body.len());
            push_lines(&mut lines, &body[at..end], stderr);
            // A truncated final frame ends the body; `end` is already clamped
            // so this terminates rather than looping on a short read.
            if end == at && length > 0 {
                break;
            }
            at = end;
        }
    } else {
        push_lines(&mut lines, body, false);
    }

    // The daemon applies `tail` per stream, not to the merged result, so a
    // container writing to both can come back with more than was asked for.
    if lines.len() > tail {
        lines.drain(..lines.len() - tail);
    }
    lines
}

/// Demultiplex only what is *complete*, and say how much that was.
///
/// The streaming sibling of [`demultiplex`], and a sibling rather than a
/// generalisation because the two disagree about exactly one case, in
/// opposite and correct directions: **a truncated final frame.**
///
/// [`demultiplex`] has the whole body, so a short final frame is all there
/// will ever be and showing its bytes beats discarding them. Here another
/// chunk is almost certainly a few milliseconds away, so the same bytes are
/// an incomplete frame to hold onto — emitting them would split one log line
/// across two, permanently, every time a line straddled a chunk boundary.
///
/// Returns the lines and the number of bytes the caller may drop. Whatever is
/// left is a partial frame and must be kept.
pub fn demultiplex_prefix(body: &[u8], tail: usize) -> (Vec<LogLine>, usize) {
    let mut lines = Vec::new();
    let mut consumed = 0usize;

    if framed(body) {
        let mut at = 0usize;
        while at + 8 <= body.len() {
            let stderr = body[at] == 2;
            let length =
                u32::from_be_bytes([body[at + 4], body[at + 5], body[at + 6], body[at + 7]])
                    as usize;
            let start = at + 8;
            let Some(end) = start.checked_add(length) else { break };
            if end > body.len() {
                // The payload has not all arrived. Stop here and keep it.
                break;
            }
            push_lines(&mut lines, &body[start..end], stderr);
            at = end;
            consumed = at;
        }
    } else if let Some(newline) = body.iter().rposition(|b| *b == b'\n') {
        // A TTY container: no framing at all, just bytes. The last newline is
        // the only place a line is known to have ended.
        push_lines(&mut lines, &body[..=newline], false);
        consumed = newline + 1;
    }

    if lines.len() > tail {
        lines.drain(..lines.len() - tail);
    }
    (lines, consumed)
}

fn framed(body: &[u8]) -> bool {
    body.len() >= 8 && body[0] <= 2 && body[1] == 0 && body[2] == 0 && body[3] == 0
}

fn push_lines(out: &mut Vec<LogLine>, chunk: &[u8], stderr: bool) {
    // Lossy on purpose. A log line is whatever the process wrote, which is
    // not guaranteed to be UTF-8, and refusing to show an operator the line
    // that explains their outage because of one bad byte is the wrong answer.
    for line in String::from_utf8_lossy(chunk).lines() {
        if !line.is_empty() {
            out.push(LogLine { stderr, text: line.to_string() });
        }
    }
}

#[cfg(test)]
mod log_tests {
    use super::*;

    fn frame(stream: u8, payload: &str) -> Vec<u8> {
        let mut out = vec![stream, 0, 0, 0];
        out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        out.extend_from_slice(payload.as_bytes());
        out
    }

    #[test]
    fn a_partial_frame_is_held_back_rather_than_split() {
        // The bug this function exists to prevent. A log line straddling two
        // HTTP chunks must arrive once and whole; emitting the first half
        // would break every line that crossed a chunk boundary, which on a
        // busy container is a steady fraction of them.
        let whole = frame(1, "first line\n");
        let mut partial = whole.clone();
        partial.extend(frame(2, "second line\n"));
        let cut = whole.len() + 12; // mid-payload of the second frame

        let (lines, consumed) = demultiplex_prefix(&partial[..cut], usize::MAX);

        assert_eq!(lines.len(), 1, "only the complete frame is emitted");
        assert_eq!(lines[0].text, "first line");
        assert_eq!(consumed, whole.len(), "the partial frame is left in the buffer");
    }

    #[test]
    fn the_held_back_bytes_complete_on_the_next_chunk() {
        let mut body = frame(1, "first line\n");
        body.extend(frame(2, "second line\n"));
        let cut = body.len() - 5;

        let (first, consumed) = demultiplex_prefix(&body[..cut], usize::MAX);
        // What a caller does: drop what was consumed, keep the rest, append.
        let mut pending = body[consumed..cut].to_vec();
        pending.extend_from_slice(&body[cut..]);
        let (second, _) = demultiplex_prefix(&pending, usize::MAX);

        let texts: Vec<&str> = first.iter().chain(second.iter()).map(|l| &*l.text).collect();
        assert_eq!(texts, ["first line", "second line"]);
        assert!(second.iter().any(|l| l.stderr), "the stream tag survives the split");
    }

    #[test]
    fn a_tty_stream_consumes_only_through_the_last_newline() {
        // No framing at all, so the only place a line is known to have ended
        // is a newline. A trailing partial line waits for the rest.
        let body = b"complete line\nhalf a li";

        let (lines, consumed) = demultiplex_prefix(body, usize::MAX);

        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0].text, "complete line");
        assert_eq!(consumed, 14);
    }

    #[test]
    fn a_framed_body_keeps_the_stream_each_line_came_from() {
        // The whole diagnostic value: the line explaining a crash is almost
        // always the one on stderr.
        let mut body = frame(1, "listening on 8080\n");
        body.extend(frame(2, "panic: cannot bind\n"));

        let lines = demultiplex(&body, 100);

        assert_eq!(lines.len(), 2);
        assert!(!lines[0].stderr);
        assert_eq!(lines[0].text, "listening on 8080");
        assert!(lines[1].stderr);
        assert_eq!(lines[1].text, "panic: cannot bind");
    }

    #[test]
    fn one_frame_may_carry_several_lines() {
        let body = frame(1, "one\ntwo\nthree\n");
        assert_eq!(demultiplex(&body, 100).len(), 3);
    }

    #[test]
    fn a_tty_container_has_no_frame_headers_and_is_read_raw() {
        // Docker merges both streams itself when the container has a TTY and
        // sends the bytes unframed. Reading those 8 bytes as a header renders
        // mojibake at the start of every line.
        let lines = demultiplex(b"plain line\nanother\n", 100);

        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0].text, "plain line");
        assert!(!lines[0].stderr);
    }

    #[test]
    fn the_merged_result_is_trimmed_to_what_was_asked_for() {
        // Docker applies `tail` per stream, so a container writing to both
        // can answer with more lines than were requested.
        let mut body = frame(1, "a\nb\nc\n");
        body.extend(frame(2, "d\ne\nf\n"));

        let lines = demultiplex(&body, 2);

        assert_eq!(lines.len(), 2);
        assert_eq!(lines[1].text, "f");
    }

    #[test]
    fn a_truncated_final_frame_does_not_loop_forever() {
        // A body cut mid-frame is what a daemon restart looks like from here.
        let mut body = frame(1, "complete\n");
        body.extend_from_slice(&[1, 0, 0, 0, 0, 0, 0, 200]);
        body.extend_from_slice(b"short");

        let lines = demultiplex(&body, 100);

        assert_eq!(lines[0].text, "complete");
    }

    #[test]
    fn a_line_that_is_not_utf8_is_shown_rather_than_dropped() {
        // Refusing to show the line that explains an outage because of one
        // bad byte is the wrong answer.
        let mut body = vec![1u8, 0, 0, 0];
        let payload = b"bad \xff byte\n";
        body.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        body.extend_from_slice(payload);

        assert_eq!(demultiplex(&body, 100).len(), 1);
    }
}
