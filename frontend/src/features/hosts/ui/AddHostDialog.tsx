/**
 * Two ways to make a machine managed, and neither is the fallback.
 *
 * **Install it from here** (ADR-0019): the Controller opens one SSH
 * connection, puts the agent there, runs the installer and hangs up. The
 * credential is a parameter of that request and nothing keeps it.
 *
 * **Copy a command** (ADR-0011): the operator carries it to the machine. This
 * is the answer for a host behind NAT, an air-gapped one, and anybody who
 * would rather not type a root password into a browser — so it is a tab beside
 * the first, not a link at the bottom.
 *
 * The token is minted either way and is the same single sighting it always
 * was: the Controller keeps a digest, so there is no route that could show it
 * again and no amount of UI could make one.
 *
 * ## What this component must not do
 *
 * Hold the credential anywhere it outlives the dialog. It lives in `useState`
 * here, goes to `useDeploy.start`, and dies when this unmounts — no storage,
 * no URL, no lift into a parent. That is the same discipline the token has and
 * the reason both are in this file rather than in the panel.
 */

import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import type { JoinToken } from '../../../api/types';
import { rowView, submitReason, summarise } from '../model/deploy';
import { tokenLife } from '../model/hosts';
import type { Deployment } from '../model/useDeploy';

interface Props {
  readonly token: JoinToken;
  /** From `GET /agents/enrollment`. Changes what happens after either path. */
  readonly autoApprove: boolean;
  /** The run, and how to start one. */
  readonly deployment: Deployment;
  /** False on a read-only Controller: the form says so instead of failing. */
  readonly canDeploy: boolean;
  /** Whether the dashboard is served over loopback, for the warning below. */
  readonly loopback: boolean;
  readonly onClose: () => void;
}

type Tab = 'deploy' | 'command';

export function AddHostDialog({
  token,
  autoApprove,
  deployment,
  canDeploy,
  loopback,
  onClose,
}: Props) {
  const [tab, setTab] = useState<Tab>('deploy');
  const [now, setNow] = useState(() => Date.now());
  const [copied, setCopied] = useState<'install' | 'manual' | null>(null);
  const [clipboardFailed, setClipboardFailed] = useState(false);

  // The credential. Never leaves this component except as a request body.
  const [hosts, setHosts] = useState('');
  const [user, setUser] = useState('root');
  const [useKey, setUseKey] = useState(false);
  const [password, setPassword] = useState('');
  const [privateKey, setPrivateKey] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [refusal, setRefusal] = useState<string | null>(null);

  const life = tokenLife(token.expires_at, now);
  const run = deployment.run;
  const summary = summarise(run);
  const secret = useKey ? privateKey : password;
  const blocked = submitReason(hosts, user, secret, canDeploy);

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
      // A run in flight is not closed by a stray Escape: the credential is
      // already gone, but this dialog is the only view of what is happening.
      if (summary.busy) return;
      event.preventDefault();
      // Capture + stopImmediate so the app shell does not also dismiss Hosts.
      event.stopImmediatePropagation();
      onClose();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onClose, summary.busy]);

  const copy = async (which: 'install' | 'manual') => {
    try {
      await navigator.clipboard.writeText(which === 'install' ? token.install : token.manual);
      setCopied(which);
      setClipboardFailed(false);
    } catch {
      setClipboardFailed(true);
    }
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (blocked) return;
    setRefusal(null);
    const refused = await deployment.start({
      hosts,
      user,
      password: useKey ? '' : password,
      privateKey: useKey ? privateKey : '',
      passphrase: useKey ? passphrase : '',
    });
    if (refused) {
      setRefusal(refused);
      return;
    }
    // Dropped the moment it has been sent. There is no retry that would need
    // it again — a retry asks, which is the honest cost of not keeping it.
    setPassword('');
    setPrivateKey('');
    setPassphrase('');
  };

  /**
   * Portalled to the body, and it has to be.
   *
   * This dialog is rendered from inside the Hosts panel, and that panel has a
   * `backdrop-filter` — which makes it the containing block for every
   * `position: fixed` descendant. Left in place, the modal laid itself out
   * inside a 340px column and clipped the install command at its edge.
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

        <div className="tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'deploy'}
            className={`tabs__tab${tab === 'deploy' ? ' tabs__tab--on' : ''}`}
            onClick={() => setTab('deploy')}
          >
            Install it from here
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'command'}
            className={`tabs__tab${tab === 'command' ? ' tabs__tab--on' : ''}`}
            onClick={() => setTab('command')}
          >
            Copy a command
          </button>
        </div>

        {tab === 'deploy' ? (
          <>
            <p className="modal__lead">
              The Controller logs in over SSH, installs the agent, and hangs up. It does
              this one machine at a time and stops if one fails.
            </p>

            <form onSubmit={(event) => void submit(event)}>
              <label className="field">
                <span className="field__label">Addresses</span>
                <textarea
                  className="field__input field__input--area"
                  rows={3}
                  value={hosts}
                  spellCheck={false}
                  placeholder={'10.0.0.5\n10.0.0.6\ndb-01.internal:2222'}
                  disabled={summary.busy}
                  onChange={(event) => setHosts(event.target.value)}
                />
                <span className="field__hint">
                  One per line, or separated by commas. <code>host:port</code> if sshd is
                  not on 22; an IPv6 address with a port is written{' '}
                  <code>[fd00::1]:22</code>.
                </span>
              </label>

              <div className="field-row">
                <label className="field">
                  <span className="field__label">Log in as</span>
                  <input
                    className="field__input"
                    value={user}
                    autoComplete="off"
                    spellCheck={false}
                    disabled={summary.busy}
                    onChange={(event) => setUser(event.target.value)}
                  />
                </label>
                <div className="field">
                  <span className="field__label">Using</span>
                  <div className="segmented">
                    <button
                      type="button"
                      className={`segmented__part${!useKey ? ' segmented__part--on' : ''}`}
                      disabled={summary.busy}
                      onClick={() => setUseKey(false)}
                    >
                      Password
                    </button>
                    <button
                      type="button"
                      className={`segmented__part${useKey ? ' segmented__part--on' : ''}`}
                      disabled={summary.busy}
                      onClick={() => setUseKey(true)}
                    >
                      Private key
                    </button>
                  </div>
                </div>
              </div>

              {useKey ? (
                <>
                  <label className="field">
                    <span className="field__label">Private key</span>
                    <textarea
                      className="field__input field__input--area field__input--mono"
                      rows={3}
                      value={privateKey}
                      spellCheck={false}
                      placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                      disabled={summary.busy}
                      onChange={(event) => setPrivateKey(event.target.value)}
                    />
                    <span className="field__hint">
                      The whole file, including both <code>-----</code> lines.
                    </span>
                  </label>
                  <label className="field">
                    <span className="field__label">Passphrase, if it has one</span>
                    <input
                      className="field__input"
                      type="password"
                      value={passphrase}
                      autoComplete="off"
                      disabled={summary.busy}
                      onChange={(event) => setPassphrase(event.target.value)}
                    />
                  </label>
                </>
              ) : (
                <label className="field">
                  <span className="field__label">Password</span>
                  <input
                    className="field__input"
                    type="password"
                    value={password}
                    autoComplete="off"
                    disabled={summary.busy}
                    onChange={(event) => setPassword(event.target.value)}
                  />
                </label>
              )}

              <p className="modal__note">
                Root, or an account that can <code>sudo</code> without a password — the
                agent installs as a system service. Whatever you type is used to open one
                connection and is never written down: there is no host list, no stored key,
                and nothing to reconnect with afterwards.
              </p>

              {!loopback ? (
                <p className="modal__warn">
                  This dashboard is not on loopback and has no login of its own, so this
                  form sends a root credential over your network in the clear. Reach it
                  over <code>ssh -L</code> instead, or put your own login page in front of
                  it.
                </p>
              ) : null}

              {blocked && hosts.trim() !== '' ? (
                <p className="modal__note">{blocked}</p>
              ) : null}
              {refusal ? <p className="modal__warn">{refusal}</p> : null}

              <button
                type="submit"
                className="action action--primary modal__submit"
                disabled={blocked !== null || summary.busy}
              >
                {summary.busy ? 'Installing…' : 'Install the agent'}
              </button>
            </form>

            {run ? (
              <div className="deploy">
                <ul className="deploy__list">
                  {run.hosts.map((entry) => {
                    const view = rowView(entry);
                    return (
                      <li key={`${entry.host}:${entry.port}`} className="deploy__row">
                        <span className={`deploy__dot deploy__dot--${view.tone}`} />
                        <span className="deploy__host">{view.address}</span>
                        <span className="deploy__phase">{view.label}</span>
                        {view.detail ? (
                          <span className="deploy__detail">{view.detail}</span>
                        ) : null}
                        {view.fingerprint ? (
                          <span className="deploy__print" title="Host key it answered with">
                            {view.fingerprint}
                          </span>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
                {summary.headline ? (
                  <p className={`deploy__summary deploy__summary--${summary.tone}`}>
                    {summary.headline}
                  </p>
                ) : null}
              </div>
            ) : null}
          </>
        ) : (
          <>
            <p className="modal__lead">
              Run this on the machine you want to manage. It installs the agent as a
              service, which then generates its own key, never sends it, and dials the
              Controller — nothing needs to reach the host.
            </p>

            <div className="token">
              <input
                className="token__command"
                readOnly
                value={token.install}
                aria-label="Install command"
                onFocus={(event) => event.currentTarget.select()}
              />
              <button
                type="button"
                className="control token__copy"
                onClick={() => void copy('install')}
              >
                {copied === 'install' ? 'Copied' : 'Copy'}
              </button>
            </div>

            {clipboardFailed ? (
              <p className="modal__note">
                The browser would not write to the clipboard. Select the command above and
                copy it by hand.
              </p>
            ) : null}

            <p className="modal__note">
              This is the way in for a host the Controller cannot reach — behind NAT, on a
              laptop, on a network with no inbound SSH — and it needs nothing opened on
              that machine.
            </p>

            {/* Folded away, because it answers a different question: the host
                already has the binary. Offering both at the same size would
                make the first decision here "which of these am I", for a
                distinction most people meet once. */}
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
                <button
                  type="button"
                  className="control token__copy"
                  onClick={() => void copy('manual')}
                >
                  {copied === 'manual' ? 'Copied' : 'Copy'}
                </button>
              </div>
              <p>
                Also what to use on a network with no route to GitHub: build the binary
                with <code>scripts/build-agent.sh</code>, copy it over, and run{' '}
                <code>install-agent.sh --binary</code> to get the service as well.
              </p>
            </details>

            <p className="modal__warn">
              This token is shown once. The Controller keeps only a digest of it, so
              closing this dialog loses it for good — if you need it again, add the host
              again and mint a new one. It is single use and expires on its own.
            </p>
          </>
        )}

        <p className="modal__note">
          {autoApprove
            ? 'This Controller approves agents on enrollment, so a host starts contributing to the topology as soon as it connects.'
            : 'A new host then appears in Hosts as awaiting approval. It contributes nothing to the topology until you approve it there.'}{' '}
          Upgrading an agent later needs none of this: the Controller pushes a signed
          release down the connection the host already holds.
        </p>

        <div className="modal__meta">
          <span className="row__label">CA fingerprint</span>
          <span className="row__value row__value--mono">{token.ca_fingerprint}</span>
        </div>

        <footer className="modal__foot">
          <button
            type="button"
            className="action action--primary"
            disabled={summary.busy}
            onClick={() => {
              deployment.dismiss();
              onClose();
            }}
          >
            {summary.busy ? 'Installing…' : 'Done — discard this token'}
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  );
}
