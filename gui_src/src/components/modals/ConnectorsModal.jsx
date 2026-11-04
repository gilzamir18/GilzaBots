import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Cable, Plus, Trash2, Pencil, RefreshCw, Unplug, LogOut, ChevronDown, ChevronRight, ExternalLink, TriangleAlert } from 'lucide-react';
import { handleExternalClick } from '../../utils/openExternal';

// MCP connectors panel (PROJECT_DESIGN 2.25): external MCP servers whose tools
// the chat agent can use. The server never returns secret values; an edit that
// leaves a secret field empty keeps the stored value.

const FAST_REFRESH_MS = 1500;
const SLOW_REFRESH_MS = 6000;

const fieldStyle = { padding: '6px 8px', borderRadius: '6px', width: '100%', boxSizing: 'border-box' };
const labelStyle = { display: 'flex', flexDirection: 'column', gap: '4px', fontSize: '12px', minWidth: 0 };
const buttonStyle = {
  padding: '4px 8px', borderRadius: '6px', cursor: 'pointer', background: 'transparent',
  color: 'var(--vscode-text-fg)', border: '1px solid var(--vscode-border)', display: 'inline-flex',
  alignItems: 'center', gap: '4px', fontSize: '12px',
};
const primaryButtonStyle = {
  ...buttonStyle, background: 'var(--vscode-button-background, #5847d6)', color: 'var(--vscode-button-foreground, #fff)',
  border: '1px solid transparent',
};

const STATUS_COLORS = {
  connected: 'var(--vscode-fg-success, #3fb950)',
  connecting: 'var(--vscode-textLink-foreground, #4aa3ff)',
  authorizing: 'var(--vscode-textLink-foreground, #4aa3ff)',
  error: 'var(--vscode-fg-danger, #f85149)',
  disconnected: 'var(--vscode-descriptionForeground, #999)',
  off: 'var(--vscode-descriptionForeground, #999)',
};

const emptyForm = () => ({
  id: '', name: '', transport: 'http', url: '', auth: 'none', bearerToken: '',
  oauthClientId: '', oauthClientSecret: '', headers: [], command: '', args: '', env: [], cwd: '',
  approval: 'always', enabled: true, connectTimeout: 60, presetKey: '', presetArgs: {}, oauthScope: '',
});

const PRESET_CATEGORIES = ['social', 'productivity', 'development', 'local'];

// The form for a catalog entry: its connector fields, with the values the user
// must supply left empty (env rows by name, the token, an argument such as a folder).
const formFromPreset = (preset) => {
  const c = preset.connector;
  const envInputs = preset.inputs.filter(i => i.kind === 'env');
  return {
    ...emptyForm(),
    id: c.id, name: c.name, transport: c.transport, url: c.url || '', auth: c.auth || 'none',
    command: c.command || '', args: (c.args || []).join('\n'), approval: c.approval || 'always',
    connectTimeout: c.connect_timeout || 60, oauthScope: c.oauth_scope || '',
    env: envInputs.map(i => ({ name: i.name, value: '', stored: false })),
    presetKey: preset.key,
    presetArgs: Object.fromEntries(preset.inputs.filter(i => i.kind === 'arg').map(i => [i.name, ''])),
  };
};

const formFromConnector = (c) => ({
  id: c.id, name: c.name, transport: c.transport, url: c.url || '', auth: c.auth, bearerToken: '',
  oauthClientId: c.oauth_client_id || '', oauthClientSecret: '',
  headers: (c.headers || []).map(h => ({ name: h.name, value: '', stored: h.set })),
  command: c.command || '', args: (c.args || []).join('\n'),
  env: (c.env || []).map(e => ({ name: e.name, value: '', stored: e.set })),
  cwd: c.cwd || '', approval: c.approval, enabled: c.enabled, connectTimeout: c.connect_timeout || 60,
  presetKey: '', presetArgs: {}, oauthScope: c.oauth_scope || '',
  hasBearerToken: c.has_bearer_token, hasOauthClientSecret: c.has_oauth_client_secret,
});

// Name/value rows; a stored row left empty keeps its value (value: null).
const rowsForApi = (rows) => rows
  .filter(r => r.name.trim())
  .map(r => ({ name: r.name.trim(), value: r.value === '' && r.stored ? null : r.value }));

function KeyValueRows({ rows, onChange, namePlaceholder, valuePlaceholder, keepPlaceholder, t }) {
  const update = (index, patch) => onChange(rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
      {rows.map((row, index) => (
        <div key={index} style={{ display: 'flex', gap: '6px' }}>
          <input style={{ ...fieldStyle, flex: 1 }} value={row.name} placeholder={namePlaceholder}
            onChange={e => update(index, { name: e.target.value })} />
          <input style={{ ...fieldStyle, flex: 2 }} type="password" autoComplete="off" value={row.value}
            placeholder={row.stored ? keepPlaceholder : valuePlaceholder}
            onChange={e => update(index, { value: e.target.value })} />
          <button type="button" style={buttonStyle} aria-label={t('common.delete', 'Delete')}
            onClick={() => onChange(rows.filter((_, i) => i !== index))}><Trash2 size={12} /></button>
        </div>
      ))}
      <button type="button" style={{ ...buttonStyle, alignSelf: 'flex-start' }}
        onClick={() => onChange([...rows, { name: '', value: '', stored: false }])}>
        <Plus size={12} /> {t('connectors.addRow')}
      </button>
    </div>
  );
}

function ToolSelection({ connector, onSaved, setError, t }) {
  const all = connector.tools || [];
  const [selected, setSelected] = useState(() => new Set(
    connector.enabled_tools === null || connector.enabled_tools === undefined
      ? all.map(tool => tool.name) : connector.enabled_tools,
  ));
  const [busy, setBusy] = useState(false);
  const toggle = (name) => setSelected(prev => {
    const next = new Set(prev);
    if (next.has(name)) next.delete(name); else next.add(name);
    return next;
  });
  const save = async () => {
    setBusy(true);
    try {
      const everything = all.every(tool => selected.has(tool.name));
      const res = await fetch('/api/connectors/tools', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: connector.id, enabled_tools: everything ? null : [...selected] }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);
      onSaved();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };
  if (!all.length) {
    return <div style={{ fontSize: '12px', opacity: 0.8 }}>{t('connectors.noToolsYet')}</div>;
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
      <div style={{ display: 'flex', gap: '6px', alignItems: 'center', fontSize: '12px' }}>
        <span>{t('connectors.toolsSelected', { count: selected.size, total: all.length })}</span>
        <button type="button" style={buttonStyle} onClick={() => setSelected(new Set(all.map(tool => tool.name)))}>{t('connectors.selectAll')}</button>
        <button type="button" style={buttonStyle} onClick={() => setSelected(new Set())}>{t('connectors.selectNone')}</button>
        <button type="button" style={primaryButtonStyle} disabled={busy} onClick={save}>{t('common.save', 'Save')}</button>
      </div>
      <div style={{ maxHeight: 240, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '4px' }}>
        {all.map(tool => (
          <label key={tool.name} style={{ display: 'flex', gap: '8px', alignItems: 'flex-start', fontSize: '12px' }}>
            <input type="checkbox" checked={selected.has(tool.name)} onChange={() => toggle(tool.name)} />
            <span style={{ minWidth: 0 }}>
              <code>{tool.name}</code>
              {tool.annotations?.readOnlyHint && <span style={{ opacity: 0.7 }}> · {t('connectors.readOnlyHint')}</span>}
              {tool.description && (
                <span style={{ display: 'block', opacity: 0.75, overflowWrap: 'anywhere' }}>
                  {tool.description.split('\n')[0].slice(0, 240)}
                </span>
              )}
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}

/** The MCP connectors: list, connect (with OAuth in the system browser), edit, choose tools. */
export default function ConnectorsModal({ onClose }) {
  const { t } = useTranslation();
  const [connectors, setConnectors] = useState([]);
  const [storeError, setStoreError] = useState('');
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(null); // null | 'new' | connector id
  const [form, setForm] = useState(emptyForm);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [presets, setPresets] = useState([]);

  useEffect(() => {
    fetch('/api/connectors/presets').then(r => r.json()).then(p => setPresets(p.presets || [])).catch(() => { });
  }, []);
  const activePreset = presets.find(p => p.key === form.presetKey);
  const presetText = (preset, field) => t(`connectors.presets.${preset.key}.${field}`, preset[field]);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch('/api/connectors');
      const payload = await res.json();
      setConnectors(payload.connectors || []);
      setStoreError(payload.error || '');
    } catch (err) {
      setError(t('connectors.loadFailed', { error: err.message }));
    }
  }, [t]);

  const busyConnecting = connectors.some(c => ['connecting', 'authorizing'].includes(c.state?.status));
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, busyConnecting ? FAST_REFRESH_MS : SLOW_REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh, busyConnecting]);

  const post = async (url, body) => {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);
    return payload;
  };

  const act = async (url, body) => {
    setError('');
    try {
      await post(url, body);
      await refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  const startEdit = (connector) => {
    setError('');
    setEditing(connector ? connector.id : 'new');
    setForm(connector ? formFromConnector(connector) : emptyForm());
  };

  const buildPayload = () => {
    const payload = {
      name: form.name.trim() || form.id.trim(), transport: form.transport, approval: form.approval, enabled: form.enabled,
      connect_timeout: Number(form.connectTimeout) || 60,
    };
    if (editing === 'new') {
      payload.id = form.id.trim();
      if (form.presetKey) payload.preset = form.presetKey;
    }
    if (form.transport === 'http') {
      Object.assign(payload, { url: form.url.trim(), auth: form.auth, headers: rowsForApi(form.headers), command: '', args: [], env: [], cwd: '' });
      if (form.auth === 'bearer' && form.bearerToken) payload.bearer_token = form.bearerToken;
      if (form.auth === 'oauth') {
        payload.oauth_scope = form.oauthScope.trim();
        payload.oauth_client_id = form.oauthClientId.trim();
        if (form.oauthClientSecret) payload.oauth_client_secret = form.oauthClientSecret;
        if (!form.oauthClientId.trim()) payload.clear_oauth_client_secret = true;
      }
    } else {
      Object.assign(payload, {
        url: '', headers: [], auth: 'none', command: form.command.trim(),
        args: [...form.args.split('\n').map(a => a.trim()).filter(Boolean),
          ...Object.values(form.presetArgs || {}).map(v => v.trim()).filter(Boolean)],
        env: rowsForApi(form.env), cwd: form.cwd.trim(),
      });
    }
    return payload;
  };

  // What the chosen catalog entry still needs from the user.
  const missingPresetInputs = () => {
    if (!activePreset) return [];
    return activePreset.inputs.filter(input => {
      if (input.kind === 'bearer') return !form.bearerToken.trim();
      if (input.kind === 'arg') return !(form.presetArgs[input.name] || '').trim();
      const row = form.env.find(r => r.name === input.name);
      return !row || !row.value.trim();
    }).map(input => (input.kind === 'bearer' ? t('connectors.fieldBearer') : input.name));
  };

  const save = async () => {
    const missing = missingPresetInputs();
    if (missing.length) {
      setError(t('connectors.presetMissing', { names: missing.join(', ') }));
      return;
    }
    setBusy(true); setError('');
    try {
      if (editing === 'new') {
        const created = await post('/api/connectors/create', { connector: buildPayload() });
        // Saving an OAuth connector is asking to use it: go straight to the sign-in.
        if (created.connector?.auth === 'oauth' && created.connector?.enabled) {
          await post('/api/connectors/connect', { id: created.connector.id });
        }
      } else {
        await post('/api/connectors/update', { id: editing, connector: buildPayload() });
      }
      setEditing(null);
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const describeTarget = (c) => (c.transport === 'http' ? c.url : [c.command, ...(c.args || [])].join(' '));
  const statusOf = (c) => (c.enabled ? (c.state?.status || 'disconnected') : 'off');
  const set = (patch) => setForm(prev => ({ ...prev, ...patch }));

  const formView = useMemo(() => editing !== null && (
    <section style={{ border: '1px solid var(--vscode-border)', borderRadius: '8px', padding: '12px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
      <h3 style={{ margin: 0, fontSize: '13px' }}>{editing === 'new' ? t('connectors.addTitle') : t('connectors.editTitle', { name: form.name })}</h3>
      {editing === 'new' && presets.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          <span style={{ fontSize: '12px' }}>{t('connectors.catalogTitle')}</span>
          {PRESET_CATEGORIES.map(category => {
            const items = presets.filter(p => p.category === category);
            if (!items.length) return null;
            return (
              <div key={category} style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', alignItems: 'center' }}>
                <span style={{ fontSize: '11px', opacity: 0.7, minWidth: 90 }}>{t(`connectors.categories.${category}`, category)}</span>
                {items.map(preset => (
                  <button key={preset.key} type="button" title={presetText(preset, 'description')}
                    style={form.presetKey === preset.key ? primaryButtonStyle : buttonStyle}
                    onClick={() => { setError(''); setForm(formFromPreset(preset)); }}>
                    {presetText(preset, 'name')}
                  </button>
                ))}
              </div>
            );
          })}
          <span style={{ fontSize: '11px', opacity: 0.7 }}>{t('connectors.catalogHint')}</span>
        </div>
      )}
      {activePreset && (
        <div style={{ padding: '8px', borderRadius: '6px', background: 'rgba(74,163,255,0.08)', fontSize: '12px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
          <strong>{presetText(activePreset, 'name')}</strong>
          <span>{presetText(activePreset, 'description')}</span>
          {activePreset.setup && <span style={{ opacity: 0.85 }}>{presetText(activePreset, 'setup')}</span>}
          {!activePreset.requirement_found && (
            <span style={{ color: 'var(--vscode-fg-warning, #d29922)', display: 'inline-flex', gap: '4px', alignItems: 'center' }}>
              <TriangleAlert size={12} /> {t('connectors.requirementMissing', { command: activePreset.requires })}
            </span>
          )}
          {activePreset.docs_url && (
            <a href={activePreset.docs_url} onClick={handleExternalClick(activePreset.docs_url)}
              style={{ color: 'var(--vscode-textLink-foreground, #4aa3ff)', display: 'inline-flex', gap: '4px', alignItems: 'center' }}>
              <ExternalLink size={12} /> {t('connectors.docsLink')}
            </a>
          )}
        </div>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '10px' }}>
        <label style={labelStyle}>{t('connectors.fieldId')}
          <input style={fieldStyle} value={form.id} disabled={editing !== 'new'} placeholder="x"
            onChange={e => set({ id: e.target.value.toLowerCase() })} />
          <span style={{ opacity: 0.7 }}>{t('connectors.fieldIdHint')}</span>
        </label>
        <label style={labelStyle}>{t('connectors.fieldName')}
          <input style={fieldStyle} value={form.name} placeholder="X" onChange={e => set({ name: e.target.value })} />
        </label>
        <label style={labelStyle}>{t('connectors.fieldTransport')}
          <select style={fieldStyle} value={form.transport} onChange={e => set({ transport: e.target.value })}>
            <option value="http">{t('connectors.transportHttp')}</option>
            <option value="stdio">{t('connectors.transportStdio')}</option>
          </select>
        </label>
        <label style={labelStyle}>{t('connectors.fieldApproval')}
          <select style={fieldStyle} value={form.approval} onChange={e => set({ approval: e.target.value })}>
            <option value="always">{t('connectors.approvalAlways')}</option>
            <option value="mode">{t('connectors.approvalMode')}</option>
          </select>
        </label>
      </div>
      <p style={{ margin: 0, fontSize: '11px', opacity: 0.75 }}>
        {form.approval === 'always' ? t('connectors.approvalAlwaysHint') : t('connectors.approvalModeHint')}
      </p>

      {form.transport === 'http' ? (
        <>
          <label style={labelStyle}>{t('connectors.fieldUrl')}
            <input style={fieldStyle} value={form.url} placeholder="https://example.com/mcp" onChange={e => set({ url: e.target.value })} />
          </label>
          <label style={labelStyle}>{t('connectors.fieldAuth')}
            <select style={fieldStyle} value={form.auth} onChange={e => set({ auth: e.target.value })}>
              <option value="none">{t('connectors.authNone')}</option>
              <option value="oauth">{t('connectors.authOauth')}</option>
              <option value="bearer">{t('connectors.authBearer')}</option>
            </select>
          </label>
          {form.auth === 'oauth' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <p style={{ margin: 0, fontSize: '11px', opacity: 0.75 }}>{t('connectors.oauthHint')}</p>
              <label style={labelStyle}>{t('connectors.fieldScope')}
                <input style={fieldStyle} value={form.oauthScope} placeholder={t('connectors.scopePlaceholder')}
                  onChange={e => set({ oauthScope: e.target.value })} />
              </label>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '10px' }}>
                <label style={labelStyle}>{t('connectors.fieldClientId')}
                  <input style={fieldStyle} value={form.oauthClientId} placeholder={t('connectors.optional')} onChange={e => set({ oauthClientId: e.target.value })} />
                </label>
                <label style={labelStyle}>{t('connectors.fieldClientSecret')}
                  <input style={fieldStyle} type="password" autoComplete="off" value={form.oauthClientSecret}
                    placeholder={form.hasOauthClientSecret ? t('connectors.keepStored') : t('connectors.optional')}
                    onChange={e => set({ oauthClientSecret: e.target.value })} />
                </label>
              </div>
            </div>
          )}
          {form.auth === 'bearer' && (
            <label style={labelStyle}>{t('connectors.fieldBearer')}
              <input style={fieldStyle} type="password" autoComplete="off" value={form.bearerToken}
                placeholder={form.hasBearerToken ? t('connectors.keepStored') : ''} onChange={e => set({ bearerToken: e.target.value })} />
            </label>
          )}
          <div style={labelStyle}>{t('connectors.fieldHeaders')}
            <KeyValueRows rows={form.headers} onChange={rows => set({ headers: rows })} t={t}
              namePlaceholder="X-Api-Key" valuePlaceholder={t('connectors.value')} keepPlaceholder={t('connectors.keepStored')} />
          </div>
        </>
      ) : (
        <>
          <p style={{ margin: 0, fontSize: '11px', color: 'var(--vscode-fg-warning, #d29922)' }}>{t('connectors.stdioWarning')}</p>
          <label style={labelStyle}>{t('connectors.fieldCommand')}
            <input style={fieldStyle} value={form.command} placeholder="npx" onChange={e => set({ command: e.target.value })} />
          </label>
          <label style={labelStyle}>{t('connectors.fieldArgs')}
            <textarea style={{ ...fieldStyle, minHeight: 64, fontFamily: 'monospace' }} value={form.args}
              placeholder={'-y\n@modelcontextprotocol/server-filesystem\n~/notes'} onChange={e => set({ args: e.target.value })} />
          </label>
          <div style={labelStyle}>{t('connectors.fieldEnv')}
            <KeyValueRows rows={form.env} onChange={rows => set({ env: rows })} t={t}
              namePlaceholder="API_KEY" valuePlaceholder={t('connectors.value')} keepPlaceholder={t('connectors.keepStored')} />
          </div>
          {Object.keys(form.presetArgs || {}).map(name => (
            <label key={name} style={labelStyle}>{t(`connectors.presetArg.${name}`, name)}
              <input style={fieldStyle} value={form.presetArgs[name]} placeholder="~/notes"
                onChange={e => set({ presetArgs: { ...form.presetArgs, [name]: e.target.value } })} />
            </label>
          ))}
          <label style={labelStyle}>{t('connectors.fieldConnectTimeout')}
            <input style={fieldStyle} type="number" min={5} max={900} value={form.connectTimeout}
              onChange={e => set({ connectTimeout: e.target.value })} />
          </label>
          <label style={labelStyle}>{t('connectors.fieldCwd')}
            <input style={fieldStyle} value={form.cwd} placeholder={t('connectors.optional')} onChange={e => set({ cwd: e.target.value })} />
          </label>
        </>
      )}
      <label style={{ display: 'inline-flex', gap: '6px', alignItems: 'center', fontSize: '12px' }}>
        <input type="checkbox" checked={form.enabled} onChange={e => set({ enabled: e.target.checked })} /> {t('connectors.fieldEnabled')}
      </label>
      <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
        <button type="button" style={buttonStyle} onClick={() => setEditing(null)}>{t('common.cancel', 'Cancel')}</button>
        <button type="button" style={primaryButtonStyle} disabled={busy} onClick={save}>{t('common.save', 'Save')}</button>
      </div>
    </section>
  ), [editing, form, busy, t, presets, activePreset]);

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 9999, display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'rgba(0,0,0,0.7)',
    }} onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="vscode-modal" role="dialog" aria-label={t('connectors.title')} style={{
        borderRadius: '12px', width: 'min(900px, 94vw)', maxHeight: '88vh', padding: '20px',
        boxShadow: '0 24px 64px rgba(0,0,0,0.6)', display: 'flex', flexDirection: 'column', gap: '12px', overflow: 'hidden',
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2 style={{ margin: 0, fontSize: '18px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Cable size={18} aria-hidden="true" /> {t('connectors.title')}
          </h2>
          <button onClick={onClose} aria-label={t('common.close', 'Close')} style={{ background: 'transparent', border: 'none', color: 'var(--vscode-text-fg)', cursor: 'pointer', fontSize: '18px' }}>✕</button>
        </div>
        <p style={{ margin: 0, fontSize: '12px', opacity: 0.8 }}>{t('connectors.intro')}</p>

        {storeError && <div style={{ background: 'rgba(255,0,0,0.1)', color: 'var(--vscode-fg-danger)', padding: '8px', borderRadius: '6px', fontSize: '12px', whiteSpace: 'pre-wrap' }}>{storeError}</div>}
        {error && <div style={{ background: 'rgba(255,0,0,0.1)', color: 'var(--vscode-fg-danger)', padding: '8px', borderRadius: '6px', fontSize: '12px', whiteSpace: 'pre-wrap' }}>{error}</div>}

        <div style={{ overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '10px', minHeight: 0 }}>
          {formView}
          {editing === null && (
            <button type="button" style={{ ...buttonStyle, alignSelf: 'flex-start' }} onClick={() => startEdit(null)}>
              <Plus size={12} /> {t('connectors.add')}
            </button>
          )}
          {connectors.length === 0 && editing === null && !storeError && (
            <div style={{ fontSize: '12px', opacity: 0.75 }}>{t('connectors.empty')}</div>
          )}
          {connectors.map(c => {
            const status = statusOf(c);
            const state = c.state || {};
            return (
              <div key={c.id} style={{ border: '1px solid var(--vscode-border)', borderRadius: '8px', padding: '10px', display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '12px' }}>
                <div style={{ display: 'flex', gap: '10px', alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap' }}>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <span style={{ width: 8, height: 8, borderRadius: '50%', background: STATUS_COLORS[status] || 'inherit', flex: 'none' }} />
                      <strong>{c.name}</strong>
                      <span style={{ opacity: 0.7 }}>· {t(`connectors.status.${status}`, status)}</span>
                    </div>
                    <div style={{ opacity: 0.75, overflowWrap: 'anywhere' }}><code>{c.id}</code> · {describeTarget(c)}</div>
                    <div style={{ opacity: 0.75 }}>
                      {t(`connectors.authLabel.${c.auth}`, c.auth)} · {c.approval === 'always' ? t('connectors.approvalAlways') : t('connectors.approvalMode')}
                      {' · '}{t('connectors.toolCount', { count: c.active_tool_count, total: (c.tools || []).length })}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                    {c.enabled && !['connecting', 'authorizing'].includes(status) && (
                      <button type="button" style={primaryButtonStyle} onClick={() => act('/api/connectors/connect', { id: c.id })}>
                        <RefreshCw size={12} /> {status === 'connected' ? t('connectors.reconnect') : t('connectors.connect')}
                      </button>
                    )}
                    {status === 'connected' && (
                      <button type="button" style={buttonStyle} onClick={() => act('/api/connectors/disconnect', { id: c.id })}>
                        <Unplug size={12} /> {t('connectors.disconnect')}
                      </button>
                    )}
                    {state.has_credentials && (
                      <button type="button" style={buttonStyle} title={t('connectors.signOutHint')}
                        onClick={() => act('/api/connectors/disconnect', { id: c.id, forget_credentials: true })}>
                        <LogOut size={12} /> {t('connectors.signOut')}
                      </button>
                    )}
                    <button type="button" style={buttonStyle} onClick={() => act('/api/connectors/update', { id: c.id, connector: { enabled: !c.enabled } })}>
                      {c.enabled ? t('connectors.turnOff') : t('connectors.turnOn')}
                    </button>
                    <button type="button" style={buttonStyle} aria-label={t('common.edit', 'Edit')} onClick={() => startEdit(c)}><Pencil size={12} /></button>
                    {confirmDelete === c.id ? (
                      <>
                        <button type="button" style={{ ...buttonStyle, color: 'var(--vscode-fg-danger)' }} onClick={() => { setConfirmDelete(null); act('/api/connectors/delete', { id: c.id }); }}>{t('connectors.confirmDelete')}</button>
                        <button type="button" style={buttonStyle} onClick={() => setConfirmDelete(null)}>{t('common.cancel', 'Cancel')}</button>
                      </>
                    ) : (
                      <button type="button" style={buttonStyle} aria-label={t('common.delete', 'Delete')} onClick={() => setConfirmDelete(c.id)}><Trash2 size={12} /></button>
                    )}
                  </div>
                </div>

                {status === 'authorizing' && (
                  <div style={{ padding: '8px', borderRadius: '6px', background: 'rgba(74,163,255,0.08)', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    <span>{state.browser_opened ? t('connectors.authorizingBrowser') : t('connectors.authorizingNoBrowser')}</span>
                    {state.authorization_url && (
                      <a href={state.authorization_url} onClick={handleExternalClick(state.authorization_url)}
                        style={{ overflowWrap: 'anywhere', color: 'var(--vscode-textLink-foreground, #4aa3ff)' }}>{t('connectors.openAuthorization')}</a>
                    )}
                    <button type="button" style={{ ...buttonStyle, alignSelf: 'flex-start' }} onClick={() => act('/api/connectors/cancel', { id: c.id })}>{t('common.cancel', 'Cancel')}</button>
                  </div>
                )}
                {status === 'error' && state.error && (
                  <div style={{ color: 'var(--vscode-fg-danger)', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{state.error}</div>
                )}

                <button type="button" style={{ ...buttonStyle, border: 'none', padding: 0, alignSelf: 'flex-start' }}
                  onClick={() => setExpanded(expanded === c.id ? null : c.id)}>
                  {expanded === c.id ? <ChevronDown size={12} /> : <ChevronRight size={12} />} {t('connectors.chooseTools')}
                </button>
                {expanded === c.id && <ToolSelection key={`${c.id}-${c.tools_refreshed_at}`} connector={c} onSaved={refresh} setError={setError} t={t} />}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
