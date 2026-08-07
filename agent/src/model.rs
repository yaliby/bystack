//! Docker Engine API types.
//!
//! Only the fields the Controller's mapper actually reads are declared. A
//! daemon that adds fields costs us nothing — serde ignores what it was not
//! asked about — and a daemon that removes one leaves a default rather than
//! failing the whole List.
//!
//! **That promise needs `null_to_default`, not `#[serde(default)]`.** `default`
//! covers a field that is *absent*; it does nothing for a field that is present
//! and `null`, which fails the deserializer instead. Docker emits `null` rather
//! than `[]` or `{}` in a dozen ordinary places — `Aliases` on an endpoint,
//! `IPAM.Config` on a network, `Labels` on a volume, `RepoTags` on an untagged
//! image — and one of them anywhere in the payload rejects the entire List,
//! which is a host that never syncs at all. So every field takes both: `default`
//! for absent, `null_to_default` for null.
//!
//! `BTreeMap` rather than `HashMap` for labels, and that is not a style
//! choice: these structures are hashed to decide what to send, and a
//! `HashMap` iterates in an order that varies per process and per insertion.
//! Hashing one would make every container look changed on every List — the
//! same class of failure as hashing `Status`, arriving by a different route.

use serde::{Deserialize, Deserializer};
use std::collections::BTreeMap;

pub type Labels = BTreeMap<String, String>;

/// Read `null` as the type's default instead of as a parse error.
///
/// Paired with `#[serde(default)]` on every field: one handles absent, the
/// other handles null, and Docker produces both.
fn null_to_default<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    Ok(Option::<T>::deserialize(deserializer)?.unwrap_or_default())
}

#[derive(Debug, Deserialize, Default)]
pub struct Info {
    #[serde(rename = "ID", default, deserialize_with = "null_to_default")]
    pub id: String,
    #[serde(rename = "Name", default, deserialize_with = "null_to_default")]
    pub name: String,
    #[serde(rename = "ServerVersion", default, deserialize_with = "null_to_default")]
    pub server_version: String,
    #[serde(rename = "OperatingSystem", default, deserialize_with = "null_to_default")]
    pub operating_system: String,
    #[serde(rename = "KernelVersion", default, deserialize_with = "null_to_default")]
    pub kernel_version: String,
    #[serde(rename = "Architecture", default, deserialize_with = "null_to_default")]
    pub architecture: String,
    #[serde(rename = "NCPU", default, deserialize_with = "null_to_default")]
    pub ncpu: i32,
    #[serde(rename = "MemTotal", default, deserialize_with = "null_to_default")]
    pub mem_total: i64,
    #[serde(rename = "ContainersRunning", default, deserialize_with = "null_to_default")]
    pub containers_running: i32,
    #[serde(rename = "Containers", default, deserialize_with = "null_to_default")]
    pub containers_total: i32,
}

#[derive(Debug, Deserialize, Default, Hash)]
pub struct Container {
    #[serde(rename = "Id", default, deserialize_with = "null_to_default")]
    pub id: String,
    #[serde(rename = "Names", default, deserialize_with = "null_to_default")]
    pub names: Vec<String>,
    #[serde(rename = "Image", default, deserialize_with = "null_to_default")]
    pub image: String,
    #[serde(rename = "ImageID", default, deserialize_with = "null_to_default")]
    pub image_id: String,
    #[serde(rename = "Command", default, deserialize_with = "null_to_default")]
    pub command: String,
    #[serde(rename = "Created", default, deserialize_with = "null_to_default")]
    pub created: i64,

    /// The closed state vocabulary: `running`, `paused`, `exited`. Hashed.
    #[serde(rename = "State", default, deserialize_with = "null_to_default")]
    pub state: String,

    /// Docker's rendered status line — `"Up 3 hours"`.
    ///
    /// Deliberately **not** part of the content hash; see [`crate::hashset`].
    /// It is skipped there rather than omitted here because the Controller
    /// still needs it: the exit code lives inside it.
    #[serde(rename = "Status", default, deserialize_with = "null_to_default")]
    pub status: String,

    #[serde(rename = "Labels", default, deserialize_with = "null_to_default")]
    pub labels: Labels,
    #[serde(rename = "Ports", default, deserialize_with = "null_to_default")]
    pub ports: Vec<Port>,
    #[serde(rename = "Mounts", default, deserialize_with = "null_to_default")]
    pub mounts: Vec<Mount>,
    #[serde(rename = "NetworkSettings", default, deserialize_with = "null_to_default")]
    pub network_settings: NetworkSettings,
}

#[derive(Debug, Deserialize, Default, Hash)]
pub struct Port {
    #[serde(rename = "PrivatePort", default, deserialize_with = "null_to_default")]
    pub private_port: u32,
    /// Absent when the port is not published outside the engine.
    #[serde(rename = "PublicPort", default, deserialize_with = "null_to_default")]
    pub public_port: u32,
    #[serde(rename = "Type", default, deserialize_with = "null_to_default")]
    pub protocol: String,
    #[serde(rename = "IP", default, deserialize_with = "null_to_default")]
    pub ip: String,
}

#[derive(Debug, Deserialize, Default, Hash)]
pub struct Mount {
    /// `volume` or `bind`. The Controller keeps bind mounts as container
    /// attributes rather than inventing a node for a host path.
    #[serde(rename = "Type", default, deserialize_with = "null_to_default")]
    pub kind: String,
    #[serde(rename = "Name", default, deserialize_with = "null_to_default")]
    pub name: String,
    #[serde(rename = "Destination", default, deserialize_with = "null_to_default")]
    pub destination: String,
    #[serde(rename = "Mode", default, deserialize_with = "null_to_default")]
    pub mode: String,
    #[serde(rename = "RW", default, deserialize_with = "null_to_default")]
    pub rw: bool,
}

#[derive(Debug, Deserialize, Default, Hash)]
pub struct NetworkSettings {
    #[serde(rename = "Networks", default, deserialize_with = "null_to_default")]
    pub networks: BTreeMap<String, EndpointSettings>,
}

#[derive(Debug, Deserialize, Default, Hash)]
pub struct EndpointSettings {
    #[serde(rename = "NetworkID", default, deserialize_with = "null_to_default")]
    pub network_id: String,
    #[serde(rename = "IPAddress", default, deserialize_with = "null_to_default")]
    pub ip_address: String,
    #[serde(rename = "Aliases", default, deserialize_with = "null_to_default")]
    pub aliases: Vec<String>,
}

#[derive(Debug, Deserialize, Default, Hash)]
pub struct Network {
    #[serde(rename = "Id", default, deserialize_with = "null_to_default")]
    pub id: String,
    #[serde(rename = "Name", default, deserialize_with = "null_to_default")]
    pub name: String,
    #[serde(rename = "Driver", default, deserialize_with = "null_to_default")]
    pub driver: String,
    #[serde(rename = "Scope", default, deserialize_with = "null_to_default")]
    pub scope: String,
    #[serde(rename = "Internal", default, deserialize_with = "null_to_default")]
    pub internal: bool,
    #[serde(rename = "Attachable", default, deserialize_with = "null_to_default")]
    pub attachable: bool,
    #[serde(rename = "Ingress", default, deserialize_with = "null_to_default")]
    pub ingress: bool,
    #[serde(rename = "Labels", default, deserialize_with = "null_to_default")]
    pub labels: Labels,
    #[serde(rename = "IPAM", default, deserialize_with = "null_to_default")]
    pub ipam: Ipam,
}

#[derive(Debug, Deserialize, Default, Hash)]
pub struct Ipam {
    #[serde(rename = "Config", default, deserialize_with = "null_to_default")]
    pub config: Vec<IpamConfig>,
}

#[derive(Debug, Deserialize, Default, Hash)]
pub struct IpamConfig {
    #[serde(rename = "Subnet", default, deserialize_with = "null_to_default")]
    pub subnet: String,
}

#[derive(Debug, Deserialize, Default, Hash)]
pub struct Volume {
    #[serde(rename = "Name", default, deserialize_with = "null_to_default")]
    pub name: String,
    #[serde(rename = "Driver", default, deserialize_with = "null_to_default")]
    pub driver: String,
    #[serde(rename = "Mountpoint", default, deserialize_with = "null_to_default")]
    pub mountpoint: String,
    #[serde(rename = "Scope", default, deserialize_with = "null_to_default")]
    pub scope: String,
    #[serde(rename = "CreatedAt", default, deserialize_with = "null_to_default")]
    pub created_at: String,
    #[serde(rename = "Labels", default, deserialize_with = "null_to_default")]
    pub labels: Labels,
}

/// `GET /volumes` wraps its list, unlike every other list endpoint.
#[derive(Debug, Deserialize, Default)]
pub struct VolumeList {
    #[serde(rename = "Volumes", default, deserialize_with = "null_to_default")]
    pub volumes: Vec<Volume>,
}

#[derive(Debug, Deserialize, Default, Hash)]
pub struct Image {
    #[serde(rename = "Id", default, deserialize_with = "null_to_default")]
    pub id: String,
    #[serde(rename = "RepoTags", default, deserialize_with = "null_to_default")]
    pub repo_tags: Vec<String>,
    #[serde(rename = "RepoDigests", default, deserialize_with = "null_to_default")]
    pub repo_digests: Vec<String>,
    #[serde(rename = "Size", default, deserialize_with = "null_to_default")]
    pub size: i64,
    #[serde(rename = "Created", default, deserialize_with = "null_to_default")]
    pub created: i64,
    #[serde(rename = "Labels", default, deserialize_with = "null_to_default")]
    pub labels: Labels,
}

/// One line of `GET /events`.
///
/// The agent reads only `Type`. An event says *that* something changed in a
/// slice; which entity it was is deliberately ignored, because acting on the
/// id would mean reasoning about what became orphaned and what happens when
/// events arrive out of order. Re-Listing the affected slice is one local
/// call, always correct, and makes no ordering assumptions.
#[derive(Debug, Deserialize, Default)]
pub struct Event {
    #[serde(rename = "Type", default, deserialize_with = "null_to_default")]
    pub kind: String,
}
