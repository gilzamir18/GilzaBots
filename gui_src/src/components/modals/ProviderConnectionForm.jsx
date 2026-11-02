import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import OpenRouterOAuthButton from './OpenRouterOAuthButton';

/**
 * Provider connection fields (label/provider/API key/API base URL), shared by
 * the standalone Add Connection modal (`asForm`, default) and `ModelForm`'s
 * inline "new connection" expansion (`asForm={false}`, since nesting an HTML
 * `<form>` inside `ModelForm`'s own `<form>` would be invalid and break
 * submit routing for both).
 *
 * A connection's id is generated once from its label (or provider, when no
 * label is given) and never changes on edit -- editing only replaces the
 * label/provider/api_key/api_base fields in place, so models referencing the
 * connection by id never need to be migrated to a new id.
 */
export function slugifyConnectionLabel(value) {
  const slug = (value || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return slug || 'connection';
}

export default function ProviderConnectionForm({
  editingConnection,
  existingConnections = [],
  onSubmit,
  onOAuthConnected,
  actions,
  formStyle,
  asForm = true,
  onCancel,
  submitLabel,
}) {
  const { t } = useTranslation();

  const [label, setLabel] = useState('');
  const [provider, setProvider] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [apiBase, setApiBase] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    if (editingConnection) {
      setLabel(editingConnection.label || '');
      setProvider(editingConnection.provider || '');
      setApiKey(editingConnection.api_key || '');
      setApiBase(editingConnection.api_base || '');
    }
  }, [editingConnection]);

  const reset = () => {
    setLabel('');
    setProvider('');
    setApiKey('');
    setApiBase('');
    setError('');
  };

  const handleOAuthSuccess = (result) => {
    const conn = result?.connection;
    if (conn) {
      setLabel(conn.label || 'OpenRouter');
      setProvider(conn.provider || 'openrouter');
      setApiKey(conn.api_key || '');
      setApiBase(conn.api_base || 'https://openrouter.ai/api/v1');
    }
    onOAuthConnected?.(result);
  };

  const showOpenRouterOAuth = !provider.trim() || provider.trim().toLowerCase() === 'openrouter';

  const handleSubmit = (e) => {
    e?.preventDefault?.();
    setError('');

    const trimmedLabel = label.trim();
    const trimmedProvider = provider.trim();

    if (!trimmedLabel || !trimmedProvider) {
      setError(t('connectionForm.requiredError'));
      return;
    }

    let id = editingConnection?.id;
    if (!id) {
      const base = slugifyConnectionLabel(trimmedLabel || trimmedProvider);
      const existingIds = new Set(existingConnections.map(c => c.id));
      id = base;
      let suffix = 2;
      while (existingIds.has(id)) {
        id = `${base}-${suffix}`;
        suffix += 1;
      }
    }

    onSubmit({
      id,
      label: trimmedLabel,
      provider: trimmedProvider,
      api_key: apiKey,
      api_base: apiBase.trim(),
    }, { reset });
  };

  const fields = (
    <div className="vscode-modal-content" style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {error && (
        <div style={{ color: 'var(--vscode-errorForeground)', fontSize: '12px', padding: '8px', background: 'rgba(244,135,113,0.1)', borderRadius: '4px' }}>
          {error}
        </div>
      )}

      {showOpenRouterOAuth && (
        <div
          style={{
            padding: '10px 12px',
            border: '1px solid var(--vscode-widget-border)',
            borderRadius: '6px',
            background: 'var(--vscode-editor-inactiveSelectionBackground)',
            display: 'flex',
            flexDirection: 'column',
            gap: '8px',
          }}
        >
          <div style={{ fontSize: '12px', color: 'var(--vscode-descriptionForeground)' }}>
            {t(
              'openRouterOAuth.formHint',
              'Using OpenRouter? Sign in with your browser to create a key and load the default free model (openrouter/free) automatically.'
            )}
          </div>
          <OpenRouterOAuthButton
            label={label.trim()}
            connectionId={editingConnection?.id || ''}
            onSuccess={handleOAuthSuccess}
          />
        </div>
      )}

      <div className="vscode-form-group">
        <label>{t('connectionForm.labelLabel')}</label>
        <input
          type="text"
          className="vscode-settings-input"
          value={label}
          onChange={e => setLabel(e.target.value)}
          placeholder={t('connectionForm.labelPlaceholder')}
        />
      </div>

      <div className="vscode-form-group">
        <label>{t('connectionForm.providerLabel')}</label>
        <input
          type="text"
          className="vscode-settings-input"
          value={provider}
          onChange={e => setProvider(e.target.value)}
          placeholder={t('connectionForm.providerPlaceholder')}
        />
      </div>

      <div className="vscode-form-group">
        <label>{t('connectionForm.apiKeyLabel')}</label>
        <input
          type="password"
          className="vscode-settings-input"
          value={apiKey}
          onChange={e => setApiKey(e.target.value)}
          placeholder={t('connectionForm.apiKeyPlaceholder')}
        />
      </div>

      <div className="vscode-form-group">
        <label>{t('connectionForm.apiBaseLabel')}</label>
        <input
          type="text"
          className="vscode-settings-input"
          value={apiBase}
          onChange={e => setApiBase(e.target.value)}
          placeholder={t('connectionForm.apiBasePlaceholder')}
        />
      </div>
    </div>
  );

  if (!asForm) {
    return (
      <div style={formStyle}>
        {fields}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', marginTop: '8px' }}>
          {onCancel && (
            <button type="button" className="vscode-button-secondary" onClick={onCancel}>
              {t('common.cancel', 'Cancel')}
            </button>
          )}
          <button type="button" className="vscode-button" onClick={handleSubmit}>
            {submitLabel || t('common.save', 'Save')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} style={formStyle}>
      {fields}
      {actions}
    </form>
  );
}
