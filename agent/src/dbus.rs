//! A D-Bus client, in the amount this agent needs and no more.
//!
//! The same call `Cargo.toml` records for `hyper` and ADR-0013 records for the
//! whole binary, made once more and in the other direction. There we took a
//! dependency because the chunked-transfer decoder sits on the path that
//! carries every event and its correctness is the whole real-time story; here
//! we do not, because what systemd asks of a client is a fixed-width header, a
//! type-length-value body and a four-line SASL exchange — and the alternative
//! costs a dependency tree measured against a binary that is measured
//! (ARCHITECTURE §11).
//!
//! What is implemented: the client half of the handshake, method calls,
//! replies, errors, and signals. What is not: server behaviour, unix fd
//! passing, and every authentication mechanism except `EXTERNAL`, which is the
//! one a local process on the same kernel uses and the only one whose
//! credentials the kernel itself vouches for.
//!
//! **Two ways in, and the second is not a fallback for its own sake.** The
//! system bus is the ordinary route. `/run/systemd/private` is a direct peer
//! connection to PID 1 that exists whether or not `dbus-daemon` is running,
//! which is exactly the shape of a minimal container image — the environment
//! `docs/OPEN-WORK.md`'s verification runs in, and one where the ordinary
//! route is simply absent.
//!
//! ## The one rule that matters when editing this
//!
//! **Alignment is measured from the start of the message, not from the start
//! of the field.** Every type has an alignment (8 for `t`, 4 for `u`, and so
//! on) and a value begins at the next multiple of it. Get that wrong and the
//! bytes still parse — as different values — which is the failure mode this
//! module is written to make hard: one `align_to` on both sides, and no
//! hand-counted offsets anywhere.

use std::collections::VecDeque;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;

/// The system bus, where systemd normally answers.
const SYSTEM_BUS: &str = "/run/dbus/system_bus_socket";

/// PID 1's own socket. No bus daemon in the middle, so no `Hello` and no
/// destination on any message — it is a direct peer connection, and it is
/// what `systemctl` itself falls back to.
const PRIVATE_BUS: &str = "/run/systemd/private";

/// Refuse a message larger than this before allocating for it.
///
/// The spec's own ceiling is 128 MiB. Ours is the agent's memory budget: a
/// `GetAll` on a unit is a few kilobytes and `ListUnits` on a large machine is
/// a few hundred, so anything past this is a bug at the far end or a reply we
/// have lost our place in — and allocating for it on somebody else's hardware
/// is the failure this bound exists to refuse.
const MAX_MESSAGE: u32 = 8 * 1024 * 1024;

const LITTLE_ENDIAN: u8 = b'l';
const PROTOCOL_VERSION: u8 = 1;

const MSG_METHOD_CALL: u8 = 1;
const MSG_METHOD_RETURN: u8 = 2;
const MSG_ERROR: u8 = 3;
const MSG_SIGNAL: u8 = 4;

const FIELD_PATH: u8 = 1;
const FIELD_INTERFACE: u8 = 2;
const FIELD_MEMBER: u8 = 3;
const FIELD_ERROR_NAME: u8 = 4;
const FIELD_REPLY_SERIAL: u8 = 5;
const FIELD_DESTINATION: u8 = 6;
const FIELD_SIGNATURE: u8 = 8;

#[derive(Debug)]
pub enum DbusError {
    /// No socket, or nobody listening on it. The overwhelmingly common
    /// failure, and not an error worth a stack trace: a machine without
    /// systemd is a machine where this feature does not apply.
    Connect(String),
    Io(std::io::Error),
    Auth(String),
    /// The peer answered, and the answer was "no". Carries D-Bus's own error
    /// name and message, both of which are written for a person.
    Call { name: String, message: String },
    Protocol(String),
}

impl std::fmt::Display for DbusError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Connect(e) => write!(f, "cannot reach systemd: {e}"),
            Self::Io(e) => write!(f, "bus i/o failed: {e}"),
            Self::Auth(e) => write!(f, "bus authentication failed: {e}"),
            Self::Call { name, message } => write!(f, "{name}: {message}"),
            Self::Protocol(e) => write!(f, "malformed bus message: {e}"),
        }
    }
}

impl From<std::io::Error> for DbusError {
    fn from(e: std::io::Error) -> Self {
        Self::Io(e)
    }
}

// --------------------------------------------------------------------------
// Values
// --------------------------------------------------------------------------

/// A decoded D-Bus value.
///
/// Complete on the read side rather than only the parts we consume, and that
/// is a decision rather than thoroughness for its own sake: `GetAll` returns a
/// dictionary of variants holding whatever systemd felt like putting there —
/// exec-start arrays, address structs, rate-limit pairs — and a parser that
/// could not represent one of them could not *skip past* it either. Every
/// value after the one it choked on would be read at the wrong offset, and the
/// symptom would be plausible-looking wrong numbers rather than an error.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Byte(u8),
    Bool(bool),
    I16(i16),
    U16(u16),
    I32(i32),
    U32(u32),
    I64(i64),
    U64(u64),
    F64(f64),
    Str(String),
    Path(String),
    Signature(String),
    Array(Vec<Value>),
    Struct(Vec<Value>),
    Variant(Box<Value>),
    DictEntry(Box<Value>, Box<Value>),
}

impl Value {
    /// The string inside, through any number of variants.
    ///
    /// Everything read out of `GetAll` arrives wrapped in a variant, and every
    /// caller wants what is underneath; unwrapping at each call site would be
    /// the same three lines in eleven places.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::Str(s) | Self::Path(s) | Self::Signature(s) => Some(s),
            Self::Variant(inner) => inner.as_str(),
            _ => None,
        }
    }

    /// The integer inside, whatever width it was sent as.
    ///
    /// Widths are systemd's business, not ours: `MainPID` is a `u`,
    /// `ActiveEnterTimestamp` is a `t`, and a caller that had to know which
    /// would break the day one of them changed.
    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Self::Byte(v) => Some(u64::from(*v)),
            Self::U16(v) => Some(u64::from(*v)),
            Self::U32(v) => Some(u64::from(*v)),
            Self::U64(v) => Some(*v),
            Self::I16(v) => u64::try_from(*v).ok(),
            Self::I32(v) => u64::try_from(*v).ok(),
            Self::I64(v) => u64::try_from(*v).ok(),
            Self::Bool(v) => Some(u64::from(*v)),
            Self::Variant(inner) => inner.as_u64(),
            _ => None,
        }
    }

    /// Look one key up in an `a{sv}`, the shape every `GetAll` answers in.
    pub fn field(&self, key: &str) -> Option<&Value> {
        let Self::Array(entries) = self else {
            return None;
        };
        entries.iter().find_map(|entry| match entry {
            Self::DictEntry(k, v) if k.as_str() == Some(key) => Some(v.as_ref()),
            _ => None,
        })
    }
}

// --------------------------------------------------------------------------
// Signatures
// --------------------------------------------------------------------------

/// Split one complete type off the front of a signature.
///
/// "Complete" is the whole subtlety: `aa{sv}` is one type, and so is
/// `(sasbttttuii)`. Reading a container's element type by taking one character
/// is the bug that makes nested values decode at the wrong offset.
fn split_type(signature: &str) -> Result<(&str, &str), DbusError> {
    let bytes = signature.as_bytes();
    if bytes.is_empty() {
        return Err(DbusError::Protocol("empty signature".into()));
    }
    let mut end = 0;
    // Array markers stack: `aai` is an array of arrays of int, and the type
    // does not end until the element type does.
    while end < bytes.len() && bytes[end] == b'a' {
        end += 1;
    }
    if end >= bytes.len() {
        return Err(DbusError::Protocol(format!("dangling array in {signature:?}")));
    }
    match bytes[end] {
        b'(' | b'{' => {
            let open = bytes[end];
            let close = if open == b'(' { b')' } else { b'}' };
            let mut depth = 0;
            while end < bytes.len() {
                if bytes[end] == open {
                    depth += 1;
                } else if bytes[end] == close {
                    depth -= 1;
                    if depth == 0 {
                        end += 1;
                        break;
                    }
                }
                end += 1;
            }
            if depth != 0 {
                return Err(DbusError::Protocol(format!("unbalanced {signature:?}")));
            }
        }
        _ => end += 1,
    }
    Ok((&signature[..end], &signature[end..]))
}

/// Where a value of this type may begin, relative to the start of the message.
fn alignment(signature: &str) -> usize {
    match signature.as_bytes().first().copied().unwrap_or(b'y') {
        b'y' | b'g' | b'v' => 1,
        b'n' | b'q' => 2,
        b'b' | b'i' | b'u' | b's' | b'o' | b'a' => 4,
        _ => 8, // x t d ( {
    }
}

// --------------------------------------------------------------------------
// Encoding
// --------------------------------------------------------------------------

#[derive(Default)]
struct Encoder {
    bytes: Vec<u8>,
}

impl Encoder {
    /// Pad to the next multiple of `to`.
    ///
    /// The one place padding is written, so "aligned relative to the start of
    /// the message" is a property of this function rather than a rule every
    /// call site has to remember.
    fn align(&mut self, to: usize) {
        while self.bytes.len() % to != 0 {
            self.bytes.push(0);
        }
    }

    fn u8(&mut self, value: u8) {
        self.bytes.push(value);
    }

    fn u32(&mut self, value: u32) {
        self.align(4);
        self.bytes.extend_from_slice(&value.to_le_bytes());
    }

    fn i32(&mut self, value: i32) {
        self.align(4);
        self.bytes.extend_from_slice(&value.to_le_bytes());
    }

    fn string(&mut self, value: &str) {
        self.u32(value.len() as u32);
        self.bytes.extend_from_slice(value.as_bytes());
        self.bytes.push(0);
    }

    fn signature(&mut self, value: &str) {
        self.bytes.push(value.len() as u8);
        self.bytes.extend_from_slice(value.as_bytes());
        self.bytes.push(0);
    }

    /// One header field: `(yv)`, aligned to 8 as every struct is.
    fn field(&mut self, code: u8, type_signature: &str, write: impl FnOnce(&mut Self)) {
        self.align(8);
        self.u8(code);
        self.signature(type_signature);
        write(self);
    }
}

/// What a method call carries as its body.
///
/// A tiny closed set rather than a general encoder, because it is a closed set:
/// systemd's manager takes names, modes and a signal number, and the read side
/// is where the generality is actually needed. An argument type that is not
/// here is one nothing sends.
pub enum Arg<'a> {
    Str(&'a str),
    /// The signal number for `KillUnit`, and the only integer anything sends.
    I32(i32),
}

impl Arg<'_> {
    fn signature(&self) -> &'static str {
        match self {
            Self::Str(_) => "s",
            Self::I32(_) => "i",
        }
    }

    fn encode(&self, out: &mut Encoder) {
        match self {
            Self::Str(v) => out.string(v),
            Self::I32(v) => out.i32(*v),
        }
    }
}

// --------------------------------------------------------------------------
// Decoding
// --------------------------------------------------------------------------

struct Decoder<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl<'a> Decoder<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, at: 0 }
    }

    fn align(&mut self, to: usize) {
        while self.at % to != 0 {
            self.at += 1;
        }
    }

    fn take(&mut self, count: usize) -> Result<&'a [u8], DbusError> {
        let end = self.at.checked_add(count).ok_or_else(|| overrun(count))?;
        if end > self.bytes.len() {
            return Err(overrun(count));
        }
        let slice = &self.bytes[self.at..end];
        self.at = end;
        Ok(slice)
    }

    fn u8(&mut self) -> Result<u8, DbusError> {
        Ok(self.take(1)?[0])
    }

    fn u32(&mut self) -> Result<u32, DbusError> {
        self.align(4);
        let bytes = self.take(4)?;
        Ok(u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
    }

    fn string(&mut self) -> Result<String, DbusError> {
        let length = self.u32()? as usize;
        let raw = self.take(length)?.to_vec();
        self.take(1)?; // the NUL, which is not counted in the length
        String::from_utf8(raw).map_err(|e| DbusError::Protocol(format!("not utf-8: {e}")))
    }

    fn signature_string(&mut self) -> Result<String, DbusError> {
        let length = self.u8()? as usize;
        let raw = self.take(length)?.to_vec();
        self.take(1)?;
        String::from_utf8(raw).map_err(|e| DbusError::Protocol(format!("not utf-8: {e}")))
    }

    /// One value of the given type.
    ///
    /// Recursive, and bounded by the signature rather than by the data: a
    /// signature is at most 255 bytes and each level of nesting consumes at
    /// least one of them, so the depth is bounded by the same field the spec
    /// bounds.
    fn value(&mut self, signature: &str) -> Result<Value, DbusError> {
        let kind = signature.as_bytes()[0];
        match kind {
            b'y' => Ok(Value::Byte(self.u8()?)),
            b'b' => Ok(Value::Bool(self.u32()? != 0)),
            b'n' | b'q' => {
                self.align(2);
                let bytes = self.take(2)?;
                let raw = u16::from_le_bytes([bytes[0], bytes[1]]);
                Ok(if kind == b'n' { Value::I16(raw as i16) } else { Value::U16(raw) })
            }
            b'i' => Ok(Value::I32(self.u32()? as i32)),
            b'u' => Ok(Value::U32(self.u32()?)),
            b'x' | b't' | b'd' => {
                self.align(8);
                let bytes = self.take(8)?;
                let mut wide = [0u8; 8];
                wide.copy_from_slice(bytes);
                let raw = u64::from_le_bytes(wide);
                Ok(match kind {
                    b'x' => Value::I64(raw as i64),
                    b't' => Value::U64(raw),
                    _ => Value::F64(f64::from_bits(raw)),
                })
            }
            b's' => Ok(Value::Str(self.string()?)),
            b'o' => Ok(Value::Path(self.string()?)),
            b'g' => Ok(Value::Signature(self.signature_string()?)),
            b'v' => {
                let inner = self.signature_string()?;
                let (first, _) = split_type(&inner)?;
                Ok(Value::Variant(Box::new(self.value(first)?)))
            }
            b'a' => {
                let (element, _) = split_type(&signature[1..])?;
                let length = self.u32()? as usize;
                // The length is in bytes and is measured *after* the padding
                // that precedes the first element -- so the alignment has to
                // happen before the end offset is computed, or an array of
                // structs ends one padding's worth too early.
                self.align(alignment(element));
                let end = self.at + length;
                if end > self.bytes.len() {
                    return Err(overrun(length));
                }
                let mut items = Vec::new();
                while self.at < end {
                    items.push(self.value(element)?);
                }
                self.at = end;
                Ok(Value::Array(items))
            }
            b'(' => {
                self.align(8);
                let mut rest = &signature[1..signature.len() - 1];
                let mut fields = Vec::new();
                while !rest.is_empty() {
                    let (first, tail) = split_type(rest)?;
                    fields.push(self.value(first)?);
                    rest = tail;
                }
                Ok(Value::Struct(fields))
            }
            b'{' => {
                self.align(8);
                let inner = &signature[1..signature.len() - 1];
                let (key_type, value_tail) = split_type(inner)?;
                let (value_type, _) = split_type(value_tail)?;
                let key = self.value(key_type)?;
                let value = self.value(value_type)?;
                Ok(Value::DictEntry(Box::new(key), Box::new(value)))
            }
            other => Err(DbusError::Protocol(format!(
                "unsupported type {:?}",
                other as char
            ))),
        }
    }
}

fn overrun(wanted: usize) -> DbusError {
    DbusError::Protocol(format!("message ended while reading {wanted} byte(s)"))
}

// --------------------------------------------------------------------------
// Messages
// --------------------------------------------------------------------------

#[derive(Debug)]
pub struct Message {
    pub kind: u8,
    pub reply_serial: u32,
    pub path: String,
    pub interface: String,
    pub member: String,
    pub error_name: String,
    pub body: Vec<Value>,
}

impl Message {
    /// The first string in the body, which is where D-Bus puts an error's
    /// explanation and where most of systemd's replies put their answer.
    fn first_string(&self) -> String {
        self.body.first().and_then(Value::as_str).unwrap_or("").to_string()
    }
}

// --------------------------------------------------------------------------
// The connection
// --------------------------------------------------------------------------

pub struct Bus {
    stream: UnixStream,
    serial: u32,
    /// Signals that arrived while we were waiting for a reply.
    ///
    /// A method call and a broadcast share one socket, and systemd is entitled
    /// to emit a `PropertiesChanged` between our question and its answer.
    /// Dropping those would make the watch miss exactly the events that happen
    /// while it is busy — which is every event caused by something we just
    /// did.
    signals: VecDeque<Message>,
    /// Whether a destination has to be named on every message. False on the
    /// private socket, where there is only one peer and naming one is an
    /// error rather than a courtesy.
    routed: bool,
}

impl Bus {
    /// Connect, authenticate, and be ready to call.
    ///
    /// The system bus first, PID 1's private socket second. Both are tried
    /// before failing, and the error names the last one attempted, because an
    /// operator on a machine with neither wants to know the feature is
    /// unavailable rather than which of two paths was missing.
    pub async fn connect() -> Result<Self, DbusError> {
        let address = std::env::var("DBUS_SYSTEM_BUS_ADDRESS")
            .ok()
            .and_then(|value| {
                value
                    .split(',')
                    .find_map(|part| part.strip_prefix("unix:path=").map(str::to_string))
            })
            .unwrap_or_else(|| SYSTEM_BUS.to_string());

        match Self::open(&address, true).await {
            Ok(bus) => Ok(bus),
            Err(first) => match Self::open(PRIVATE_BUS, false).await {
                Ok(bus) => Ok(bus),
                // The system bus's failure is the one reported: it is the
                // ordinary route, and "no such file" on the private socket
                // would send a reader looking in the wrong place.
                Err(_) => Err(first),
            },
        }
    }

    async fn open(path: &str, routed: bool) -> Result<Self, DbusError> {
        let stream = UnixStream::connect(path)
            .await
            .map_err(|e| DbusError::Connect(format!("{path}: {e}")))?;
        let mut bus = Self { stream, serial: 0, signals: VecDeque::new(), routed };
        bus.authenticate().await?;
        if routed {
            // Required by the spec before anything else on a routed bus: the
            // daemon assigns our unique name here, and messages sent before it
            // are refused. Nothing needs the name itself.
            bus.call(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "Hello",
                &[],
            )
            .await?;
        }
        Ok(bus)
    }

    /// SASL EXTERNAL: the kernel already knows who we are.
    ///
    /// The uid is sent as the hex encoding of its *decimal spelling* — 1000
    /// becomes "31303030" — which looks like a mistake and is what the spec
    /// says. The peer does not take our word for it either way; it reads the
    /// credentials off the socket.
    async fn authenticate(&mut self) -> Result<(), DbusError> {
        // The leading NUL is not part of the text protocol and is not
        // optional: it is what the peer reads credentials alongside.
        let uid = unsafe { libc_getuid() };
        let hex: String = uid.to_string().bytes().map(|b| format!("{b:02x}")).collect();
        self.stream.write_all(b"\0").await?;
        self.stream.write_all(format!("AUTH EXTERNAL {hex}\r\n").as_bytes()).await?;

        let reply = self.read_line().await?;
        if !reply.starts_with("OK") {
            return Err(DbusError::Auth(format!("peer refused EXTERNAL: {reply}")));
        }
        self.stream.write_all(b"BEGIN\r\n").await?;
        Ok(())
    }

    async fn read_line(&mut self) -> Result<String, DbusError> {
        // Byte at a time, deliberately. This runs exactly once per connection
        // and the alternative -- a buffered reader -- would have to hand its
        // unconsumed bytes to the binary phase, which begins immediately after
        // the newline.
        let mut line = Vec::new();
        loop {
            let mut byte = [0u8; 1];
            self.stream.read_exact(&mut byte).await?;
            if byte[0] == b'\n' {
                break;
            }
            if byte[0] != b'\r' {
                line.push(byte[0]);
            }
            if line.len() > 512 {
                return Err(DbusError::Auth("peer sent an oversized greeting".into()));
            }
        }
        String::from_utf8(line).map_err(|e| DbusError::Auth(e.to_string()))
    }

    /// Call one method and wait for its answer.
    ///
    /// Errors come back as [`DbusError::Call`] carrying D-Bus's own error name
    /// and message. That distinction matters upward: `NoSuchUnit` is an answer
    /// about a unit, and a broken socket is an answer about the machine, and
    /// the caller treats them completely differently.
    pub async fn call(
        &mut self,
        destination: &str,
        path: &str,
        interface: &str,
        member: &str,
        args: &[Arg<'_>],
    ) -> Result<Message, DbusError> {
        self.serial = self.serial.wrapping_add(1).max(1);
        let serial = self.serial;

        let mut body = Encoder::default();
        let mut signature = String::new();
        for arg in args {
            signature.push_str(arg.signature());
            arg.encode(&mut body);
        }

        let mut out = Encoder::default();
        out.u8(LITTLE_ENDIAN);
        out.u8(MSG_METHOD_CALL);
        out.u8(0); // flags: we want a reply
        out.u8(PROTOCOL_VERSION);
        out.u32(body.bytes.len() as u32);
        out.u32(serial);

        let mut fields = Encoder::default();
        fields.field(FIELD_PATH, "o", |e| e.string(path));
        fields.field(FIELD_INTERFACE, "s", |e| e.string(interface));
        fields.field(FIELD_MEMBER, "s", |e| e.string(member));
        if self.routed {
            fields.field(FIELD_DESTINATION, "s", |e| e.string(destination));
        }
        if !signature.is_empty() {
            fields.field(FIELD_SIGNATURE, "g", |e| e.signature(&signature));
        }
        // The array's own length precedes it and covers only the entries, so
        // it is written from the finished buffer rather than predicted.
        out.u32(fields.bytes.len() as u32);
        out.bytes.extend_from_slice(&fields.bytes);
        // The body begins on an 8-byte boundary regardless of where the header
        // happened to end. This single line is the difference between a
        // request systemd answers and one it closes the connection over.
        out.align(8);
        out.bytes.extend_from_slice(&body.bytes);

        self.stream.write_all(&out.bytes).await?;

        loop {
            let message = self.read_message().await?;
            match message.kind {
                MSG_METHOD_RETURN if message.reply_serial == serial => return Ok(message),
                MSG_ERROR if message.reply_serial == serial => {
                    return Err(DbusError::Call {
                        name: message.error_name.clone(),
                        message: message.first_string(),
                    })
                }
                MSG_SIGNAL => self.remember(message),
                // A reply to a serial we are no longer waiting for. Not
                // possible today -- calls are sequential on this connection --
                // and dropped rather than trusted if it ever becomes so.
                _ => {}
            }
        }
    }

    /// The next signal, waiting for one if none has arrived yet.
    ///
    /// **Cancel-safe only between messages.** A caller that drops this future
    /// mid-read leaves the socket at a byte boundary nobody knows, which is
    /// why the watch runs on a connection of its own rather than in a
    /// `select!` beside the scan path -- the same shape `Engine::watch` uses,
    /// and for a stronger reason: an HTTP event stream that loses its place
    /// ends, and this one would go on decoding rubbish.
    pub async fn next_signal(&mut self) -> Result<Message, DbusError> {
        loop {
            if let Some(signal) = self.signals.pop_front() {
                return Ok(signal);
            }
            let message = self.read_message().await?;
            if message.kind == MSG_SIGNAL {
                return Ok(message);
            }
        }
    }

    fn remember(&mut self, signal: Message) {
        // Bounded, because a signal nobody is reading is not worth memory on
        // somebody else's machine. Dropping the oldest is right for the same
        // reason it is right for a live log: what a watch does with a signal
        // is re-read the state, and the newest one is the one that matters.
        if self.signals.len() >= 256 {
            self.signals.pop_front();
        }
        self.signals.push_back(signal);
    }

    async fn read_message(&mut self) -> Result<Message, DbusError> {
        let mut fixed = [0u8; 16];
        self.stream.read_exact(&mut fixed).await?;
        if fixed[0] != LITTLE_ENDIAN {
            // Every peer we can reach is on this machine, so this is a
            // corrupted stream rather than a big-endian daemon -- and reading
            // on is how a corrupted stream becomes plausible-looking data.
            return Err(DbusError::Protocol("peer is not little-endian".into()));
        }
        let kind = fixed[1];
        let body_length = u32::from_le_bytes([fixed[4], fixed[5], fixed[6], fixed[7]]);
        let fields_length = u32::from_le_bytes([fixed[12], fixed[13], fixed[14], fixed[15]]);
        if body_length > MAX_MESSAGE || fields_length > MAX_MESSAGE {
            return Err(DbusError::Protocol("message is implausibly large".into()));
        }

        let mut fields = vec![0u8; fields_length as usize];
        self.stream.read_exact(&mut fields).await?;

        // The body starts at the next 8-byte boundary after the header, and
        // the header is 16 bytes plus the field array -- so the padding is a
        // function of the field array's length alone.
        let padding = (8 - (fields_length as usize % 8)) % 8;
        if padding > 0 {
            let mut discard = vec![0u8; padding];
            self.stream.read_exact(&mut discard).await?;
        }

        let mut body_bytes = vec![0u8; body_length as usize];
        self.stream.read_exact(&mut body_bytes).await?;

        let mut header = Message {
            kind,
            reply_serial: 0,
            path: String::new(),
            interface: String::new(),
            member: String::new(),
            error_name: String::new(),
            body: Vec::new(),
        };

        // The field array is decoded as if it began where it does in the
        // message -- offset 16 -- because alignment is measured from the start
        // of the message and a struct inside it is 8-aligned. 16 is already a
        // multiple of 8, so decoding it as a standalone buffer gives the same
        // offsets; that equality is why this can be a separate `Decoder` at
        // all, and it is worth stating rather than discovering.
        let mut signature = String::new();
        let mut reader = Decoder::new(&fields);
        while reader.at < fields.len() {
            reader.align(8);
            if reader.at >= fields.len() {
                break;
            }
            let code = reader.u8()?;
            let field_signature = reader.signature_string()?;
            let (first, _) = split_type(&field_signature)?;
            let value = reader.value(first)?;
            match code {
                FIELD_PATH => header.path = value.as_str().unwrap_or("").to_string(),
                FIELD_INTERFACE => header.interface = value.as_str().unwrap_or("").to_string(),
                FIELD_MEMBER => header.member = value.as_str().unwrap_or("").to_string(),
                FIELD_ERROR_NAME => header.error_name = value.as_str().unwrap_or("").to_string(),
                FIELD_REPLY_SERIAL => header.reply_serial = value.as_u64().unwrap_or(0) as u32,
                FIELD_SIGNATURE => signature = value.as_str().unwrap_or("").to_string(),
                _ => {}
            }
        }

        let mut body_reader = Decoder::new(&body_bytes);
        let mut rest = signature.as_str();
        while !rest.is_empty() {
            let (first, tail) = split_type(rest)?;
            header.body.push(body_reader.value(first)?);
            rest = tail;
        }

        Ok(header)
    }
}

// The one libc call this file makes, declared rather than depended on.
//
// `libc` as a crate would be a dependency for a single function whose
// signature has not changed since the 1970s and cannot: `getuid` takes
// nothing, returns a `uid_t`, and is infallible by specification.
extern "C" {
    #[link_name = "getuid"]
    fn libc_getuid() -> u32;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_complete_type_is_split_off_whole() {
        assert_eq!(split_type("s").unwrap(), ("s", ""));
        assert_eq!(split_type("ss").unwrap(), ("s", "s"));
        assert_eq!(split_type("a{sv}s").unwrap(), ("a{sv}", "s"));
        assert_eq!(split_type("aa{sv}").unwrap(), ("aa{sv}", ""));
        // The shape `GetAll` on a service returns, and the reason the parser
        // is complete rather than partial: taking one character here would
        // read every subsequent property at the wrong offset.
        assert_eq!(split_type("a(sasbttttuii)u").unwrap(), ("a(sasbttttuii)", "u"));
    }

    #[test]
    fn an_unbalanced_signature_is_refused_rather_than_guessed() {
        assert!(split_type("(ss").is_err());
        assert!(split_type("a").is_err());
        assert!(split_type("").is_err());
    }

    /// The round trip that matters: a struct inside an array is 8-aligned, so
    /// the three padding bytes after a 5-byte string are part of the encoding
    /// and a decoder that skips them reads the next field from the wrong
    /// place.
    #[test]
    fn nested_values_decode_at_the_offsets_they_were_written_at() {
        let mut out = Encoder::default();
        // a(su): [("hello", 7), ("x", 9)]
        let mut elements = Encoder::default();
        elements.align(8);
        elements.string("hello");
        elements.u32(7);
        elements.align(8);
        elements.string("x");
        elements.u32(9);

        out.u32(elements.bytes.len() as u32);
        out.align(8);
        out.bytes.extend_from_slice(&elements.bytes);

        let decoded = Decoder::new(&out.bytes).value("a(su)").unwrap();
        assert_eq!(
            decoded,
            Value::Array(vec![
                Value::Struct(vec![Value::Str("hello".into()), Value::U32(7)]),
                Value::Struct(vec![Value::Str("x".into()), Value::U32(9)]),
            ])
        );
    }

    #[test]
    fn a_variant_dictionary_is_read_by_key() {
        let mut entries = Encoder::default();
        entries.align(8);
        entries.string("ActiveState");
        entries.signature("s");
        entries.string("active");
        entries.align(8);
        entries.string("MainPID");
        entries.signature("u");
        entries.u32(4242);

        let mut out = Encoder::default();
        out.u32(entries.bytes.len() as u32);
        out.align(8);
        out.bytes.extend_from_slice(&entries.bytes);

        let decoded = Decoder::new(&out.bytes).value("a{sv}").unwrap();
        assert_eq!(decoded.field("ActiveState").and_then(Value::as_str), Some("active"));
        assert_eq!(decoded.field("MainPID").and_then(Value::as_u64), Some(4242));
        // A key that is not there is `None` rather than a default, so a
        // property systemd stopped sending cannot read as zero.
        assert_eq!(decoded.field("NRestarts"), None);
    }

    #[test]
    fn a_truncated_body_is_an_error_rather_than_a_shorter_answer() {
        let mut out = Encoder::default();
        out.u32(12); // claims twelve bytes of array
        out.align(8);
        out.bytes.extend_from_slice(b"abc");
        assert!(Decoder::new(&out.bytes).value("a(su)").is_err());
    }
}
