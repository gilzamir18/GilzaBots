import React, { useEffect, useRef, useState } from 'react';
import { ExternalLink, Loader2, LogIn, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useModelCatalog } from '../../contexts/ModelCatalogProvider.jsx';
import { handleExternalClick } from '../../utils/openExternal';

/**
 * Reusable OpenRouter OAuth PKCE button and consent helper block.
 *
 * Follows the same loopback redirect + system browser + clickable fallback URL
 * pattern used by Google Drive in `CloudSyncModal` and `CloudDownloadModal`.
 */
export default function OpenRouterOAuthButton({
  label = '',
  connectionId = '',
  onSuccess,
  buttonClassName = 'vscode-button-secondary',
  buttonStyle,
  buttonLabel,
  showStatus = true,
  disabled = false,
}) {
  const { t } = useTranslation();
  const {
    startOpenRouterOAuth,
    completeOpenRouterOAuth,
    cancelOpenRouterOAuth,
  } = useModelCatalog();

  const [connecting, setConnecting] = useState(false);
  const [pendingAuthUrl, setPendingAuthUrl] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const mountedRef = useRef(true);
  const pendingSessionRef = useRef(null);
  const cancelledRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (pendingSessionRef.current) {
        cancelOpenRouterOAuth(pendingSessionRef.current);
        pendingSessionRef.current = null;
      }
    };
  }, [cancelOpenRouterOAuth]);

  const handleStart = async () => {
    if (connecting || disabled) return;
    setConnecting(true);
    setError('');
    setNotice('');
    setPendingAuthUrl('');
    cancelledRef.current = false;

    try {
      const challenge = await startOpenRouterOAuth({ label, connectionId });
      if (!mountedRef.current) return;
      if (!challenge.ok) {
        setError(challenge.error || t('openRouterOAuth.failed', 'OpenRouter sign-in failed.'));
        return;
      }

      pendingSessionRef.current = challenge.session || null;
      setPendingAuthUrl(challenge.authorization_url || '');

      const result = await completeOpenRouterOAuth({
        session: challenge.session,
        label,
        connectionId,
      });
      if (!mountedRef.current || cancelledRef.current) return;

      pendingSessionRef.current = null;
      setPendingAuthUrl('');

      if (!result.ok) {
        setError(result.error || t('openRouterOAuth.failed', 'OpenRouter sign-in failed.'));
        return;
      }

      const connLabel = result.connection?.label || result.connection?.id || 'OpenRouter';
      const defaultModelId = result.default_model?.id || 'openrouter/openrouter/free';
      setNotice(
        t('openRouterOAuth.connectedWithFreeModel', {
          defaultValue: 'Connected to {{label}} and loaded free model {{modelId}}.',
          label: connLabel,
          modelId: defaultModelId,
        })
      );
      onSuccess?.(result);
    } catch (e) {
      if (mountedRef.current && !cancelledRef.current) {
        setError(String(e));
      }
    } finally {
      if (mountedRef.current) {
        setConnecting(false);
      }
    }
  };

  const handleCancel = async () => {
    cancelledRef.current = true;
    const session = pendingSessionRef.current;
    pendingSessionRef.current = null;
    setPendingAuthUrl('');
    setConnecting(false);
    if (session) {
      await cancelOpenRouterOAuth(session);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
        <button
          type="button"
          className={buttonClassName}
          style={buttonStyle}
          onClick={handleStart}
          disabled={connecting || disabled}
        >
          {connecting ? <Loader2 size={14} className="animate-spin" /> : <LogIn size={14} />}
          {connecting
            ? t('openRouterOAuth.connecting', 'Waiting for OpenRouter…')
            : (buttonLabel || t('openRouterOAuth.connectBtn', 'Sign in with OpenRouter'))}
        </button>

        {connecting && (
          <button
            type="button"
            className="vscode-button-secondary"
            onClick={handleCancel}
            title={t('common.cancel', 'Cancel')}
          >
            <X size={14} /> {t('common.cancel', 'Cancel')}
          </button>
        )}
      </div>

      {pendingAuthUrl && (
        <div
          style={{
            fontSize: '11px',
            color: 'var(--vscode-descriptionForeground)',
            lineHeight: 1.5,
            padding: '8px',
            border: '1px solid var(--vscode-widget-border)',
            borderRadius: '4px',
            background: 'var(--vscode-input-background)',
          }}
        >
          <div style={{ marginBottom: '4px' }}>
            {t('openRouterOAuth.waitingBrowser', 'Waiting for you to finish in the browser…')}
          </div>
          <a
            href={pendingAuthUrl}
            onClick={handleExternalClick(pendingAuthUrl)}
            target="_blank"
            rel="noreferrer"
            style={{ color: 'var(--vscode-textLink-foreground, var(--vscode-accent))', wordBreak: 'break-all', cursor: 'pointer' }}
          >
            <ExternalLink size={11} style={{ display: 'inline', marginRight: '4px' }} />
            {pendingAuthUrl}
          </a>
        </div>
      )}

      {showStatus && error && (
        <div
          role="alert"
          style={{
            color: 'var(--vscode-errorForeground)',
            fontSize: '12px',
            padding: '8px',
            background: 'rgba(244,135,113,0.1)',
            borderRadius: '4px',
          }}
        >
          {error}
        </div>
      )}

      {showStatus && notice && (
        <div
          role="status"
          style={{
            padding: '8px',
            border: '1px solid var(--vscode-widget-border)',
            background: 'var(--vscode-input-background)',
            color: 'var(--vscode-descriptionForeground)',
            fontSize: '12px',
            borderRadius: '4px',
          }}
        >
          {notice}
        </div>
      )}
    </div>
  );
}
