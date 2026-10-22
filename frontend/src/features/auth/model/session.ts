/**
 * Who is signed in, and how to find out (ADR-0020).
 *
 * Pure of React on purpose: every decision about what an answer *means* is
 * here, where a test can hand it a fake `fetch`, and `useSession.ts` is only
 * the lifecycle around it.
 *
 * The session itself is an `HttpOnly` cookie. This file never holds a token and
 * cannot read one — that is the point of the cookie — so "signed in" here means
 * "the Controller just said so", asked again every half minute.
 */

/** `GET /api/v1/auth/status`. Answers without a session. */
export interface AuthStatus {
  readonly auth_required: boolean;
  /** No account exists yet: the next visitor creates the administrator. */
  readonly setup_required: boolean;
  readonly setup_token_required: boolean;
  readonly sso: boolean;
}

export type Session =
  | { readonly kind: 'checking' }
  /** The Controller did not answer. Not the same as being signed out. */
  | { readonly kind: 'unreachable' }
  /** `auth.required` is off on this Controller: nothing to sign in to. */
  | { readonly kind: 'open' }
  | {
      readonly kind: 'signed-out';
      readonly status: AuthStatus;
      /** Why we are here, when it was not simply "first visit". */
      readonly notice: string | null;
    }
  | { readonly kind: 'signed-in'; readonly username: string };

export type Outcome =
  | { readonly ok: true; readonly username: string }
  | { readonly ok: false; readonly error: string };

/** The ticket BYheaven puts in the URL fragment, or `null`. */
export function ssoTicket(hash: string): string | null {
  return new URLSearchParams(hash.replace(/^#/, '')).get('sso');
}

/** The fragment with the ticket removed, so it never stays in the address bar. */
export function withoutSso(hash: string): string {
  const params = new URLSearchParams(hash.replace(/^#/, ''));
  params.delete('sso');
  const rest = params.toString();
  return rest ? `#${rest}` : '';
}

/** A sentence for a person, from whatever the Controller answered with. */
export async function readError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === 'string') return body.detail;
  } catch {
    /* fall through to the status line */
  }
  return `The Controller answered ${response.status}.`;
}

export async function post(baseUrl: string, path: string, body?: unknown): Promise<Outcome> {
  try {
    const response = await fetch(new URL(`/api/v1/auth/${path}`, baseUrl), {
      method: 'POST',
      headers: body === undefined ? {} : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) return { ok: false, error: await readError(response) };
    const parsed = response.status === 204 ? null : ((await response.json()) as { username: string });
    return { ok: true, username: parsed?.username ?? '' };
  } catch {
    return { ok: false, error: 'Could not reach the Controller.' };
  }
}

/**
 * Ask the Controller where this browser stands.
 *
 * Two questions, in this order: does anyone need to sign in at all, and if so
 * is this browser already. The second is a plain GET with the cookie, so a
 * `401` is the ordinary answer for a fresh visit and not an error.
 */
export async function resolveSession(
  baseUrl: string,
  notice: string | null = null,
): Promise<Session> {
  let status: AuthStatus;
  try {
    const response = await fetch(new URL('/api/v1/auth/status', baseUrl));
    if (!response.ok) return { kind: 'unreachable' };
    status = (await response.json()) as AuthStatus;
  } catch {
    return { kind: 'unreachable' };
  }
  if (!status.auth_required) return { kind: 'open' };

  try {
    const me = await fetch(new URL('/api/v1/auth/me', baseUrl));
    if (me.ok) return { kind: 'signed-in', username: ((await me.json()) as { username: string }).username };
  } catch {
    return { kind: 'unreachable' };
  }
  return { kind: 'signed-out', status, notice };
}
