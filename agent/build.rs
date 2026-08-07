//! Generate the wire types from the checked-in descriptor set.
//!
//! `prost-build` normally shells out to `protoc`. We feed it a compiled
//! `FileDescriptorSet` instead, produced by
//! `backend/scripts/generate_proto.py` and committed alongside the `.proto`.
//!
//! The point is that building the agent requires nothing but a Rust
//! toolchain. That is worth more here than anywhere else in the project: the
//! agent is the component cross-compiled for every architecture in a fleet,
//! and a `protoc` dependency would have to be satisfied in each of those
//! toolchains — including the ones running in whatever CI a contributor has.

use std::path::PathBuf;

fn main() {
    let descriptor = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("proto/agent.bin");

    // Rerun on the descriptor, not on the `.proto`: the descriptor is what
    // this build actually consumes, and watching the source would silently
    // pass on a schema edit that was never regenerated.
    println!("cargo:rerun-if-changed={}", descriptor.display());

    let mut config = prost_build::Config::new();
    config.skip_protoc_run();
    config.file_descriptor_set_path(&descriptor);
    config
        .compile_protos(&["bystack/agent/v1/agent.proto"], &["."])
        .expect("failed to generate wire types from proto/agent.bin");
}
