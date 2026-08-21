import { describe, expect, it } from 'vitest';
import type { ControllerSelf, ControllerUpdate } from '../../../api/types';
import {
  controllerOffer,
  describeUpdate,
  expectSilence,
  progressOf,
  updateFailed,
} from './controller';

function update(overrides: Partial<ControllerUpdate> = {}): ControllerUpdate {
  return {
    phase: 'success',
    version: '0.5.0',
    detail: '',
    cascade: '',
    updated_at: 1,
    running: false,
    ...overrides,
  };
}

function self(overrides: Partial<ControllerSelf> = {}): ControllerSelf {
  return {
    version: '0.4.0',
    updatable: true,
    reason: '',
    read_only: false,
    update: null,
    ...overrides,
  };
}

describe('controllerOffer', () => {
  it('makes no claim before the first answer', () => {
    // Offering a button before knowing whether there is an updater is how an
    // operator gets told to press something that cannot work.
    expect(controllerOffer(null)).toBeNull();
  });

  it('says why there is no button rather than showing a broken one', () => {
    const offer = controllerOffer(self({ updatable: false, reason: 'this is a container' }));
    expect(offer).toEqual({ kind: 'unavailable', reason: 'this is a container' });
  });

  it('offers an update when nothing has been asked for', () => {
    expect(controllerOffer(self())).toEqual({ kind: 'idle', version: '0.4.0' });
  });

  it('watches a run rather than offering another', () => {
    const offer = controllerOffer(self({ update: update({ phase: 'applying', running: true }) }));
    expect(offer?.kind).toBe('running');
  });

  it('reports a finished run until it is dismissed', () => {
    expect(controllerOffer(self({ update: update() }))?.kind).toBe('ended');
  });
});

describe('progressOf', () => {
  it('never goes backwards as a run advances', () => {
    // A bar that retreats reads as a failure. The weights are uneven on
    // purpose -- fetching is tens of megabytes and applying is a rename -- but
    // the order has to be monotonic whatever the weights are.
    const phases = ['fetching', 'verifying', 'applying', 'probation', 'cascading', 'success'];
    const values = phases.map((phase) => progressOf(update({ phase })));
    for (let index = 1; index < values.length; index += 1) {
      expect(values[index]).toBeGreaterThanOrEqual(values[index - 1]);
    }
    expect(values.at(-1)).toBe(1);
  });

  it('fills the bar for a run that ended badly', () => {
    // The bar is not a claim about success. Leaving it part-full under a
    // failure message reads as "still going", which is the one thing a
    // terminal state must not look like.
    expect(progressOf(update({ phase: 'rolled_back' }))).toBe(1);
    expect(progressOf(update({ phase: 'failed' }))).toBe(1);
  });
});

describe('describeUpdate', () => {
  it('prefers the sentence the process that did the work wrote', () => {
    // The manager has seen the error. A summary composed here would bury the
    // part that says what to do.
    const detail = 'curl exited 22 fetching bystack-controller-x86_64';
    expect(describeUpdate(update({ phase: 'failed', detail }))).toBe(detail);
  });

  it('warns that the page is about to go quiet', () => {
    // The one thing an operator must not conclude during `applying` is that
    // the dashboard has broken.
    expect(describeUpdate(update({ phase: 'applying' }))).toMatch(/go quiet/);
  });

  it('says a rollback left the machine working', () => {
    const said = describeUpdate(update({ phase: 'rolled_back', version: '0.4.0' }));
    expect(said).toMatch(/previous Controller was put back/);
    expect(said).toContain('0.4.0');
  });
});

describe('updateFailed', () => {
  it('counts a rollback, because the published release does not run here', () => {
    // The rollback is the system working. It is still something somebody has
    // to know before pressing the button again.
    expect(updateFailed(update({ phase: 'rolled_back' }))).toBe(true);
    expect(updateFailed(update({ phase: 'failed' }))).toBe(true);
    expect(updateFailed(update({ phase: 'success' }))).toBe(false);
  });
});

describe('expectSilence', () => {
  it('is true exactly while the backend is being replaced', () => {
    // What this gates is whether a failed fetch is an error or the operation
    // working. Getting it wrong replaces a progress bar with "cannot reach the
    // Controller" at the moment the operator most needs to be told to wait.
    expect(expectSilence(controllerOffer(self({ update: update({ running: true }) })))).toBe(true);
    expect(expectSilence(controllerOffer(self()))).toBe(false);
    expect(expectSilence(null)).toBe(false);
  });
});
