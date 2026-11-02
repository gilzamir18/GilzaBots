import React, { useState } from 'react';
import { X, Search, Plus, Trash2, Edit2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useCustomDialog } from './CustomDialogProvider';
import OpenRouterOAuthButton from './OpenRouterOAuthButton';

export default function EditProvidersModal({
  connections,
  models,
  onClose,
  onDeleteConnection,
  onEditConnection,
  onAddConnection,
}) {
  const { t } = useTranslation();
  const { showConfirm, showAlert } = useCustomDialog();
  const [searchTerm, setSearchTerm] = useState('');

  const modelCountByConnection = (models || []).reduce((acc, model) => {
    const id = model.connection_id;
    if (!id) return acc;
    acc[id] = (acc[id] || 0) + 1;
    return acc;
  }, {});

  const filteredConnections = (connections || []).filter(c =>
    (c.label || '').toLowerCase().includes(searchTerm.toLowerCase()) ||
    (c.provider || '').toLowerCase().includes(searchTerm.toLowerCase())
  );

  const handleDelete = async (connection) => {
    if (!(await showConfirm(t('editProvidersModal.deleteConfirm', { label: connection.label || connection.id })))) {
      return;
    }
    const result = await onDeleteConnection(connection.id);
    if (result && result.ok === false) {
      await showAlert(result.error || t('editProvidersModal.deleteFailed'));
    }
  };

  return (
    <div className="vscode-modal-overlay" onClick={onClose}>
      <div className="vscode-modal" style={{ width: '600px', maxHeight: 'calc(80 * var(--ui-vh))', display: 'flex', flexDirection: 'column' }} onClick={e => e.stopPropagation()}>
        <div className="vscode-modal-header">
          <h2>{t('editProvidersModal.title')}</h2>
          <button onClick={onClose} className="vscode-modal-close"><X size={16} /></button>
        </div>

        <div className="vscode-modal-content" style={{ display: 'flex', flexDirection: 'column', gap: '16px', flex: 1, minHeight: 0 }}>

          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
            <div style={{ position: 'relative', flex: 1, minWidth: '180px' }}>
              <Search size={14} style={{ position: 'absolute', left: '8px', top: '50%', transform: 'translateY(-50%)', color: 'var(--vscode-descriptionForeground)' }} />
              <input
                type="text"
                className="vscode-settings-input"
                placeholder={t('editProvidersModal.searchPlaceholder')}
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                style={{ width: '100%', paddingLeft: '28px' }}
              />
            </div>
            <button className="vscode-button" onClick={onAddConnection}>
              <Plus size={14} /> {t('editProvidersModal.addConnection')}
            </button>
          </div>

          <OpenRouterOAuthButton />

          <div style={{ overflowY: 'auto', flex: 1, border: '1px solid var(--vscode-widget-border)', borderRadius: '4px' }}>
            {filteredConnections.length === 0 ? (
              <div style={{ padding: '20px', textAlign: 'center', color: 'var(--vscode-descriptionForeground)' }}>
                {t('editProvidersModal.noConnections')}
              </div>
            ) : (
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--vscode-widget-border)', background: 'var(--vscode-editor-inactiveSelectionBackground)' }}>
                    <th style={{ padding: '8px', textAlign: 'left' }}>{t('editProvidersModal.label')}</th>
                    <th style={{ padding: '8px', textAlign: 'left' }}>{t('editProvidersModal.provider')}</th>
                    <th style={{ padding: '8px', textAlign: 'left' }}>{t('editModelsModal.apiBaseUrl', 'API Base URL')}</th>
                    <th style={{ padding: '8px', textAlign: 'left' }}>{t('editProvidersModal.modelsUsing')}</th>
                    <th style={{ padding: '8px', textAlign: 'right' }}>{t('editModelsModal.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredConnections.map((connection) => (
                    <tr key={connection.id} style={{ borderBottom: '1px solid var(--vscode-widget-border)' }}>
                      <td style={{ padding: '8px' }}>{connection.label}</td>
                      <td style={{ padding: '8px' }}>{connection.provider}</td>
                      <td style={{ padding: '8px', color: 'var(--vscode-descriptionForeground)' }}>{connection.api_base}</td>
                      <td style={{ padding: '8px', color: 'var(--vscode-descriptionForeground)' }}>{modelCountByConnection[connection.id] || 0}</td>
                      <td style={{ padding: '8px', textAlign: 'right' }}>
                        <button
                          className="vscode-icon-button"
                          title={t('common.edit')}
                          onClick={() => onEditConnection(connection)}
                        >
                          <Edit2 size={14} />
                        </button>
                        <button
                          className="vscode-icon-button vscode-icon-button-danger"
                          title={t('common.delete')}
                          onClick={() => handleDelete(connection)}
                        >
                          <Trash2 size={14} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="vscode-modal-footer">
          <button className="vscode-button-secondary" onClick={onClose}>
            {t('common.close', 'Close')}
          </button>
        </div>
      </div>
    </div>
  );
}
