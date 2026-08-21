//! `bystack-manager` — the Controller's local lifecycle, and nothing else.
//!
//! ADR-0017 gave the fleet a way to be upgraded from the dashboard: the
//! Controller distributes a signed release and each host verifies it against a
//! key the Controller does not have. It left one machine out, necessarily —
//! **the Controller has no parent to push to it.** Upgrading it is still an
//! ssh, a `pip install` and a `systemctl restart`, on the one box whose whole
//! purpose is to make that unnecessary elsewhere.
//!
//! ADR-0018 closes that, and closes it by adding as little as possible:
//!
//! * **The same trust contract.** Same keys, same `{name, version, arch,
//!   sha256, released_at}` manifest, same `scripts/sign-agent.py`. The only
//!   field that differs is `name`, which is `bystack-controller` instead of
//!   `bystack-agent`. `bystack-release` is that contract, linked by both
//!   binaries, so there is one parser and one key set in the tree.
//! * **Pull instead of push.** There is nobody to push, so this fetches from
//!   GitHub Releases — over `curl`, because the transport is not what makes
//!   the bytes worth executing and never was.
//! * **One folder.** Everything lives under `/opt/bystack`: the Controller,
//!   this binary, the two files they talk through, and the fleet's artifacts.
//!
//! ## What runs, and when
//!
//! ```text
//! bystack-manager.path       watches /opt/bystack/ipc/update.intent, which the
//!                            dashboard's "update" button makes the Controller
//!                            write. No socket, no listener, no argument the
//!                            Controller can parameterise.
//! bystack-manager.service    Type=oneshot, root, network. `bystack-manager
//!                            apply`: fetch, verify, stop, swap, start, and
//!                            poll /healthz until the new Controller says it
//!                            is the version that was installed. Exits.
//! bystack-manager-rollback   ConditionPathExists=…/state/probation, at boot.
//!   .service                 Undoes a swap whose probation never finished --
//!                            the one failure `apply` cannot handle, because
//!                            it is `apply` not being there any more.
//! ```
//!
//! **No resident root daemon.** The agent needs an independent timer because
//! its probation is cleared by evidence arriving on somebody else's
//! connection; the Controller's evidence is a GET to loopback, so the process
//! that made the change can wait for it and undo it, and the only thing left
//! for a unit to cover is that process dying.
//!
//! ## What this deliberately does not do
//!
//! * **No listener and no port.** One outbound connection to a compiled-in
//!   repository, and one to loopback.
//! * **No arbitrary execution.** The intent file names a version, which is
//!   checked for being one and composed into a URL under a repository this
//!   binary was built with. It cannot name a URL, a path or a command.
//! * **No downgrade.** The floor is what `bystack-controller --version`
//!   reports, read from the file that actually runs. Rollback is the operation
//!   that goes backwards, and it is local and automatic.
//! * **Nothing about the fleet's *decisions*.** Phase two drops signed agent
//!   artifacts where the Controller already looks for them. Which hosts get
//!   them, in what order, and what happens when one refuses is ADR-0017's
//!   staged rollout, unchanged.

mod apply;
mod ipc;
mod layout;
mod shell;

use layout::Layout;

const VERSION: &str = env!("CARGO_PKG_VERSION");

const USAGE: &str = "\
bystack-manager — installs a signed Controller release (ADR-0018)

    bystack-manager request [version]   ask for an update. Default: the newest
    bystack-manager apply               act on /opt/bystack/ipc/update.intent
    bystack-manager rollback            undo a swap whose probation never finished
    bystack-manager status              print what the last run reported
    bystack-manager --version

`request` is what the dashboard's button does, from a terminal: it writes the
intent file and the path unit does the rest. It needs no root -- only the
ability to write in ipc/, which is what the Controller has.

`apply` and `rollback` need root and are started by systemd:
bystack-manager.path watches the intent file, and
bystack-manager-rollback.service is conditioned on the probation file.
Running them by hand is supported and is how you debug one.

    BYSTACK_HOME           where the Controller lives. Default /opt/bystack
    BYSTACK_REPO           which repository to pull from. Default yaliby/bystack
    BYSTACK_RELEASE_BASE   a mirror to pull from instead. The signature still
                           has to hold, so this is an operational choice and
                           not a way around one.
";

fn main() {
    let layout = Layout::from_env();
    let mut args = std::env::args().skip(1);
    let command = args.next().unwrap_or_default();

    let result = match command.as_str() {
        "request" => request(&layout, args.next()),
        "apply" => apply::run(&layout),
        "rollback" => apply::rollback(&layout),
        "status" => status(&layout),
        "--version" | "-V" => {
            println!("bystack-manager {VERSION}");
            return;
        }
        "--help" | "-h" | "" => {
            println!("{USAGE}");
            return;
        }
        other => {
            eprintln!("bystack-manager: unknown command {other:?}\n\n{USAGE}");
            std::process::exit(2);
        }
    };

    if let Err(error) = result {
        eprintln!("bystack-manager: {error}");
        std::process::exit(1);
    }
}

/// Write the intent file, which is the whole of asking for an update.
///
/// The same document the Controller writes, from the same code that parses it
/// — so a hand-driven update and a button-driven one are the same run, and
/// there is no debugging path that exercises something the product does not.
fn request(layout: &Layout, version: Option<String>) -> Result<(), String> {
    let version = version.unwrap_or_default();
    if !version.is_empty() && !bystack_release::is_version(&version) {
        return Err(format!(
            "{version:?} is not a version. Give one like 0.5.0, or none at all for \
             whatever is newest."
        ));
    }
    let intent = ipc::Intent {
        version: version.clone(),
        health: String::new(),
        requested_at: ipc::unix_time(),
    };
    let path = layout.intent();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
    }
    std::fs::write(&path, intent.render())
        .map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    println!(
        "Asked for {}. bystack-manager.path will pick it up; watch it with \
         `bystack-manager status`.",
        if version.is_empty() { "the newest release".into() } else { version }
    );
    Ok(())
}

/// Print the status file, or say plainly that there has never been a run.
///
/// Printed verbatim rather than reformatted. This is what the Controller
/// reads and what the dashboard draws, and an operator debugging a
/// disagreement between the three needs to see the bytes rather than a third
/// rendering of them.
fn status(layout: &Layout) -> Result<(), String> {
    match ipc::read_document(&layout.status()) {
        Ok(text) => {
            print!("{text}");
            Ok(())
        }
        Err(_) if !layout.status().exists() => {
            println!("No update has been run on this machine.");
            println!("({} does not exist)", layout.status().display());
            Ok(())
        }
        Err(e) => Err(e),
    }
}
