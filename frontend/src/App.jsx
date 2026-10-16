import { useCallback, useEffect, useRef, useState } from 'react';
import { confirmSignUp, currentEmail, resendCode, signIn, signOut, signUp } from './auth.js';
import { listFiles, retagFile, shareFile, uploadFile } from './api.js';

const EXPIRY = [[300, '5 minutes'], [900, '15 minutes'], [3600, '1 hour']];

const fmtSize = (b) => (b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`);
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString() : '—');

export default function App() {
  const [email, setEmail] = useState(undefined); // undefined = still checking
  useEffect(() => { currentEmail().then(setEmail); }, []);
  if (email === undefined) return <div className="center muted">Loading…</div>;
  return email
    ? <Dashboard email={email} onSignOut={() => { signOut(); setEmail(null); }} />
    : <AuthScreen onSignedIn={setEmail} />;
}

/* ------------------------------------------------------------------ Auth */
function AuthScreen({ onSignedIn }) {
  const [mode, setMode] = useState('signin'); // signin | signup | confirm
  const [form, setForm] = useState({ email: '', password: '', code: '' });
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  async function submit(e) {
    e.preventDefault();
    setBusy(true); setMsg(null);
    try {
      if (mode === 'signup') {
        await signUp(form.email, form.password);
        setMode('confirm');
        setMsg({ ok: true, text: 'Check your email for a 6-digit verification code.' });
      } else if (mode === 'confirm') {
        await confirmSignUp(form.email, form.code);
        await signIn(form.email, form.password);
        onSignedIn(form.email);
      } else {
        await signIn(form.email, form.password);
        onSignedIn(form.email);
      }
    } catch (err) {
      if (err.code === 'UserNotConfirmedException') setMode('confirm');
      setMsg({ ok: false, text: err.message || String(err) });
    } finally { setBusy(false); }
  }

  return (
    <div className="auth-wrap">
      <section className="auth-brand">
        <p className="eyebrow">AWS Serverless · AI-Powered Access Control</p>
        <h1>CloudVault</h1>
        <p className="lede">Secure file storage and sharing. Every file is encrypted with KMS, tagged by AI, and shared only through links that expire.</p>
        <ul className="pills"><li>Serverless</li><li>SSE-KMS Encrypted</li><li>AI Auto-Tagging</li></ul>
      </section>
      <form className="card auth-card" onSubmit={submit}>
        <h2>{mode === 'signup' ? 'Create account' : mode === 'confirm' ? 'Verify email' : 'Sign in'}</h2>
        <label>Email<input type="email" required value={form.email} onChange={set('email')} autoComplete="email" /></label>
        {mode !== 'confirm' && (
          <label>Password
            <input type="password" required minLength={8} value={form.password} onChange={set('password')}
              autoComplete={mode === 'signup' ? 'new-password' : 'current-password'} />
            {mode === 'signup' && <small className="muted">8+ characters with upper-case, lower-case and a number</small>}
          </label>
        )}
        {mode === 'confirm' && (
          <>
            <label>Verification code<input inputMode="numeric" required value={form.code} onChange={set('code')} /></label>
            {!form.password && <label>Password<input type="password" required value={form.password} onChange={set('password')} /></label>}
          </>
        )}
        {msg && <p className={msg.ok ? 'note ok' : 'note err'}>{msg.text}</p>}
        <button className="btn primary" disabled={busy}>{busy ? 'Please wait…' : mode === 'signup' ? 'Create account' : mode === 'confirm' ? 'Verify & sign in' : 'Sign in'}</button>
        <div className="auth-links">
          {mode === 'signin' && <button type="button" className="link" onClick={() => { setMode('signup'); setMsg(null); }}>New here? Create an account</button>}
          {mode !== 'signin' && <button type="button" className="link" onClick={() => { setMode('signin'); setMsg(null); }}>Back to sign in</button>}
          {mode === 'confirm' && <button type="button" className="link" onClick={() => resendCode(form.email).then(() => setMsg({ ok: true, text: 'Code re-sent.' }), (e) => setMsg({ ok: false, text: e.message }))}>Resend code</button>}
        </div>
      </form>
    </div>
  );
}

/* ------------------------------------------------------------------ Dashboard */
function Dashboard({ email, onSignOut }) {
  const [files, setFiles] = useState([]);
  const [query, setQuery] = useState('');
  const [error, setError] = useState(null);
  const [uploading, setUploading] = useState([]);
  const [dragging, setDragging] = useState(false);
  const [shareFor, setShareFor] = useState(null);
  const inputRef = useRef();

  const refresh = useCallback(async (tag = query) => {
    try { setFiles((await listFiles(tag)).files); setError(null); } catch (e) { setError(e.message); }
  }, [query]);

  useEffect(() => { refresh(''); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // While any file is still being uploaded/analysed, poll every 3 s.
  const processing = files.some((f) => f.status !== 'READY');
  useEffect(() => {
    if (!processing) return undefined;
    const t = setInterval(() => refresh(), 3000);
    return () => clearInterval(t);
  }, [processing, refresh]);

  async function handleFiles(list) {
    for (const file of Array.from(list)) {
      setUploading((u) => [...u, file.name]);
      try { await uploadFile(file); } catch (e) { setError(`${file.name}: ${e.message}`); }
      setUploading((u) => u.filter((n) => n !== file.name));
    }
    refresh();
  }

  const stats = {
    total: files.length,
    sensitive: files.filter((f) => f.sensitive).length,
    shared: files.reduce((n, f) => n + (f.shareCount || 0), 0),
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand"><span className="lock">🔐</span> CloudVault</div>
        <div className="user"><span className="muted">{email}</span><button className="btn ghost" onClick={onSignOut}>Sign out</button></div>
      </header>

      <main className="container">
        <section className="stats">
          <div><strong>{stats.total}</strong><span>files in your vault</span></div>
          <div><strong>{stats.sensitive}</strong><span>flagged sensitive by AI</span></div>
          <div><strong>{stats.shared}</strong><span>share links issued</span></div>
        </section>

        <section
          className={`dropzone ${dragging ? 'drag' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); handleFiles(e.dataTransfer.files); }}
          onClick={() => inputRef.current.click()}
        >
          <input ref={inputRef} type="file" multiple hidden onChange={(e) => { handleFiles(e.target.files); e.target.value = ''; }} />
          <p className="dz-title">Drop files here or click to upload</p>
          <p className="muted">Images, PDFs, text, Office docs · up to 25 MB · encrypted with KMS on arrival</p>
          {uploading.length > 0 && <p className="note ok">Uploading {uploading.join(', ')}…</p>}
        </section>

        <form className="search" onSubmit={(e) => { e.preventDefault(); refresh(query); }}>
          <input placeholder="Search by AI tag or file name (e.g. laptop, invoice, sensitive)" value={query} onChange={(e) => setQuery(e.target.value)} />
          <button className="btn primary">Search</button>
          {query && <button type="button" className="btn ghost" onClick={() => { setQuery(''); refresh(''); }}>Clear</button>}
        </form>

        {error && <p className="note err">{error}</p>}

        <section className="card table-card">
          {files.length === 0 ? (
            <p className="empty muted">{query ? 'No files match that search.' : 'Your vault is empty — upload a file to see AI tagging in action.'}</p>
          ) : (
            <table>
              <thead><tr><th>File</th><th>AI tags</th><th>Size</th><th>Uploaded</th><th /></tr></thead>
              <tbody>
                {files.map((f) => (
                  <tr key={f.fileId}>
                    <td>
                      <div className="fname">{f.fileName}</div>
                      <Status file={f} />
                    </td>
                    <td className="tags">{(f.tags || []).map((t) => (
                      <button key={t} className={`tag ${t === 'sensitive' ? 'danger' : ''}`} onClick={() => { setQuery(t); refresh(t); }}>{t}</button>
                    ))}</td>
                    <td className="nowrap">{fmtSize(f.size)}</td>
                    <td className="nowrap muted">{fmtDate(f.uploadedAt)}</td>
                    <td><div className="actions">
                      <button className="btn small primary" disabled={f.status !== 'READY'} onClick={() => setShareFor(f)}>Share</button>
                      <button className="btn small ghost" disabled={f.status !== 'READY'} onClick={() => retagFile(f.fileId).then(() => refresh(), (e) => setError(e.message))}>Re-tag</button>
                    </div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </main>

      {shareFor && <ShareDialog file={shareFor} onClose={() => { setShareFor(null); refresh(); }} />}
    </div>
  );
}

function Status({ file }) {
  if (file.status === 'PENDING_UPLOAD') return <span className="badge wait">Uploading…</span>;
  if (file.status !== 'READY') return <span className="badge wait">Analysing…</span>;
  if (file.sensitive) return <span className="badge danger" title={(file.piiTypes || []).join(', ')}>Sensitive · {(file.piiTypes || []).join(', ')}</span>;
  return <span className="badge ok">Encrypted · Ready</span>;
}

function ShareDialog({ file, onClose }) {
  const [expiresIn, setExpiresIn] = useState(file.sensitive ? 300 : 900);
  const [link, setLink] = useState(null);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(false);

  async function create() {
    setError(null); setLink(null);
    try { setLink(await shareFile(file.fileId, expiresIn)); } catch (e) { setError(e.message); }
  }

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <h2>Share “{file.fileName}”</h2>
        {file.sensitive && <p className="note err">AI found personal data ({(file.piiTypes || []).join(', ')}). Links for this file are limited to 15 minutes.</p>}
        <label>Link expires after
          <select value={expiresIn} onChange={(e) => setExpiresIn(Number(e.target.value))}>
            {EXPIRY.map(([s, l]) => <option key={s} value={s} disabled={file.sensitive && s > 900}>{l}{file.sensitive && s > 900 ? ' (blocked by AI rule)' : ''}</option>)}
          </select>
        </label>
        <button className="btn primary" onClick={create}>Create signed link</button>
        {error && <p className="note err">{error}</p>}
        {link && (
          <div className="link-box">
            <input readOnly value={link.url} onFocus={(e) => e.target.select()} />
            <button className="btn small primary" onClick={() => navigator.clipboard.writeText(link.url).then(() => setCopied(true))}>{copied ? 'Copied' : 'Copy'}</button>
            <p className="muted">Stops working at {new Date(link.expiresAt).toLocaleTimeString()}.</p>
          </div>
        )}
        <button className="btn ghost close" onClick={onClose}>Close</button>
      </div>
    </div>
  );
}
