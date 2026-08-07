/**
 * The chrome that explains an empty or untrustworthy canvas.
 *
 * Presentation only — every judgement about what is wrong is made in
 * `model/status.ts` and arrives here already decided.
 */

import type { EmptyExplanation, SystemStatus } from '../model/status';

/** A strip across the top when the picture below cannot be trusted. */
export function StatusBanner({ status }: { readonly status: SystemStatus }) {
  if (!status.banner || !status.detail) return null;
  return (
    <div className={`banner banner--${status.tone}`} role="status" aria-live="polite">
      <span className="banner__dot" aria-hidden="true" />
      <span className="banner__text">{status.detail}</span>
    </div>
  );
}

/** Shown in place of the topology when there is nothing to draw. */
export function EmptyState({ explanation }: { readonly explanation: EmptyExplanation }) {
  return (
    <div className="empty-state" role="status">
      <div className={`empty-state__card empty-state__card--${explanation.tone}`}>
        <h2>{explanation.title}</h2>
        <p>{explanation.body}</p>
      </div>
    </div>
  );
}
