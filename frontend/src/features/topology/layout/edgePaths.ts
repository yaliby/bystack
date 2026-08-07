/**
 * Extract absolute polyline points from an ELK layout result.
 *
 * ELK orthogonal routing puts bend points on edge sections. Without these,
 * every edge collapses to the same mid-Y elbow and the topology becomes
 * unreadable. Adapted from DockGraph's edgePaths approach (smooth U-turns).
 */

import type { ElkExtendedEdge, ElkNode } from 'elkjs/lib/elk.bundled.js';
import type { Point } from './elkLayout';

const UTURN_THRESHOLD = 30;

export function extractEdgePolylines(
  node: ElkNode,
  offsetX: number,
  offsetY: number,
  out: Map<string, Point[]>,
): void {
  for (const edge of (node as { edges?: ElkExtendedEdge[] }).edges ?? []) {
    if (out.has(edge.id)) continue;
    const sections = edge.sections;
    if (!sections?.length) continue;

    const points: Point[] = [];
    for (const section of sections) {
      const start: Point = {
        x: section.startPoint.x + offsetX,
        y: section.startPoint.y + offsetY,
      };
      const prev = points[points.length - 1];
      if (!prev || Math.abs(start.x - prev.x) > 0.5 || Math.abs(start.y - prev.y) > 0.5) {
        points.push(start);
      }
      for (const bend of section.bendPoints ?? []) {
        points.push({ x: bend.x + offsetX, y: bend.y + offsetY });
      }
      points.push({
        x: section.endPoint.x + offsetX,
        y: section.endPoint.y + offsetY,
      });
    }

    if (points.length >= 2) out.set(edge.id, smoothUturns(points));
  }

  for (const child of node.children ?? []) {
    extractEdgePolylines(child, offsetX + (child.x ?? 0), offsetY + (child.y ?? 0), out);
  }
}

function smoothUturns(points: Point[]): Point[] {
  const result = points.map((p) => ({ ...p }));
  let changed = true;

  while (changed) {
    changed = false;
    for (let i = 0; i < result.length - 3; i++) {
      const a = result[i];
      const b = result[i + 1];
      const c = result[i + 2];
      const d = result[i + 3];

      if (
        Math.abs(a.y - b.y) < 0.5 &&
        Math.abs(b.x - c.x) < 0.5 &&
        Math.abs(c.y - d.y) < 0.5
      ) {
        const abDir = Math.sign(b.x - a.x);
        const cdDir = Math.sign(d.x - c.x);
        const stubLen = Math.abs(b.y - c.y);
        if (abDir !== 0 && cdDir !== 0 && abDir !== cdDir && stubLen < UTURN_THRESHOLD) {
          if (Math.abs(a.x - d.x) < 0.5) result.splice(i + 1, 2);
          else result.splice(i + 1, 2, { x: a.x, y: c.y });
          changed = true;
          break;
        }
      }

      if (
        Math.abs(a.x - b.x) < 0.5 &&
        Math.abs(b.y - c.y) < 0.5 &&
        Math.abs(c.x - d.x) < 0.5
      ) {
        const abDir = Math.sign(b.y - a.y);
        const cdDir = Math.sign(d.y - c.y);
        const stubLen = Math.abs(b.x - c.x);
        if (abDir !== 0 && cdDir !== 0 && abDir !== cdDir && stubLen < UTURN_THRESHOLD) {
          if (Math.abs(a.y - d.y) < 0.5) result.splice(i + 1, 2);
          else result.splice(i + 1, 2, { x: c.x, y: a.y });
          changed = true;
          break;
        }
      }
    }
  }

  return result;
}
