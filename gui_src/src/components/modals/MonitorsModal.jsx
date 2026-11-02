import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Radar, Pause, Play, FlaskConical, Trash2, Plus, ChevronDown, ChevronRight, MessageSquare } from 'lucide-react';

// Conditions that apply to each check type; mirrors opalatex/monitors/spec.py
// (_CONDITIONS_FOR_CHECK). The server validates again and its message is shown.
const CONDITIONS_FOR_CHECK = {
  http: ['changed', 'delta', 'gt', 'ge', 'lt', 'le', 'equals', 'not_equals', 'contains', 'regex'],
  command: ['exit_code', 'changed', 'contains', 'regex', 'equals', 'gt', 'lt', 'delta'],
  file: ['changed', 'exists', 'missing'],
  timer: ['always'],
  llm: ['llm'],
};
const VALUE_CONDITIONS = ['equals', 'not_equals', 'contains', 'gt', 'ge', 'lt', 'le', 'exit_code'];
const REFRESH_MS = 5000;

// Long free text (an LLM check's question, a description) clamped to a few
// lines; "show more" opens it in a bounded box that scrolls, so one monitor
// cannot stretch its table row over the whole panel.
function ClampedText({ text, lines = 3, expandedMaxHeight = 160 }) {
  const { t } = useTranslation();
  const ref = useRef(null);
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || expanded) return undefined;
    // An element with overflow:hidden keeps the scroll offset it had while
    // expanded; without this the clamped view starts mid-text after a collapse.
    el.scrollTop = 0;
    const measure = () => setOverflows(el.scrollHeight > el.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text, expanded, lines]);

  if (!text) return null;
  const clamped = { display: '-webkit-box', WebkitLineClamp: lines, WebkitBoxOrient: 'vertical', overflow: 'hidden' };
  const open = { maxHeight: expandedMaxHeight, overflowY: 'auto', paddingRight: 4 };
  return (
    <div>
      <div ref={ref} title={expanded ? undefined : text}
        style={{ overflowWrap: 'anywhere', whiteSpace: 'pre-wrap', ...(expanded ? open : clamped) }}>
        {text}
      </div>
      {(overflows || expanded) && (
        <button type="button" onClick={() => setExpanded(v => !v)}
          style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontSize: '11px', color: 'var(--vscode-textLink-foreground, var(--vscode-accent, #4daafc))' }}>
          {expanded ? t('monitors.showLess') : t('monitors.showMore')}
        </button>
      )}
    </div>
  );
}

const formatTime = (epoch) => (epoch ? new Date(epoch * 1000).toLocaleString() : '—');

const formatInterval = (seconds, t) => {
  if (seconds % 86400 === 0) return t('monitors.everyDays', { count: seconds / 86400 });
  if (seconds % 3600 === 0) return t('monitors.everyHours', { count: seconds / 3600 });
  if (seconds % 60 === 0) return t('monitors.everyMinutes', { count: seconds / 60 });
  return t('monitors.everySeconds', { count: seconds });
};

const shortValue = (value) => {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') {
    if ('output' in value || 'exit_code' in value) return String(value.output || `exit ${value.exit_code}`);
    if ('summary' in value) return String(value.summary || value.value || '');
    if ('exists' in value) return value.exists ? 'exists' : 'missing';
    return JSON.stringify(value);
  }
  return String(value);
};

const statusColor = (status) => ({
  active: 'var(--vscode-fg-success, #3fb950)',
  paused: 'var(--vscode-descriptionForeground, #999)',
  completed: 'var(--vscode-textLink-foreground, #4aa3ff)',
  expired: 'var(--vscode-descriptionForeground, #999)',
  cancelled: 'var(--vscode-descriptionForeground, #999)',
  error: 'var(--vscode-fg-danger, #f85149)',
}[status] || 'inherit');

const emptyForm = () => ({
  name: '', checkType: 'http', url: '', jsonPath: '', regex: '', command: '', path: '', watch: 'mtime',
  question: '', condition: 'changed', value: '', threshold: '', direction: 'any', intervalMinutes: '5',
  firstMinutes: '', message: '', once: false, ttlHours: '', wakeMode: 'none', resumePrompt: '',
});

const fieldStyle = { padding: '6px 8px', borderRadius: '6px', width: '100%', boxSizing: 'border-box' };
const labelStyle = { display: 'flex', flexDirection: 'column', gap: '4px', fontSize: '12px', minWidth: 0 };
const buttonStyle = {
  padding: '4px 8px', borderRadius: '6px', cursor: 'pointer', background: 'transparent',
  color: 'var(--vscode-text-fg)', border: '1px solid var(--vscode-border)', display: 'inline-flex',
  alignItems: 'center', gap: '4px', fontSize: '12px',
};

/**
 * Monitors panel: the monitors of every project, the daemon and its login
 * service, and a form to add one.
 */
export default function MonitorsModal({ onClose, activeProject, activeChatId, onOpenChat, onEventsSeen }) {
  const { t } = useTranslation();
  const [data, setData] = useState({ monitors: [], events: [], daemon: null });
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [expanded, setExpanded] = useState(null);
  const [events, setEvents] = useState({});
  const [testResults, setTestResults] = useState({});
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(emptyForm);
  const [busy, setBusy] = useState(false);
  const [showFinished, setShowFinished] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch('/api/monitors');
      if (res.ok) setData(await res.json());
    } catch (err) {
      setError(t('monitors.loadFailed', { error: err.message }));
    }
  }, [t]);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  // Opening the panel is seeing its events: the activity-bar count clears.
  // Events arriving while it stays open are listed here as they come.
  useEffect(() => {
    fetch('/api/monitors/events/seen', { method: 'POST' })
      .then(() => onEventsSeen?.())
      .catch(() => { });
  }, [(data.events || []).length ? data.events[0].id : 0]);

  const post = async (url, body) => {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);
    return payload;
  };

  const act = async (id, action) => {
    setError('');
    try {
      await post('/api/monitors/action', { id, action });
      setConfirmDelete(null);
      refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  const test = async (id) => {
    setTestResults(prev => ({ ...prev, [id]: { pending: true } }));
    try {
      const result = await post('/api/monitors/test', { id });
      setTestResults(prev => ({ ...prev, [id]: result }));
    } catch (err) {
      setTestResults(prev => ({ ...prev, [id]: { ok: false, error: err.message } }));
    }
  };

  const toggleHistory = async (id) => {
    if (expanded === id) { setExpanded(null); return; }
    setExpanded(id);
    try {
      const res = await fetch(`/api/monitors/events?id=${encodeURIComponent(id)}`);
      if (res.ok) {
        const payload = await res.json();
        setEvents(prev => ({ ...prev, [id]: payload.events || [] }));
      }
    } catch (err) { /* the list still shows */ }
  };

  const setAutostart = async (enabled) => {
    setError('');
    try {
      await post('/api/monitors/autostart', { enabled });
      refresh();
    } catch (err) {
      setError(t('monitors.autostartFailed', { error: err.message }));
    }
  };

  const clearEvents = async () => {
    setError('');
    try {
      await post('/api/monitors/events/clear', {});
      onEventsSeen?.();
      refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  const startDaemon = async () => {
    setError('');
    try {
      const result = await post('/api/monitors/daemon/start', {});
      if (result.error) setError(result.error);
      refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  const notifyTest = async () => {
    setError(''); setNotice('');
    try {
      const result = await post('/api/monitors/notify-test', {
        title: 'OpalaTex', body: t('monitors.testNotificationBody'),
      });
      setNotice(t('monitors.testNotificationSent', { backend: result.backend }));
    } catch (err) {
      setError(t('monitors.testNotificationFailed', { error: err.message }));
    }
  };

  const update = (key) => (event) => {
    const value = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
    setForm(prev => {
      const next = { ...prev, [key]: value };
      if (key === 'checkType') next.condition = CONDITIONS_FOR_CHECK[value][0];
      // Waking the current chat is usually a task wait (once); a watch and a
      // new chat each time keep going. Only a default: the box stays editable.
      if (key === 'wakeMode') next.once = value === 'origin';
      return next;
    });
  };

  const buildSpec = () => {
    const check = { type: form.checkType };
    if (form.checkType === 'http') Object.assign(check, { url: form.url.trim(), json_path: form.jsonPath.trim(), regex: form.regex.trim() });
    if (form.checkType === 'command') check.command = form.command;
    if (form.checkType === 'file') Object.assign(check, { path: form.path.trim(), watch: form.watch });
    if (form.checkType === 'llm') check.question = form.question;
    const condition = { type: form.condition };
    if (form.condition === 'regex') condition.pattern = form.value;
    else if (form.condition === 'delta') Object.assign(condition, { threshold: form.threshold, direction: form.direction });
    else if (VALUE_CONDITIONS.includes(form.condition)) condition.value = form.value;
    // 'origin' wakes the chat open now; 'new' opens a chat for every wake-up.
    const wakes = form.wakeMode === 'new' || (form.wakeMode === 'origin' && activeChatId);
    const minutes = (text) => (text === '' ? null : Math.round(parseFloat(text) * 60));
    return {
      name: form.name.trim(),
      check,
      condition,
      action: wakes
        ? { type: 'notify_and_resume', message: form.message, prompt: form.resumePrompt, chat: form.wakeMode }
        : { type: 'notify', message: form.message },
      interval_seconds: minutes(form.intervalMinutes),
      first_check_seconds: minutes(form.firstMinutes),
      ttl_seconds: form.ttlHours === '' ? null : Math.round(parseFloat(form.ttlHours) * 3600),
      // The user's choice; the form preselects it per wake mode (update below).
      repeat: !form.once,
      project: activeProject?.name || '',
      project_path: activeProject?.project_path || '',
      chat_id: form.wakeMode === 'new' ? '' : (activeChatId || ''),
    };
  };

  const create = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await post('/api/monitors/create', { spec: buildSpec() });
      setNotice(result.value !== null && result.value !== undefined
        ? t('monitors.createdWithValue', { name: result.monitor.name, value: result.value })
        : t('monitors.created', { name: result.monitor.name }));
      setForm(emptyForm());
      setShowForm(false);
      refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const describeTrigger = (monitor) => {
    const check = monitor.check || {};
    const condition = monitor.condition || {};
    const target = {
      http: check.url,
      command: check.command,
      file: check.path,
      timer: t('monitors.checkTypes.timer'),
      llm: check.question,
    }[check.type] || check.type;
    let cond = t(`monitors.conditions.${condition.type}`, condition.type);
    if (condition.value !== undefined) cond += ` ${condition.value}`;
    if (condition.pattern) cond += ` /${condition.pattern}/`;
    if (condition.threshold !== undefined) cond += ` ±${condition.threshold}`;
    const action = monitor.action || {};
    const wakes = ['resume_chat', 'notify_and_resume'].includes(action.type)
      ? (action.chat === 'new' ? t('monitors.wakesNewChat') : t('monitors.wakesSameChat'))
      : '';
    return { target, cond, every: formatInterval(monitor.interval_seconds, t), wakes, once: !monitor.repeat };
  };

  const visible = useMemo(() => (data.monitors || [])
    .filter(m => showFinished || m.status === 'active' || m.status === 'paused')
    .slice().reverse(), [data.monitors, showFinished]);
  const daemon = data.daemon;
  const autostart = daemon?.autostart;

  return (
    // No backdrop-filter: the window often composites in software
    // (--disable-gpu), where blurring the whole app behind the panel cost
    // ~100 ms per keystroke in the form (measured: 134 ms vs 33 ms per key).
    <div style={{
      position: 'fixed', inset: 0, zIndex: 9999, display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'rgba(0,0,0,0.7)',
    }} onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="vscode-modal" role="dialog" aria-label={t('monitors.title')} style={{
        borderRadius: '12px', width: 'min(960px, 94vw)', maxHeight: '88vh', padding: '20px',
        boxShadow: '0 24px 64px rgba(0,0,0,0.6)', display: 'flex', flexDirection: 'column', gap: '12px',
        overflow: 'hidden',
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2 style={{ margin: 0, fontSize: '18px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Radar size={18} aria-hidden="true" /> {t('monitors.title')}
          </h2>
          <button onClick={onClose} aria-label={t('common.close', 'Close')} style={{ background: 'transparent', border: 'none', color: 'var(--vscode-text-fg)', cursor: 'pointer', fontSize: '18px' }}>✕</button>
        </div>
        <p style={{ margin: 0, fontSize: '12px', opacity: 0.8 }}>{t('monitors.intro')}</p>

        {daemon && (
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '12px', fontSize: '12px', padding: '8px 10px', border: '1px solid var(--vscode-border)', borderRadius: '8px' }}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: daemon.running ? 'var(--vscode-fg-success, #3fb950)' : 'var(--vscode-descriptionForeground, #999)' }} />
              {daemon.running ? t('monitors.daemonRunning', { pid: daemon.pid }) : t('monitors.daemonStopped')}
            </span>
            {!daemon.running && <button style={buttonStyle} onClick={startDaemon}>{t('monitors.startDaemon')}</button>}
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }} title={autostart?.available ? '' : autostart?.reason}>
              <input type="checkbox" checked={Boolean(autostart?.installed)} disabled={!autostart?.available && !autostart?.installed}
                onChange={(e) => setAutostart(e.target.checked)} />
              {t('monitors.startAtLogin')}{autostart?.backend ? ` (${autostart.backend})` : ''}
            </label>
            <button style={buttonStyle} onClick={notifyTest}>{t('monitors.testNotification')}</button>
          </div>
        )}

        {error && <div style={{ background: 'rgba(255,0,0,0.1)', color: 'var(--vscode-fg-danger)', padding: '8px', borderRadius: '6px', fontSize: '12px', whiteSpace: 'pre-wrap' }}>{error}</div>}
        {notice && <div style={{ background: 'rgba(0,255,0,0.08)', color: 'var(--vscode-fg-success)', padding: '8px', borderRadius: '6px', fontSize: '12px' }}>{notice}</div>}

        <div style={{ overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '12px', minHeight: 0 }}>
          {(data.events || []).length > 0 && (
            <section>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                <h3 style={{ fontSize: '13px', margin: 0 }}>{t('monitors.eventsTitle')}</h3>
                <button style={buttonStyle} onClick={clearEvents} title={t('monitors.clearEventsHint')}>
                  <Trash2 size={12} /> {t('monitors.clearEvents')}
                </button>
              </div>
              <div style={{ maxHeight: 220, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {data.events.map(event => {
                  const failed = event.status !== 'done';
                  const description = (failed ? (event.error || t('monitors.eventFailed')) : (event.result || event.display || '')).replace(/\s+/g, ' ').trim();
                  return (
                    <div key={event.id} style={{
                      display: 'flex', gap: '10px', alignItems: 'flex-start', justifyContent: 'space-between',
                      padding: '8px', borderRadius: '8px', fontSize: '12px',
                      border: `1px solid ${failed ? 'var(--vscode-fg-danger, #f85149)' : 'var(--vscode-border)'}`,
                      background: event.seen ? 'transparent' : 'rgba(74,163,255,0.08)',
                    }}>
                      <div style={{ minWidth: 0 }}>
                        <div>
                          <strong>{t('monitors.eventHeadline', { name: event.monitor_name })}</strong>
                          {': '}
                          <span style={{ overflowWrap: 'anywhere' }}>{description.length > 280 ? `${description.slice(0, 279)}…` : description}</span>
                        </div>
                        <div style={{ opacity: 0.7 }}>
                          {formatTime(event.finished_at || event.created_at)} · {event.project}
                          {event.new_chat ? ` · ${t('monitors.eventNewChat')}` : ''}
                        </div>
                      </div>
                      {event.chat_id && (
                        <button style={{ ...buttonStyle, whiteSpace: 'nowrap' }} onClick={() => onOpenChat?.(event)}>
                          <MessageSquare size={12} /> {t('monitors.openChat')}
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          )}

          <section>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
              <h3 style={{ fontSize: '13px', margin: 0 }}>{t('monitors.listTitle')}</h3>
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                <label style={{ fontSize: '12px', display: 'inline-flex', gap: '4px', alignItems: 'center' }}>
                  <input type="checkbox" checked={showFinished} onChange={(e) => setShowFinished(e.target.checked)} />
                  {t('monitors.showFinished')}
                </label>
                <button style={buttonStyle} onClick={() => setShowForm(v => !v)}><Plus size={14} /> {t('monitors.newMonitor')}</button>
              </div>
            </div>

            {showForm && (
              <div style={{ border: '1px solid var(--vscode-border)', borderRadius: '8px', padding: '10px', marginBottom: '10px', display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '8px' }}>
                <label style={labelStyle}>{t('monitors.form.name')}
                  <input className="vscode-settings-input" style={fieldStyle} value={form.name} onChange={update('name')} />
                </label>
                <label style={labelStyle}>{t('monitors.form.checkType')}
                  <select className="vscode-settings-input" style={fieldStyle} value={form.checkType} onChange={update('checkType')}>
                    {Object.keys(CONDITIONS_FOR_CHECK).map(type => <option key={type} value={type}>{t(`monitors.checkTypes.${type}`)}</option>)}
                  </select>
                </label>
                {form.checkType === 'http' && (<>
                  <label style={{ ...labelStyle, gridColumn: '1 / -1' }}>URL
                    <input className="vscode-settings-input" style={fieldStyle} value={form.url} placeholder="https://economia.awesomeapi.com.br/json/last/USD-BRL" onChange={update('url')} />
                  </label>
                  <label style={labelStyle}>{t('monitors.form.jsonPath')}
                    <input className="vscode-settings-input" style={fieldStyle} value={form.jsonPath} placeholder="USDBRL.bid" onChange={update('jsonPath')} />
                  </label>
                  <label style={labelStyle}>{t('monitors.form.regex')}
                    <input className="vscode-settings-input" style={fieldStyle} value={form.regex} onChange={update('regex')} />
                  </label>
                </>)}
                {form.checkType === 'command' && (
                  <label style={{ ...labelStyle, gridColumn: '1 / -1' }}>{t('monitors.form.command')}
                    <input className="vscode-settings-input" style={fieldStyle} value={form.command} onChange={update('command')} />
                  </label>
                )}
                {form.checkType === 'file' && (<>
                  <label style={labelStyle}>{t('monitors.form.path')}
                    <input className="vscode-settings-input" style={fieldStyle} value={form.path} onChange={update('path')} />
                  </label>
                  <label style={labelStyle}>{t('monitors.form.watch')}
                    <select className="vscode-settings-input" style={fieldStyle} value={form.watch} onChange={update('watch')}>
                      <option value="mtime">{t('monitors.form.watchMtime')}</option>
                      <option value="content">{t('monitors.form.watchContent')}</option>
                    </select>
                  </label>
                </>)}
                {form.checkType === 'llm' && (
                  <label style={{ ...labelStyle, gridColumn: '1 / -1' }}>{t('monitors.form.question')}
                    <textarea className="vscode-settings-input" style={{ ...fieldStyle, minHeight: 54 }} value={form.question} onChange={update('question')} />
                    <span style={{ opacity: 0.7 }}>{t('monitors.form.llmCost')}</span>
                  </label>
                )}
                <label style={labelStyle}>{t('monitors.form.condition')}
                  <select className="vscode-settings-input" style={fieldStyle} value={form.condition} onChange={update('condition')}>
                    {CONDITIONS_FOR_CHECK[form.checkType].map(cond => <option key={cond} value={cond}>{t(`monitors.conditions.${cond}`)}</option>)}
                  </select>
                </label>
                {(VALUE_CONDITIONS.includes(form.condition) || form.condition === 'regex') && (
                  <label style={labelStyle}>{form.condition === 'regex' ? t('monitors.form.pattern') : t('monitors.form.value')}
                    <input className="vscode-settings-input" style={fieldStyle} value={form.value} onChange={update('value')} />
                  </label>
                )}
                {form.condition === 'delta' && (<>
                  <label style={labelStyle}>{t('monitors.form.threshold')}
                    <input className="vscode-settings-input" style={fieldStyle} type="number" step="any" value={form.threshold} onChange={update('threshold')} />
                  </label>
                  <label style={labelStyle}>{t('monitors.form.direction')}
                    <select className="vscode-settings-input" style={fieldStyle} value={form.direction} onChange={update('direction')}>
                      <option value="any">{t('monitors.form.directionAny')}</option>
                      <option value="up">{t('monitors.form.directionUp')}</option>
                      <option value="down">{t('monitors.form.directionDown')}</option>
                    </select>
                  </label>
                </>)}
                <label style={labelStyle}>{t('monitors.form.intervalMinutes')}
                  <input className="vscode-settings-input" style={fieldStyle} type="number" min="0.17" step="any" value={form.intervalMinutes} onChange={update('intervalMinutes')} />
                </label>
                <label style={labelStyle}>{t('monitors.form.firstMinutes')}
                  <input className="vscode-settings-input" style={fieldStyle} type="number" min="0" step="any" value={form.firstMinutes} onChange={update('firstMinutes')} />
                </label>
                <label style={labelStyle}>{t('monitors.form.ttlHours')}
                  <input className="vscode-settings-input" style={fieldStyle} type="number" min="0" step="any" value={form.ttlHours} onChange={update('ttlHours')} />
                </label>
                <label style={{ ...labelStyle, gridColumn: '1 / -1' }}>{t('monitors.form.message')}
                  <input className="vscode-settings-input" style={fieldStyle} value={form.message} placeholder={t('monitors.form.messagePlaceholder')} onChange={update('message')} />
                </label>
                <label style={{ ...labelStyle, flexDirection: 'row', alignItems: 'center' }}>
                  <input type="checkbox" checked={form.once} onChange={update('once')} /> {t('monitors.form.once')}
                </label>
                <label style={labelStyle}>{t('monitors.form.wakeMode')}
                  <select className="vscode-settings-input" style={fieldStyle} value={form.wakeMode} onChange={update('wakeMode')}>
                    <option value="none">{t('monitors.form.wakeNone')}</option>
                    <option value="origin" disabled={!activeChatId} title={activeChatId ? '' : t('monitors.form.noChat')}>{t('monitors.form.wakeOrigin')}</option>
                    <option value="new" disabled={!activeProject}>{t('monitors.form.wakeNew')}</option>
                  </select>
                </label>
                {form.wakeMode !== 'none' && (
                  <label style={{ ...labelStyle, gridColumn: '1 / -1' }}>{t('monitors.form.resumePrompt')}
                    <textarea className="vscode-settings-input" style={{ ...fieldStyle, minHeight: 54 }} value={form.resumePrompt} onChange={update('resumePrompt')} />
                    {form.wakeMode === 'new' && <span style={{ opacity: 0.7 }}>{t('monitors.form.wakeNewHint')}</span>}
                  </label>
                )}
                <div style={{ gridColumn: '1 / -1', display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
                  <button style={buttonStyle} onClick={() => { setShowForm(false); setForm(emptyForm()); }}>{t('common.cancel', 'Cancel')}</button>
                  <button className="vscode-button" style={{ padding: '6px 12px', borderRadius: '6px', fontWeight: 600 }} disabled={busy || !form.name.trim()} onClick={create}>
                    {busy ? t('monitors.form.checking') : t('monitors.form.create')}
                  </button>
                </div>
              </div>
            )}

            {visible.length === 0 ? (
              <div style={{ fontSize: '12px', opacity: 0.7, padding: '12px 0' }}>{t('monitors.empty')}</div>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                  <thead>
                    <tr style={{ textAlign: 'left', borderBottom: '1px solid var(--vscode-border)' }}>
                      <th style={{ padding: '6px' }}>{t('monitors.columns.name')}</th>
                      <th style={{ padding: '6px' }}>{t('monitors.columns.type')}</th>
                      <th style={{ padding: '6px' }}>{t('monitors.columns.trigger')}</th>
                      <th style={{ padding: '6px' }}>{t('monitors.columns.status')}</th>
                      <th style={{ padding: '6px' }}>{t('monitors.columns.actions')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map(monitor => {
                      const trigger = describeTrigger(monitor);
                      const result = testResults[monitor.id];
                      const isOpen = expanded === monitor.id;
                      const finished = !['active', 'paused'].includes(monitor.status);
                      return (
                        <React.Fragment key={monitor.id}>
                          <tr style={{ borderBottom: '1px solid var(--vscode-border)', verticalAlign: 'top' }}>
                            <td style={{ padding: '6px', minWidth: 160 }}>
                              <button onClick={() => toggleHistory(monitor.id)} style={{ ...buttonStyle, border: 'none', padding: 0, fontWeight: 600, fontSize: '12px' }} aria-expanded={isOpen}>
                                {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />} {monitor.name}
                              </button>
                              <div style={{ opacity: 0.7 }}><ClampedText text={`${monitor.project || '—'}${monitor.description ? ` · ${monitor.description}` : ''}`} /></div>
                            </td>
                            <td style={{ padding: '6px', whiteSpace: 'nowrap' }}>
                              {/* From the action, not the stored owner: a monitor that wakes a new chat at each firing is a watch. */}
                              {['resume_chat', 'notify_and_resume'].includes(monitor.action?.type) && monitor.action?.chat !== 'new'
                                ? t('monitors.typeTaskWait') : t('monitors.typeWatch')}
                            </td>
                            <td style={{ padding: '6px', minWidth: 200, maxWidth: 320 }}>
                              <ClampedText text={trigger.target} />
                              <div style={{ opacity: 0.8 }}>{trigger.cond} · {trigger.every}{trigger.once ? ` · ${t('monitors.firesOnce')}` : ''}</div>
                              {trigger.wakes && <div style={{ opacity: 0.8 }}>→ {trigger.wakes}</div>}
                            </td>
                            <td style={{ padding: '6px', minWidth: 150 }}>
                              <div style={{ color: statusColor(monitor.status), fontWeight: 600 }}>{t(`monitors.status.${monitor.status}`, monitor.status)}</div>
                              <div style={{ opacity: 0.8 }}>{t('monitors.lastCheck')}: {formatTime(monitor.last_check_at)}</div>
                              {monitor.status === 'active' && <div style={{ opacity: 0.8 }}>{t('monitors.nextCheck')}: {formatTime(monitor.next_check_at)}</div>}
                              {monitor.has_value && shortValue(monitor.last_value) && <div style={{ overflowWrap: 'anywhere' }}>{t('monitors.lastValue')}: {shortValue(monitor.last_value).slice(0, 120)}</div>}
                              {monitor.last_error && <div style={{ color: 'var(--vscode-fg-danger)' }}><ClampedText text={monitor.last_error} /></div>}
                            </td>
                            <td style={{ padding: '6px', whiteSpace: 'nowrap' }}>
                              <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
                                {monitor.status === 'active' && <button style={buttonStyle} onClick={() => act(monitor.id, 'pause')} title={t('monitors.pause')}><Pause size={12} /> {t('monitors.pause')}</button>}
                                {monitor.status === 'paused' && <button style={buttonStyle} onClick={() => act(monitor.id, 'resume')} title={t('monitors.resume')}><Play size={12} /> {t('monitors.resume')}</button>}
                                <button style={buttonStyle} onClick={() => test(monitor.id)} title={t('monitors.testHint')}><FlaskConical size={12} /> {t('monitors.test')}</button>
                                {confirmDelete === monitor.id ? (
                                  <button style={{ ...buttonStyle, color: 'var(--vscode-fg-danger)' }} onClick={() => act(monitor.id, finished ? 'delete' : 'cancel')}>
                                    {finished ? t('monitors.confirmDelete') : t('monitors.confirmCancel')}
                                  </button>
                                ) : (
                                  <button style={buttonStyle} onClick={() => setConfirmDelete(monitor.id)} title={finished ? t('monitors.delete') : t('monitors.cancel')}>
                                    <Trash2 size={12} /> {finished ? t('monitors.delete') : t('monitors.cancel')}
                                  </button>
                                )}
                              </div>
                              {result && (
                                <div style={{ marginTop: '4px', whiteSpace: 'normal', maxWidth: 240 }}>
                                  {result.pending ? t('monitors.testing')
                                    : result.ok ? (
                                      <>
                                        {/* The value can be a whole LLM answer: clamp it, keep the verdict visible. */}
                                        <ClampedText text={t('monitors.testValue', { value: result.value })} />
                                        <div style={{ opacity: 0.8 }}>{t('monitors.testVerdict', { met: result.met ? t('common.yes', 'yes') : t('common.no', 'no'), fire: result.would_fire ? t('common.yes', 'yes') : t('common.no', 'no') })}</div>
                                      </>
                                    )
                                      : <div style={{ color: 'var(--vscode-fg-danger)' }}><ClampedText text={result.error} /></div>}
                                </div>
                              )}
                            </td>
                          </tr>
                          {isOpen && (
                            <tr>
                              <td colSpan={5} style={{ padding: '6px 6px 10px 24px' }}>
                                {(events[monitor.id] || []).length === 0 ? <span style={{ opacity: 0.7 }}>{t('monitors.noHistory')}</span> : (
                                  <div style={{ maxHeight: 180, overflowY: 'auto', fontFamily: 'var(--vscode-editor-font-family, monospace)' }}>
                                    {events[monitor.id].map(event => (
                                      <div key={event.id} style={{ display: 'flex', gap: '8px' }}>
                                        <span style={{ opacity: 0.7, whiteSpace: 'nowrap' }}>{formatTime(event.at)}</span>
                                        <span style={{ minWidth: 90 }}>{t(`monitors.events.${event.kind}`, event.kind)}</span>
                                        <span style={{ overflowWrap: 'anywhere' }}>{event.message}</span>
                                      </div>
                                    ))}
                                  </div>
                                )}
                              </td>
                            </tr>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
