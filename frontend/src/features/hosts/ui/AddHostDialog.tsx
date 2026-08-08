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
  const [copied, setCopied] = useState(false);
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
    const timer = window.setTimeout(() => setCopied(false), 2_000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(token.install);
      setCopied(true);
      setClipboardFailed(false);
    } catch {
      // Denied permission, or an insecure origin. The command is in a
      // selectable field either way, so this is a hint rather than a failure.
      setClipboardFailed(true);
    }
  };

  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label="Add a host">
      <div className="modal__card">
        <header className="modal__head">
          <h2>Add a host</h2>
          <span className={`token-clock${life.expired ? ' token-clock--expired' : ''}`}>
            {life.expired ? 'Token expired' : `Expires in ${life.clock}`}
          </span>
        </header>

        <p className="modal__lead">
          Run this on the machine you want to manage. The agent generates its own key,
          never sends it, and dials the Controller — nothing needs to reach the host.
        </p>

        <div className="token">
          <input
            className="token__command"
            readOnly
            value={token.install}
            aria-label="Install command"
            onFocus={(event) => event.currentTarget.select()}
          />
          <button type="button" className="control token__copy" onClick={() => void copy()}>
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>

        {clipboardFailed ? (
          <p className="modal__note">
            The browser would not write to the clipboard. Select the command above and copy
            it by hand.
          </p>
        ) : null}

        {/* The known gap, named rather than papered over with a download link
            that would 404. Packaging is tracked separately (MIGRATION §6.7). */}
        <p className="modal__note">
          Assumes <code>bystack-agent</code> is already on that host. There is no installer
          yet; build it with <code>cargo build --release</code> in <code>agent/</code>.
        </p>

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
          <button type="button" className="action action--safe" onClick={onClose}>
            Done — discard this token
          </button>
        </footer>
      </div>
    </div>
  );
}
