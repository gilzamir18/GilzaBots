import React, { useState, useEffect } from 'react';
import { X, Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';

export default function SearchChatsModal({
  onClose,
  activeProject,
  onSwitchChat
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!query.trim() || !activeProject) {
      setResults([]);
      setError(null);
      return;
    }

    const delayDebounceFn = setTimeout(() => {
      setLoading(true);
      setError(null);
      fetch(`/api/chat/search?project_name=${encodeURIComponent(activeProject)}&q=${encodeURIComponent(query)}`)
        .then(res => res.json())
        .then(data => {
          if (data.error) {
            setError(data.error);
            setResults([]);
          } else {
            setResults(data.results || []);
          }
        })
        .catch(err => {
          console.error(err);
          setError("Failed to fetch search results.");
        })
        .finally(() => {
          setLoading(false);
        });
    }, 300);

    return () => clearTimeout(delayDebounceFn);
  }, [query, activeProject]);

  const handleSelect = (chatId) => {
    onSwitchChat(chatId);
    onClose();
  };

  return (
    <div className="vscode-modal-overlay">
      <div className="vscode-modal" style={{ width: '400px', maxHeight: '500px', display: 'flex', flexDirection: 'column' }}>
        <div className="vscode-modal-header">
          <span className="vscode-sidebar-title" style={{ color: 'var(--vscode-text-fg)', display: 'flex', alignItems: 'center', gap: '6px' }}>
            <Search size={14} />
            {t('chat.searchChats', 'Search Chats')}
          </span>
          <button onClick={onClose} style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--vscode-text-subtle)' }}>
            <X size={14} />
          </button>
        </div>

        <div className="vscode-modal-content flex flex-col" style={{ gap: '12px', flex: 1, overflow: 'hidden' }}>
          <input
            autoFocus
            type="text"
            placeholder={t('chat.searchPlaceholder', 'Type to search chat content...')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />

          {error && <div style={{ color: 'var(--vscode-errorForeground)', fontSize: '12px' }}>{error}</div>}

          <div style={{ overflowY: 'auto', flex: 1, display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {loading && <div style={{ color: 'var(--vscode-text-muted)', fontStyle: 'italic', fontSize: '12px' }}>{t('chat.searching', 'Searching...')}</div>}
            
            {!loading && query.trim() !== '' && results.length === 0 && !error && (
              <div style={{ color: 'var(--vscode-text-muted)', fontStyle: 'italic', fontSize: '12px' }}>{t('chat.noResults', 'No results found.')}</div>
            )}

            {!loading && results.map((res, i) => (
              <div 
                key={res.id + '-' + i} 
                onClick={() => handleSelect(res.id)}
                style={{
                  background: 'var(--vscode-input-bg)',
                  padding: '10px',
                  borderRadius: '4px',
                  cursor: 'pointer',
                  border: '1px solid var(--vscode-border)'
                }}
                onMouseEnter={(e) => e.currentTarget.style.border = '1px solid var(--vscode-accent)'}
                onMouseLeave={(e) => e.currentTarget.style.border = '1px solid var(--vscode-border)'}
              >
                <div style={{ fontWeight: 'bold', fontSize: '13px', color: 'var(--vscode-text-fg)', marginBottom: '4px' }}>
                  {res.name}
                </div>
                <div style={{ fontSize: '11px', color: 'var(--vscode-text-muted)', whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: '60px', overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical' }}>
                  {res.snippet}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
