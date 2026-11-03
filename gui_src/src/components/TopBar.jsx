import React, { useState, useEffect } from 'react';
import { Cpu, RefreshCw, Settings2, Settings } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import ModelSelect from './ModelSelect.jsx';

export default function TopBar({
  activeProject,
  globalModels,
  onRefreshModels,
  onEditModels,
  onModelChange,
}) {
  const { t } = useTranslation();

  const projectModel = activeProject?.model || '';
  const projectWorkerModel = activeProject?.worker_model || '';

  return (
    <div className="vscode-topbar" style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '4px 16px',
      background: 'var(--vscode-titleBar-activeBackground, #1e1e1e)',
      borderBottom: '1px solid var(--vscode-widget-border, #3c3c3c)',
      minHeight: '35px',
      userSelect: 'none'
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        {/* App Title / Logo Area could go here */}
        <span style={{ fontWeight: 'bold', color: 'var(--vscode-titleBar-activeForeground, var(--vscode-text-fg))', fontSize: '13px' }}>
          GilzaBots
        </span>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
        {/* Orchestrator Model Selector */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <Settings2 size={14} style={{ color: 'var(--vscode-descriptionForeground, #888888)' }} title={t('topBar.orchestratorModel')} />
          <span style={{ fontSize: '12px', color: 'var(--vscode-descriptionForeground, #888888)' }}>{t('topBar.orchestrator')}:</span>
          <ModelSelect
            style={{ width: '180px', padding: '2px 4px', fontSize: '12px', height: '24px' }}
            value={projectModel}
            onChange={(val) => onModelChange('model', val)}
            globalModels={globalModels}
            onEditModels={onEditModels}
            onRefreshModels={onRefreshModels}
            disabled={!activeProject}
            placeholder={t('topBar.selectModel')}
          />
        </div>

        {/* Worker Model Selector */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <Cpu size={14} style={{ color: 'var(--vscode-descriptionForeground, #888888)' }} title={t('topBar.workerModel')} />
          <span style={{ fontSize: '12px', color: 'var(--vscode-descriptionForeground, #888888)' }}>{t('topBar.worker')}:</span>
          <ModelSelect
            style={{ width: '180px', padding: '2px 4px', fontSize: '12px', height: '24px' }}
            value={projectWorkerModel}
            onChange={(val) => onModelChange('worker_model', val)}
            globalModels={globalModels}
            onEditModels={onEditModels}
            onRefreshModels={onRefreshModels}
            disabled={!activeProject}
            placeholder={t('topBar.selectWorker')}
          />
        </div>
      </div>
      
      <div style={{ display: 'flex', alignItems: 'center', width: '60px' }}>
         {/* Placeholder for right side alignment */}
      </div>
    </div>
  );
}
