/**
 * The one and only sighting of a join token.
 *
 * The Controller stores a digest, not the token (`infra/agentca/tokens.py`),
 * so there is no route that could show this again and no amount of UI could
 * make one. That shapes the whole dialog: the command is presented ready to
 * copy, the expiry runs as a clock rather than sitting as a timestamp, and the
 * consequence of closing is written on the screen rather than assumed.
 *
 * Nothing here persists anything. The token lives in this component's state
 * and dies with it — no storage, no URL, no lift into a parent that outlives
 * the dialog.
 */

import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import type { JoinToken } from '../../../api/types';
import { tokenLife } from '../model/hosts';

interface Props {
  readonly token: JoinToken;
  /** From `GET /agents/enrollment`. Changes what happens after the paste. */
  readonly autoApprove: boolean;
  readonly onClose: () => void;
}

export function AddHostDialog({ token, autoApprove, onClose }: Props) {
  const [now, setNow] = useState(() => Date.now());
  /** Which command was last copied, if any. Two buttons, one answer. */
  const [copied, setCopied] = useState<'install' | 'manual' | null>(null);
  const [clipboardFailed, setClipboardFailed] = useState(false);

  const life = tokenLife(token.expires_at, now);

  // One tick a second, and only while there is something to count down.
  useEffect(() => {
    if (life.expired) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [life.expired]);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(null), 2_000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      // Capture + stopImmediate so the app shell does not also dismiss Hosts.
      event.stopImmediatePropagation();
      onClose();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onClose]);

  const copy = async (which: 'install' | 'manual') => {
    try {
      await navigator.clipboard.writeText(which === 'install' ? token.install : token.manual);
      setCopied(which);
      setClipboardFailed(false);
    } catch {
      // Denied permission, or an insecure origin. The command is in a
      // selectable field either way, so this is a hint rather than a failure.
      setClipboardFailed(true);
    }
  };

  /**
   * Portalled to the body, and it has to be.
   *
   * This dialog is rendered from inside the Hosts panel, and that panel has a
   * `backdrop-filter` — which makes it the containing block for every
   * `position: fixed` descendant. Left in place, the modal laid itself out
   * inside a 340px column and clipped the install command at its edge: the one
   * string this whole screen exists to hand over, cut in half.
   */
  return createPortal(
    <div className="modal" role="dialog" aria-modal="true" aria-label="Add a host">
      <div className="modal__card">
        <header className="modal__head">
          <h2>Add a host</h2>
          <span className={`token-clock${life.expired ? ' token-clock--expired' : ''}`}>
            {life.expired ? 'Token expired' : `Expires in ${life.clock}`}
          </span>
        </header>

        <p className="modal__lead">
          Run this on the machine you want to manage. It installs the agent as a service,
          which then generates its own key, never sends it, and dials the Controller —
          nothing needs to reach the host.
        </p>

        <div className="token">
          <input
            className="token__command"
            readOnly
            value={token.install}
            aria-label="Install command"
            onFocus={(event) => event.currentTarget.select()}
          />
          <button type="button" className="control token__copy" onClick={() => void copy('install')}>
            {copied === 'install' ? 'Copied' : 'Copy'}
          </button>
        </div>

        {clipboardFailed ? (
          <p className="modal__note">
            The browser would not write to the clipboard. Select the command above and copy
            it by hand.
          </p>
        ) : null}

        {/* Second, and folded away, because it answers a different question:
            the host already has the binary. Offering both at the same size
            would make the first decision on this screen "which of these am I",
            for a distinction most people meet once. */}
        <details className="modal__note">
          <summary>The agent is already on that host</summary>
          <div className="token">
            <input
              className="token__command"
              readOnly
              value={token.manual}
              aria-label="Command for a host that already has the agent"
              onFocus={(event) => event.currentTarget.select()}
            />
            <button type="button" className="control token__copy" onClick={() => void copy('manual')}>
              {copied === 'manual' ? 'Copied' : 'Copy'}
            </button>
          </div>
          <p>
            Also what to use on a network with no route to GitHub: build the binary with{' '}
            <code>scripts/build-agent.sh</code>, copy it over, and run{' '}
            <code>install-agent.sh --binary</code> to get the service as well.
          </p>
        </details>

        <p className="modal__warn">
          This token is shown once. The Controller keeps only a digest of it, so closing
          this dialog loses it for good — if you need it again, add the host again and mint
          a new one. It is single use and expires on its own.
        </p>

        <p className="modal__note">
          {autoApprove
            ? 'This Controller approves agents on enrollment, so the host will start contributing to the topology as soon as it connects.'
            : 'The host will then appear in Hosts as awaiting approval. It contributes nothing to the topology until you approve it there.'}
        </p>

        <div className="modal__meta">
          <span className="row__label">CA fingerprint</span>
          <span className="row__value row__value--mono">{token.ca_fingerprint}</span>
        </div>

        <footer className="modal__foot">
          <button type="button" className="action action--primary" onClick={onClose}>
            Done — discard this token
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  );
}
