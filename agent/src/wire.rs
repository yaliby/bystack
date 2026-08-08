//! The generated contract, and Docker's vocabulary poured into it.
//!
//! Field-for-field and nothing more. The agent ships Docker's own vocabulary
//! upward and lets the Controller interpret it (ADR-0009 §1), so there is
//! nothing to decide in this file — which is exactly the property that keeps
//! the seam honest. If a conversion here ever needed to know what a field
//! *means*, the seam would be in the wrong place.

#![allow(clippy::all)]

include!(concat!(env!("OUT_DIR"), "/bystack.agent.v1.rs"));

use crate::model;

pub const AGENT_VERSION: &str = env!("CARGO_PKG_VERSION");

impl From<&model::Info> for EngineInfo {
    fn from(info: &model::Info) -> Self {
        Self {
            id: info.id.clone(),
            name: info.name.clone(),
            server_version: info.server_version.clone(),
            operating_system: info.operating_system.clone(),
            kernel_version: info.kernel_version.clone(),
            architecture: info.architecture.clone(),
            ncpu: info.ncpu,
            mem_total: info.mem_total,
            containers_running: info.containers_running,
            containers_total: info.containers_total,
        }
    }
}

impl From<&model::Container> for Container {
    fn from(source: &model::Container) -> Self {
        Self {
            id: source.id.clone(),
            names: source.names.clone(),
            image: source.image.clone(),
            image_id: source.image_id.clone(),
            command: source.command.clone(),
            created: source.created,
            state: source.state.clone(),
            // Carried but never hashed -- see `hashset`. The Controller
            // extracts the exit code from it and discards the prose.
            status_text: source.status.clone(),
            labels: source.labels.clone().into_iter().collect(),
            ports: source.ports.iter().map(Port::from).collect(),
            networks: source
                .network_settings
                .networks
                .iter()
                .map(|(name, settings)| NetworkAttachment {
                    network_id: settings.network_id.clone(),
                    name: name.clone(),
                    ipv4: settings.ip_address.clone(),
                    aliases: settings.aliases.clone(),
                })
                .collect(),
            mounts: source.mounts.iter().map(Mount::from).collect(),
            // Normalized by `health()`, so the Controller sees one vocabulary
            // whatever the daemon on this host is old enough to send.
            health: source.health().to_string(),
            // Zero unless the informer inspected this container, which it
            // does only while the container is restarting. Zero is also the
            // truth for everything else.
            restart_count: source.restart_count,
        }
    }
}

impl From<&model::Port> for Port {
    fn from(port: &model::Port) -> Self {
        Self {
            private_port: port.private_port,
            public_port: port.public_port,
            protocol: port.protocol.clone(),
            host_ip: port.ip.clone(),
        }
    }
}

impl From<&model::Mount> for Mount {
    fn from(mount: &model::Mount) -> Self {
        Self {
            r#type: mount.kind.clone(),
            name: mount.name.clone(),
            destination: mount.destination.clone(),
            mode: mount.mode.clone(),
            rw: mount.rw,
        }
    }
}

impl From<&model::Network> for Network {
    fn from(network: &model::Network) -> Self {
        Self {
            id: network.id.clone(),
            name: network.name.clone(),
            driver: network.driver.clone(),
            scope: network.scope.clone(),
            internal: network.internal,
            attachable: network.attachable,
            ingress: network.ingress,
            subnets: network
                .ipam
                .config
                .iter()
                .filter(|c| !c.subnet.is_empty())
                .map(|c| c.subnet.clone())
                .collect(),
            labels: network.labels.clone().into_iter().collect(),
        }
    }
}

impl From<&model::Volume> for Volume {
    fn from(volume: &model::Volume) -> Self {
        Self {
            name: volume.name.clone(),
            driver: volume.driver.clone(),
            mountpoint: volume.mountpoint.clone(),
            scope: volume.scope.clone(),
            created_at: volume.created_at.clone(),
            labels: volume.labels.clone().into_iter().collect(),
        }
    }
}

impl From<&model::Image> for Image {
    fn from(image: &model::Image) -> Self {
        Self {
            id: image.id.clone(),
            repo_tags: image.repo_tags.clone(),
            repo_digests: image.repo_digests.clone(),
            size: image.size,
            created: image.created,
            labels: image.labels.clone().into_iter().collect(),
        }
    }
}

/// Which slice a Docker event type invalidates.
///
/// Returns `None` for anything else. The daemon already filters server-side,
/// so this is the second line of defence rather than the first.
pub fn slice_for_event(kind: &str) -> Option<Slice> {
    match kind {
        "container" => Some(Slice::Container),
        "network" => Some(Slice::Network),
        "volume" => Some(Slice::Volume),
        _ => None,
    }
}

pub fn entity(id: String, body: entity::Body) -> Entity {
    Entity { id, body: Some(body) }
}

pub fn envelope(seq: u64, payload: envelope::Payload) -> Envelope {
    Envelope { seq, payload: Some(payload) }
}
