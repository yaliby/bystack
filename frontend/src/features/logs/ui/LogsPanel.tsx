/**
 * The selected container's recent output.
 *
 * Presentation only. What may be read comes from the node's kind, what an
 * answer means comes from `logs.ts`, and this file renders both.
 *
 * Collapsed until asked, because opening it costs a round trip to an agent on
 * a remote host — and because an inspector that led with two hundred lines of
 * log would bury the identity and the actions the operator came here for.
 */

import type { Logs } from '../model/useLogs';
import { LOGGABLE_KINDS, canRefresh, noticeOf, stderrCount } from '../model/logs';

interface Props {
  readonly logs: Logs;
  /** The selected node's kind. Networks and volumes write nothing. */
  readonly kind: string;
}

export function LogsPanel({ logs, kind }: Props) {
  if (!LOGGABLE_KINDS.has(kind)) return null;

  const notice = noticeOf(logs.state);
  const errors = stderrCount(logs.state);

  return (
    <section className="inspector__section">
      <h3>
        <button
          type="button"
          className="logs__toggle"
          aria-expanded={logs.open}
          onClick={logs.toggle}
        >
          <span className="logs__caret" aria-hidden="true">
            {logs.open ? '▾' : '▸'}
          </span>
          Logs
        </button>
        {logs.open && errors > 0 ? (
          <span className="logs__errors">{errors} on stderr</span>
        ) : null}
        {logs.open && canRefresh(logs.state) ? (
          <button type="button" className="logs__refresh" onClick={logs.refresh}>
            Refresh
          </button>
        ) : null}
      </h3>

      {logs.open ? (
        <>
          {notice ? <p className={`logs__notice logs__notice--${notice.tone}`}>{notice.text}</p> : null}
          {logs.state.kind === 'ready' && logs.state.logs.lines.length > 0 ? (
            <ol className="logs">
              {logs.state.logs.lines.map((line, index) => (
                <li
                  // Index, deliberately: a log is an ordered list of possibly
                  // identical lines with no identity of their own, and the
                  // whole list is replaced on every read.
                  key={index}
                  className={line.stderr ? 'logs__line logs__line--stderr' : 'logs__line'}
                >
                  {line.text}
                </li>
              ))}
            </ol>
          ) : null}
          {/* The tail is bounded, and saying so is the difference between "this
              is all of it" and "this is the end of it". */}
          {logs.state.kind === 'ready' && logs.state.logs.lines.length > 0 ? (
            <p className="logs__foot muted">Last {logs.state.logs.lines.length} lines</p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
