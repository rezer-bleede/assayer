'use client';

import { useEffect, useState, type FormEvent } from 'react';
import { API_URL } from '@/lib/api';
import { applyTheme, writeChoice, type ThemeChoice } from '@/lib/theme';

type Status = { setup_required: boolean; google_enabled: boolean; sso_enabled: boolean };

export function AuthGate({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [signedIn, setSignedIn] = useState(false);
  const [inviteToken, setInviteToken] = useState('');
  const [inviteEmail, setInviteEmail] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    if (typeof window !== 'undefined') setInviteToken(new URLSearchParams(window.location.search).get('invite') ?? '');
    const invite = typeof window !== 'undefined' ? new URLSearchParams(window.location.search).get('invite') : null;
    if (invite) void fetch(`${API_URL}/api/auth/invite/${encodeURIComponent(invite)}`, { cache: 'no-store' })
      .then((r) => r.ok ? r.json() : null).then((data) => { if (live && data?.email) setInviteEmail(data.email); });
    void (async () => {
      try {
        const s = await fetch(`${API_URL}/api/auth/status`, { cache: 'no-store' }).then((r) => r.json());
        if (!live) return;
        setStatus(s);
        if (!s.setup_required) {
          const me = await fetch(`${API_URL}/api/auth/me`, { credentials: 'include', cache: 'no-store' });
          if (live && me.ok) {
            const data = await me.json();
            if (invite) {
              const joined = await fetch(`${API_URL}/api/auth/join-invite`, {
                method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: invite }),
              });
              if (!joined.ok) {
                const detail = await joined.json().catch(() => ({}));
                throw new Error(detail.detail || 'Could not join this workspace.');
              }
            }
            const theme = data.user?.settings?.theme as ThemeChoice | undefined;
            if (theme === 'system' || theme === 'light' || theme === 'dark') {
              writeChoice(theme);
              applyTheme(theme);
            }
            if (invite) window.history.replaceState({}, '', '/');
            setSignedIn(true);
          }
        }
      } catch (err) {
        if (live) setError(err instanceof Error ? err.message : 'Unable to reach the Assayer API.');
      }
    })();
    return () => { live = false; };
  }, []);

  if (signedIn) return <>{children}</>;
  if (!status) return <div className="flex min-h-dvh items-center justify-center text-sm text-muted">{error || 'Connecting to assayer…'}</div>;
  return <LoginScreen status={status} inviteToken={inviteToken} inviteEmail={inviteEmail} initialError={error} onSuccess={() => { window.history.replaceState({}, '', '/'); setSignedIn(true); }} />;
}

function LoginScreen({ status, inviteToken, inviteEmail, initialError, onSuccess }: { status: Status; inviteToken: string; inviteEmail: string; initialError: string; onSuccess: () => void }) {
  const [setup] = useState(status.setup_required);
  const acceptingInvite = Boolean(inviteToken);
  const [existingAccount, setExistingAccount] = useState(false);
  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [setupToken, setSetupToken] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => { if (inviteEmail) setEmail(inviteEmail); }, [inviteEmail]);
  useEffect(() => { if (initialError) setError(initialError); }, [initialError]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const endpoint = setup ? 'bootstrap' : acceptingInvite && !existingAccount ? 'accept-invite' : 'login';
      const response = await fetch(`${API_URL}/api/auth/${endpoint}`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(setup ? { email, password, name, setup_token: setupToken } : acceptingInvite ? { email, password, name, token: inviteToken } : { email, password }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not sign in. Check your details and try again.');
      if (acceptingInvite && existingAccount) {
        const joined = await fetch(`${API_URL}/api/auth/join-invite`, {
          method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: inviteToken }),
        });
        const result = await joined.json();
        if (!joined.ok) throw new Error(result.detail || 'Could not join this workspace.');
      }
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not sign in. Try again.');
    } finally { setBusy(false); }
  }

  return (
    <main className="auth-page">
      <section className="auth-card">
        <img src="/logo.svg" alt="" className="h-11 w-11" />
        <p className="mt-5 text-xs font-semibold uppercase tracking-[.16em] text-muted">assayer</p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">{setup ? 'Set up your workspace' : acceptingInvite ? 'Join your team' : 'Welcome back'}</h1>
        <p className="mt-2 text-sm leading-6 text-muted">{setup ? 'Create the administrator account to get started. Existing chats and projects will be added to your workspace.' : acceptingInvite ? existingAccount ? 'Sign in to join the invited workspace.' : 'Create your account to join the invited workspace.' : 'Sign in to continue to your QA workspace.'}</p>
        <form className="mt-7 space-y-4" onSubmit={submit}>
          {(setup || (acceptingInvite && !existingAccount)) && <label className="auth-label">Your name<input className="field mt-1.5" autoComplete="name" required value={name} onChange={(e) => setName(e.target.value)} /></label>}
          <label className="auth-label">Work email<input className="field mt-1.5" type="email" autoComplete="email" required value={email} readOnly={acceptingInvite} onChange={(e) => setEmail(e.target.value)} /></label>
          {acceptingInvite && <p className="-mt-2 text-[11px] text-muted">Use the email address this invitation was sent to.</p>}
          <label className="auth-label">Password<input className="field mt-1.5" type="password" autoComplete={setup || (acceptingInvite && !existingAccount) ? 'new-password' : 'current-password'} minLength={setup || (acceptingInvite && !existingAccount) ? 12 : undefined} required value={password} onChange={(e) => setPassword(e.target.value)} /><span className="mt-1 block text-[11px] font-normal text-muted">{setup || (acceptingInvite && !existingAccount) ? 'Use at least 12 characters.' : 'Enter your current password.'}</span></label>
          {setup && <label className="auth-label">One-time setup token<input className="field mt-1.5" type="password" required minLength={16} value={setupToken} onChange={(e) => setSetupToken(e.target.value)} /><span className="mt-1 block text-[11px] font-normal text-muted">Set BOOTSTRAP_TOKEN in your deployment environment.</span></label>}
          {error && <p role="alert" className="rounded-lg border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-600">{error}</p>}
          <button className="btn-primary w-full py-2.5" type="submit" disabled={busy}>{busy ? 'Please wait…' : setup ? 'Create administrator' : acceptingInvite && !existingAccount ? 'Create account and join' : 'Sign in'}</button>
        </form>
        {acceptingInvite && <button className="mt-3 w-full text-xs text-muted hover:text-fg" onClick={() => { setExistingAccount((value) => !value); setPassword(''); setError(''); }}>{existingAccount ? 'New to assayer? Create an account' : 'Already have an account? Sign in to join'}</button>}
        {!setup && (status.google_enabled || status.sso_enabled) && <div className="mt-4 grid grid-cols-2 gap-2">{status.google_enabled && <a className="btn-ghost" href={`${API_URL}/api/auth/google`}>Continue with Google</a>}{status.sso_enabled && <a className="btn-ghost" href={`${API_URL}/api/auth/sso`}>Company SSO</a>}</div>}
        <p className="mt-6 border-t border-border pt-4 text-center text-[11px] leading-5 text-muted">Your workspace data is available only to your signed-in account and invited teammates.</p>
      </section>
    </main>
  );
}
