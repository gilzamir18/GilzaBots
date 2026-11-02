import React from 'react';
import { X, Settings } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  UI_SCALE_MAX,
  UI_SCALE_MIN,
  UI_SCALE_PRESETS,
  UI_SCALE_STEP,
  presetForScale,
} from '../../utils/uiScale';
import { useCustomDialog } from './CustomDialogProvider';
import SpeechVoiceManager from './SpeechVoiceManager.jsx';
import i18n from '../../i18n/index.js';
import { setPreference } from '../../utils/preferences';
import { writeClipboard } from '../../utils/clipboard';
import { openExternal } from '../../utils/openExternal';

// Target languages offered by the "Translate to" setting. The values are the
// locale codes persisted by /api/settings/translation; "" follows the UI
// language and TRANSLATE_CUSTOM switches the field to free-text entry.
const TRANSLATE_LANGUAGES = [
  { value: 'pt-BR', labelKey: 'settingsModal.translateLangPtBR', label: 'Portugu\u00eas (Brasil)' },
  { value: 'en', labelKey: 'settingsModal.translateLangEn', label: 'English' },
  { value: 'es', labelKey: 'settingsModal.translateLangEs', label: 'Espa\u00f1ol' },
  { value: 'fr', labelKey: 'settingsModal.translateLangFr', label: 'Fran\u00e7ais' },
  { value: 'de', labelKey: 'settingsModal.translateLangDe', label: 'Deutsch' },
  { value: 'it', labelKey: 'settingsModal.translateLangIt', label: 'Italiano' },
  { value: 'ja', labelKey: 'settingsModal.translateLangJa', label: '\u65e5\u672c\u8a9e' },
  { value: 'zh', labelKey: 'settingsModal.translateLangZh', label: '\u4e2d\u6587' },
  { value: 'ru', labelKey: 'settingsModal.translateLangRu', label: '\u0420\u0443\u0441\u0441\u043a\u0438\u0439' },
];

const TRANSLATE_CUSTOM = '__custom__';
const DIRECT_INSTALL_COMMAND = 'curl -fsSL https://raw.githubusercontent.com/opalacoderdev/OpalaTex/master/install.sh -o opalatex-install.sh && bash opalatex-install.sh';
const FEEDBACK_ISSUE_URL = 'https://github.com/opalacoderdev/OpalaTex/issues/new';

// IDE global settings modal (theme, font size, tab size, word wrap, minimap, optional deps).
export default function SettingsModal({
  onClose,
  settingsTab,
  setSettingsTab,
  theme,
  setTheme,
  uiScale,
  applyUiScale,
  editorFontSize,
  setEditorFontSize,
  editorTabSize,
  setEditorTabSize,
  editorWordWrap,
  setEditorWordWrap,
  editorMinimap,
  setEditorMinimap,
  isInstallingDeps,
  installDepsStatus,
  installDepsLog,
  onInstallDeps,
  onLanguageChange,
  ephemeralParams,
  setEphemeralParams,
  panelMaxLines,
  setPanelMaxLines,
  thoughtContextTokens,
  chatThoughtPreviewTokens,
  onChatThoughtPreviewTokensChange,
  onThoughtContextTokensChange, onSpeechSettingsChange, }) {
  const { t } = useTranslation();
  const { showAlert, showConfirm } = useCustomDialog();
  const [selectedLang, setSelectedLang] = React.useState('');
  const [opalatexHome, setOpalaTexHome] = React.useState('');
  const [opalatexHomeError, setOpalaTexHomeError] = React.useState('');
  const [draftSynctexEnabled, setDraftSynctexEnabled] = React.useState(false);
  const [webengineGpu, setWebengineGpu] = React.useState('auto');
  const [workspaceHiddenExtensions, setWorkspaceHiddenExtensions] = React.useState([]);
  const [tectonicInstallMessage, setTectonicInstallMessage] = React.useState('');
  const [pandocInstallMessage, setPandocInstallMessage] = React.useState('');
  // Edited as text and saved on blur: clamping each keystroke would turn a
  // partially typed number into the minimum.
  const [thoughtTokensInput, setThoughtTokensInput] = React.useState(String(thoughtContextTokens ?? 32000));
  React.useEffect(() => { setThoughtTokensInput(String(thoughtContextTokens ?? 32000)); }, [thoughtContextTokens]);
  const [chatPreviewTokensInput, setChatPreviewTokensInput] = React.useState(String(chatThoughtPreviewTokens ?? 1000));
  React.useEffect(() => { setChatPreviewTokensInput(String(chatThoughtPreviewTokens ?? 1000)); }, [chatThoughtPreviewTokens]);
  const [promptEvolutionIterations, setPromptEvolutionIterations] = React.useState(1);
  const [promptEvolutionMaxTokens, setPromptEvolutionMaxTokens] = React.useState(4096);
  const [translateTargetLang, setTranslateTargetLang] = React.useState('');
  const [isCustomTranslateLang, setIsCustomTranslateLang] = React.useState(false);
  const [imageGen, setImageGen] = React.useState({ enabled: true, model: '', size: '1024x1024', output_dir: 'figures' });
  const [imageModels, setImageModels] = React.useState([]);
  const [speech, setSpeech] = React.useState({
    enabled: false, engine: 'local', local_voice: '',
    model: '', voice: '', response_format: 'mp3', speed: 1.0,
  });
  const [speechModels, setSpeechModels] = React.useState([]);
  const [speechFormats, setSpeechFormats] = React.useState(['mp3', 'opus', 'aac', 'flac', 'wav']);
  const [dictation, setDictation] = React.useState({
    enabled: false, model: 'small', language: '', compute_type: 'int8',
  });
  const [dictationModels, setDictationModels] = React.useState([]);
  const [dictationBusy, setDictationBusy] = React.useState('');
  const [isRestarting, setIsRestarting] = React.useState(false);
  const [runtimeInfo, setRuntimeInfo] = React.useState({ platform: '', running_in_snap: false, version: '0.2.31' });
  const [sandbox, setSandbox] = React.useState({
    enabled: false,
    image: 'python:3.12-slim',
    network: 'bridge',
  });
  const [sandboxStatus, setSandboxStatus] = React.useState(null);
  const [sandboxBusy, setSandboxBusy] = React.useState(false);
  const [sandboxMessage, setSandboxMessage] = React.useState('');

  const activeTab = (settingsTab === 'preferences' || !['general', 'dependencies', 'about'].includes(settingsTab))
    ? 'general'
    : settingsTab;

  React.useEffect(() => {
    fetch('/api/settings/language')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => { if (cfg?.lang !== undefined) setSelectedLang(cfg.lang); })
      .catch(() => { });

    fetch('/api/settings/opalatexhome')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data) return;
        // A configured directory the backend could not open is reported here
        // rather than silently replaced by the fallback it is actually using.
        setOpalaTexHomeError(data.error || '');
        const shown = data.error ? (data.configured_path || data.path) : data.path;
        if (shown) setOpalaTexHome(shown);
      })
      .catch(() => { });

    fetch('/api/settings/latex')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        if (cfg?.draft_synctex_enabled !== undefined) {
          setDraftSynctexEnabled(Boolean(cfg.draft_synctex_enabled));
        }
      })
      .catch(() => { });

    fetch('/api/settings/workspace')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        if (Array.isArray(cfg?.hidden_file_extensions)) {
          setWorkspaceHiddenExtensions(cfg.hidden_file_extensions);
        }
      })
      .catch(() => { });

    fetch('/api/settings/graphics')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        if (cfg?.webengine_gpu) setWebengineGpu(cfg.webengine_gpu);
      })
      .catch(() => { });

    fetch('/api/settings/image-generation')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        if (!cfg) return;
        const { models, routes, ...rest } = cfg;
        setImageGen(prev => ({ ...prev, ...rest }));
        setImageModels(Array.isArray(models) ? models : []);
      })
      .catch(() => { });

    fetch('/api/settings/speech')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        if (!cfg) return;
        const { models, routes, formats, problem, ...rest } = cfg;
        setSpeech(prev => ({ ...prev, ...rest }));
        setSpeechModels(Array.isArray(models) ? models : []);
        if (Array.isArray(formats) && formats.length) setSpeechFormats(formats);
      })
      .catch(() => { });

    fetch('/api/settings/transcription')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        if (!cfg) return;
        const { models, compute_types, downloads, problem, ...rest } = cfg;
        setDictation(prev => ({ ...prev, ...rest }));
        setDictationModels(Array.isArray(models) ? models : []);
      })
      .catch(() => { });

    fetch('/api/settings/translation')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        if (!cfg) return;
        const saved = String(cfg.translate_target_lang || '');
        setTranslateTargetLang(saved);
        setIsCustomTranslateLang(Boolean(saved) && !TRANSLATE_LANGUAGES.some(l => l.value === saved));
      })
      .catch(() => { });

    fetch('/api/settings/prompt-evolution')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        if (cfg?.prompt_evolution_iterations !== undefined) {
          setPromptEvolutionIterations(Math.max(1, Number(cfg.prompt_evolution_iterations) || 1));
        }
        if (cfg?.prompt_evolution_max_tokens !== undefined) {
          setPromptEvolutionMaxTokens(Math.max(1, Number(cfg.prompt_evolution_max_tokens) || 4096));
        }
      })
      .catch(() => { });

    fetch('/api/app/environment')
      .then(r => r.ok ? r.json() : null)
      .then(info => {
        if (info) setRuntimeInfo(previous => ({ ...previous, ...info }));
      })
      .catch(() => { });

    fetch('/api/settings/sandbox')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        if (!cfg) return;
        const { status, ...rest } = cfg;
        setSandbox(prev => ({ ...prev, ...rest }));
        if (status) setSandboxStatus(status);
      })
      .catch(() => { });
  }, []);

  // Restart-only settings are read once, before the window exists, so the user
  // is asked right away; declining keeps the saved value with a reminder that
  // it is still pending. Shared by every setting with that shape.
  const performRestart = async () => {
    setIsRestarting(true);
    try {
      const res = await fetch('/api/app/restart', { method: 'POST' });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || body?.error) {
        setIsRestarting(false);
        await showAlert(t('settingsModal.restartError') + (body?.error || res.status));
      }
      // On success the server exits and relaunches; this window closes with it.
    } catch (err) {
      setIsRestarting(false);
      await showAlert(t('settingsModal.restartError') + err);
    }
  };

  // QtWebEngine reads its graphics flags before the browser is created, so this
  // choice reaches the window on the next launch and not before.
  const saveGraphicsSettings = async (mode) => {
    setWebengineGpu(mode);
    let body = {};
    try {
      const res = await fetch('/api/settings/graphics', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ webengine_gpu: mode }),
      });
      body = await res.json().catch(() => ({}));
    } catch (err) {
      await showAlert(t('settingsModal.graphicsSaveError', 'Could not save the graphics setting: ') + err);
      return;
    }
    if (!body?.requiresRestart) return;

    const shouldRestart = await showConfirm(
      t('settingsModal.graphicsRestartConfirm', 'This setting only applies to a new app window. Restart OpalaTex now?')
    );
    if (!shouldRestart) {
      await showAlert(t('settingsModal.graphicsRestartAlert', 'The setting was saved and will apply the next time OpalaTex starts.'));
      return;
    }
    await performRestart();
  };

  const saveLatexSettings = (draftSynctexValue) => {
    fetch('/api/settings/latex', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft_synctex_enabled: draftSynctexValue }),
    }).catch(() => { });
  };

  const saveImageGenSettings = (next) => {
    setImageGen(next);
    fetch('/api/settings/image-generation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(next),
    }).catch(() => { });
  };

  // Readiness is recomputed by the backend on every one of these, so each
  // announces it: the chat's microphone gates its own presence on the answer
  // and has no other moment to re-read (App.jsx: speechSettingsSignal).
  const saveSpeechSettings = (next) => {
    setSpeech(next);
    fetch('/api/settings/speech', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(next),
    }).catch(() => { }).finally(() => onSpeechSettingsChange?.());
  };

  const saveDictationSettings = (next) => {
    setDictation(next);
    fetch('/api/settings/transcription', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(next),
    }).catch(() => { }).finally(() => onSpeechSettingsChange?.());
  };

  const runDictationModelAction = async (size, action) => {
    setDictationBusy(size);
    try {
      const res = await fetch('/api/transcription/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ size, action }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && Array.isArray(data.models)) setDictationModels(data.models);
    } finally {
      setDictationBusy('');
      // Downloading or removing a model is what flips readiness most often.
      onSpeechSettingsChange?.();
    }
  };

  const saveTranslateTargetLang = (value) => {
    fetch('/api/settings/translation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ translate_target_lang: value }),
    }).catch(() => { });
  };

  const saveThoughtContextTokens = (value) => {
    fetch('/api/settings/thoughts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ thought_context_tokens: value }),
    })
      .then(r => (r.ok ? r.json() : null))
      .then(cfg => { if (cfg?.thought_context_tokens !== undefined) onThoughtContextTokensChange?.(cfg.thought_context_tokens); })
      .catch(() => { });
  };

  const saveChatThoughtPreviewTokens = (value) => {
    fetch('/api/settings/thoughts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_thought_preview_tokens: value }),
    })
      .then(r => (r.ok ? r.json() : null))
      .then(cfg => { if (cfg?.chat_thought_preview_tokens !== undefined) onChatThoughtPreviewTokensChange?.(cfg.chat_thought_preview_tokens); })
      .catch(() => { });
  };

  const savePromptEvolutionSettings = (iterations, maxTokens) => {
    const numIterations = Math.max(1, Math.floor(Number(iterations) || 1));
    const numMaxTokens = Math.max(1, Math.min(65536, Math.floor(Number(maxTokens) || 4096)));
    fetch('/api/settings/prompt-evolution', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt_evolution_iterations: numIterations, prompt_evolution_max_tokens: numMaxTokens }),
    }).catch(() => { });
  };

  const saveSandboxSettings = (next) => {
    setSandbox(next);
    fetch('/api/settings/sandbox', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(next),
    })
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data?.status) setSandboxStatus(data.status);
      })
      .catch(() => { });
  };

  const handleVerifySandbox = async () => {
    setSandboxBusy(true);
    setSandboxMessage('');
    try {
      const res = await fetch('/api/settings/sandbox/verify', { method: 'POST' });
      const data = await res.json();
      if (data?.status) {
        setSandboxStatus(data.status);
        if (data.status.ready) {
          setSandboxMessage(t('settingsModal.sandboxReady', 'Docker sandbox is verified and operational.'));
        }
      }
    } catch (err) {
      setSandboxMessage(t('settingsModal.sandboxVerifyFailed', 'Verification failed: ') + String(err));
    } finally {
      setSandboxBusy(false);
    }
  };

  const handlePullSandboxImage = async () => {
    setSandboxBusy(true);
    setSandboxMessage('');
    try {
      const res = await fetch('/api/settings/sandbox/pull', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image: sandbox.image }),
      });
      const data = await res.json();
      if (data?.status) setSandboxStatus(data.status);
      if (data?.message) setSandboxMessage(data.message);
    } catch (err) {
      setSandboxMessage(t('settingsModal.sandboxPullFailed', 'Pull failed: ') + String(err));
    } finally {
      setSandboxBusy(false);
    }
  };

  const handleRecreateSandbox = async () => {
    setSandboxBusy(true);
    setSandboxMessage('');
    try {
      const res = await fetch('/api/settings/sandbox/recreate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectPath: '' }),
      });
      const data = await res.json();
      if (data?.message) setSandboxMessage(data.message);
    } catch (err) {
      setSandboxMessage(String(err));
    } finally {
      setSandboxBusy(false);
    }
  };

  // Saving OPALATEX_HOME only takes effect on the next launch, so the user is
  // asked right away whether to restart; declining leaves the saved value in
  // place with a reminder that it is still pending.
  const saveOpalaTexHome = async () => {
    let data;
    try {
      const res = await fetch('/api/settings/opalatexhome', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: opalatexHome })
      });
      data = await res.json().catch(() => ({}));
      if (!res.ok || data?.error) {
        const message = data?.error || String(res.status);
        setOpalaTexHomeError(message);
        await showAlert(t('settingsModal.globalDataDirError') + message);
        return;
      }
      setOpalaTexHomeError('');
    } catch (err) {
      setOpalaTexHomeError(String(err));
      await showAlert(t('settingsModal.globalDataDirError') + err);
      return;
    }

    if (!data?.requiresRestart) {
      await showAlert(t('settingsModal.globalDataDirSaved'));
      return;
    }

    const shouldRestart = await showConfirm(t('settingsModal.globalDataDirRestartConfirm'));
    if (!shouldRestart) {
      await showAlert(t('settingsModal.globalDataDirRestartAlert'));
      return;
    }

    await performRestart();
  };

  const updateEphemeralParam = (key, val) => {
    const updated = { ...ephemeralParams };
    if (val === '' || val === undefined) delete updated[key];
    else updated[key] = val;
    setEphemeralParams(updated);
    setPreference('ephemeralParams', JSON.stringify(updated));
  };

  const copyDirectInstallCommand = async () => {
    const copied = await writeClipboard(DIRECT_INSTALL_COMMAND);
    await showAlert(copied
      ? t('settingsModal.directInstallCopied')
      : t('settingsModal.directInstallCopyFailed'));
  };

  const openFeedbackIssue = async () => {
    const params = new URLSearchParams({
      title: t('settingsModal.feedbackIssueTitle'),
      body: t('settingsModal.feedbackIssueBody', {
        version: runtimeInfo.version || 'unknown',
        platform: runtimeInfo.platform || navigator.platform || 'unknown',
        distribution: runtimeInfo.running_in_snap
          ? t('settingsModal.feedbackDistributionSnap')
          : t('settingsModal.feedbackDistributionOther'),
      }),
    });
    const url = `${FEEDBACK_ISSUE_URL}?${params.toString()}`;
    if (!await openExternal(url)) {
      await showAlert(t('settingsModal.feedbackOpenFailed', { url }));
    }
  };

  return (
    <div className="vscode-modal-overlay">
      <div className="vscode-modal flex flex-col" style={{ width: '640px', maxHeight: 'calc(85 * var(--ui-vh))', padding: 0 }}>
        {/* Header */}
        <div className="vscode-modal-header titlebar">
          <div className="vscode-modal-header-title">
            <Settings size={16} />
            <span>{t('settingsModal.title')}</span>
          </div>
          <button className="vscode-modal-close" onClick={onClose} aria-label={t('common.close', 'Close')}>
            <X size={16} />
          </button>
        </div>

        {/* Tabs */}
        <div className="vscode-modal-tabs titlebar">
          <button
            className={`vscode-modal-tab${activeTab === 'general' ? ' active' : ''}`}
            onClick={() => setSettingsTab('general')}
          >
            {t('settingsModal.tabGeneral', 'General')}
          </button>
          <button
            className={`vscode-modal-tab${activeTab === 'dependencies' ? ' active' : ''}`}
            onClick={() => setSettingsTab('dependencies')}
          >
            {t('settingsModal.tabDependencies', 'Dependencies')}
          </button>
          <button
            className={`vscode-modal-tab${activeTab === 'about' ? ' active' : ''}`}
            onClick={() => setSettingsTab('about')}
          >
            {t('settingsModal.tabAbout')}
          </button>
        </div>

        {/* Content */}
        <div className="vscode-modal-content flex-1 overflow-y-auto flex flex-col" style={{ gap: '16px' }}>
          {activeTab === 'general' && (
            <>
              {/* Language */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.language')}</label>
                <select
                  value={selectedLang}
                  onChange={(e) => {
                    const newLang = e.target.value;
                    setSelectedLang(newLang);
                    const targetLang = newLang || (navigator.language.startsWith('pt') ? 'pt-BR' : 'en');
                    i18n.changeLanguage(targetLang);
                    onLanguageChange?.(newLang);
                  }}
                  className="vscode-settings-input"
                  style={{ width: '100%' }}
                >
                  <option value="">{t('settingsModal.languageSystem', 'System (automatic)')}</option>
                  <option value="pt-BR">{t('settingsModal.languagePtBR', 'Português (Brasil)')}</option>
                  <option value="en">{t('settingsModal.languageEn', 'English')}</option>
                </select>
              </div>

              {/* Theme */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.colorTheme')}</label>
                <select value={theme} onChange={(e) => setTheme(e.target.value)} className="vscode-settings-input" style={{ width: '100%' }}>
                  <option value="dark">{t('settingsModal.themeDark')}</option>
                  <option value="light">{t('settingsModal.themeLight')}</option>
                </select>
              </div>

              {/* Interface size (accessibility) — scales the whole interface,
                  not just text, so icons, padding and click targets grow with
                  it. The named steps cover the common case; the slider below
                  reaches further for those who need it. */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>
                  {t('settingsModal.interfaceSize', 'Interface size')}
                </label>
                <div className="flex" style={{ gap: '6px' }}>
                  {UI_SCALE_PRESETS.map((preset) => {
                    const isActive = presetForScale(uiScale)?.id === preset.id;
                    return (
                      <button
                        key={preset.id}
                        type="button"
                        onClick={() => applyUiScale(preset.scale)}
                        aria-pressed={isActive}
                        className="vscode-settings-input"
                        style={{
                          flex: 1,
                          cursor: 'pointer',
                          textAlign: 'center',
                          borderColor: isActive ? 'var(--vscode-accent)' : 'var(--vscode-input-border)',
                          background: isActive ? 'var(--vscode-list-activeSelectionBg)' : 'var(--vscode-input-bg)',
                          color: isActive ? 'var(--vscode-list-activeSelectionFg)' : 'var(--vscode-input-fg)',
                        }}
                      >
                        {t(`settingsModal.interfaceSize_${preset.id}`, preset.id)}
                      </button>
                    );
                  })}
                </div>
                <div className="flex items-center" style={{ gap: '8px' }}>
                  <input
                    type="range"
                    min={UI_SCALE_MIN}
                    max={UI_SCALE_MAX}
                    step={UI_SCALE_STEP}
                    value={uiScale}
                    onChange={(e) => applyUiScale(Number(e.target.value))}
                    aria-label={t('settingsModal.interfaceSize', 'Interface size')}
                    style={{ flex: 1, accentColor: 'var(--vscode-accent)' }}
                  />
                  <span style={{ minWidth: '48px', textAlign: 'right', color: 'var(--vscode-text-fg)' }}>
                    {Math.round(uiScale * 100)}%
                  </span>
                </div>
                <span style={{ color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.interfaceSizeHint', 'Scales the whole interface, not just text. Also available anywhere with Ctrl+Shift+Plus, Ctrl+Shift+Minus and Ctrl+Shift+0.')}
                </span>
              </div>

              {/* Font size */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.editorFontSize')}</label>
                <input
                  type="number" min="10" max="30" value={editorFontSize}
                  onChange={(e) => { const val = Number(e.target.value); setEditorFontSize(val); setPreference('editorFontSize', val); }}
                  className="vscode-settings-input"
                  style={{ width: '100%' }}
                />
              </div>

              {/* Tab size */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.tabSize')}</label>
                <select value={editorTabSize} onChange={(e) => { const val = Number(e.target.value); setEditorTabSize(val); setPreference('editorTabSize', val); }} className="vscode-settings-input" style={{ width: '100%' }}>
                  <option value={2}>{t('settingsModal.twoSpaces')}</option>
                  <option value={4}>{t('settingsModal.fourSpaces')}</option>
                  <option value={8}>{t('settingsModal.eightSpaces')}</option>
                </select>
              </div>

              {/* Word wrap */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.wordWrap')}</label>
                <select value={editorWordWrap} onChange={(e) => { setEditorWordWrap(e.target.value); setPreference('editorWordWrap', e.target.value); }} className="vscode-settings-input" style={{ width: '100%' }}>
                  <option value="on">{t('settingsModal.wordWrapOn')}</option>
                  <option value="off">{t('settingsModal.wordWrapOff')}</option>
                </select>
              </div>

              {/* Minimap */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.minimap')}</label>
                <select value={editorMinimap} onChange={(e) => { setEditorMinimap(e.target.value); setPreference('editorMinimap', e.target.value); }} className="vscode-settings-input" style={{ width: '100%' }}>
                  <option value="on">{t('settingsModal.minimapOn')}</option>
                  <option value="off">{t('settingsModal.minimapOff')}</option>
                </select>
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>{t('settingsModal.minimapHint')}</span>
              </div>

              {/* Translate to (PDF viewer translation target) */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.translateTo', 'Translate to')}</label>
                <select
                  value={isCustomTranslateLang ? TRANSLATE_CUSTOM : translateTargetLang}
                  onChange={(e) => {
                    const value = e.target.value;
                    if (value === TRANSLATE_CUSTOM) {
                      setIsCustomTranslateLang(true);
                      return;
                    }
                    setIsCustomTranslateLang(false);
                    setTranslateTargetLang(value);
                    saveTranslateTargetLang(value);
                  }}
                  className="vscode-settings-input"
                  style={{ width: '100%' }}
                >
                  <option value="">{t('settingsModal.translateToInterface', 'Same as interface language')}</option>
                  {TRANSLATE_LANGUAGES.map(lang => (
                    <option key={lang.value} value={lang.value}>{t(lang.labelKey, lang.label)}</option>
                  ))}
                  <option value={TRANSLATE_CUSTOM}>{t('settingsModal.translateToOther', 'Other (type a language)')}</option>
                </select>
                {isCustomTranslateLang && (
                  <input
                    type="text"
                    value={translateTargetLang}
                    placeholder={t('settingsModal.translateToOtherPlaceholder', 'e.g. Norwegian')}
                    onChange={(e) => setTranslateTargetLang(e.target.value)}
                    onBlur={(e) => saveTranslateTargetLang(e.target.value.trim())}
                    className="vscode-settings-input"
                    style={{ width: '100%' }}
                  />
                )}
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.translateToHint', 'Target language used by the "Translate selection" action in the PDF viewer and the Markdown preview.')}
                </span>
              </div>

              {/* Pronunciation. Next to "Translate to" on purpose: the passage
                  someone most often wants to hear is the one they just had
                  translated, and the two share a language vocabulary. */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.speech', 'Pronunciation')}</label>
                <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'var(--vscode-text-fg)' }}>
                  <input
                    type="checkbox"
                    checked={!!speech.enabled}
                    onChange={(e) => saveSpeechSettings({ ...speech, enabled: e.target.checked })}
                  />
                  {t('settingsModal.speechEnabled', 'Offer "Pronounce selection" in document menus')}
                </label>

                {speech.enabled && (
                  <>
                    <div className="flex" style={{ gap: '12px' }}>
                      <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px' }}>
                        <input
                          type="radio"
                          name="speech-engine"
                          checked={(speech.engine || 'local') === 'local'}
                          onChange={() => saveSpeechSettings({ ...speech, engine: 'local' })}
                        />
                        {t('settingsModal.speechEngineLocal', 'Downloaded voice (offline)')}
                      </label>
                      <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px' }}>
                        <input
                          type="radio"
                          name="speech-engine"
                          checked={speech.engine === 'remote'}
                          onChange={() => saveSpeechSettings({ ...speech, engine: 'remote' })}
                        />
                        {t('settingsModal.speechEngineRemote', 'Speech endpoint')}
                      </label>
                    </div>

                    {(speech.engine || 'local') === 'local' ? (
                      <SpeechVoiceManager
                        selected={speech.local_voice || ''}
                        onSelect={(key) => saveSpeechSettings({ ...speech, local_voice: key })}
                        onChanged={onSpeechSettingsChange}
                      />
                    ) : speechModels.length === 0 ? (
                      <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                        {t('settingsModal.speechNoModels', 'No speech-capable model is registered. Add one in "Edit Models" and tick "Speech synthesis" — a hosted one, or a local server such as Kokoro-FastAPI, openedai-speech, LocalAI or Piper registered with its api_base.')}
                      </span>
                    ) : (
                      <>
                        <select
                          className="vscode-settings-input"
                          style={{ width: '100%' }}
                          value={speech.model || ''}
                          onChange={(e) => saveSpeechSettings({ ...speech, model: e.target.value })}
                        >
                          <option value="">{t('settingsModal.speechModelPlaceholder', 'Select a speech model...')}</option>
                          {speechModels.map(m => (
                            <option key={m.id} value={m.id}>
                              {m.id}{m.connection_label ? ` (${m.connection_label})` : ''}
                            </option>
                          ))}
                        </select>
                        <div className="flex" style={{ gap: '8px' }}>
                          <div className="flex flex-col" style={{ gap: '4px', flex: 1 }}>
                            <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.speechVoice', 'Voice')}</label>
                            <input
                              type="text"
                              className="vscode-settings-input"
                              value={speech.voice || ''}
                              placeholder="alloy"
                              onChange={(e) => setSpeech({ ...speech, voice: e.target.value })}
                              onBlur={() => saveSpeechSettings(speech)}
                            />
                          </div>
                          <div className="flex flex-col" style={{ gap: '4px', flex: 1 }}>
                            <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.speechFormat', 'Audio format')}</label>
                            <select
                              className="vscode-settings-input"
                              value={speech.response_format || 'mp3'}
                              onChange={(e) => saveSpeechSettings({ ...speech, response_format: e.target.value })}
                            >
                              {speechFormats.map(fmt => (
                                <option key={fmt} value={fmt}>{fmt}</option>
                              ))}
                            </select>
                          </div>
                        </div>
                      </>
                    )}

                    <div className="flex flex-col" style={{ gap: '4px', maxWidth: '140px' }}>
                      <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.speechSpeed', 'Speed')}</label>
                      <input
                        type="number"
                        min="0.25"
                        max="4"
                        step="0.05"
                        className="vscode-settings-input"
                        value={speech.speed ?? 1}
                        onChange={(e) => setSpeech({ ...speech, speed: e.target.value })}
                        onBlur={() => saveSpeechSettings({ ...speech, speed: Number(speech.speed) || 1 })}
                      />
                    </div>
                  </>
                )}

                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.speechHint', 'Reads a selected excerpt aloud in the PDF viewer, the Markdown preview and the chat. A downloaded voice runs entirely on this machine and needs no account; it uses the system espeak-ng to turn text into phonemes. The browser\'s own voices are not used: the app window ships no speech engine.')}
                </span>
              </div>

              {/* Dictation. Below Pronunciation because they are the two
                  halves of the same idea, and this is the one that listens. */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.dictation', 'Dictation')}</label>
                <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'var(--vscode-text-fg)' }}>
                  <input
                    type="checkbox"
                    checked={!!dictation.enabled}
                    onChange={(e) => saveDictationSettings({ ...dictation, enabled: e.target.checked })}
                  />
                  {t('settingsModal.dictationEnabled', 'Offer a microphone button in the chat')}
                </label>

                {dictation.enabled && (
                  <>
                    <div className="flex" style={{ gap: '8px' }}>
                      <div className="flex flex-col" style={{ gap: '4px', flex: 1 }}>
                        <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.dictationLanguage', 'Language')}</label>
                        <select
                          className="vscode-settings-input"
                          value={dictation.language || ''}
                          onChange={(e) => saveDictationSettings({ ...dictation, language: e.target.value })}
                        >
                          <option value="">{t('settingsModal.dictationLanguageAuto', 'Detect automatically')}</option>
                          {TRANSLATE_LANGUAGES.map(lang => (
                            <option key={lang.value} value={lang.value.split('-')[0]}>{lang.label}</option>
                          ))}
                        </select>
                      </div>
                    </div>

                    <div className="speech-voice-list">
                      {dictationModels.map((m) => (
                        <div
                          key={m.size}
                          className={`speech-voice-row${dictation.model === m.size ? ' is-selected' : ''}`}
                        >
                          <label className="speech-voice-label">
                            <input
                              type="radio"
                              name="dictation-model"
                              checked={dictation.model === m.size}
                              disabled={!m.installed}
                              onChange={() => saveDictationSettings({ ...dictation, model: m.size })}
                            />
                            <span className="speech-voice-name">
                              {m.size}
                              <span className="speech-voice-lang">{m.size_mb} MB</span>
                            </span>
                          </label>
                          {dictationBusy === m.size ? (
                            <span className="speech-voice-progress">{t('settingsModal.dictationDownloading', 'Downloading…')}</span>
                          ) : m.installed ? (
                            <button
                              type="button"
                              className="speech-voice-btn"
                              disabled={Boolean(dictationBusy)}
                              onClick={() => runDictationModelAction(m.size, 'remove')}
                            >
                              {t('settingsModal.dictationRemove', 'Remove')}
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="speech-voice-btn"
                              disabled={Boolean(dictationBusy)}
                              onClick={() => runDictationModelAction(m.size, 'download')}
                            >
                              {t('settingsModal.dictationDownload', 'Download')}
                            </button>
                          )}
                        </div>
                      ))}
                    </div>
                  </>
                )}

                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.dictationHint', 'Records from the microphone and types what you said into the chat composer, for you to review before sending. The model runs on this machine, so nothing is uploaded. Larger models are markedly more accurate outside English: on a Portuguese test sentence, tiny and base both misheard a word while small reproduced it exactly.')}
                </span>
              </div>

              {/* Prompt Evolution Iterations */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.promptEvolutionIterations', 'Prompt Evolution Iterations')}</label>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={promptEvolutionIterations}
                  onChange={(e) => {
                    const val = Math.max(1, Math.floor(Number(e.target.value) || 1));
                    setPromptEvolutionIterations(val);
                    savePromptEvolutionSettings(val, promptEvolutionMaxTokens);
                  }}
                  className="vscode-settings-input"
                  style={{ width: '100%' }}
                />
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.promptEvolutionIterationsHint', 'Number of refinement iterations executed when evolving a prompt (default: 1).')}
                </span>
              </div>

              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.promptEvolutionMaxTokens', 'Prompt Evolution Max Tokens')}</label>
                <input
                  type="number"
                  min="1"
                  max="65536"
                  step="1"
                  value={promptEvolutionMaxTokens}
                  onChange={(e) => {
                    const val = Math.max(1, Math.min(65536, Math.floor(Number(e.target.value) || 4096)));
                    setPromptEvolutionMaxTokens(val);
                    savePromptEvolutionSettings(promptEvolutionIterations, val);
                  }}
                  className="vscode-settings-input"
                  style={{ width: '100%' }}
                />
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.promptEvolutionMaxTokensHint', 'Maximum generated tokens for each prompt-evolution iteration (default: 4096).')}
                </span>
              </div>

              {/* Agent reasoning size */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.thoughtContextTokens', 'Agent reasoning size (tokens)')}</label>
                <input
                  type="number"
                  min="1000"
                  max="1000000"
                  step="1000"
                  value={thoughtTokensInput}
                  onChange={(e) => setThoughtTokensInput(e.target.value)}
                  onBlur={(e) => saveThoughtContextTokens(e.target.value)}
                  className="vscode-settings-input"
                  style={{ width: '100%' }}
                />
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.thoughtContextTokensHint', 'The chat shows the most recent agent reasoning up to this size. Resuming an interrupted turn replays its reasoning in full up to this size, or a summary written by the worker model beyond it. All reasoning is always stored (default: 32000).')}
                </span>
              </div>
              {/* How much of the running reasoning the chat itself shows */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.chatThoughtPreviewTokens', 'Reasoning shown in the chat (tokens)')}</label>
                <input
                  type="number"
                  min="100"
                  max="100000"
                  step="100"
                  value={chatPreviewTokensInput}
                  onChange={(e) => setChatPreviewTokensInput(e.target.value)}
                  onBlur={(e) => saveChatThoughtPreviewTokens(e.target.value)}
                  className="vscode-settings-input"
                  style={{ width: '100%' }}
                />
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.chatThoughtPreviewTokensHint', 'While a turn runs, the chat keeps only the last lines of reasoning in view. The whole of it is read in the Agent Thinking panel, which the chat links to (default: 1000).')}
                </span>
              </div>

              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.latexCompilation')}</label>
                <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'var(--vscode-text-fg)' }}>
                  <input
                    type="checkbox"
                    checked={draftSynctexEnabled}
                    onChange={(e) => {
                      const checked = e.target.checked;
                      setDraftSynctexEnabled(checked);
                      saveLatexSettings(checked);
                    }}
                  />
                  <span>{t('settingsModal.draftSynctexEnabled')}</span>
                </label>
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>{t('settingsModal.draftSynctexHint')}</span>
              </div>

              {/* Graphics mode for the app window itself. The embedded browser
                  loads the graphics driver into the OpalaTex process, so a
                  driver fault closes the whole application with no message;
                  this is the recovery for a machine where that happens. */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.graphics', 'Window graphics')}</label>
                <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'var(--vscode-text-fg)' }}>
                  <input
                    type="checkbox"
                    checked={webengineGpu === 'auto'}
                    disabled={isRestarting}
                    onChange={(e) => saveGraphicsSettings(e.target.checked ? 'auto' : 'off')}
                  />
                  <span>{t('settingsModal.graphicsHardwareAcceleration', 'Use hardware acceleration (GPU) for the app window')}</span>
                </label>
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.graphicsHint', 'The app window renders through the graphics driver, which runs inside the OpalaTex process: if the driver faults, the window closes with no error message. Turn this off if OpalaTex closes by itself. Takes effect after a restart.')}
                </span>
              </div>

              {/* Image generation */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.imageGeneration')}</label>
                <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'var(--vscode-text-fg)' }}>
                  <input
                    type="checkbox"
                    checked={!!imageGen.enabled}
                    onChange={(e) => saveImageGenSettings({ ...imageGen, enabled: e.target.checked })}
                  />
                  {t('settingsModal.imageGenerationEnabled')}
                </label>

                {imageModels.length === 0 ? (
                  <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                    {t('settingsModal.imageGenerationNoModels')}
                  </span>
                ) : (
                  <>
                    <select
                      className="vscode-settings-input"
                      style={{ width: '100%' }}
                      value={imageGen.model || ''}
                      onChange={(e) => saveImageGenSettings({ ...imageGen, model: e.target.value })}
                    >
                      <option value="">{t('settingsModal.imageGenerationModelPlaceholder')}</option>
                      {imageModels.map(m => (
                        <option key={m.id} value={m.id}>
                          {m.id}{m.connection_label ? ` (${m.connection_label})` : ''}
                        </option>
                      ))}
                    </select>
                    <div className="flex" style={{ gap: '8px' }}>
                      <div className="flex flex-col" style={{ gap: '4px', flex: 1 }}>
                        <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.imageGenerationSize')}</label>
                        <input
                          type="text"
                          className="vscode-settings-input"
                          value={imageGen.size || ''}
                          placeholder="1024x1024"
                          onChange={(e) => setImageGen({ ...imageGen, size: e.target.value })}
                          onBlur={() => saveImageGenSettings(imageGen)}
                        />
                      </div>
                      <div className="flex flex-col" style={{ gap: '4px', flex: 1 }}>
                        <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.imageGenerationOutputDir')}</label>
                        <input
                          type="text"
                          className="vscode-settings-input"
                          value={imageGen.output_dir || ''}
                          placeholder="figures"
                          onChange={(e) => setImageGen({ ...imageGen, output_dir: e.target.value })}
                          onBlur={() => saveImageGenSettings(imageGen)}
                        />
                      </div>
                    </div>
                  </>
                )}
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.imageGenerationHint')}
                </span>
              </div>

              {/* Docker Sandbox */}
              <div className="flex flex-col" style={{ gap: '8px', borderTop: '1px solid var(--vscode-border)', paddingTop: '12px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.sandboxTitle', 'Docker Sandbox')}</label>
                  {sandboxStatus && (
                    <span style={{
                      fontSize: '11px',
                      padding: '2px 8px',
                      borderRadius: '10px',
                      fontWeight: 600,
                      backgroundColor: sandboxStatus.ready ? 'rgba(78, 201, 176, 0.15)' : 'rgba(244, 71, 71, 0.15)',
                      color: sandboxStatus.ready ? 'var(--battery-good, #4ec9b0)' : 'var(--battery-critical, #f44747)',
                    }}>
                      {sandboxStatus.ready ? t('settingsModal.sandboxStatusReady', 'Ready') : t('settingsModal.sandboxStatusIssue', 'Needs Attention')}
                    </span>
                  )}
                </div>

                <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'var(--vscode-text-fg)' }}>
                  <input
                    type="checkbox"
                    checked={!!sandbox.enabled}
                    onChange={(e) => saveSandboxSettings({ ...sandbox, enabled: e.target.checked })}
                  />
                  {t('settingsModal.sandboxEnabled', 'Execute agent commands in Docker sandbox (Orchestrator & Worker)')}
                </label>

                <div className="flex" style={{ gap: '8px' }}>
                  <div className="flex flex-col" style={{ gap: '4px', flex: 2 }}>
                    <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>
                      {t('settingsModal.sandboxImage', 'Docker Image')}
                    </label>
                    <input
                      type="text"
                      className="vscode-settings-input"
                      value={sandbox.image || ''}
                      placeholder="python:3.12-slim"
                      onChange={(e) => setSandbox({ ...sandbox, image: e.target.value })}
                      onBlur={() => saveSandboxSettings(sandbox)}
                    />
                  </div>
                  <div className="flex flex-col" style={{ gap: '4px', flex: 1 }}>
                    <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>
                      {t('settingsModal.sandboxNetwork', 'Network')}
                    </label>
                    <select
                      className="vscode-settings-input"
                      value={sandbox.network || 'bridge'}
                      onChange={(e) => saveSandboxSettings({ ...sandbox, network: e.target.value })}
                    >
                      <option value="bridge">bridge (Internet)</option>
                      <option value="none">none (Isolated)</option>
                    </select>
                  </div>
                </div>

                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginTop: '4px' }}>
                  <button
                    type="button"
                    className="vscode-button"
                    disabled={sandboxBusy}
                    onClick={handleVerifySandbox}
                  >
                    {sandboxBusy ? t('settingsModal.sandboxChecking', 'Checking...') : t('settingsModal.sandboxVerifyBtn', 'Verify Docker')}
                  </button>

                  {sandboxStatus && !sandboxStatus.image_found && sandboxStatus.daemon_running && (
                    <button
                      type="button"
                      className="vscode-button"
                      disabled={sandboxBusy}
                      onClick={handlePullSandboxImage}
                    >
                      {sandboxBusy ? t('settingsModal.sandboxPulling', 'Pulling...') : t('settingsModal.sandboxPullBtn', 'Pull Docker Image')}
                    </button>
                  )}

                  <button
                    type="button"
                    className="vscode-button secondary"
                    style={{ background: 'transparent', border: '1px solid var(--vscode-border)', color: 'var(--vscode-text-fg)' }}
                    disabled={sandboxBusy}
                    onClick={handleRecreateSandbox}
                  >
                    {t('settingsModal.sandboxRecreateBtn', 'Reset Project Container')}
                  </button>
                </div>

                {sandboxStatus && sandboxStatus.problem && (
                  <div style={{
                    padding: '8px 12px',
                    borderRadius: '6px',
                    backgroundColor: 'rgba(244, 71, 71, 0.12)',
                    border: '1px solid rgba(244, 71, 71, 0.3)',
                    fontSize: '11px',
                    color: 'var(--vscode-text-fg)',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '6px',
                  }}>
                    <span>{sandboxStatus.problem}</span>
                    {sandboxStatus.action_command && (
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <code style={{
                          backgroundColor: 'var(--vscode-input-bg, #1e1e2e)',
                          padding: '4px 8px',
                          borderRadius: '4px',
                          fontSize: '11px',
                          userSelect: 'all',
                        }}>
                          {sandboxStatus.action_command}
                        </code>
                        <button
                          type="button"
                          className="vscode-button"
                          style={{ padding: '2px 8px', fontSize: '11px' }}
                          onClick={() => writeClipboard(sandboxStatus.action_command)}
                        >
                          {t('common.copy', 'Copy')}
                        </button>
                      </div>
                    )}
                  </div>
                )}

                {sandboxMessage && (
                  <span role="status" style={{ fontSize: '11px', color: 'var(--battery-good)' }}>
                    {sandboxMessage}
                  </span>
                )}

                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.sandboxHint', 'When enabled, the commands and scripts the Orchestrator and Worker run execute inside a persistent Docker container with the project mounted at /workspace. Changing the image or network recreates the container on the next command, discarding what was installed in it.')}
                </span>
              </div>

              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.workspaceFiles')}</label>
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)', lineHeight: 1.4 }}>
                  {t('settingsModal.hiddenFileExtensionsHint', {
                    extensions: workspaceHiddenExtensions.join(', '),
                  })}
                </span>
              </div>

              {/* Panel max lines */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.panelMaxLines')}</label>
                <input
                  type="number" min="100" max="10000" step="100"
                  value={panelMaxLines}
                  onChange={(e) => {
                    const val = Math.max(100, Math.min(10000, Number(e.target.value) || 1000));
                    setPanelMaxLines(val);
                  }}
                  className="vscode-settings-input"
                  style={{ width: '100%' }}
                />
                <span style={{ fontSize: '11px', color: 'var(--vscode-text-muted)' }}>{t('settingsModal.panelMaxLinesHint')}</span>
              </div>

              {/* Global Data Directory */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.globalDataDir')}</label>
                <div style={{ display: 'flex', gap: '8px' }}>
                  <input
                    type="text"
                    value={opalatexHome}
                    onChange={(e) => setOpalaTexHome(e.target.value)}
                    className="vscode-settings-input"
                    style={{ flex: 1 }}
                    placeholder={t('settingsModal.globalDataDirPlaceholder')}
                  />
                  <button
                    onClick={saveOpalaTexHome}
                    disabled={isRestarting}
                    className="vscode-button"
                  >
                    {isRestarting ? t('settingsModal.restarting') : t('settingsModal.save')}
                  </button>
                </div>
                {opalatexHomeError && (
                  <span style={{ fontSize: '11px', color: 'var(--vscode-errorForeground)' }}>{opalatexHomeError}</span>
                )}
                <span style={{ fontSize: '11px', color: 'var(--vscode-text-muted)' }}>{t('settingsModal.globalDataDirHint')}</span>
              </div>



              {/* Ephemeral Agent Settings */}
              <div className="flex flex-col" style={{ gap: '6px', borderTop: '1px solid var(--vscode-border)', paddingTop: '12px', marginTop: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.ephemeralAgentTitle')}</label>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
                  <div className="flex flex-col" style={{ gap: '4px' }}>
                    <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.ephemeralMaxTokens')}</label>
                    <input type="number" min="1" className="vscode-settings-input" placeholder={t('settingsModal.ephemeralUnlimited')}
                      value={ephemeralParams?.max_tokens || ''}
                      onChange={e => updateEphemeralParam('max_tokens', e.target.value ? Number(e.target.value) : undefined)} />
                  </div>
                  <div className="flex flex-col" style={{ gap: '4px' }}>
                    <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.ephemeralContextWindow')}</label>
                    <input type="number" min="1" className="vscode-settings-input" placeholder={t('settingsModal.ephemeralContextWindowPlaceholder')}
                      value={ephemeralParams?.num_ctx || ''}
                      onChange={e => updateEphemeralParam('num_ctx', e.target.value ? Number(e.target.value) : undefined)} />
                  </div>
                  <div className="flex flex-col" style={{ gap: '4px' }}>
                    <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.ephemeralTemperature')}</label>
                    <input type="number" step="0.1" min="0" max="2" className="vscode-settings-input" placeholder="0.7"
                      value={ephemeralParams?.temperature ?? ''}
                      onChange={e => updateEphemeralParam('temperature', e.target.value ? parseFloat(e.target.value) : undefined)} />
                  </div>
                  <div className="flex flex-col" style={{ gap: '4px' }}>
                    <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.ephemeralReasoningEffort')}</label>
                    <select className="vscode-settings-input"
                      value={ephemeralParams?.reasoning_effort || 'none'}
                      onChange={e => updateEphemeralParam('reasoning_effort', e.target.value)}>
                      <option value="none">{t('common.optionNone', 'None')}</option>
                      <option value="low">{t('common.optionLow', 'Low')}</option>
                      <option value="medium">{t('common.optionMedium', 'Medium')}</option>
                      <option value="high">{t('common.optionHigh', 'High')}</option>
                      <option value="xhigh">{t('common.optionXHigh', 'Extra high')}</option>
                    </select>
                  </div>
                  <div className="flex flex-col" style={{ gap: '4px' }}>
                    <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.ephemeralMaxIterations')}</label>
                    <input type="number" min="1" className="vscode-settings-input" placeholder="10"
                      value={ephemeralParams?.max_iterations || ''}
                      onChange={e => updateEphemeralParam('max_iterations', e.target.value ? Number(e.target.value) : undefined)} />
                  </div>
                  <div className="flex flex-col" style={{ gap: '4px' }}>
                    <label style={{ fontSize: '11px', color: 'var(--vscode-text-subtle)' }}>{t('settingsModal.ephemeralMaxToolCalls')}</label>
                    <input type="number" min="1" className="vscode-settings-input" placeholder="10"
                      value={ephemeralParams?.max_tool_calls || ''}
                      onChange={e => updateEphemeralParam('max_tool_calls', e.target.value ? Number(e.target.value) : undefined)} />
                  </div>
                </div>
              </div>

            </>
          )}

          {activeTab === 'dependencies' && (
            <>
              {/* Install Tectonic */}
              <div className="flex flex-col" style={{ gap: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.installTectonicTitle')}</label>
                <div style={{ display: 'flex', gap: '8px' }}>
                  <button
                    onClick={() => {
                      setTectonicInstallMessage('');
                      const btn = document.getElementById('btnInstallTectonic');
                      if (btn) btn.disabled = true;
                      fetch('/api/settings/install-tectonic', { method: 'POST' })
                        .then(r => r.json())
                        .then(async res => {
                          if (res.success) {
                            setTectonicInstallMessage(t('settingsModal.installTectonicSuccess'));
                          } else {
                            await showAlert(t('settingsModal.installTectonicError') + res.error);
                          }
                          if (btn) btn.disabled = false;
                        })
                        .catch(async err => {
                          await showAlert(t('settingsModal.connectionError') + err);
                          if (btn) btn.disabled = false;
                        });
                    }}
                    id="btnInstallTectonic"
                    className="vscode-button"
                  >
                    {t('settingsModal.installTectonicBtn')}
                  </button>
                </div>
                {tectonicInstallMessage && (
                  <span role="status" style={{ fontSize: '11px', color: 'var(--battery-good)' }}>
                    {tectonicInstallMessage}
                  </span>
                )}
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>{t('settingsModal.installTectonicHint')}</span>
              </div>

              {/* Install Pandoc */}
              <div className="flex flex-col" style={{ gap: '6px', borderTop: '1px solid var(--vscode-border)', paddingTop: '12px', marginTop: '6px' }}>
                <label className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.installPandocTitle')}</label>
                <div style={{ display: 'flex', gap: '8px' }}>
                  <button
                    onClick={() => {
                      setPandocInstallMessage('');
                      const btn = document.getElementById('btnInstallPandoc');
                      if (btn) btn.disabled = true;
                      fetch('/api/settings/install-pandoc', { method: 'POST' })
                        .then(r => r.json())
                        .then(async res => {
                          if (res.success) {
                            setPandocInstallMessage(t('settingsModal.installPandocSuccess'));
                          } else {
                            await showAlert(t('settingsModal.installPandocError') + res.error);
                          }
                          if (btn) btn.disabled = false;
                        })
                        .catch(async err => {
                          await showAlert(t('settingsModal.connectionError') + err);
                          if (btn) btn.disabled = false;
                        });
                    }}
                    id="btnInstallPandoc"
                    className="vscode-button"
                  >
                    {t('settingsModal.installPandocBtn')}
                  </button>
                </div>
                {pandocInstallMessage && (
                  <span role="status" style={{ fontSize: '11px', color: 'var(--battery-good)' }}>
                    {pandocInstallMessage}
                  </span>
                )}
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>{t('settingsModal.installPandocHint')}</span>
              </div>
            </>
          )}

          {activeTab === 'about' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', color: 'var(--vscode-text-fg)' }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                <span className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.version')}</span>
                <span style={{ fontSize: '13px', fontWeight: 'bold', color: 'var(--vscode-text-fg)' }}>{runtimeInfo.version}</span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                <span className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.author')}</span>
                <span style={{ fontSize: '13px', color: 'var(--vscode-text-fg)' }}>
                  dev@opalacoder.com</span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                <span className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.license')}</span>
                <span style={{ fontSize: '13px', color: 'var(--vscode-text-fg)' }}>MIT</span>
              </div>
              {runtimeInfo.running_in_snap && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '9px', padding: '14px', border: '1px solid var(--vscode-focusBorder)', borderRadius: '8px', backgroundColor: 'var(--vscode-sidebar-bg)' }}>
                  <span className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.directInstallTitle')}</span>
                  <span style={{ fontSize: '12px', lineHeight: 1.5, color: 'var(--vscode-descriptionForeground)' }}>
                    {t('settingsModal.directInstallDescription')}
                  </span>
                  <code style={{ fontSize: '11px', padding: '8px', overflowWrap: 'anywhere', backgroundColor: 'var(--vscode-input-bg)' }}>
                    {DIRECT_INSTALL_COMMAND}
                  </code>
                  <button className="vscode-button" style={{ alignSelf: 'flex-start' }} onClick={copyDirectInstallCommand}>
                    {t('settingsModal.directInstallCopy')}
                  </button>
                  <span style={{ fontSize: '11px', lineHeight: 1.4, color: 'var(--vscode-descriptionForeground)' }}>
                    {t('settingsModal.directInstallWarning')}
                  </span>
                </div>
              )}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', padding: '14px', border: '1px solid var(--vscode-border)', borderRadius: '8px', backgroundColor: 'var(--vscode-sidebar-bg)' }}>
                <span className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.feedbackTitle')}</span>
                <span style={{ fontSize: '12px', lineHeight: 1.5, color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.feedbackDescription')}
                </span>
                <button className="vscode-button" style={{ alignSelf: 'flex-start' }} onClick={openFeedbackIssue}>
                  {t('settingsModal.feedbackButton')}
                </button>
                <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)' }}>
                  {t('settingsModal.feedbackPrivacy')}
                </span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
                <span className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.uninstallTitle')}</span>
                <span style={{ fontSize: '12px', lineHeight: 1.5, color: 'var(--vscode-descriptionForeground)' }}>
                  {runtimeInfo.running_in_snap
                    ? t('settingsModal.uninstallSnap')
                    : runtimeInfo.platform === 'Windows'
                      ? t('settingsModal.uninstallWindows')
                      : t('settingsModal.uninstallDirect')}
                </span>
              </div>
              <div style={{
                display: 'flex',
                flexWrap: 'wrap',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: '16px',
                marginTop: '6px',
                padding: '16px',
                border: '1px solid var(--vscode-border)',
                borderRadius: '8px',
                backgroundColor: 'var(--vscode-sidebar-bg)'
              }}>
                <div style={{ flex: '1 1 240px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  <span className="vscode-sidebar-section-title" style={{ padding: 0 }}>{t('settingsModal.donationTitle')}</span>
                  <span style={{ fontSize: '12px', lineHeight: '1.5', color: 'var(--vscode-description-fg, #aaaaaa)' }}>
                    {t('settingsModal.donationDescription')}
                  </span>
                  <button
                    className="vscode-button"
                    style={{ alignSelf: 'flex-start' }}
                    onClick={() => openExternal('https://www.paypal.com/donate/?business=DKWJSCLDJG6XY&no_recurring=0&item_name=Manuten%C3%A7%C3%A3o+do+Software+Open+Source+OpalaTex&currency_code=BRL')}
                  >
                    {t('settingsModal.donationButton')}
                  </button>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '5px' }}>
                  <img
                    src="/qr-code.png"
                    alt={t('settingsModal.donationQrAlt')}
                    style={{ width: '132px', height: '132px', objectFit: 'contain', padding: '6px', backgroundColor: '#ffffff', borderRadius: '6px' }}
                  />
                  <span style={{ fontSize: '11px', color: 'var(--vscode-description-fg, #aaaaaa)' }}>
                    {t('settingsModal.donationScan')}
                  </span>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="vscode-modal-footer">
          <button onClick={onClose} className="vscode-button">{t('settingsModal.close')}</button>
        </div>
      </div>
    </div>
  );
}
