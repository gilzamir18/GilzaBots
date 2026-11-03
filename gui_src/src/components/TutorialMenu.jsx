import React, { useState } from 'react';
import { ChevronDown, ChevronRight, GraduationCap, MessageCircleQuestion } from 'lucide-react';
import { useTranslation } from 'react-i18next';

// Clickable question menu shown inside the built-in tutorial chat.
//
// The questions and their answers live in `gilzabots/guides/tutorial.<lang>.md` and are
// served by `/api/tutorial/*`; this component only renders what the backend sent and
// reports which topic was clicked. Answering happens server-side without an LLM call,
// so the tutorial works before any provider or model has been registered.
export default function TutorialMenu({ topics = [], onSelectTopic, disabled = false }) {
  const { t } = useTranslation();
  const [isOpen, setIsOpen] = useState(true);
  const [pendingTopic, setPendingTopic] = useState('');

  if (!topics.length) return null;

  const handleSelect = async (topicId) => {
    if (disabled || pendingTopic) return;
    setPendingTopic(topicId);
    try {
      await onSelectTopic?.(topicId);
    } finally {
      setPendingTopic('');
    }
  };

  return (
    <div
      style={{
        borderTop: '1px solid var(--vscode-border)',
        background: 'var(--vscode-sidebar-bg)',
        flexShrink: 0,
        maxHeight: '40%',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <button
        type="button"
        onClick={() => setIsOpen(open => !open)}
        title={isOpen ? t('tutorial.collapse', 'Collapse the tutorial menu') : t('tutorial.expand', 'Expand the tutorial menu')}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '6px',
          width: '100%',
          padding: '6px 10px',
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          color: 'var(--vscode-text-fg)',
          font: 'inherit',
          fontSize: '11px',
          textTransform: 'uppercase',
          letterSpacing: '0.4px',
          textAlign: 'left',
        }}
      >
        {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <GraduationCap size={13} />
        <span style={{ fontWeight: 600 }}>{t('tutorial.menuTitle', 'Tutorial topics')}</span>
      </button>

      {isOpen && (
        <div style={{ overflowY: 'auto', padding: '0 8px 8px' }}>
          <div
            style={{
              fontSize: '11px',
              color: 'var(--vscode-description-fg, var(--vscode-text-fg))',
              opacity: 0.8,
              padding: '0 2px 6px',
              lineHeight: 1.4,
            }}
          >
            {t('tutorial.menuHint', 'Pick a question to get an instant answer, or type your own below.')}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {topics.map(topic => (
              <button
                key={topic.id}
                type="button"
                onClick={() => handleSelect(topic.id)}
                disabled={disabled || Boolean(pendingTopic)}
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: '8px',
                  width: '100%',
                  padding: '6px 8px',
                  background: 'transparent',
                  border: '1px solid var(--vscode-border)',
                  borderRadius: '4px',
                  color: 'var(--vscode-text-fg)',
                  cursor: disabled || pendingTopic ? 'not-allowed' : 'pointer',
                  opacity: disabled || (pendingTopic && pendingTopic !== topic.id) ? 0.5 : 1,
                  font: 'inherit',
                  fontSize: '12px',
                  lineHeight: 1.4,
                  textAlign: 'left',
                }}
              >
                <MessageCircleQuestion size={13} style={{ flexShrink: 0, marginTop: '2px' }} />
                <span>{topic.question}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
