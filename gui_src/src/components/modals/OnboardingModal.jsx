import React, { useState, useEffect } from 'react';
import { Loader2, Monitor, Terminal, CheckCircle, X, Settings2, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import ModelForm from './ModelForm';
import OpenRouterOAuthButton from './OpenRouterOAuthButton';
import ModelSelect from '../ModelSelect';
import { useModelCatalog } from '../../contexts/ModelCatalogProvider.jsx';

const PILOT_PROJECT_PATH = '~/GilzaBotsPilot';

export default function OnboardingModal({ onClose, onComplete }) {
  const { t, i18n } = useTranslation();
  const [step, setStep] = useState(1);
  const [hardware, setHardware] = useState(null);
  const [ollamaStatus, setOllamaStatus] = useState(null);
  const [isInstalling, setIsInstalling] = useState(false);

  // Models registered here go straight into the shared catalog, so the chat
  // toolbar and the project dialogs list them without an app reload. Nothing is
  // pre-selected: the pilot project only gets a model when the user picks one.
  const { models: catalogModels, saveModel, loadLocalOllamaModels, connections: catalogConnections, saveConnection } = useModelCatalog();
  const [pilotModel, setPilotModel] = useState('');
  const [savedModelId, setSavedModelId] = useState('');
  const [registerError, setRegisterError] = useState('');
  const [isDiscovering, setIsDiscovering] = useState(false);
  const [discoveryResult, setDiscoveryResult] = useState(null);

  useEffect(() => {
    fetch('/api/hardware')
      .then(res => res.json())
      .then(data => setHardware(data))
      .catch(console.error);

    fetch('/api/ollama/status')
      .then(res => res.json())
      .then(data => setOllamaStatus(data))
      .catch(console.error);
  }, []);

  const finishOnboarding = async (config) => {
    try {
      await fetch('/api/gilzabots/create-project', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config)
      });
      await fetch('/api/onboarding/complete', { method: 'POST' });
      onComplete();
    } catch (e) {
      console.error(e);
      onComplete(); // proceed anyway to not block user
    }
  };

  // The pilot project carries a model only when the user selected one here.
  const createPilotProject = (modelId) => {
    const config = {
      project_name: t('onboarding.pilotProjectName'),
      project_path: PILOT_PROJECT_PATH,
      mode: 'plan'
    };
    if (modelId) config.model = modelId;
    return finishOnboarding(config);
  };

  const handleClose = async () => {
    try {
      await fetch('/api/onboarding/complete', { method: 'POST' });
    } catch (e) {}
    onComplete();
  };

  const handleInstallOllama = async () => {
    setIsInstalling(true);
    setStep(3); // Mostra o progresso de instalação

    try {
      const r = await fetch('/api/settings/install-ollama', { method: 'POST' });
      const data = await r.json();
      if (!data.success) {
        console.error("Falha ao iniciar instalação do Ollama:", data.error);
        setStep(4); // Exibe fallback para instalação manual
      }
    } catch (e) {
      console.error("Erro ao chamar install-ollama:", e);
      setStep(4);
    }
    setIsInstalling(false);
  };

  const handleRegisterModel = async (modelData, { reset } = {}) => {
    setSavedModelId('');
    setRegisterError('');
    const result = await saveModel(modelData);
    if (!result.ok) {
      // A rejected registration must be visible: silently swallowing it left the
      // user believing the model had been added to the catalog.
      setRegisterError(result.error || 'model_save_failed');
      return;
    }
    reset?.();
    // The freshly registered entry is the natural choice for the pilot project,
    // but it stays changeable in the selector below.
    setPilotModel(modelData.id);
    setSavedModelId(modelData.id);
  };

  const handleOAuthConnected = (result) => {
    setRegisterError('');
    const defaultModelId = result?.default_model?.id || '';
    if (defaultModelId) {
      setPilotModel(defaultModelId);
      setSavedModelId(defaultModelId);
    }
    setStep(5);
  };

  const handleDiscoverLocalOllama = async () => {
    setIsDiscovering(true);
    setDiscoveryResult(null);
    setSavedModelId('');
    try {
      setDiscoveryResult(await loadLocalOllamaModels());
    } finally {
      setIsDiscovering(false);
    }
  };

  // Same status vocabulary as the Edit Models dialog.
  const discoveryResultText = () => {
    if (!discoveryResult) return '';
    if (discoveryResult.status === 'loaded') {
      return discoveryResult.count > 0
        ? t('editModelsModal.localOllamaLoaded', { count: discoveryResult.count })
        : t('editModelsModal.localOllamaAlreadyConfigured');
    }
    if (discoveryResult.status === 'ollama_not_installed') {
      return t('editModelsModal.ollamaNotInstalled');
    }
    if (discoveryResult.status === 'ollama_unavailable') {
      return t('editModelsModal.ollamaUnavailable');
    }
    return t('editModelsModal.loadLocalOllamaFailed');
  };

  const vram = hardware ? parseFloat(hardware.vram_gb) || 0 : 0;
  const isHighEnd = vram >= 8;

  return (
    <div className="vscode-modal-overlay" style={{ zIndex: 9999 }}>
      <div className="vscode-modal" style={{ maxWidth: '600px', width: '90%', maxHeight: 'calc(90 * var(--ui-vh))', overflowY: 'auto', padding: '32px', borderRadius: '12px', position: 'relative' }}>

        <button
          onClick={handleClose}
          className="vscode-modal-close"
          style={{ position: 'absolute', top: '16px', right: '16px' }}
          title={t('newProjectModal.cancel')}
        >
          <X size={20} />
        </button>

        {step === 1 && (
          <div style={{ textAlign: 'center' }}>
            <h1 style={{ fontSize: '24px', fontWeight: 'bold', marginBottom: '16px', color: 'var(--vscode-text-fg)' }}>{t('onboarding.welcome')}</h1>
            <p style={{ color: 'var(--vscode-text-fg)', marginBottom: '32px', lineHeight: '1.5' }}>
              {t('onboarding.analyzingMessage')}
            </p>
            {hardware ? (
              <button
                className="vscode-button"
                style={{ padding: '12px 24px', fontSize: '16px', borderRadius: '6px' }}
                onClick={() => setStep(2)}
              >
                {t('onboarding.viewRecommendation')}
              </button>
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', color: 'var(--vscode-accent)' }}>
                <Loader2 size={24} className="animate-spin" />
                <span>{t('onboarding.analyzingMachine')}</span>
              </div>
            )}
          </div>
        )}

        {step === 2 && (
          <div>
            <h2 style={{ fontSize: '20px', fontWeight: 'bold', marginBottom: '16px', color: 'var(--vscode-text-fg)', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Monitor size={20} />
              {t('onboarding.hardwareDetected', { vram: hardware?.vram_gb, ram: hardware?.ram_gb })}
            </h2>

            {isHighEnd ? (
              <div style={{ backgroundColor: 'rgba(74, 222, 128, 0.12)', padding: '16px', borderRadius: '8px', border: '1px solid rgba(74, 222, 128, 0.35)', marginBottom: '24px' }}>
                <h3 style={{ color: 'var(--battery-good)', margin: '0 0 8px 0', fontSize: '16px' }}>{t('onboarding.localTitle')}</h3>
                <p style={{ color: 'var(--vscode-text-fg)', margin: 0, fontSize: '14px', lineHeight: '1.5' }}>
                  {t('onboarding.localMessage')}
                </p>
              </div>
            ) : (
              <div style={{ backgroundColor: 'rgba(250, 204, 21, 0.12)', padding: '16px', borderRadius: '8px', border: '1px solid rgba(250, 204, 21, 0.35)', marginBottom: '24px' }}>
                <h3 style={{ color: 'var(--battery-low)', margin: '0 0 8px 0', fontSize: '16px' }}>{t('onboarding.cloudTitle')}</h3>
                <p style={{ color: 'var(--vscode-text-fg)', margin: 0, fontSize: '14px', lineHeight: '1.5' }}>
                  {t('onboarding.cloudMessage')}
                </p>
              </div>
            )}

            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              <button
                className="vscode-button-secondary"
                style={{ padding: '14px', fontSize: '15px', borderRadius: '6px', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px' }}
                onClick={() => setStep(3)} // step 3 = Ollama
                disabled={isInstalling}
              >
                <Terminal size={18} />
                {t('onboarding.installOllamaBtn')}
              </button>

              <button
                className="vscode-button-secondary"
                style={{ padding: '14px', fontSize: '15px', borderRadius: '6px', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px' }}
                onClick={() => setStep(5)} // step 5 = model registration
              >
                <Settings2 size={18} />
                {t('onboarding.configThirdPartyKey', 'Configure Third-Party API Key')}
              </button>

              <OpenRouterOAuthButton
                buttonClassName="vscode-button-secondary"
                buttonStyle={{ padding: '14px', fontSize: '15px', borderRadius: '6px', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', width: '100%' }}
                buttonLabel={t('openRouterOAuth.onboardingBtn', 'Sign in with OpenRouter (Free Model Included)')}
                onSuccess={handleOAuthConnected}
              />
            </div>
          </div>
        )}

        {step === 3 && (
          <div style={{ textAlign: 'center' }}>
            <h2 style={{ fontSize: '20px', fontWeight: 'bold', marginBottom: '16px', color: 'var(--vscode-text-fg)' }}>{t('onboarding.preparingOllama')}</h2>
            {ollamaStatus?.installed ? (
              ollamaStatus.is_supported ? (
                <div>
                  <CheckCircle size={48} color="var(--battery-good)" style={{ margin: '0 auto 16px auto' }} />
                  <p style={{ color: 'var(--vscode-text-fg)', marginBottom: '24px' }}>
                    {t('onboarding.ollamaInstalledCompatible', { version: ollamaStatus.version || 'unknown' })}
                  </p>
                  <button className="vscode-button" onClick={() => setStep(5)}>
                    {t('onboarding.continueToModels')}
                  </button>
                </div>
              ) : (
                <div>
                  <h3 style={{ color: 'var(--battery-low)', marginBottom: '16px' }}>{t('onboarding.updateRequired')}</h3>
                  <p style={{ color: 'var(--vscode-text-fg)', marginBottom: '24px', lineHeight: '1.5' }}>
                    {t('onboarding.updateMessage', { version: ollamaStatus.version })}
                  </p>
                  <button className="vscode-button" onClick={handleInstallOllama} disabled={isInstalling}>
                    {isInstalling ? t('onboarding.installStarted') : t('onboarding.downloadUpdateBtn')}
                  </button>
                  <button className="vscode-button-secondary" style={{ marginLeft: '12px' }} onClick={() => setStep(5)}>
                    {t('onboarding.ignoreStartBtn')}
                  </button>
                </div>
              )
            ) : (
              <div>
                <p style={{ color: 'var(--vscode-text-fg)', marginBottom: '24px' }}>{t('onboarding.installInstructions')}</p>
                <button className="vscode-button" onClick={handleInstallOllama} disabled={isInstalling}>
                  {isInstalling ? t('onboarding.installStarted') : t('onboarding.downloadInstallBtn')}
                </button>
              </div>
            )}
          </div>
        )}

        {step === 4 && (
          <div>
            <h2 style={{ fontSize: '20px', fontWeight: 'bold', marginBottom: '16px', color: 'var(--vscode-text-fg)' }}>{t('onboarding.manualInstallTitle')}</h2>
            <p style={{ color: 'var(--vscode-text-fg)', marginBottom: '16px', lineHeight: '1.5' }}>
              {t('onboarding.manualInstallMessage')}
            </p>
            <div style={{ backgroundColor: 'var(--vscode-terminal-bg)', padding: '12px', borderRadius: '6px', border: '1px solid var(--vscode-border)', marginBottom: '24px', fontFamily: 'monospace', color: 'var(--battery-good)' }}>
              curl -fsSL https://ollama.com/install.sh | sh
            </div>
            <button className="vscode-button" onClick={() => setStep(5)}>
              {t('onboarding.alreadyInstalledBtn')}
            </button>
          </div>
        )}

        {step === 5 && (
          <div>
            <h2 style={{ fontSize: '20px', fontWeight: 'bold', marginBottom: '8px', color: 'var(--vscode-text-fg)' }}>{t('onboarding.registerModelTitle')}</h2>
            <p style={{ color: 'var(--vscode-text-fg)', marginBottom: '20px', fontSize: '14px', lineHeight: '1.5' }}>
              {t('onboarding.registerModelMessage')}
            </p>

            {/* Auto-discovery of whatever the user actually has installed locally,
                so no model name needs to be typed or suggested. */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '16px' }}>
              <button
                type="button"
                className="vscode-button-secondary"
                onClick={handleDiscoverLocalOllama}
                disabled={isDiscovering}
              >
                <RefreshCw size={14} />
                {isDiscovering
                  ? t('editModelsModal.loadingLocalOllama')
                  : t('editModelsModal.loadLocalOllama')}
              </button>
            </div>

            {discoveryResult && (
              <div role="status" style={{ padding: '8px', marginBottom: '16px', border: '1px solid var(--vscode-widget-border)', background: 'var(--vscode-input-background)', color: 'var(--vscode-descriptionForeground)', fontSize: '12px' }}>
                {discoveryResultText()}
              </div>
            )}

            <div style={{ border: '1px solid var(--vscode-border)', borderRadius: '6px', padding: '4px 12px 12px 12px', marginBottom: '16px' }}>
              <ModelForm
                existingModels={catalogModels}
                connections={catalogConnections}
                onSaveConnection={saveConnection}
                onOAuthConnected={handleOAuthConnected}
                onSubmit={handleRegisterModel}
                actions={(
                  <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px', padding: '0 16px 8px 16px' }}>
                    <button type="submit" className="vscode-button">
                      {t('onboarding.registerModelBtn')}
                    </button>
                  </div>
                )}
              />
            </div>

            {registerError && (
              <div role="alert" style={{ padding: '8px', marginBottom: '16px', border: '1px solid rgba(244, 135, 113, 0.35)', background: 'rgba(244, 135, 113, 0.12)', color: 'var(--vscode-errorForeground)', fontSize: '12px' }}>
                {t('onboarding.modelRegisterFailed', { error: registerError })}
              </div>
            )}

            {savedModelId && (
              <div role="status" style={{ padding: '8px', marginBottom: '16px', border: '1px solid rgba(74, 222, 128, 0.35)', background: 'rgba(74, 222, 128, 0.12)', color: 'var(--battery-good)', fontSize: '12px' }}>
                {t('onboarding.modelRegistered', { id: savedModelId })}
              </div>
            )}

            <div className="flex flex-col" style={{ gap: '4px', marginBottom: '24px' }}>
              <label style={{ fontSize: '12px', color: 'var(--vscode-text-fg)' }}>{t('onboarding.pilotModelLabel')}</label>
              <ModelSelect
                value={pilotModel}
                onChange={setPilotModel}
                globalModels={catalogModels}
                showActions={false}
                placeholder={t('onboarding.pilotModelNone')}
                style={{ width: '100%' }}
              />
              <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>{t('onboarding.pilotModelHint')}</span>
            </div>

            <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end' }}>
              <button className="vscode-button-secondary" onClick={() => createPilotProject('')}>
                {t('onboarding.skipModelBtn')}
              </button>
              <button className="vscode-button" onClick={() => createPilotProject(pilotModel)}>
                {t('onboarding.createPilotBtn')}
              </button>
            </div>
          </div>
        )}

      </div>
    </div>
  );
}
