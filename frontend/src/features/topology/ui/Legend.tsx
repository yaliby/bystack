/**
 * Compact legend for the card topology language.
 */

import type { EdgeKind } from '../../../api/types';
import { EDGE_DASH, EDGE_LABEL, STATUS_LABEL } from './theme';
import type { Palette, StatusRole } from './theme';

const EDGES: readonly EdgeKind[] = ['depends_on', 'mounts', 'exposed_on', 'hosts'];

const EDGE_CAPTION: Partial<Record<EdgeKind, string>> = {
  depends_on: 'depends on · flow',
  mounts: 'mounts volume',
  exposed_on: 'published on host',
  hosts: 'watched on host',
};

const STATUSES: readonly StatusRole[] = ['good', 'warning', 'serious', 'critical', 'neutral'];

export function Legend({ palette }: { readonly palette: Palette }) {
  return (
    <div className="legend">
      <div className="legend__group">
        <h4>Links</h4>
        {EDGES.map((kind) => (
          <div key={kind} className="legend__item">
            <svg width="36" height="12" aria-hidden="true">
              <line
                x1="1"
                y1="6"
                x2="35"
                y2="6"
                stroke={palette.edgeColor[kind]}
                strokeWidth="2"
                strokeDasharray={(EDGE_DASH[kind] ?? []).join(' ') || undefined}
                strokeLinecap="round"
              />
              {kind === 'depends_on' ? (
                <>
                  <circle cx="10" cy="6" r="1.8" fill={palette.edgeColor[kind]} opacity="0.7" />
                  <circle cx="22" cy="6" r="1.8" fill={palette.edgeColor[kind]} opacity="0.7" />
                </>
              ) : null}
            </svg>
            <span>{EDGE_CAPTION[kind] ?? EDGE_LABEL[kind]}</span>
          </div>
        ))}
      </div>

      <div className="legend__group">
        <h4>State</h4>
        {STATUSES.map((status) => (
          <div key={status} className="legend__item">
            <svg width="20" height="12" aria-hidden="true">
              <circle cx="8" cy="6" r="4.5" fill={palette.status[status]} />
            </svg>
            <span>{STATUS_LABEL[status]}</span>
          </div>
        ))}
      </div>

      {/* Reading the canvas correctly depends on knowing what it leaves out. A
          missing network card is a deliberate choice, and unexplained it looks
          exactly like a discovery bug. */}
      <div className="legend__group">
        <h4>Canvas</h4>
        {/* The three workload kinds share a card shape on purpose, so the word
            on the second line is the only thing telling them apart. Saying so
            here is cheaper than an operator learning it by being surprised. */}
        <div className="legend__item legend__item--note">
          Every card names its kind on the second line
        </div>
        <div className="legend__item legend__item--note">
          compose service · systemd unit · process
        </div>
        <div className="legend__item legend__item--note">
          A frame is a compose project — its network is the frame
        </div>
        <div className="legend__item legend__item--note">
          Click a link · drag a card or a frame
        </div>
      </div>
    </div>
  );
}
