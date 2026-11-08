import React, { useState } from 'react';
import { X, Search, Plus, Trash2, Edit2, RefreshCw, Star } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useCustomDialog } from './CustomDialogProvider';
import OpenRouterOAuthButton from './OpenRouterOAuthButton';

export default function EditModelsModal({
  globalModels,
  onClose,
  onDeleteModel,
  onEditModel,
  onAddModel,
  onManageConnections,
  onLoadLocalOllama,
  onSetDefaultModel
}) {
  const { t } = useTranslation();
  const { showConfirm } = useCustomDialog();
  const [searchTerm, setSearchTerm] = useState('');
  const [isLoadingLocalOllama, setIsLoadingLocalOllama] = useState(false);
  const [localOllamaResult, setLocalOllamaResult] = useState(null);

  const handleLoadLocalOllama = async () => {
    setIsLoadingLocalOllama(true);
    setLocalOllamaResult(null);
    try {
      setLocalOllamaResult(await onLoadLocalOllama());
    } finally {
      setIsLoadingLocalOllama(false);
    }
  };

  const localOllamaResultText = () => {
    if (!localOllamaResult) return '';
    if (localOllamaResult.status === 'loaded') {
      return localOllamaResult.count > 0
        ? t('editModelsModal.localOllamaLoaded', { count: localOllamaResult.count })
        : t('editModelsModal.localOllamaAlreadyConfigured');
    }
    if (localOllamaResult.status === 'ollama_not_installed') {
      return t('editModelsModal.ollamaNotInstalled');
    }
    if (localOllamaResult.status === 'ollama_unavailable') {
      return t('editModelsModal.ollamaUnavailable');
    }
    return t('editModelsModal.loadLocalOllamaFailed');
  };


  const filteredModels = globalModels.filter(m => 
    (m.id || '').toLowerCase().includes(searchTerm.toLowerCase()) ||
    (m.provider || '').toLowerCase().includes(searchTerm.toLowerCase()) ||
    (m.name || '').toLowerCase().includes(searchTerm.toLowerCase())
  );

  return (
    <div className="vscode-modal-overlay" onClick={onClose}>
      <div className="vscode-modal" style={{ width: '1050px', maxWidth: 'calc(95 * var(--ui-vw))', maxHeight: 'calc(80 * var(--ui-vh))', display: 'flex', flexDirection: 'column' }} onClick={e => e.stopPropagation()}>
        <div className="vscode-modal-header">
          <h2>{t('editModelsModal.title')}</h2>
          <button onClick={onClose} className="vscode-modal-close"><X size={16} /></button>
        </div>
        
        <div className="vscode-modal-content" style={{ display: 'flex', flexDirection: 'column', gap: '16px', flex: 1, minHeight: 0 }}>
          
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
            <div style={{ position: 'relative', flex: 1, minWidth: '180px' }}>
              <Search size={14} style={{ position: 'absolute', left: '8px', top: '50%', transform: 'translateY(-50%)', color: 'var(--vscode-descriptionForeground)' }} />
              <input
                type="text"
                className="vscode-settings-input"
                placeholder={t('editModelsModal.searchPlaceholder')}
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                style={{ width: '100%', paddingLeft: '28px' }}
              />
            </div>
            <button
              className="vscode-button-secondary"
              onClick={handleLoadLocalOllama}
              disabled={isLoadingLocalOllama}
            >
              <RefreshCw size={14} />
              {isLoadingLocalOllama
                ? t('editModelsModal.loadingLocalOllama')
                : t('editModelsModal.loadLocalOllama')}
            </button>
            <button className="vscode-button-secondary" onClick={onManageConnections}>
              {t('editModelsModal.manageConnections')}
            </button>
            <button className="vscode-button" onClick={onAddModel}>
              <Plus size={14} /> {t('editModelsModal.addProviderModel')}
            </button>
          </div>

          <OpenRouterOAuthButton />

          {localOllamaResult && (
            <div role="status" style={{ padding: '8px', border: '1px solid var(--vscode-widget-border)', background: 'var(--vscode-input-background)', color: 'var(--vscode-descriptionForeground)', fontSize: '12px' }}>
              {localOllamaResultText()}
            </div>
          )}

          <div style={{ overflow: 'auto', flex: 1, border: '1px solid var(--vscode-widget-border)', borderRadius: '4px' }}>
            {filteredModels.length === 0 ? (
              <div style={{ padding: '20px', textAlign: 'center', color: 'var(--vscode-descriptionForeground)' }}>
                {t('editModelsModal.noModels')}
              </div>
            ) : (
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--vscode-widget-border)', background: 'var(--vscode-editor-inactiveSelectionBackground)' }}>
                    <th style={{ padding: '8px', textAlign: 'center', whiteSpace: 'nowrap' }} title={t('editModelsModal.defaultColumnHint')}>{t('editModelsModal.defaultColumn')}</th>
                    <th style={{ padding: '8px', textAlign: 'left', whiteSpace: 'nowrap' }}>{t('editModelsModal.provider')}</th>
                    <th style={{ padding: '8px', textAlign: 'left', whiteSpace: 'nowrap' }}>{t('editModelsModal.modelName')}</th>
                    <th style={{ padding: '8px', textAlign: 'left', whiteSpace: 'nowrap' }}>ID</th>
                    <th style={{ padding: '8px', textAlign: 'left', whiteSpace: 'nowrap' }}>{t('editModelsModal.thinking')}</th>
                    <th style={{ padding: '8px', textAlign: 'left', whiteSpace: 'nowrap' }}>{t('modelForm.promptProfileLabel')}</th>
                    <th style={{ padding: '8px', textAlign: 'left', whiteSpace: 'nowrap' }}>{t('modelForm.orchestratorPolicyLabel')}</th>
                    <th style={{ padding: '8px', textAlign: 'left', whiteSpace: 'nowrap' }}>{t('modelForm.numCtxLabel')}</th>
                    <th style={{ padding: '8px', textAlign: 'right', whiteSpace: 'nowrap' }}>{t('editModelsModal.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredModels.map((model) => (
                    <tr key={model.id} style={{ borderBottom: '1px solid var(--vscode-widget-border)' }} title={model.api_base ? `${t('editModelsModal.apiBaseUrl', 'API Base URL')}: ${model.api_base}` : undefined}>
                      <td style={{ padding: '8px', textAlign: 'center', whiteSpace: 'nowrap' }}>
                        {/* The global default: what a chat runs on when neither the chat nor its project names a model. */}
                        {model.is_default ? (
                          <span
                            title={t('editModelsModal.isDefault')}
                            aria-label={t('editModelsModal.isDefault')}
                            style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', color: 'var(--vscode-textLink-foreground)' }}
                          >
                            <Star size={14} fill="currentColor" />
                            {t('editModelsModal.defaultBadge')}
                          </span>
                        ) : (
                          <button
                            className="vscode-icon-button"
                            title={t('editModelsModal.setAsDefault')}
                            aria-label={t('editModelsModal.setAsDefault')}
                            onClick={() => onSetDefaultModel?.(model.id)}
                          >
                            <Star size={14} />
                          </button>
                        )}
                      </td>
                      <td style={{ padding: '8px', whiteSpace: 'nowrap' }}>{model.connection_label || model.provider}</td>
                      <td style={{ padding: '8px' }}>{model.name}</td>
                      <td style={{ padding: '8px', color: 'var(--vscode-descriptionForeground)' }}>{model.id}</td>
                      <td style={{ padding: '8px', whiteSpace: 'nowrap', color: model.supports_thinking ? 'var(--vscode-textLink-foreground)' : 'var(--vscode-descriptionForeground)' }}>
                        {model.supports_thinking ? t('common.yes', 'Yes') : t('common.no', 'No')}
                      </td>
                      <td style={{ padding: '8px', whiteSpace: 'nowrap', color: model.prompt_profile === 'light' ? 'var(--vscode-textLink-foreground)' : 'var(--vscode-descriptionForeground)' }}>
                        {t(`modelForm.promptProfile.${model.prompt_profile === 'light' ? 'light' : 'full'}`)}
                      </td>
                      <td style={{ padding: '8px', whiteSpace: 'nowrap', color: model.orchestrator_policy === 'delegate' ? 'var(--vscode-textLink-foreground)' : 'var(--vscode-descriptionForeground)' }}>
                        {t(`modelForm.orchestratorPolicy.${model.orchestrator_policy === 'delegate' ? 'delegate' : 'direct'}`)}
                      </td>
                      <td style={{ padding: '8px', whiteSpace: 'nowrap', color: 'var(--vscode-descriptionForeground)' }}>
                        {model.num_ctx || (model.auto_num_ctx
                          ? t(model.auto_num_ctx_source === 'provider' ? 'modelForm.numCtxAutoProvider' : 'modelForm.numCtxAutoLocal', { value: model.auto_num_ctx.toLocaleString() })
                          : t('modelForm.numCtxAutoUnknown'))}
                      </td>
                      <td style={{ padding: '8px', textAlign: 'right', whiteSpace: 'nowrap' }}>
                        <button 
                          className="vscode-icon-button"
                          title={t('common.edit')}
                          onClick={() => onEditModel(model)}
                        >
                          <Edit2 size={14} />
                        </button>
                        <button 
                          className="vscode-icon-button vscode-icon-button-danger"
                          title={t('common.delete')}
                          onClick={async () => {
                            if (await showConfirm(t('editModelsModal.deleteConfirm', { id: model.id }))) {
                              onDeleteModel(model.id);
                            }
                          }}
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
