# Architecture Decision Records

ADR-0001 … ADR-0007 predate this directory and are recorded inline in
[`../../ARCHITECTURE.md`](../../ARCHITECTURE.md):

| ADR  | Decision                                              | Status                        |
|------|-------------------------------------------------------|-------------------------------|
| 0001 | Ephemeral graph; four durable categories only          | Accepted                      |
| 0002 | Two-layer identity, URNs, Engine ID as host identity   | Accepted                      |
| 0003 | Provider independence; correlation above providers     | Accepted                      |
| 0004 | Informer pattern: List → Watch → Resync                | Accepted (relocated by 0008)  |
| 0005 | Transport abstraction                                  | **Superseded by 0008**        |
| 0006 | Resource budgets                                       | Accepted; **§5 superseded by 0008** |
| 0007 | Docker Engine HTTP API directly, not `docker-py`       | Accepted (moot on Controller) |

From the Controller/Agent pivot onward, each decision is its own file.

| ADR  | Decision                                              | Status    |
|------|-------------------------------------------------------|-----------|
| [0008](0008-controller-agent-topology.md) | Central Controller + lightweight Agents  | Accepted |
| [0009](0009-agent-wire-protocol.md)       | WebSocket + Protobuf; authoritative deltas | Accepted |
| [0010](0010-agent-implementation-language.md) | The Agent is written in Go           | **Superseded by 0013** |
| [0011](0011-agent-trust-and-enrollment.md)| mTLS, single-use join tokens, Engine ID identity | Accepted |
| [0012](0012-operations-and-audit.md)      | Operations: lifecycle only, logical targets, no optimistic updates | Accepted |
| [0013](0013-agent-in-rust.md)             | The Agent is written in Rust; supersedes 0010 | Accepted |
| [0014](0014-no-user-identity.md)          | No user identity; the network is the boundary, and destructive verbs stay out | Accepted |
| [0015](0015-agent-upgrade.md)             | Agent upgrade: the Controller reports skew, the host applies it | Accepted |

A record is written when a decision is *hard to reverse* or when the reasoning
will not be obvious to someone reading the code a year from now. Everything
else is a comment.
