import React, { useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowLeftRight, Download, Upload } from 'lucide-react';

// Moving monitors and connectors to another machine (gilzabots/transfer.py).
// Export downloads a bundle without secrets. Import reads a bundle, checks it
// with a dry run, lets the user supply the secrets the bundle left out and map
// projects that do not exist here, then imports: connectors arrive disabled,
// monitors paused.

const buttonStyle = {
  padding: '4px 8px', borderRadius: '6px', cursor: 'pointer', background: 'transparent',
  color: 'var(--vscode-text-fg)', border: '1px solid var(--vscode-border)', display: 'inline-flex',
  alignItems: 'center', gap: '4px', fontSize: '12px',
};
const primaryButtonStyle = {
  ...buttonStyle, background: 'var(--vscode-button-background, #5847d6)', color: 'var(--vscode-button-foreground, #fff)',
  border: '1px solid transparent',
};
const fieldStyle = { padding: '4px 6px', borderRadius: '6px', fontSize: '12px', minWidth: 0 };
const errorStyle = { background: 'rgba(255,0,0,0.1)', color: 'var(--vscode-fg-danger)', padding: '8px', borderRadius: '6px', fontSize: '12px', whiteSpace: 'pre-wrap' };

const STATUS_COLORS = {
  ready: 'var(--vscode-fg-success, #3fb950)',
  imported: 'var(--vscode-fg-success, #3fb950)',
  exists: 'var(--vscode-descriptionForeground, #999)',
};
const statusColor = (status) => STATUS_COLORS[status] || 'var(--vscode-fg-danger, #f85149)';

async function postJson(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

export default function TransferModal({ onClose, onImported }) {
  const { t } = useTranslation();
  const fileRef = useRef(null);
  const [includeConnectors, setIncludeConnectors] = useState(true);
  const [includeMonitors, setIncludeMonitors] = useState(true);
  const [notes, setNotes] = useState([]);
  const [exported, setExported] = useState('');
  const [bundle, setBundle] = useState(null);
  const [fileName, setFileName] = useState('');
  const [report, setReport] = useState(null);
  const [secrets, setSecrets] = useState({});
  const [projectMap, setProjectMap] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const doExport = async () => {
    setBusy(true); setError(''); setExported('');
    try {
      const data = await postJson('/api/transfer/export', { include_connectors: includeConnectors, include_monitors: includeMonitors });
      const text = JSON.stringify(data.bundle, null, 2);
      const blob = new Blob([text + '\n'], { type: 'application/json;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `gilzabots-transfer-${new Date().toISOString().split('T')[0]}.json`;
      a.click();
      URL.revokeObjectURL(url);
      setNotes(data.notes || []);
      setExported(t('transfer.exported', {
        connectors: data.bundle.connectors.length, monitors: data.bundle.monitors.length,
      }));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const check = async (nextBundle, dryRun) => {
    setBusy(true); setError('');
    try {
      const data = await postJson('/api/transfer/import', {
        bundle: nextBundle, dry_run: dryRun, secrets, project_map: projectMap,
      });
      setReport(data);
      if (!dryRun && data.imported && onImported) onImported();
    } catch (e) {
      setError(e.message);
      setReport(null);
    } finally {
      setBusy(false);
    }
  };

  const pickFile = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setReport(null); setSecrets({}); setProjectMap({}); setError('');
    try {
      const parsed = JSON.parse(await file.text());
      setBundle(parsed);
      setFileName(file.name);
      await check(parsed, true);
    } catch (e) {
      setBundle(null);
      setError(t('transfer.unreadable', { error: e.message }));
    }
  };

  const setSecret = (key, name, value) => setSecrets(prev => ({ ...prev, [key]: { ...(prev[key] || {}), [name]: value } }));

  const missingProjects = useMemo(() => {
    if (!report) return [];
    const names = new Set();
    (bundle?.monitors || []).forEach(m => { if (m.project) names.add(m.project); });
    return [...names].filter(name => !(report.projects || []).includes(name));
  }, [report, bundle]);

  const done = report && !report.dry_run;
  const readyCount = report?.dry_run ? report.imported : 0;

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 10000, display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'rgba(0,0,0,0.7)',
    }} onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="vscode-modal" role="dialog" aria-label={t('transfer.title')} style={{
        borderRadius: '12px', width: 'min(760px, 94vw)', maxHeight: '88vh', padding: '20px',
        boxShadow: '0 24px 64px rgba(0,0,0,0.6)', display: 'flex', flexDirection: 'column', gap: '12px', overflow: 'hidden',
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2 style={{ margin: 0, fontSize: '18px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <ArrowLeftRight size={18} aria-hidden="true" /> {t('transfer.title')}
          </h2>
          <button onClick={onClose} aria-label={t('common.close', 'Close')} style={{ background: 'transparent', border: 'none', color: 'var(--vscode-text-fg)', cursor: 'pointer', fontSize: '18px' }}>✕</button>
        </div>
        <p style={{ margin: 0, fontSize: '12px', opacity: 0.8 }}>{t('transfer.intro')}</p>
        {error && <div style={errorStyle}>{error}</div>}

        <div style={{ overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '14px', minHeight: 0 }}>
          <section style={{ border: '1px solid var(--vscode-border)', borderRadius: '8px', padding: '10px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <h3 style={{ margin: 0, fontSize: '13px' }}>{t('transfer.exportTitle')}</h3>
            <div style={{ display: 'flex', gap: '14px', fontSize: '12px', flexWrap: 'wrap', alignItems: 'center' }}>
              <label style={{ display: 'inline-flex', gap: '6px', alignItems: 'center' }}>
                <input type="checkbox" checked={includeConnectors} onChange={e => setIncludeConnectors(e.target.checked)} /> {t('transfer.connectors')}
              </label>
              <label style={{ display: 'inline-flex', gap: '6px', alignItems: 'center' }}>
                <input type="checkbox" checked={includeMonitors} onChange={e => setIncludeMonitors(e.target.checked)} /> {t('transfer.monitors')}
              </label>
              <button type="button" style={primaryButtonStyle} disabled={busy || (!includeConnectors && !includeMonitors)} onClick={doExport}>
                <Download size={12} /> {t('transfer.export')}
              </button>
            </div>
            <div style={{ fontSize: '11px', opacity: 0.75 }}>{t('transfer.exportHint')}</div>
            {exported && <div style={{ fontSize: '12px' }}>{exported}</div>}
            {notes.map(note => <div key={note} style={{ fontSize: '11px', opacity: 0.8 }}>{note}</div>)}
          </section>

          <section style={{ border: '1px solid var(--vscode-border)', borderRadius: '8px', padding: '10px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <h3 style={{ margin: 0, fontSize: '13px' }}>{t('transfer.importTitle')}</h3>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center', fontSize: '12px', flexWrap: 'wrap' }}>
              <input ref={fileRef} type="file" accept=".json,application/json" style={{ display: 'none' }} onChange={pickFile} />
              <button type="button" style={buttonStyle} disabled={busy} onClick={() => fileRef.current?.click()}>
                <Upload size={12} /> {t('transfer.chooseFile')}
              </button>
              {fileName && <span style={{ opacity: 0.8 }}>{fileName}</span>}
            </div>
            <div style={{ fontSize: '11px', opacity: 0.75 }}>{t('transfer.importHint')}</div>

            {report && missingProjects.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', fontSize: '12px' }}>
                <strong>{t('transfer.mapProjects')}</strong>
                {missingProjects.map(name => (
                  <label key={name} style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                    <span style={{ minWidth: '140px' }}>{name} →</span>
                    <select className="vscode-input" style={fieldStyle} value={projectMap[name] || ''}
                      onChange={e => setProjectMap(prev => ({ ...prev, [name]: e.target.value }))}>
                      <option value="">{t('transfer.notMapped')}</option>
                      {(report.projects || []).map(p => <option key={p} value={p}>{p}</option>)}
                    </select>
                  </label>
                ))}
              </div>
            )}

            {report && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {report.results.length === 0 && <div style={{ fontSize: '12px', opacity: 0.75 }}>{t('transfer.emptyBundle')}</div>}
                {report.results.map(r => (
                  <div key={`${r.kind}:${r.key}`} style={{ border: '1px solid var(--vscode-border)', borderRadius: '6px', padding: '6px 8px', fontSize: '12px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
                      <span style={{ opacity: 0.7 }}>{t(`transfer.kind.${r.kind}`)}</span>
                      <strong>{r.name}</strong>
                      {r.project && <span style={{ opacity: 0.7 }}>[{r.project}]</span>}
                      <span style={{ marginLeft: 'auto', color: statusColor(r.status) }}>{t(`transfer.status.${r.status}`, r.status)}</span>
                    </div>
                    {r.message && <div style={{ opacity: 0.85, whiteSpace: 'pre-wrap' }}>{r.message}</div>}
                    {(r.warnings || []).map(w => <div key={w} style={{ color: 'var(--vscode-fg-warning, #d29922)' }}>{w}</div>)}
                    {!done && (r.missing || []).map(name => (
                      <label key={name} style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                        <span style={{ minWidth: '160px', fontFamily: 'monospace' }}>{name}</span>
                        <input className="vscode-input" type="password" autoComplete="off" style={{ ...fieldStyle, flex: 1 }}
                          value={secrets[r.key]?.[name] || ''} onChange={e => setSecret(r.key, name, e.target.value)} />
                      </label>
                    ))}
                  </div>
                ))}
                {done ? (
                  <div style={{ fontSize: '12px' }}>{t('transfer.done', { imported: report.imported, existing: report.existing, failed: report.failed })}</div>
                ) : (
                  <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                    <button type="button" style={buttonStyle} disabled={busy} onClick={() => check(bundle, true)}>{t('transfer.recheck')}</button>
                    <button type="button" style={primaryButtonStyle} disabled={busy || readyCount === 0} onClick={() => check(bundle, false)}>
                      {t('transfer.importButton', { count: readyCount })}
                    </button>
                  </div>
                )}
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
