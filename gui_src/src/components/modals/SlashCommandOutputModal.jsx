import React, { useEffect, useState } from 'react';
import { Check, Copy, RefreshCw, Terminal, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { FormattedMessage } from '../../utils/formatMessage';

// The window a chat `/` command answers in.
//
// A slash command is an operation on the project or the chat, not a turn of
// the conversation: writing the command and its output into the chat as two
// bubbles put text the agent never saw among the messages it did, and the
// next reload made them disappear again. The output lives here instead; the
// conversation shows only what the command actually changed in it (a cleared
// history, a compaction divider), reloaded from the store.
//
// `output` is `{ command, status: 'running' | 'confirm' | 'done' | 'error', text }`.
// While a confirmation is pending the confirm dialog sits on top of this one.
export default function SlashCommandOutputModal({ output, onClose, projectPath }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const busy = output?.status === 'running' || output?.status === 'confirm';
  // A long command (/compact calls the model) may be dismissed while it runs;
  // its result reopens this window. Only a pending confirmation pins it.
  const pinned = output?.status === 'confirm';

  useEffect(() => { setCopied(false); }, [output?.text]);

  useEffect(() => {
    // A pending confirmation owns the keyboard: Escape here would close the
    // window the confirm dialog's answer is about to fill.
    if (!output || output.status === 'confirm') return undefined;
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [output, onClose]);

  if (!output) return null;

  const text = String(output.text || '');
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="vscode-modal-overlay" role="presentation" onMouseDown={pinned ? undefined : onClose}>
      <div
        className="vscode-modal flex flex-col slash-command-output"
        role="dialog"
        aria-modal="true"
        aria-labelledby="slash-command-output-title"
        onMouseDown={(event) => event.stopPropagation()}
        style={{ width: '720px', maxWidth: '92%', maxHeight: 'calc(80 * var(--ui-vh))', padding: 0 }}
      >
        <div className="vscode-modal-header titlebar">
          <div className="vscode-modal-header-title" id="slash-command-output-title">
            <Terminal size={16} />
            <code className="slash-command-output-name">{output.command}</code>
          </div>
          <button
            type="button"
            className="vscode-modal-close"
            onClick={onClose}
            disabled={pinned}
            aria-label={t('common.close', 'Close')}
          >
            <X size={16} />
          </button>
        </div>
        <div className="vscode-modal-content slash-command-output-body" aria-live="polite">
          {busy ? (
            <div className="slash-command-output-status">
              <RefreshCw size={14} className="animate-spin" />
              <span>
                {output.status === 'confirm'
                  ? t('slashCommand.waitingConfirmation', 'Waiting for your confirmation…')
                  : t('slashCommand.running', 'Running…')}
              </span>
            </div>
          ) : text.trim() ? (
            <div className={output.status === 'error' ? 'slash-command-output-error' : undefined}>
              <FormattedMessage content={text} projectPath={projectPath} />
            </div>
          ) : (
            <div className="slash-command-output-status">
              {t('slashCommand.noOutput', 'The command finished without output.')}
            </div>
          )}
        </div>
        <div className="vscode-modal-footer">
          <button
            type="button"
            className="vscode-button-secondary"
            onClick={copy}
            disabled={busy || !text.trim()}
            style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}
          >
            {copied ? <Check size={14} /> : <Copy size={14} />}
            {copied ? t('slashCommand.copied', 'Copied') : t('slashCommand.copy', 'Copy output')}
          </button>
          <button type="button" className="vscode-button" onClick={onClose} disabled={pinned} autoFocus>
            {t('common.close', 'Close')}
          </button>
        </div>
      </div>
    </div>
  );
}
