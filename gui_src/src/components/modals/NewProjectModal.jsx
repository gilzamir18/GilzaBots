import React, { useState } from 'react';
import { X, FolderOpen, AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useModelValidation } from './useModelValidation';
import ModelSelect from '../ModelSelect';
import { globalDefaultModelId } from '../../utils/chatModels.js';

// Modal for registering a new project.
export default function NewProjectModal({
  globalModels = [],
  onClose,
  onSubmit,
  newProjName, setNewProjName,
  newProjPath, setNewProjPath,
  newProjDesc, setNewProjDesc,
  newProjModel, setNewProjModel,
  newProjWorkerModel, setNewProjWorkerModel,
  newProjMode, setNewProjMode,
  newProjApiKey, setNewProjApiKey,
  newProjApiBase, setNewProjApiBase,
  newProjWorkerApiKey, setNewProjWorkerApiKey,
  newProjWorkerApiBase, setNewProjWorkerApiBase,
  newProjModelParams, setNewProjModelParams,
  newProjWorkerModelParams, setNewProjWorkerModelParams,
  newProjError,
  onOpenDirPicker,
}) {
  const { t } = useTranslation();
  const { hardware, modelStatus } = useModelValidation(newProjModel, newProjApiBase);
  const { hardware: workerHardware, modelStatus: workerModelStatus } = useModelValidation(newProjWorkerModel, newProjWorkerApiBase || newProjApiBase);

  const [activeTab, setActiveTab] = useState('geral');

  const isWindows = navigator.userAgent.toLowerCase().includes('windows');
  const dynamicPathHint = isWindows ? t('newProjectModal.pathHintWindows', 'Ex: C:\\\\Projects') : t('newProjectModal.pathHintUnix', 'Ex: /home/user/projects');

  const getBorderColor = (status) => {
    if (status === 'green') return 'var(--vscode-fg-success)';
    if (status === 'yellow') return 'var(--vscode-fg-warning)';
    if (status === 'red') return 'var(--vscode-fg-danger)';
    return undefined;
  };

  const handleParamChange = (setter, key, val) => {
    setter(prev => ({ ...prev, [key]: val }));
  };

  const setProjectModel = (value, target) => {
    if (target === 'worker') {
      setNewProjWorkerModel(value);
      return;
    }

    setNewProjModel(value);
  };

  const tabs = [
    { id: 'geral', label: t('editProjectModal.tabGeneral') },
    { id: 'orquestrador', label: t('editProjectModal.tabOrchestrator') },
    { id: 'worker', label: t('editProjectModal.tabWorker') }
  ];

  return (
    <div className="vscode-modal-overlay">
      <div className="vscode-modal">
        <div className="vscode-modal-header" style={{ borderBottom: 'none' }}>
          <span className="vscode-sidebar-title" style={{ color: 'var(--vscode-text-fg)' }}>{t('newProjectModal.title')}</span>
          <button type="button" className="vscode-modal-close" onClick={onClose} aria-label={t('common.close', 'Close')}>
            <X size={14} />
          </button>
        </div>

        {/* Tab Navigation */}
        <div className="vscode-modal-tabs">
          {tabs.map(tab => (
            <button
              key={tab.id}
              type="button"
              className={`vscode-modal-tab${activeTab === tab.id ? ' active' : ''}`}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <form onSubmit={onSubmit} className="vscode-modal-content flex flex-col overflow-y-auto flex-1" style={{ gap: '12px' }}>
          
          {/* GERAL TAB */}
          {activeTab === 'geral' && (
            <>
              {/* Project name */}
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('newProjectModal.projectName')}</label>
                <input
                  type="text"
                  value={newProjName}
                  onChange={(e) => setNewProjName(e.target.value)}
                  placeholder={t('newProjectModal.projectNamePlaceholder')}
                  required
                />
              </div>

              {/* Project path */}
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('newProjectModal.projectPath')}</label>
                <div style={{ display: 'flex', gap: '6px' }}>
                  <input
                    type="text"
                    value={newProjPath}
                    onChange={(e) => setNewProjPath(e.target.value)}
                    placeholder={dynamicPathHint}
                    required
                    style={{ flex: 1 }}
                  />
                  <button type="button" className="vscode-button" style={{ padding: '4px 8px', whiteSpace: 'nowrap' }}
                    onClick={() => onOpenDirPicker('new', newProjPath || '~')}>
                    <FolderOpen size={14} />
                  </button>
                </div>
              </div>

              {/* Description */}
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('newProjectModal.description')}</label>
                <textarea
                  value={newProjDesc}
                  onChange={(e) => setNewProjDesc(e.target.value)}
                  placeholder={t('newProjectModal.descriptionPlaceholder')}
                  rows={2}
                  style={{ resize: 'none' }}
                />
              </div>

              {/* Execution mode */}
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('newProjectModal.executionMode')}</label>
                <select value={newProjMode} onChange={(e) => setNewProjMode(e.target.value)}>
                  <option value="auto">{t('newProjectModal.modeAuto')}</option>
                  <option value="plan">{t('newProjectModal.modePlan')}</option>
                  <option value="edit">{t('newProjectModal.modeEdit')}</option>
                </select>
              </div>
            </>
          )}

          {/* ORQUESTRADOR TAB */}
          {activeTab === 'orquestrador' && (
            <>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('newProjectModal.aiModel')}</label>
                {/* The catalog is the only source of project models, so the field cannot
                    hold a model the project would not be able to resolve at run time. */}
                <ModelSelect
                  value={newProjModel || ''}
                  onChange={value => setProjectModel(value, 'main')}
                  globalModels={globalModels}
                  showActions={false}
                  className=""
                  placeholder={globalDefaultModelId(globalModels)
                    ? t('modelSelect.followGlobalDefault', { model: globalDefaultModelId(globalModels) })
                    : t('newProjectModal.modelPlaceholder')}
                  style={{ borderColor: getBorderColor(modelStatus), borderWidth: modelStatus !== 'unknown' ? '2px' : '1px' }}
                />
                {modelStatus === 'green' && <span style={{ fontSize: '11px', color: 'var(--vscode-fg-success)' }}>{t('editProjectModal.modelSuitable')}</span>}
                {modelStatus === 'yellow' && <span style={{ fontSize: '11px', color: 'var(--vscode-fg-warning)' }}>{t('editProjectModal.modelMayBeSlow')}</span>}
                {modelStatus === 'red' && <span style={{ fontSize: '11px', color: 'var(--vscode-fg-danger)' }}>{t('editProjectModal.modelMayExceedVram')}</span>}
              </div>

              {/* Advanced params for Orchestrator */}
              <details style={{ background: 'var(--vscode-input-bg)', padding: '8px', borderRadius: '4px', border: '1px solid var(--vscode-border)' }}>
                <summary style={{ cursor: 'pointer', fontSize: '12px', color: 'var(--vscode-text-fg)', fontWeight: 'bold' }}>{t('editProjectModal.advancedOrchestrator')}</summary>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '12px' }}>
                  <div style={{ display: 'flex', gap: '12px' }}>
                     <div className="flex flex-col flex-1" style={{ gap: '4px' }}>
                        <label style={{ fontSize: '11px', color: 'var(--vscode-text-fg)' }}>{t('editProjectModal.maxHeartbeats')}</label>
                        <input type="number" value={newProjModelParams?.max_heartbeats ?? ''} onChange={e => handleParamChange(setNewProjModelParams, 'max_heartbeats', e.target.value ? parseInt(e.target.value) : undefined)} className="vscode-settings-input" placeholder={t('editProjectModal.defaultMemgptHeartbeats')} />
                     </div>
                     <div className="flex flex-col flex-1" style={{ gap: '4px' }}>
                        <label style={{ fontSize: '11px', color: 'var(--vscode-text-fg)' }}>{t('editProjectModal.loopDetectionLimit')}</label>
                        <input type="number" value={newProjModelParams?.loop_detection_limit ?? ''} onChange={e => handleParamChange(setNewProjModelParams, 'loop_detection_limit', e.target.value ? parseInt(e.target.value) : undefined)} className="vscode-settings-input" placeholder="Ex: 3" />
                     </div>
                  </div>
                  <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
                     <div className="flex flex-col flex-1" style={{ gap: '4px' }}>
                        <label style={{ fontSize: '11px', color: 'var(--vscode-text-fg)' }}>{t('editProjectModal.maxIdleHeartbeats')}</label>
                        <input type="number" min="1" max="10" value={newProjModelParams?.max_idle_heartbeats ?? ''} onChange={e => handleParamChange(setNewProjModelParams, 'max_idle_heartbeats', e.target.value ? parseInt(e.target.value) : undefined)} className="vscode-settings-input" placeholder="Ex: 2" />
                     </div>
                  </div>
                  <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
                     <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', userSelect: 'none' }}>
                        <input type="checkbox"
                           checked={newProjModelParams?.loop_detection ?? true}
                           onChange={e => handleParamChange(setNewProjModelParams, 'loop_detection', e.target.checked)} />
                         <span style={{ fontSize: '11px', color: 'var(--vscode-text-fg)' }}>{t('editProjectModal.enableLoopDetection')}</span>
                     </label>
                     <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', userSelect: 'none' }}>
                        <input type="checkbox"
                           checked={!!newProjModelParams?.empty_response_reasoning_fallback}
                           onChange={e => handleParamChange(setNewProjModelParams, 'empty_response_reasoning_fallback', e.target.checked)} />
                         <span style={{ fontSize: '11px', color: 'var(--vscode-text-fg)' }}>{t('editProjectModal.enableEmptyResponseThinking')}</span>
                     </label>
                  </div>
                </div>
              </details>
            </>
          )}

          {/* WORKER TAB */}
          {activeTab === 'worker' && (
            <>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('editProjectModal.workerModel')}</label>
                <ModelSelect
                  value={newProjWorkerModel || ''}
                  onChange={value => setProjectModel(value, 'worker')}
                  globalModels={globalModels}
                  showActions={false}
                  className=""
                  placeholder={t('newProjectModal.workerModelPlaceholder')}
                  style={{ borderColor: getBorderColor(workerModelStatus), borderWidth: workerModelStatus !== 'unknown' ? '2px' : '1px' }}
                />
                {workerModelStatus === 'green' && <span style={{ fontSize: '11px', color: 'var(--vscode-fg-success)' }}>{t('editProjectModal.modelSuitable')}</span>}
                {workerModelStatus === 'yellow' && <span style={{ fontSize: '11px', color: 'var(--vscode-fg-warning)' }}>{t('editProjectModal.modelMayBeSlow')}</span>}
                {workerModelStatus === 'red' && <span style={{ fontSize: '11px', color: 'var(--vscode-fg-danger)' }}>{t('editProjectModal.modelMayExceedVram')}</span>}
              </div>

              {/* Advanced params for Worker */}
              <details style={{ background: 'var(--vscode-input-bg)', padding: '8px', borderRadius: '4px', border: '1px solid var(--vscode-border)' }}>
                <summary style={{ cursor: 'pointer', fontSize: '12px', color: 'var(--vscode-text-fg)', fontWeight: 'bold' }}>{t('editProjectModal.advancedWorker')}</summary>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '12px' }}>
                  <div style={{ display: 'flex', gap: '12px' }}>
                     <div className="flex flex-col flex-1" style={{ gap: '4px' }}>
                        <label style={{ fontSize: '11px', color: 'var(--vscode-text-fg)' }}>{t('editProjectModal.maxIterations')}</label>
                        <input type="number" value={newProjWorkerModelParams?.max_iterations ?? ''} onChange={e => handleParamChange(setNewProjWorkerModelParams, 'max_iterations', e.target.value ? parseInt(e.target.value) : undefined)} className="vscode-settings-input" placeholder="Ex: 40" />
                     </div>
                     <div className="flex flex-col flex-1" style={{ gap: '4px' }}>
                        <label style={{ fontSize: '11px', color: 'var(--vscode-text-fg)' }}>{t('editProjectModal.loopDetectionLimit')}</label>
                        <input type="number" value={newProjWorkerModelParams?.loop_detection_limit ?? ''} onChange={e => handleParamChange(setNewProjWorkerModelParams, 'loop_detection_limit', e.target.value ? parseInt(e.target.value) : undefined)} className="vscode-settings-input" placeholder="Ex: 3" />
                     </div>
                  </div>
                  <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
                     <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', userSelect: 'none' }}>
                        <input type="checkbox"
                           checked={newProjWorkerModelParams?.loop_detection ?? true}
                           onChange={e => handleParamChange(setNewProjWorkerModelParams, 'loop_detection', e.target.checked)} />
                        <span style={{ fontSize: '11px', color: 'var(--vscode-text-fg)' }}>{t('editProjectModal.enableLoopDetection')}</span>
                     </label>
                  </div>
                </div>
              </details>
            </>
          )}

          {newProjError && (
            <div style={{ color: 'var(--vscode-errorForeground)', fontSize: '11px', marginTop: '4px', whiteSpace: 'pre-wrap' }}>
              <AlertTriangle size={12} aria-hidden="true" style={{ verticalAlign: '-2px', marginRight: '4px' }} />{newProjError}
            </div>
          )}

          {/* Actions */}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', paddingTop: '12px', borderTop: '1px solid var(--vscode-border)', marginTop: '4px' }}>
            <button type="button" onClick={onClose} className="vscode-button" style={{ background: 'transparent', color: 'var(--vscode-text-fg)', border: '1px solid var(--vscode-border)' }}>
              {t('newProjectModal.cancel')}
            </button>
            <button type="submit" className="vscode-button">{t('newProjectModal.register')}</button>
          </div>
        </form>
      </div>
    </div>
  );
}
