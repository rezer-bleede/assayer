'use client';

import { useEffect, useState, type FormEvent } from 'react';
import { API_URL } from '@/lib/api';
import type { ThemeChoice } from '@/lib/theme';

type Profile = { id: string; email: string; name: string; role: string; settings: Record<string, unknown> };
type Workspace = { id: string; name: string; kind: string; role: string };

export function AccountSettings({ theme, onThemeChange }: { theme: ThemeChoice; onThemeChange: (theme: ThemeChoice) => void }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<'account' | 'preferences' | 'workspace' | 'admin'>('account');
  const [profile, setProfile] = useState<Profile | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [instructions, setInstructions] = useState('');
  const [name, setName] = useState('');
  const [teamName, setTeamName] = useState('');
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [adminSettings, setAdminSettings] = useState<Record<string, number>>({});
  const sections: Array<'account' | 'preferences' | 'workspace' | 'admin'> =
    profile?.role === 'admin' ? ['account', 'preferences', 'workspace', 'admin'] : ['account', 'preferences', 'workspace'];

  useEffect(() => {
    if (!open) return;
    void fetch(`${API_URL}/api/auth/me`, { credentials: 'include', cache: 'no-store' })
      .then((r) => r.json()).then((data) => {
        const p = data.user as Profile;
        setProfile(p); setName(p.name); setInstructions(String(p.settings?.instructions ?? ''));
        setWorkspaces(data.workspaces ?? []);
      }).catch(() => setNotice('Unable to load your account settings.'));
  }, [open]);

  useEffect(() => {
    if (!open || tab !== 'admin' || profile?.role !== 'admin') return;
    void fetch(`${API_URL}/api/auth/admin/settings`, { credentials: 'include', cache: 'no-store' })
      .then((r) => r.json()).then((data) => setAdminSettings(data.settings ?? {}))
      .catch(() => setNotice('Unable to load instance settings.'));
  }, [open, tab, profile?.role]);

  async function saveProfile(event?: FormEvent) {
    event?.preventDefault(); setBusy(true); setNotice('');
    try {
      const r = await fetch(`${API_URL}/api/auth/me`, { method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, settings: { instructions, theme } }) });
      const data = await r.json(); if (!r.ok) throw new Error(data.detail || 'Could not save settings');
      setProfile(data.user); setNotice('Settings saved.');
    } catch (e) { setNotice(e instanceof Error ? e.message : 'Could not save settings.'); }
    finally { setBusy(false); }
  }

  async function createTeam(event: FormEvent) {
    event.preventDefault(); setBusy(true); setNotice('');
    try {
      const r = await fetch(`${API_URL}/api/auth/workspaces`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: teamName }) });
      const data = await r.json(); if (!r.ok) throw new Error(data.detail || 'Could not create workspace');
      setWorkspaces((all) => [data, ...all]); setTeamName(''); setNotice('Team workspace created.');
    } catch (e) { setNotice(e instanceof Error ? e.message : 'Could not create workspace.'); }
    finally { setBusy(false); }
  }

  async function saveAdminSettings(event: FormEvent) {
    event.preventDefault(); setBusy(true); setNotice('');
    try {
      const r = await fetch(`${API_URL}/api/auth/admin/settings`, { method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(adminSettings) });
      const data = await r.json(); if (!r.ok) throw new Error(data.detail || 'Could not save instance settings');
      setAdminSettings(data.settings); setNotice('Instance settings saved. New test runs will use these limits.');
    } catch (e) { setNotice(e instanceof Error ? e.message : 'Could not save instance settings.'); }
    finally { setBusy(false); }
  }

  async function changePassword(event: FormEvent) {
    event.preventDefault(); setBusy(true); setNotice('');
    try {
      const r = await fetch(`${API_URL}/api/auth/change-password`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) });
      const data = await r.json(); if (!r.ok) throw new Error(data.detail || 'Could not update password');
      setCurrentPassword(''); setNewPassword(''); setNotice(data.status);
    } catch (e) { setNotice(e instanceof Error ? e.message : 'Could not update password.'); }
    finally { setBusy(false); }
  }

  async function switchWorkspace(workspace: Workspace) {
    const r = await fetch(`${API_URL}/api/auth/workspaces/activate`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workspace_id: workspace.id }) });
    if (r.ok) window.location.reload();
    else setNotice('Could not switch workspace.');
  }

  async function inviteToWorkspace(workspace: Workspace) {
    const email = window.prompt(`Invite someone to ${workspace.name}. Enter their email address:`)?.trim();
    if (!email) return;
    try {
      const r = await fetch(`${API_URL}/api/auth/workspaces/${encodeURIComponent(workspace.id)}/invites`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
      });
      const data = await r.json(); if (!r.ok) throw new Error(data.detail || 'Could not create invite');
      window.prompt('Share this one-time invitation link. It expires in seven days:', data.invite_url);
      setNotice('Invitation link created.');
    } catch (e) { setNotice(e instanceof Error ? e.message : 'Could not create invite.'); }
  }

  async function signOut() {
    await fetch(`${API_URL}/api/auth/logout`, { method: 'POST', credentials: 'include' });
    window.location.reload();
  }

  return <>
    <button type="button" className="account-trigger" onClick={() => { setOpen(true); setTab('account'); }} aria-label="Open account and settings">
      <span className="account-avatar">{profile?.name?.slice(0, 1).toUpperCase() ?? 'A'}</span>
      <span className="min-w-0 flex-1 text-left"><span className="block truncate text-xs font-medium">{profile?.name ?? 'Account & settings'}</span><span className="block truncate text-[10px] text-muted">{profile?.email ?? 'Manage your workspace'}</span></span>
      <span className="text-muted">···</span>
    </button>
    {open && <div className="settings-backdrop" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) setOpen(false); }}>
      <section className="settings-window" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <header className="flex items-start justify-between border-b border-border px-6 py-5"><div><p className="text-xs font-medium uppercase tracking-widest text-muted">assayer</p><h2 id="settings-title" className="mt-1 text-xl font-semibold">Settings</h2></div><button className="settings-close" onClick={() => setOpen(false)} aria-label="Close settings">×</button></header>
        <div className="settings-layout">
          <nav className="settings-nav" aria-label="Settings sections">
            {sections.map((item) => <button key={item} className={`settings-nav-item ${tab === item ? 'is-active' : ''}`} onClick={() => setTab(item)}>{item === 'admin' ? 'Administration' : item[0].toUpperCase() + item.slice(1)}</button>)}
          </nav>
          <div className="settings-content">
            {tab === 'account' && <div className="space-y-5"><form onSubmit={saveProfile} className="space-y-5"><div><h3 className="settings-heading">Your account</h3><p className="settings-description">Manage your profile and sign-in details.</p></div><label className="auth-label">Display name<input className="field mt-1.5" value={name} onChange={(e) => setName(e.target.value)} /></label><label className="auth-label">Email address<input className="field mt-1.5" value={profile?.email ?? ''} readOnly /></label><div className="settings-row"><div><p className="text-sm font-medium">Account role</p><p className="text-xs text-muted">{profile?.role === 'admin' ? 'Instance administrator' : 'Workspace member'}</p></div><span className="settings-badge">{profile?.role ?? '…'}</span></div><button disabled={busy} className="btn-primary">Save profile</button></form><form onSubmit={changePassword} className="space-y-3 border-t border-border pt-5"><h4 className="text-sm font-semibold">Change password</h4><label className="auth-label">Current password<input className="field mt-1.5" type="password" autoComplete="current-password" required value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} /></label><label className="auth-label">New password<input className="field mt-1.5" type="password" autoComplete="new-password" minLength={12} required value={newPassword} onChange={(e) => setNewPassword(e.target.value)} /><span className="mt-1 block text-[11px] font-normal text-muted">At least 12 characters. Other sessions will be signed out.</span></label><button className="btn-ghost" disabled={busy}>Update password</button></form><div className="border-t border-border pt-4"><button type="button" className="btn-ghost" onClick={() => void signOut()}>Sign out</button></div></div>}
            {tab === 'preferences' && <form onSubmit={saveProfile} className="space-y-5"><div><h3 className="settings-heading">Preferences</h3><p className="settings-description">Set the look and default guidance for your chats.</p></div><div><p className="auth-label">Color mode</p><div className="mt-2 grid grid-cols-3 gap-2">{(['system', 'light', 'dark'] as const).map((value) => <button key={value} type="button" className={`choice-card ${value === theme ? 'choice-selected' : ''}`} onClick={() => onThemeChange(value)}>{value === 'system' ? 'System' : value[0].toUpperCase() + value.slice(1)}</button>)}</div><p className="mt-2 text-xs text-muted">System mode follows your device. Choose Light or Dark to override it.</p></div><label className="auth-label">Personal instructions<textarea className="field mt-1.5 min-h-32 resize-y" maxLength={4000} placeholder="For example: Prefer concise steps and include the evidence behind findings." value={instructions} onChange={(e) => setInstructions(e.target.value)} /><span className="mt-1 block text-xs text-muted">Applied to your chats; project instructions take precedence within a project.</span></label><button disabled={busy} className="btn-primary">Save preferences</button></form>}
            {tab === 'workspace' && <div className="space-y-5"><div><h3 className="settings-heading">Workspaces</h3><p className="settings-description">Keep projects and test conversations organized by team.</p></div><div className="space-y-2">{workspaces.map((workspace) => <div key={workspace.id} className="settings-row"><div><p className="text-sm font-medium">{workspace.name}</p><p className="text-xs text-muted">{workspace.kind === 'personal' ? 'Personal workspace' : 'Team workspace'} · {workspace.role}</p></div><div className="flex gap-2"><button className="btn-ghost px-2.5 py-1.5 text-xs" onClick={() => void switchWorkspace(workspace)}>{workspace.role === 'active' ? 'Current' : 'Switch'}</button>{workspace.kind === 'team' && <button className="btn-ghost px-2.5 py-1.5 text-xs" onClick={() => void inviteToWorkspace(workspace)}>Invite</button>}</div></div>)}</div><form onSubmit={createTeam} className="border-t border-border pt-5"><label className="auth-label">Create a team workspace<input className="field mt-1.5" placeholder="Team name" value={teamName} onChange={(e) => setTeamName(e.target.value)} required /></label><button className="btn-primary mt-3" disabled={busy}>Create workspace</button><p className="mt-2 text-xs text-muted">Chats stay private to their creator. Team project sharing uses one-time invite links that expire after seven days.</p></form></div>}
            {tab === 'admin' && <form onSubmit={saveAdminSettings} className="space-y-5"><div><h3 className="settings-heading">Administration</h3><p className="settings-description">Instance-wide controls for your Assayer deployment.</p></div><div className="settings-row"><div><p className="text-sm font-medium">AI provider</p><p className="text-xs text-muted">Provider credentials remain managed by deployment environment.</p></div><span className="settings-badge">Server managed</span></div><div className="settings-row"><div><p className="text-sm font-medium">Authentication providers</p><p className="text-xs text-muted">Google and OIDC are enabled when configured by your administrator.</p></div><span className="settings-badge">Configured</span></div><h4 className="border-t border-border pt-4 text-sm font-semibold">Testing defaults</h4><div className="grid grid-cols-2 gap-3">{([['run_max_checks', 'Maximum checks per run'], ['run_max_concurrency', 'Concurrent checks'], ['run_query_timeout_seconds', 'Query timeout (seconds)'], ['run_max_row_sample', 'Failure row samples'], ['ui_visual_default_tolerance', 'Visual difference tolerance']] as const).map(([key, label]) => <label key={key} className="auth-label">{label}<input className="field mt-1.5" type="number" min="0" step={key === 'ui_visual_default_tolerance' ? '0.001' : '1'} value={adminSettings[key] ?? ''} onChange={(e) => setAdminSettings((current) => ({ ...current, [key]: Number(e.target.value) }))} /></label>)}</div><p className="text-xs leading-5 text-muted">Safety limits apply consistently when the API creates plans and when workers execute them. Provider secrets are managed in the server environment.</p><button className="btn-primary" disabled={busy}>Save instance settings</button></form>}
            {notice && <p role="status" className="mt-4 text-xs text-muted">{notice}</p>}
          </div>
        </div>
      </section>
    </div>}
  </>;
}
