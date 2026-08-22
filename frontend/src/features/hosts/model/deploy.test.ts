/**
 * What a deployment run means on screen (ADR-0019).
 *
 * The case that earns most of this file is `skipped`. A machine that already
 * runs an agent is a *correct* outcome for "make this machine managed", and a
 * UI that draws it red sends an operator to look at a host that is working.
 * The second is the failure summary: a run stops at the first host that fails,
 * so the ones after it were never attempted — and reporting those as failures
 * sends somebody to check machines nothing has touched.
 */

import { describe, expect, it } from 'vitest';
import type { DeployHost, DeployRun } from '../../../api/types';
import { rowView, submitReason, summarise } from './deploy';

function host(overrides: Partial<DeployHost> = {}): DeployHost {
  return {
    host: '10.0.0.5',
    port: 22,
    phase: 'waiting',
    detail: '',
    engine_id: '',
    fingerprint: '',
    ...overrides,
  };
}

function run(hosts: DeployHost[], overrides: Partial<DeployRun> = {}): DeployRun {
  return {
    running: false,
    started_at: 1,
    finished_at: 2,
    error: '',
    hosts,
    ...overrides,
  };
}

describe('rowView', () => {
  it('draws an already-managed host as good news, not as a failure', () => {
    expect(rowView(host({ phase: 'skipped' })).tone).toBe('good');
    expect(rowView(host({ phase: 'done' })).tone).toBe('good');
    expect(rowView(host({ phase: 'failed' })).tone).toBe('bad');
  });

  it('separates not-attempted from in-progress', () => {
    expect(rowView(host({ phase: 'waiting' })).tone).toBe('pending');
    expect(rowView(host({ phase: 'installing' })).tone).toBe('busy');
    expect(rowView(host({ phase: 'enrolling' })).tone).toBe('busy');
  });

  it('shows a port only when it is not the default', () => {
    expect(rowView(host()).address).toBe('10.0.0.5');
    expect(rowView(host({ port: 2222 })).address).toBe('10.0.0.5:2222');
  });

  it('carries the host key through, because comparing it is the whole point', () => {
    const view = rowView(host({ fingerprint: 'SHA256:abc' }));
    expect(view.fingerprint).toBe('SHA256:abc');
  });
});

describe('summarise', () => {
  it('says nothing before there has been a run', () => {
    expect(summarise(null)).toEqual({ headline: '', tone: 'pending', busy: false });
  });

  it('names the host being worked on while the run is going', () => {
    const state = summarise(
      run([host({ phase: 'done' }), host({ host: '10.0.0.6', phase: 'installing' })], {
        running: true,
        finished_at: 0,
      }),
    );
    expect(state.busy).toBe(true);
    expect(state.tone).toBe('busy');
    expect(state.headline).toContain('10.0.0.6');
  });

  it('counts a skip as managed, because it is', () => {
    const state = summarise(run([host({ phase: 'done' }), host({ phase: 'skipped' })]));
    expect(state.tone).toBe('good');
    expect(state.headline).toBe('2 hosts are managed.');
  });

  it('reports the untried hosts as untried rather than as failures', () => {
    const state = summarise(
      run([
        host({ phase: 'done' }),
        host({ host: '10.0.0.6', phase: 'failed' }),
        host({ host: '10.0.0.7' }),
        host({ host: '10.0.0.8' }),
      ]),
    );
    expect(state.tone).toBe('bad');
    expect(state.headline).toContain('10.0.0.6 failed');
    expect(state.headline).toContain('2 hosts were not attempted');
  });

  it('does not mention untried hosts when the last one failed', () => {
    const state = summarise(
      run([host({ phase: 'done' }), host({ host: '10.0.0.6', phase: 'failed' })]),
    );
    expect(state.headline).toBe('10.0.0.6 failed.');
  });

  it('says one host in the singular', () => {
    expect(summarise(run([host({ phase: 'done' })])).headline).toBe('1 host is managed.');
    const one = summarise(
      run([host({ phase: 'failed' }), host({ host: '10.0.0.6' })]),
    );
    expect(one.headline).toContain('1 host was not attempted');
  });
});

describe('submitReason', () => {
  it('refuses before anything is typed, and says which field', () => {
    expect(submitReason('', 'root', 'pw', true)).toContain('address');
    expect(submitReason('10.0.0.5', '', 'pw', true)).toContain('account');
    expect(submitReason('10.0.0.5', 'root', '', true)).toContain('password');
  });

  it('refuses on a read-only Controller before asking for anything', () => {
    const reason = submitReason('', '', '', false);
    expect(reason).toContain('read-only');
  });

  it('is null when the form is ready', () => {
    expect(submitReason('10.0.0.5\n10.0.0.6', 'root', 'pw', true)).toBeNull();
  });
});
