/**
 * The door in front of the dashboard (ADR-0020).
 *
 * Renders the dashboard only once the Controller has said who this is — or
 * that nobody needs to be (`auth.required` off, or the dev mock). Everything
 * else is one of three plain screens: a sign-in form, the first-run form that
 * creates the administrator, or "cannot reach the Controller".
 *
 * Nothing here stores anything. The session is a cookie the page cannot read,
 * and the password lives in `useState` for as long as the form is mounted.
 */

import { createContext, useContext, useState, type FormEvent, type ReactNode } from 'react';
import type { AuthStatus } from '../model/session';
import { useSession } from '../model/useSession';

interface Who {
  /** `null` when no sign-in is involved (auth off, or the demo). */
  readonly username: string | null;
  readonly signOut: () => Promise<void>;
}

const WhoContext = createContext<Who>({ username: null, signOut: async () => {} });

/** For the shell: the name to show and the way out. */
export const useWho = (): Who => useContext(WhoContext);

export function AuthGate({ baseUrl, children }: { baseUrl: string; children: ReactNode }) {
  const { session, signIn, setup, signOut } = useSession(baseUrl);

  switch (session.kind) {
    case 'open':
      return <>{children}</>;
    case 'signed-in':
      return (
        <WhoContext.Provider value={{ username: session.username, signOut }}>
          {children}
        </WhoContext.Provider>
      );
    case 'checking':
      return <Screen title="ByStack" lede="Checking the Controller…" />;
    case 'unreachable':
      return (
        <Screen
          title="ByStack"
          lede="Cannot reach the Controller."
          notice="Is it running? This page retries when you come back to it."
        />
      );
    case 'signed-out':
      return <Form status={session.status} notice={session.notice} signIn={signIn} setup={setup} />;
  }
}

function Screen({
  title,
  lede,
  notice,
  children,
}: {
  title: string;
  lede: string;
  notice?: string | null;
  children?: ReactNode;
}) {
  return (
    <main className="auth" data-theme="dark">
      <div className="auth__card">
        <h1 className="auth__title">{title}</h1>
        <p className="auth__lede">{lede}</p>
        {notice ? (
          <p className="hosts__error" role="alert">
            {notice}
          </p>
        ) : null}
        {children}
      </div>
    </main>
  );
}

function Form({
  status,
  notice,
  signIn,
  setup,
}: {
  status: AuthStatus;
  notice: string | null;
  signIn: (username: string, password: string) => Promise<string | null>;
  setup: (username: string, password: string, token: string) => Promise<string | null>;
}) {
  const creating = status.setup_required;
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [token, setToken] = useState('');
  const [error, setError] = useState<string | null>(notice);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(
      creating ? await setup(username, password, token) : await signIn(username, password),
    );
    setBusy(false);
  };

  return (
    <main className="auth" data-theme="dark">
      <form className="auth__card" onSubmit={submit}>
        <h1 className="auth__title">ByStack</h1>
        <p className="auth__lede">
          {creating ? 'Create the administrator account.' : 'Sign in to the Controller.'}
        </p>
        {error ? (
          <p className="hosts__error" role="alert">
            {error}
          </p>
        ) : null}

        <label className="field">
          <span className="field__label">Username</span>
          <input
            className="field__input"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            required
            autoFocus
          />
        </label>
        <label className="field">
          <span className="field__label">Password</span>
          <input
            className="field__input"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete={creating ? 'new-password' : 'current-password'}
            required
          />
          {creating ? (
            <span className="field__hint">At least 8 characters.</span>
          ) : null}
        </label>
        {creating && status.setup_token_required ? (
          <label className="field">
            <span className="field__label">Setup token</span>
            <input
              className="field__input"
              type="password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              autoComplete="off"
              required
            />
            <span className="field__hint">
              The Controller was started with a setup token (<code>auth.setup_token</code>).
            </span>
          </label>
        ) : null}

        <button type="submit" className="action action--primary auth__submit" disabled={busy}>
          {busy ? 'One moment…' : creating ? 'Create account' : 'Sign in'}
        </button>
        {!creating && status.sso ? (
          <p className="field__hint">Opening ByStack from BYheaven signs you in on its own.</p>
        ) : null}
      </form>
    </main>
  );
}
