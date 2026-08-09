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
import { LOGGABLE_KINDS, canRefresh, isLive, linesOf, noticeOf, stderrCount } from '../model/logs';

interface Props {
  readonly logs: Logs;
  /** The selected node's kind. Networks and volumes write nothing. */
  readonly kind: string;
}

export function LogsPanel({ logs, kind }: Props) {
  if (!LOGGABLE_KINDS.has(kind)) return null;

  const notice = noticeOf(logs.state);
  const errors = stderrCount(logs.state);
  const lines = linesOf(logs.state);
  const live = isLive(logs.state);
  const dropped = logs.state.kind === 'live' ? logs.state.dropped : 0;

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
        {/* Says whether anyone is still listening. A still panel otherwise
            reads as "nothing is happening" when it may be "nothing is
            connected", and those call for opposite reactions. */}
        {logs.open && live ? (
          <span className="logs__live" aria-label="Following live">
            ● Live
          </span>
        ) : null}
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
          {/* Never silent about a gap. A log with lines removed that does not
              say so is one an operator reads as continuous, and they will draw
              a conclusion from two lines that were never adjacent. */}
          {dropped > 0 ? (
            <p className="logs__notice logs__notice--warning">
              {dropped} line{dropped === 1 ? '' : 's'} dropped — output arrived faster
              than it could be shown.
            </p>
          ) : null}
          {lines.length > 0 ? (
            <ol className="logs">
              {lines.map((line, index) => (
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
          {lines.length > 0 ? (
            <p className="logs__foot muted">
              {live ? `${lines.length} lines, following` : `Last ${lines.length} lines`}
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
