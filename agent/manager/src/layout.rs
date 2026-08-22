//! One folder, and every path in it (ADR-0018).
//!
//! ```text
//! /opt/bystack/
//!     bin/bystack-controller        what runs. Replaced by `rename` in this
//!     bin/bystack-controller.prev   directory, so the swap is atomic and the
//!     bin/bystack-manager           previous inode survives it.
//!     bin/bystack-controller.failed left behind by a rollback: the release
//!                                   that was installed and did not serve. One
//!                                   file, overwritten by the next rollback,
//!                                   kept because "which build was it" is the
//!                                   first question afterwards. Safe to delete.
//!     ipc/update.intent             the Controller writes this. Root reads it.
//!     ipc/update.status             root writes this. The Controller reads it.
//!     state/probation               root only. What to undo, and by when.
//!     state/work/                   downloads, 0700, emptied per run.
//!     releases/                     signed agent artifacts, for the fleet.
//! ```
//!
//! **Nothing outside it.** The agent's updater scatters by necessity — the
//! binary belongs in `/usr/local/bin` because that is where a host's PATH
//! looks, and root's probation flag has to live somewhere the daemon cannot
//! write. The Controller has neither constraint: it is one deployment on one
//! machine, installed as a directory, and an operator who wants it gone should
//! be able to delete a folder.
//!
//! ## The one place the modes are load-bearing
//!
//! `ipc/` is `root:bystack`, mode `1770`. The Controller runs as `bystack` and
//! must be able to create `update.intent`; the sticky bit is what stops it
//! also being able to unlink `update.status`, which root owns. Without it, the
//! process whose update is being reported could rewrite the report — and the
//! whole reason the status file exists is to be the one thing in this feature
//! the Controller cannot say for itself.
//!
//! That directory is also the only thing `bystack-controller.service` gets
//! `ReadWritePaths=` for. It contains no executables and nothing is ever run
//! out of it.

use std::path::PathBuf;

/// Where the Controller lives. Overridable so the tests, and anyone running a
/// second Controller on one machine, do not have to be root to try this.
pub const DEFAULT_HOME: &str = "/opt/bystack";

/// The unit this stops, swaps under, and starts again.
pub const SERVICE: &str = "bystack-controller.service";

/// The file the Controller writes to ask for an update.
pub const INTENT: &str = "update.intent";

/// The file this writes so the dashboard can draw a progress bar.
pub const STATUS: &str = "update.status";

#[derive(Debug, Clone)]
pub struct Layout {
    home: PathBuf,
}

impl Layout {
    /// From `BYSTACK_HOME`, or [`DEFAULT_HOME`].
    pub fn from_env() -> Self {
        Self::at(std::env::var("BYSTACK_HOME").unwrap_or_else(|_| DEFAULT_HOME.into()))
    }

    pub fn at(home: impl Into<PathBuf>) -> Self {
        Self { home: home.into() }
    }

    /// The Controller itself: the artifact `bystack-controller-<arch>` is
    /// installed under, without the architecture suffix.
    ///
    /// The suffix is dropped on install for the same reason the agent's is:
    /// what runs on a host is one file with one name, and a path that carried
    /// the architecture would put it in every unit file, every service
    /// override and every operator's muscle memory — where it would be wrong
    /// the first time anyone moved a disk.
    pub fn controller(&self) -> PathBuf {
        self.home.join("bin").join("bystack-controller")
    }

    /// The inode the last swap replaced, kept as a hard link.
    pub fn previous(&self) -> PathBuf {
        self.home.join("bin").join("bystack-controller.prev")
    }

    pub fn ipc(&self) -> PathBuf {
        self.home.join("ipc")
    }

    pub fn intent(&self) -> PathBuf {
        self.ipc().join(INTENT)
    }

    pub fn status(&self) -> PathBuf {
        self.ipc().join(STATUS)
    }

    /// Root's own directory. **Not** `ipc/`, for the reason ADR-0017 keeps the
    /// agent's version floor out of the daemon's state directory: a probation
    /// record the updated process can edit is a rollback the updated process
    /// can cancel.
    pub fn state(&self) -> PathBuf {
        self.home.join("state")
    }

    pub fn probation(&self) -> PathBuf {
        self.state().join("probation")
    }

    /// Where a run downloads to. Emptied at the start of every run rather than
    /// at the end of one: a run that died holding a partial artifact must not
    /// leave bytes that the next run's digest check has to be the only thing
    /// standing between.
    pub fn work(&self) -> PathBuf {
        self.state().join("work")
    }

    /// Signed agent artifacts, which the Controller hands to the fleet.
    ///
    /// The Controller's `agents.releases_dir` names this, and its `ReleaseStore`
    /// rescans it on every read — so the manager dropping three files here is
    /// the whole of the cascade's plumbing (ADR-0018, phase two).
    pub fn releases(&self) -> PathBuf {
        self.home.join("releases")
    }
}
