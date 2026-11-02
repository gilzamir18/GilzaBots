import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import ProviderConnectionForm from './ProviderConnectionForm';
import {
  extraModelParamsToRows,
  parseExtraModelParamRows,
} from '../../utils/modelExtraParams';

const NEW_CONNECTION_VALUE = '__new_connection__';

/**
 * Model registration fields shared by the Add Model modal and the onboarding
 * flow: pick an already-registered provider connection (label/API key/base
 * URL) and just type the model name, instead of retyping credentials for
 * every model.
 *
 * When there are no connections yet (a brand new install, before onboarding
 * has registered one) the inline "new connection" fields auto-expand so the
 * very first model can still be registered in one flow.
 *
 * Owns the catalog entry rules so they exist in one place only: required
 * connection/name, duplicate detection against the existing catalog scoped
 * to the selected connection, and id generation (`provider/name`, suffixed
 * with the connection id when that base id is already taken by a different
 * connection's model of the same name).
 */
export default function ModelForm({
  editingModel,
  existingModels = [],
  connections = [],
  onSubmit,
  onSaveConnection,
  onOAuthConnected,
  actions,
  formStyle,
}) {
  const { t } = useTranslation();

  const [connectionId, setConnectionId] = useState('');
  const [name, setName] = useState('');
  const [supportsThinking, setSupportsThinking] = useState(false);
  const [requiresSingleSystemMessage, setRequiresSingleSystemMessage] = useState(false);
  const [promptProfile, setPromptProfile] = useState('full');
  const [orchestratorPolicy, setOrchestratorPolicy] = useState('direct');
  const [numCtx, setNumCtx] = useState('');
  const [temperature, setTemperature] = useState('');
  const [maxTokens, setMaxTokens] = useState('');
  const [seed, setSeed] = useState('');
  const [topP, setTopP] = useState('');
  const [topK, setTopK] = useState('');
  const [minP, setMinP] = useState('');
  const [frequencyPenalty, setFrequencyPenalty] = useState('');
  const [presencePenalty, setPresencePenalty] = useState('');
  const [repetitionPenalty, setRepetitionPenalty] = useState('');
  const [reasoningEffort, setReasoningEffort] = useState('');
  const [extraModelParams, setExtraModelParams] = useState([]);
  const [supportsImageGeneration, setSupportsImageGeneration] = useState(false);
  const [imageRoute, setImageRoute] = useState('images_api');
  const [supportsSpeechSynthesis, setSupportsSpeechSynthesis] = useState(false);
  const [error, setError] = useState('');
  const [showNewConnection, setShowNewConnection] = useState(connections.length === 0);

  useEffect(() => {
    if (editingModel) {
      setConnectionId(editingModel.connection_id || '');
      setName(editingModel.name || '');
      setSupportsThinking(!!editingModel.supports_thinking);
      setRequiresSingleSystemMessage(!!editingModel.requires_single_system_message);
      setPromptProfile(editingModel.prompt_profile || 'full');
      setOrchestratorPolicy(editingModel.orchestrator_policy || 'direct');
      setNumCtx(editingModel.num_ctx ? String(editingModel.num_ctx) : '');
      setTemperature(editingModel.temperature !== undefined && editingModel.temperature !== null ? String(editingModel.temperature) : '');
      setMaxTokens(editingModel.max_tokens !== undefined && editingModel.max_tokens !== null ? String(editingModel.max_tokens) : '');
      setSeed(editingModel.seed !== undefined && editingModel.seed !== null ? String(editingModel.seed) : '');
      setTopP(editingModel.top_p !== undefined && editingModel.top_p !== null ? String(editingModel.top_p) : '');
      setTopK(editingModel.top_k !== undefined && editingModel.top_k !== null ? String(editingModel.top_k) : '');
      setMinP(editingModel.min_p !== undefined && editingModel.min_p !== null ? String(editingModel.min_p) : '');
      setFrequencyPenalty(editingModel.frequency_penalty !== undefined && editingModel.frequency_penalty !== null ? String(editingModel.frequency_penalty) : '');
      setPresencePenalty(editingModel.presence_penalty !== undefined && editingModel.presence_penalty !== null ? String(editingModel.presence_penalty) : '');
      setRepetitionPenalty(editingModel.repetition_penalty !== undefined && editingModel.repetition_penalty !== null ? String(editingModel.repetition_penalty) : '');
      setReasoningEffort(editingModel.reasoning_effort || '');
      setExtraModelParams(extraModelParamsToRows(editingModel.extra_model_params));
      setSupportsImageGeneration(!!editingModel.supports_image_generation);
      setImageRoute(editingModel.image_route || 'images_api');
      setSupportsSpeechSynthesis(!!editingModel.supports_speech_synthesis);
      setShowNewConnection(false);
    } else {
      setSupportsThinking(false);
      setRequiresSingleSystemMessage(false);
      setPromptProfile('full');
      setOrchestratorPolicy('direct');
      setNumCtx('');
      setTemperature('');
      setMaxTokens('');
      setSeed('');
      setTopP('');
      setTopK('');
      setMinP('');
      setFrequencyPenalty('');
      setPresencePenalty('');
      setRepetitionPenalty('');
      setReasoningEffort('');
      setExtraModelParams([]);
      setSupportsImageGeneration(false);
      setImageRoute('images_api');
      setShowNewConnection(connections.length === 0);
    }
  }, [editingModel]);

  useEffect(() => {
    if (!editingModel && connections.length === 0) {
      setShowNewConnection(true);
    }
  }, [connections.length, editingModel]);

  const reset = () => {
    setConnectionId('');
    setName('');
    setSupportsThinking(false);
    setRequiresSingleSystemMessage(false);
    setPromptProfile('full');
    setNumCtx('');
    setTemperature('');
    setMaxTokens('');
    setSeed('');
    setTopP('');
    setTopK('');
    setMinP('');
    setFrequencyPenalty('');
    setPresencePenalty('');
    setRepetitionPenalty('');
    setReasoningEffort('');
    setExtraModelParams([]);
    setSupportsImageGeneration(false);
    setImageRoute('images_api');
    setError('');
    setShowNewConnection(connections.length === 0);
  };

  const handleConnectionChange = (e) => {
    const val = e.target.value;
    if (val === NEW_CONNECTION_VALUE) {
      setShowNewConnection(true);
      setConnectionId('');
    } else {
      setShowNewConnection(false);
      setConnectionId(val);
    }
  };

  const handleNewConnectionSubmit = async (connectionData) => {
    setError('');
    const result = await onSaveConnection(connectionData);
    if (!result || result.ok === false) {
      setError(result?.error || t('connectionForm.requiredError'));
      return;
    }
    setConnectionId(connectionData.id);
    setShowNewConnection(false);
    if (result.added_model) {
      onOAuthConnected?.({ connection: connectionData, default_model: result.added_model, added_default_model: true });
    }
  };

  const handleInlineOAuthConnected = (result) => {
    setError('');
    if (result?.connection?.id) {
      setConnectionId(result.connection.id);
      setShowNewConnection(false);
    }
    onOAuthConnected?.(result);
  };

  const selectedConnection = connections.find(c => c.id === connectionId);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');

    const trimmedName = name.trim();

    if (!connectionId || !trimmedName) {
      setError(t('modelForm.requiredError'));
      return;
    }

    const duplicate = existingModels.some(model =>
      model.id !== editingModel?.id &&
      model.connection_id === connectionId &&
      model.name === trimmedName
    );

    if (duplicate) {
      setError(t('modelForm.duplicateError'));
      return;
    }

    const trimmedNumCtx = numCtx.trim();
    if (trimmedNumCtx && (!/^\d+$/.test(trimmedNumCtx) || Number(trimmedNumCtx) < 1)) {
      setError(t('modelForm.numCtxError'));
      return;
    }

    const trimmedTemperature = temperature.trim().replace(',', '.');
    if (trimmedTemperature !== '') {
      const val = Number(trimmedTemperature);
      if (isNaN(val) || val < 0.0 || val > 2.0) {
        setError(t('modelForm.temperatureError', 'Temperature must be a number between 0.0 and 2.0.'));
        return;
      }
    }

    const trimmedMaxTokens = maxTokens.trim();
    if (trimmedMaxTokens !== '') {
      if (!/^\d+$/.test(trimmedMaxTokens) || Number(trimmedMaxTokens) < 1) {
        setError(t('modelForm.maxTokensError', 'Max tokens must be a positive whole number.'));
        return;
      }
    }

    const trimmedSeed = seed.trim();
    if (trimmedSeed !== '') {
      if (!/^\d+$/.test(trimmedSeed) || Number(trimmedSeed) < 0) {
        setError(t('modelForm.seedError', 'Seed must be a non-negative whole number.'));
        return;
      }
    }

    const trimmedTopP = topP.trim().replace(',', '.');
    if (trimmedTopP !== '') {
      const val = Number(trimmedTopP);
      if (isNaN(val) || val < 0.0 || val > 1.0) {
        setError(t('modelForm.topPError', 'Top P must be a number between 0.0 and 1.0.'));
        return;
      }
    }

    const trimmedTopK = topK.trim();
    if (trimmedTopK !== '') {
      if (!/^\d+$/.test(trimmedTopK) || Number(trimmedTopK) < 1) {
        setError(t('modelForm.topKError', 'Top K must be a positive whole number.'));
        return;
      }
    }

    const trimmedMinP = minP.trim().replace(',', '.');
    if (trimmedMinP !== '') {
      const val = Number(trimmedMinP);
      if (isNaN(val) || val < 0.0 || val > 1.0) {
        setError(t('modelForm.minPError', 'Min P must be a number between 0.0 and 1.0.'));
        return;
      }
    }

    const trimmedFrequencyPenalty = frequencyPenalty.trim().replace(',', '.');
    if (trimmedFrequencyPenalty !== '') {
      const val = Number(trimmedFrequencyPenalty);
      if (isNaN(val) || val < -2.0 || val > 2.0) {
        setError(t('modelForm.frequencyPenaltyError', 'Frequency penalty must be a number between -2.0 and 2.0.'));
        return;
      }
    }

    const trimmedPresencePenalty = presencePenalty.trim().replace(',', '.');
    if (trimmedPresencePenalty !== '') {
      const val = Number(trimmedPresencePenalty);
      if (isNaN(val) || val < -2.0 || val > 2.0) {
        setError(t('modelForm.presencePenaltyError', 'Presence penalty must be a number between -2.0 and 2.0.'));
        return;
      }
    }

    const trimmedRepetitionPenalty = repetitionPenalty.trim().replace(',', '.');
    if (trimmedRepetitionPenalty !== '') {
      const val = Number(trimmedRepetitionPenalty);
      if (isNaN(val) || val < 0.0) {
        setError(t('modelForm.repetitionPenaltyError', 'Repetition penalty must be a non-negative number.'));
        return;
      }
    }

    const parsedExtraParams = parseExtraModelParamRows(extraModelParams);
    if (parsedExtraParams.error) {
      setError(t(
        `modelForm.extraParams.errors.${parsedExtraParams.error.code}`,
        { name: parsedExtraParams.error.name || '' },
      ));
      return;
    }

    const provider = selectedConnection?.provider || '';
    const baseId = `${provider}/${trimmedName}`;
    const id = existingModels.some(model => model.id !== editingModel?.id && model.id === baseId)
      ? `${baseId}#${connectionId}`
      : baseId;

    const result = await onSubmit({
      id,
      previous_id: editingModel?.id,
      connection_id: connectionId,
      name: trimmedName,
      supports_thinking: supportsThinking,
      requires_single_system_message: requiresSingleSystemMessage,
      prompt_profile: promptProfile,
      orchestrator_policy: orchestratorPolicy,
      num_ctx: trimmedNumCtx ? Number(trimmedNumCtx) : null,
      temperature: trimmedTemperature !== '' ? Number(trimmedTemperature) : null,
      max_tokens: trimmedMaxTokens !== '' ? Number(trimmedMaxTokens) : null,
      seed: trimmedSeed !== '' ? Number(trimmedSeed) : null,
      top_p: trimmedTopP !== '' ? Number(trimmedTopP) : null,
      top_k: trimmedTopK !== '' ? Number(trimmedTopK) : null,
      min_p: trimmedMinP !== '' ? Number(trimmedMinP) : null,
      frequency_penalty: trimmedFrequencyPenalty !== '' ? Number(trimmedFrequencyPenalty) : null,
      presence_penalty: trimmedPresencePenalty !== '' ? Number(trimmedPresencePenalty) : null,
      repetition_penalty: trimmedRepetitionPenalty !== '' ? Number(trimmedRepetitionPenalty) : null,
      reasoning_effort: reasoningEffort.trim() ? reasoningEffort.trim() : null,
      extra_model_params: parsedExtraParams.params,
      supports_image_generation: supportsImageGeneration,
      image_route: supportsImageGeneration ? imageRoute : '',
      supports_speech_synthesis: supportsSpeechSynthesis,
      // One route exists today, so it is implied rather than asked for. The
      // field is still written, because the catalog is what a future engine
      // plugs into (models_store.SPEECH_ROUTES).
      speech_route: supportsSpeechSynthesis ? 'audio_speech' : '',
    }, { reset });
    if (result?.ok === false) {
      setError(result.error === 'model_save_failed'
        ? t('modelForm.saveError')
        : result.error);
    }
  };

  return (
    <form onSubmit={handleSubmit} style={formStyle}>
      <div className="vscode-modal-content" style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {error && (
          <div style={{ color: 'var(--vscode-errorForeground)', fontSize: '12px', padding: '8px', background: 'rgba(244,135,113,0.1)', borderRadius: '4px' }}>
            {error}
          </div>
        )}

        <div className="vscode-form-group">
          <label>{t('modelForm.connectionLabel')}</label>
          {connections.length > 0 && (
            <select
              className="vscode-settings-input"
              value={showNewConnection ? NEW_CONNECTION_VALUE : connectionId}
              onChange={handleConnectionChange}
            >
              <option value="">{t('modelForm.connectionPlaceholder')}</option>
              {connections.map(c => (
                <option key={c.id} value={c.id}>
                  {c.label || c.provider} ({c.provider})
                </option>
              ))}
              <option value={NEW_CONNECTION_VALUE}>{t('modelForm.newConnectionOption')}</option>
            </select>
          )}

          {showNewConnection && (
            <div style={{ marginTop: '12px', padding: '12px', border: '1px dashed var(--vscode-widget-border)', borderRadius: '4px' }}>
              <ProviderConnectionForm
                asForm={false}
                existingConnections={connections}
                onSubmit={handleNewConnectionSubmit}
                onOAuthConnected={handleInlineOAuthConnected}
                onCancel={connections.length > 0 ? () => setShowNewConnection(false) : undefined}
                submitLabel={t('modelForm.saveConnectionBtn')}
              />
            </div>
          )}
        </div>

        <div className="vscode-form-group">
          <label>{t('modelForm.modelNameLabel')}</label>
          <input
            type="text"
            className="vscode-settings-input"
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder={t('modelForm.modelNamePlaceholder')}
          />
        </div>

        <div className="vscode-form-group">
          <label>{t('modelForm.promptProfileLabel')}</label>
          <div role="radiogroup" aria-label={t('modelForm.promptProfileLabel')} style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
            {['full', 'light'].map(profile => (
              <button
                key={profile}
                type="button"
                role="radio"
                aria-checked={promptProfile === profile}
                onClick={() => setPromptProfile(profile)}
                className={promptProfile === profile ? 'vscode-button' : 'vscode-button-secondary'}
                style={{ flex: 1, fontWeight: promptProfile === profile ? 600 : 400 }}
              >
                {t(`modelForm.promptProfile.${profile}`)}
              </button>
            ))}
          </div>
          <span
            className="vscode-form-hint"
            style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}
          >
            {t(`modelForm.promptProfile.${promptProfile}Hint`)}
          </span>
        </div>

        <div className="vscode-form-group">
          <label>{t('modelForm.orchestratorPolicyLabel')}</label>
          <div role="radiogroup" aria-label={t('modelForm.orchestratorPolicyLabel')} style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
            {['direct', 'delegate'].map(policy => (
              <button
                key={policy}
                type="button"
                role="radio"
                aria-checked={orchestratorPolicy === policy}
                onClick={() => setOrchestratorPolicy(policy)}
                className={orchestratorPolicy === policy ? 'vscode-button' : 'vscode-button-secondary'}
                style={{ flex: 1, fontWeight: orchestratorPolicy === policy ? 600 : 400 }}
              >
                {t(`modelForm.orchestratorPolicy.${policy}`)}
              </button>
            ))}
          </div>
          <span
            className="vscode-form-hint"
            style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}
          >
            {t(`modelForm.orchestratorPolicy.${orchestratorPolicy}Hint`)}
          </span>
        </div>

        <div className="vscode-form-group">
          <label>{t('modelForm.numCtxLabel')}</label>
          <input
            type="number"
            min="1"
            step="1"
            className="vscode-settings-input"
            value={numCtx}
            onChange={e => setNumCtx(e.target.value)}
            placeholder={t('modelForm.numCtxPlaceholder')}
          />
          <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
            {t('modelForm.numCtxHint')}
          </span>
        </div>

        <details className="vscode-form-group">
          <summary style={{ cursor: 'pointer', fontWeight: 600 }}>
            {t('modelForm.modelParamsLabel', 'Model inference parameters')}
          </summary>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '10px' }}>
            <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
              {t('modelForm.modelParamsHint', 'Optional inference and sampling settings configured for this model. Leave empty to use provider defaults.')}
            </span>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label style={{ fontSize: '11px' }}>{t('modelForm.temperatureLabel', 'Temperature')}</label>
                <input
                  type="number"
                  step="0.1"
                  min="0"
                  max="2"
                  className="vscode-settings-input"
                  value={temperature}
                  onChange={e => setTemperature(e.target.value)}
                  placeholder={t('modelForm.temperaturePlaceholder', '0.7')}
                />
              </div>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label style={{ fontSize: '11px' }}>{t('modelForm.maxTokensLabel', 'Max tokens')}</label>
                <input
                  type="number"
                  min="1"
                  step="1"
                  className="vscode-settings-input"
                  value={maxTokens}
                  onChange={e => setMaxTokens(e.target.value)}
                  placeholder={t('modelForm.maxTokensPlaceholder', 'Unlimited')}
                />
              </div>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label style={{ fontSize: '11px' }}>{t('modelForm.seedLabel', 'Seed')}</label>
                <input
                  type="number"
                  min="0"
                  step="1"
                  className="vscode-settings-input"
                  value={seed}
                  onChange={e => setSeed(e.target.value)}
                  placeholder={t('modelForm.seedPlaceholder', 'None')}
                />
              </div>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label style={{ fontSize: '11px' }}>{t('modelForm.topPLabel', 'Top P')}</label>
                <input
                  type="number"
                  step="0.05"
                  min="0"
                  max="1"
                  className="vscode-settings-input"
                  value={topP}
                  onChange={e => setTopP(e.target.value)}
                  placeholder={t('modelForm.topPPlaceholder', '1.0')}
                />
              </div>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label style={{ fontSize: '11px' }}>{t('modelForm.topKLabel', 'Top K')}</label>
                <input
                  type="number"
                  min="1"
                  step="1"
                  className="vscode-settings-input"
                  value={topK}
                  onChange={e => setTopK(e.target.value)}
                  placeholder={t('modelForm.topKPlaceholder', 'None')}
                />
              </div>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label style={{ fontSize: '11px' }}>{t('modelForm.minPLabel', 'Min P')}</label>
                <input
                  type="number"
                  step="0.05"
                  min="0"
                  max="1"
                  className="vscode-settings-input"
                  value={minP}
                  onChange={e => setMinP(e.target.value)}
                  placeholder={t('modelForm.minPPlaceholder', '0.0')}
                />
              </div>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label style={{ fontSize: '11px' }}>{t('modelForm.frequencyPenaltyLabel', 'Frequency penalty')}</label>
                <input
                  type="number"
                  step="0.1"
                  min="-2"
                  max="2"
                  className="vscode-settings-input"
                  value={frequencyPenalty}
                  onChange={e => setFrequencyPenalty(e.target.value)}
                  placeholder={t('modelForm.frequencyPenaltyPlaceholder', '0.0')}
                />
              </div>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label style={{ fontSize: '11px' }}>{t('modelForm.presencePenaltyLabel', 'Presence penalty')}</label>
                <input
                  type="number"
                  step="0.1"
                  min="-2"
                  max="2"
                  className="vscode-settings-input"
                  value={presencePenalty}
                  onChange={e => setPresencePenalty(e.target.value)}
                  placeholder={t('modelForm.presencePenaltyPlaceholder', '0.0')}
                />
              </div>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label style={{ fontSize: '11px' }}>{t('modelForm.repetitionPenaltyLabel', 'Repetition penalty')}</label>
                <input
                  type="number"
                  step="0.1"
                  min="0"
                  className="vscode-settings-input"
                  value={repetitionPenalty}
                  onChange={e => setRepetitionPenalty(e.target.value)}
                  placeholder={t('modelForm.repetitionPenaltyPlaceholder', '1.0')}
                />
              </div>
              <div className="flex flex-col" style={{ gap: '4px' }}>
                <label style={{ fontSize: '11px' }}>{t('modelForm.reasoningEffortLabel', 'Reasoning effort')}</label>
                <select
                  className="vscode-settings-input"
                  value={reasoningEffort}
                  onChange={e => setReasoningEffort(e.target.value)}
                >
                  <option value="">{t('modelForm.reasoningEffortPlaceholder', 'Default')}</option>
                  <option value="none">{t('common.optionNone', 'None')}</option>
                  <option value="low">{t('common.optionLow', 'Low')}</option>
                  <option value="medium">{t('common.optionMedium', 'Medium')}</option>
                  <option value="high">{t('common.optionHigh', 'High')}</option>
                  <option value="xhigh">{t('common.optionXHigh', 'Extra high')}</option>
                </select>
              </div>
            </div>
          </div>
        </details>

        <details className="vscode-form-group">
          <summary style={{ cursor: 'pointer', fontWeight: 600 }}>
            {t('modelForm.extraParams.label')}
          </summary>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '10px' }}>
            <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
              {t('modelForm.extraParams.hint')}
            </span>
            {extraModelParams.map((param, index) => (
              <div key={index} style={{ display: 'grid', gridTemplateColumns: 'minmax(120px, 1fr) minmax(150px, 1.4fr) auto', gap: '8px' }}>
                <input
                  type="text"
                  className="vscode-settings-input"
                  aria-label={t('modelForm.extraParams.nameLabel')}
                  value={param.name}
                  onChange={e => setExtraModelParams(rows => rows.map((row, rowIndex) => (
                    rowIndex === index ? { ...row, name: e.target.value } : row
                  )))}
                  placeholder={t('modelForm.extraParams.namePlaceholder')}
                />
                <input
                  type="text"
                  className="vscode-settings-input"
                  aria-label={t('modelForm.extraParams.valueLabel')}
                  value={param.value}
                  onChange={e => setExtraModelParams(rows => rows.map((row, rowIndex) => (
                    rowIndex === index ? { ...row, value: e.target.value } : row
                  )))}
                  placeholder={t('modelForm.extraParams.valuePlaceholder')}
                />
                <button
                  type="button"
                  className="vscode-button-secondary"
                  onClick={() => setExtraModelParams(rows => rows.filter((_, rowIndex) => rowIndex !== index))}
                  aria-label={t('modelForm.extraParams.remove')}
                  title={t('modelForm.extraParams.remove')}
                >
                  ×
                </button>
              </div>
            ))}
            <button
              type="button"
              className="vscode-button-secondary"
              onClick={() => setExtraModelParams(rows => [...rows, { name: '', value: '' }])}
              style={{ alignSelf: 'flex-start' }}
            >
              {t('modelForm.extraParams.add')}
            </button>
          </div>
        </details>

        <div className="vscode-form-group">
          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={supportsThinking}
              onChange={e => setSupportsThinking(e.target.checked)}
            />
            {t('modelForm.supportsThinkingLabel')}
          </label>
          <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
            {t('modelForm.supportsThinkingHint')}
          </span>
        </div>

        <div className="vscode-form-group">
          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={requiresSingleSystemMessage}
              onChange={e => setRequiresSingleSystemMessage(e.target.checked)}
            />
            {t('modelForm.requiresSingleSystemMessageLabel')}
          </label>
          <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
            {t('modelForm.requiresSingleSystemMessageHint')}
          </span>
        </div>

        <div className="vscode-form-group">
          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={supportsImageGeneration}
              onChange={e => setSupportsImageGeneration(e.target.checked)}
            />
            {t('modelForm.supportsImageGenerationLabel')}
          </label>
          <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
            {t('modelForm.supportsImageGenerationHint')}
          </span>
          {supportsImageGeneration && (
            <div style={{ marginTop: '8px' }}>
              <label>{t('modelForm.imageRouteLabel')}</label>
              <select
                className="vscode-settings-input"
                value={imageRoute}
                onChange={e => setImageRoute(e.target.value)}
              >
                <option value="images_api">{t('modelForm.imageRoute.images_api')}</option>
                <option value="chat_multimodal">{t('modelForm.imageRoute.chat_multimodal')}</option>
              </select>
              <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                {t(`modelForm.imageRoute.${imageRoute}Hint`)}
              </span>
            </div>
          )}
        </div>

        <div className="vscode-form-group">
          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={supportsSpeechSynthesis}
              onChange={e => setSupportsSpeechSynthesis(e.target.checked)}
            />
            {t('modelForm.supportsSpeechSynthesisLabel', 'Speech synthesis (text to speech)')}
          </label>
          <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
            {t('modelForm.supportsSpeechSynthesisHint', 'This entry answers /v1/audio/speech and is used only for pronunciation, never for chat. Covers hosted providers and any OpenAI-compatible local server (Kokoro-FastAPI, openedai-speech, LocalAI, Piper) registered with its api_base.')}
          </span>
        </div>

        {(selectedConnection && name) && (
          <div style={{ fontSize: '12px', color: 'var(--vscode-text-muted)', marginTop: '4px' }}>
            {t('modelForm.generatedId')}: <strong style={{ color: 'var(--vscode-text-fg)' }}>{selectedConnection.provider}/{name.trim()}</strong>
          </div>
        )}
      </div>

      {actions}
    </form>
  );
}
