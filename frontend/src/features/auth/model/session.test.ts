import { afterEach, describe, expect, it, vi } from 'vitest';
import { post, readError, resolveSession, ssoTicket, withoutSso } from './session';

const BASE = 'http://controller.test';

function reply(status: number, body: unknown): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), { status });
}

/** `fetch` that answers by path, and fails the test on a path nobody planned. */
function controller(routes: Record<string, Response | Error>) {
  const stub = vi.fn(async (input: URL | RequestInfo) => {
    const path = new URL(String(input)).pathname;
    const answer = routes[path];
    if (answer === undefined) throw new Error(`unplanned request: ${path}`);
    if (answer instanceof Error) throw answer;
    return answer.clone();
  });
  vi.stubGlobal('fetch', stub);
  return stub;
}

const STATUS = {
  auth_required: true,
  setup_required: false,
  setup_token_required: false,
  sso: true,
};

afterEach(() => vi.unstubAllGlobals());

describe('the BYheaven ticket in the address bar', () => {
  it('is read from the fragment', () => {
    expect(ssoTicket('#sso=v1.abc.def')).toBe('v1.abc.def');
    expect(ssoTicket('#other=1&sso=t')).toBe('t');
  });

  it('is absent when there is none', () => {
    expect(ssoTicket('')).toBeNull();
    expect(ssoTicket('#host=3')).toBeNull();
  });

  it('is removed without disturbing anything else in the fragment', () => {
    expect(withoutSso('#sso=t')).toBe('');
    expect(withoutSso('#host=3&sso=t')).toBe('#host=3');
  });
});

describe('what the Controller said', () => {
  it('uses the detail sentence when there is one', async () => {
    expect(await readError(reply(401, { detail: 'Invalid credentials' }))).toBe('Invalid credentials');
  });

  it('falls back to the status when the detail is not a sentence', async () => {
    // FastAPI answers a malformed body with a list of problems.
    expect(await readError(reply(422, { detail: [{ msg: 'x' }] }))).toBe('The Controller answered 422.');
  });
});

describe('where this browser stands', () => {
  it('is open when the Controller wants no sign-in', async () => {
    controller({ '/api/v1/auth/status': reply(200, { ...STATUS, auth_required: false }) });
    expect(await resolveSession(BASE)).toEqual({ kind: 'open' });
  });

  it('is signed in when the cookie is good', async () => {
    controller({
      '/api/v1/auth/status': reply(200, STATUS),
      '/api/v1/auth/me': reply(200, { username: 'alice' }),
    });
    expect(await resolveSession(BASE)).toEqual({ kind: 'signed-in', username: 'alice' });
  });

  it('is signed out, with the status the form needs, when there is no cookie', async () => {
    controller({
      '/api/v1/auth/status': reply(200, { ...STATUS, setup_required: true }),
      '/api/v1/auth/me': reply(401, { detail: 'Sign in required' }),
    });
    expect(await resolveSession(BASE, 'Your session ended.')).toEqual({
      kind: 'signed-out',
      status: { ...STATUS, setup_required: true },
      notice: 'Your session ended.',
    });
  });

  it('is unreachable, not signed out, when the Controller does not answer', async () => {
    controller({ '/api/v1/auth/status': new TypeError('failed to fetch') });
    expect(await resolveSession(BASE)).toEqual({ kind: 'unreachable' });
  });
});

describe('signing in', () => {
  it('returns the name the Controller used', async () => {
    controller({ '/api/v1/auth/login': reply(200, { username: 'alice' }) });
    expect(await post(BASE, 'login', { username: 'ALICE', password: 'x' })).toEqual({
      ok: true,
      username: 'alice',
    });
  });

  it('returns the refusal as a sentence', async () => {
    controller({ '/api/v1/auth/login': reply(429, { detail: 'Too many failed attempts.' }) });
    expect(await post(BASE, 'login', { username: 'a', password: 'b' })).toEqual({
      ok: false,
      error: 'Too many failed attempts.',
    });
  });

  it('says so when the Controller cannot be reached', async () => {
    controller({ '/api/v1/auth/login': new TypeError('failed to fetch') });
    expect(await post(BASE, 'login', {})).toEqual({
      ok: false,
      error: 'Could not reach the Controller.',
    });
  });

  it('treats a bodiless logout as success', async () => {
    controller({ '/api/v1/auth/logout': reply(204, null) });
    expect(await post(BASE, 'logout')).toEqual({ ok: true, username: '' });
  });
});
