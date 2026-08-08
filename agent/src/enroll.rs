//! First contact: a token in, a certificate out.
//!
//! One frame up, one frame down, over the same WebSocket and the same
//! protobuf framing as everything else. It could have been an HTTPS POST; it
//! is not, because the agent would then carry a TLS-capable HTTP client for
//! exactly one request, in a binary whose size is a measured budget
//! (ARCHITECTURE §11). One protocol, one framing, one contract.
//!
//! The connection is authenticated in both directions before the token
//! crosses it. The agent has no certificate — that is the point — but it does
//! have the Controller CA's fingerprint, which the operator pasted along with
//! the secret, and it refuses to speak to anything whose chain does not end
//! there. Enrollment is therefore not trust-on-first-use.

use futures_util::{SinkExt, StreamExt};
use prost::Message;
use tokio_tungstenite::tungstenite::Message as WsMessage;

use crate::session::log;
use crate::trust::{self, Credentials, JoinToken};
use crate::wire::{self, envelope::Payload};

/// Redeem `token` for a certificate and write it to `state_dir`.
///
/// Returns whether the Controller left this agent awaiting approval. That is
/// not a failure: a valid token always yields a certificate, and approval is
/// the separate question of whether the agent contributes to the graph. The
/// agent connects either way and is told to come back, so approving needs no
/// second visit to the host.
pub async fn enroll(
    controller: &str,
    token: &JoinToken,
    engine_id: &str,
    state_dir: &std::path::Path,
) -> Result<bool, String> {
    let request = trust::new_request()?;
    let connector = trust::enrollment_connector(token)?;

    let (socket, _) = tokio_tungstenite::connect_async_tls_with_config(
        crate::enroll_url(controller),
        None,
        false,
        Some(connector),
    )
    .await
    .map_err(|e| trust::explain(&format!("cannot enroll with {controller}: {e}")))?;
    let (mut sink, mut stream) = socket.split();

    let frame = wire::envelope(
        1,
        Payload::EnrollRequest(wire::EnrollRequest {
            join_token: token.raw.clone(),
            engine_id: engine_id.to_string(),
            csr_pem: request.csr_pem,
            agent_version: wire::AGENT_VERSION.to_string(),
        }),
    )
    .encode_to_vec();
    sink.send(WsMessage::Binary(frame))
        .await
        .map_err(|e| format!("the controller closed during enrollment: {e}"))?;

    let response = match stream.next().await {
        Some(Ok(WsMessage::Binary(bytes))) => match wire::Envelope::decode(&bytes[..]) {
            Ok(wire::Envelope { payload: Some(Payload::EnrollResponse(response)), .. }) => response,
            Ok(_) => return Err("the controller answered enrollment with something else".into()),
            Err(e) => return Err(format!("undecodable enrollment response: {e}")),
        },
        Some(Ok(WsMessage::Close(frame))) => {
            return Err(match frame {
                Some(frame) if !frame.reason.is_empty() => {
                    format!("the controller refused enrollment: {}", frame.reason)
                }
                _ => "the controller closed the enrollment connection".to_string(),
            })
        }
        Some(Err(e)) => return Err(format!("enrollment failed: {e}")),
        _ => return Err("the controller closed before answering enrollment".into()),
    };

    if !response.accepted {
        return Err(format!("the controller refused enrollment: {}", response.reason));
    }

    Credentials {
        certificate_pem: response.certificate_pem,
        key_pem: request.key_pem,
        ca_pem: response.ca_pem,
    }
    .store(state_dir)
    .map_err(|e| format!("cannot write credentials to {}: {e}", state_dir.display()))?;

    log(&format!(
        "enrolled as {engine_id}; certificate stored in {}",
        state_dir.display()
    ));
    Ok(response.pending_approval)
}
