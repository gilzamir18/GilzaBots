import React from 'react';
import { X, Save } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import ProviderConnectionForm from './ProviderConnectionForm';

export default function AddConnectionModal({
  editingConnection,
  existingConnections = [],
  onClose,
  onSave
}) {
  const { t } = useTranslation();

  return (
    <div className="vscode-modal-overlay" onClick={onClose}>
      <div className="vscode-modal" style={{ width: '450px' }} onClick={e => e.stopPropagation()}>
        <div className="vscode-modal-header">
          <h2>{editingConnection ? t('connectionForm.editTitle') : t('connectionForm.addTitle')}</h2>
          <button onClick={onClose} className="vscode-modal-close"><X size={16} /></button>
        </div>

        <ProviderConnectionForm
          editingConnection={editingConnection}
          existingConnections={existingConnections}
          onSubmit={onSave}
          onOAuthConnected={() => onClose?.()}
          actions={(
            <div className="vscode-modal-footer">
              <button type="button" className="vscode-button-secondary" onClick={onClose}>
                {t('common.cancel', 'Cancel')}
              </button>
              <button type="submit" className="vscode-button">
                <Save size={14} /> {t('common.save', 'Save')}
              </button>
            </div>
          )}
        />
      </div>
    </div>
  );
}
