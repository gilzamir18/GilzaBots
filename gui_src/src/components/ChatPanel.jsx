import { useRef, useState, useCallback, useLayoutEffect, useEffect } from 'react';
import { MessageSquare, Cpu, HelpCircle, Check, X, ArrowRight, Eraser, Globe, Settings, Settings2, Plus, Trash2, Search, Paperclip, FileText, ZoomIn, ZoomOut, Download, Printer, GitBranch, RefreshCw, Pencil, Sparkles, MoreHorizontal, AlertTriangle, Clock, Activity, Zap, ChevronDown, Mic, Square, Brain, SlidersHorizontal, Minimize2, Maximize2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useCustomDialog } from './modals/CustomDialogProvider';
import { FormattedMessage } from '../utils/formatMessage';
import { readClipboard, readClipboardImage } from '../utils/clipboard.js';
import { base64ImageToFile, clipboardHasText, extractClipboardFiles, pastedImageName } from '../utils/clipboardAttachments.js';
import { isLocalModelId } from '../utils/models.js';
import { readChatCompact, writeChatCompact } from '../utils/chatCompact.js';
import { catalogReasoning, chatReasoningEffort, withChatReasoningEffort } from '../utils/chatReasoningEffort.js';
import { stripInlineReasoning } from '../utils/thinkTags.js';
import { useTextContextMenu } from '../hooks/useTextContextMenu.js';

import { turnEndFromContent } from '../utils/turnMarkers.js';
import { formatWaitDuration, providerWaitSeconds } from '../utils/providerWait.js';
import TextContextMenu from './TextContextMenu.jsx';
import TranslationPopup from './TranslationPopup.jsx';
import SnippetQueryPopup from './SnippetQueryPopup.jsx';
import SpeechPopup from './SpeechPopup.jsx';
import { useSnippetTranslation } from '../hooks/useSnippetTranslation.js';
import { useSnippetQuery } from '../hooks/useSnippetQuery.js';
import { useSnippetSpeech } from '../hooks/useSnippetSpeech.js';
import { useDictation } from '../hooks/useDictation.js';
import { buildQuoteExcerptPrompt } from '../utils/askAboutPrompt.js';
import SearchChatsModal from './modals/SearchChatsModal.jsx';
import ModelSelect from './ModelSelect.jsx';
import TutorialMenu from './TutorialMenu.jsx';
import { getPreference, setPreference } from '../utils/preferences.js';

const normalizeForErrorMatch = (value) => String(value || '')
  .normalize('NFD')
  .replace(/[\u0300-\u036f]/g, '')
  .toLowerCase();

const stripThinkBlocksForErrorMatch = (content = '') => (
  stripInlineReasoning(content)
);

const isRetryableAssistantErrorMessage = (msg, displayContent) => {
  if (msg?.is_error === true) return true;
  const raw = stripThinkBlocksForErrorMatch(displayContent || msg?.content || '');
  const normalized = normalizeForErrorMatch(raw);
  const start = normalized.replace(/^[^\p{L}\p{N}]+/u, '');
  return (
    normalized.includes('agent error') ||
    normalized.includes('erro do agente') ||
    start.startsWith('error:') ||
    start.startsWith('erro:') ||
    start.startsWith('failed') ||
    start.startsWith('failure') ||
    start.startsWith('falha') ||
    normalized.includes('cota de uso') ||
    normalized.includes('creditos suficientes') ||
    normalized.includes('insufficient ai credits') ||
    normalized.includes('critical worker crash') ||
    normalized.includes('err_connection_failed')
  );
};

const isHiddenChatSystemMessage = (msg) => (
  msg?.role === 'system' &&
  (
    String(msg?.content || '').startsWith('[MODE] ') ||
    String(msg?.content || '').startsWith('[PLAN APPROVED] ') ||
    String(msg?.content || '').startsWith('[PLAN REJECTED] ') ||
    String(msg?.content || '').startsWith('Achievements logged during this turn:')
  )
);

const numericMessageId = (message) => {
  if (message?.id === undefined || message?.id === null || message.id === '') return null;
  const parsed = Number(message.id);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
};

// Right-side chat panel for interacting with the GilzaBots agent.
// Distance from the bottom (in px) still treated as "at the bottom".
const BOTTOM_PIN_THRESHOLD_PX = 48;

// The provider has sent nothing for a while. The counter runs locally from the
// moment the silence began; the backend reports only its start and its end.
// It informs, and leaves the decision to the user: a slow model and a dead
// connection look identical from here.
function ProviderWaitNotice({ wait, t }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [wait]);
  const elapsed = formatWaitDuration(providerWaitSeconds(wait, now));
  return (
    <div
      role="status"
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: '6px',
        fontSize: '12px',
        color: 'var(--vscode-descriptionForeground)',
        border: '1px solid var(--vscode-widget-border, #3c3c3c)',
        borderRadius: '4px',
        padding: '6px 8px',
      }}
    >
      <Clock size={13} aria-hidden="true" style={{ flexShrink: 0, marginTop: '1px' }} />
      <span>
        {wait.phase === 'stream'
          ? t('chatPanel.providerWaitStream', { elapsed })
          : t('chatPanel.providerWait', { elapsed })}
      </span>
    </div>
  );
}

export default function ChatPanel({
  isTutorialChat = false,
  tutorialTopics = [],
  onTutorialTopic,
  chatMessages,
  chatInput,
  chatInputFocusSignal = 0,
  speechSettingsSignal = 0,
  setChatInput,
  isAgentRunning,
  // False while the running turn belongs to another chat than this one.
  isLiveTurnOnScreen = true,
  agentStepInfo = { step: 0, maxSteps: null },
  // Set while the model's provider has been silent past the backend threshold.
  providerWait = null,
  heartbeatMode = 'medium',
  setHeartbeatMode,
  queuedMessages = [],
  onCancelQueuedMessage,
  onCancelAllQueuedMessages,
  isInterruptPending = false,
  chatThoughtStream,
  onShowThinking,
  chatResponseStream,
  chatContextUsage,
  setChatContextUsage,
  activeProject,
  isChatVisible,
  setIsChatVisible,
  chatWidth,
  handleSendMessage,
  onEditUserMessage,
  onGenerateResponseForUserMessage,
  handleInterruptAgent,
  chatEndRef,
  onClearChat,
  webSearchConfig,
  setWebSearchConfig,
  activeChatId,
  setActiveChatId,
  chats,
  setChats,
  setChatMessages,
  onSwitchChat,
  pendingAttachments,
  setPendingAttachments,
  isChatMode,
  // The studio layout sizes the chat through its own grid track, so the panel
  // must fill whatever cell it is given instead of carrying a pixel width.
  fillContainer,
  // The layout key (see utils/chatCompact.js) when the host layout offers the
  // compact chat, null otherwise.
  compactLayout = null,
  globalModels = [],
  onRefreshModels,
  onEditModels,
  onModelChange,
}) {
  const { t, i18n } = useTranslation();
  const { showPrompt, showAlert, showConfirm } = useCustomDialog();
  const historyRef = useRef(null);
  const inputRef = useRef(null);
  const fileInputRef = useRef(null);
  // The context-menu paste is defined above the attachment helpers; the ref
  // lets it reach them without hoisting the whole attachment section.
  const attachClipboardImageRef = useRef(null);
  const chatActionsMenuRef = useRef(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const [uploadingFiles, setUploadingFiles] = useState(false);
  const [showChatActionsMenu, setShowChatActionsMenu] = useState(false);
  const [showHeartbeatMenu, setShowHeartbeatMenu] = useState(false);
  const heartbeatMenuRef = useRef(null);
  const [showReasoningMenu, setShowReasoningMenu] = useState(false);
  const reasoningMenuRef = useRef(null);
  // Compact chat: only the conversation and the prompt stay on screen, and the
  // toolbars open as an overlay from the header. The preference is per layout.
  const [compactPreferred, setCompactPreferred] = useState(() => readChatCompact(compactLayout));
  const [showCompactControls, setShowCompactControls] = useState(false);
  const compactControlsRef = useRef(null);
  const compactControlsToggleRef = useRef(null);
  const isCompact = Boolean(compactLayout) && compactPreferred;
  const [isEvolvingPrompt, setIsEvolvingPrompt] = useState(false);
  const [evolutionProgress, setEvolutionProgress] = useState(null);
  const evolveAbortControllerRef = useRef(null);
  const originalPromptRef = useRef('');
  const { menu, onContextMenu, handleCopy, handleSelectAll, close: closeMenu } = useTextContextMenu();

  // The conversation is a document surface too: an excerpt in a chat bubble is
  // as worth translating or hearing as one in a PDF, and it is frequently the
  // agent's own answer in a language the reader is still learning. Same request
  // lifecycle, same popups, same menu items as the viewers (§2.13).
  const {
    translation,
    translate: runTranslation,
    retry: retryTranslation,
    copy: copyTranslation,
    close: closeTranslation,
  } = useSnippetTranslation({
    model: activeProject?.model || '',
    uiLanguage: i18n.language,
  });
  // "Look up" answers in a card of its own, so a question about an excerpt of
  // the conversation does not become a turn of it (§2.13.4).
  const {
    query: lookup,
    open: openLookup,
    setQuestion: setLookupQuestion,
    submit: submitLookup,
    compose: composeLookup,
    copy: copyLookup,
    saveMarkdown: saveLookupMarkdown,
    close: closeLookup,
  } = useSnippetQuery({
    model: activeProject?.model || '',
    projectPath: activeProject?.project_path || '',
  });

  const {
    speech,
    availability: speechAvailability,
    refreshAvailability: refreshSpeechAvailability,
    speak,
    toggle: toggleSpeech,
    replay: replaySpeech,
    retry: retrySpeech,
    close: closeSpeech,
  } = useSnippetSpeech({ settingsSignal: speechSettingsSignal });

  const handleChatContextMenu = useCallback((e) => {
    closeTranslation();
    // Re-read when the menu opens so configuring speech in Settings takes
    // effect without reopening the chat.
    refreshSpeechAvailability();
    onContextMenu(e);
  }, [onContextMenu, closeTranslation, refreshSpeechAvailability]);

  // Dictation: record, stop, insert. The transcript lands in the composer for
  // the user to read and send — it never starts a turn, because transcription
  // makes mistakes and an unreviewed one would reach the agent as if it had
  // been typed.
  const handleDictatedText = useCallback((text) => {
    setChatInput((prev) => (prev.trim() ? `${prev.replace(/\s+$/, '')} ${text}` : text));
  }, [setChatInput]);

  const dictation = useDictation({
    onText: handleDictatedText,
    settingsSignal: speechSettingsSignal,
  });

  const handleQuoteExcerpt = useCallback((m) => {
    const prompt = buildQuoteExcerptPrompt(t, m?.selectedText);
    if (!prompt) return;
    // Staged, never sent: the user finishes the question and presses Send,
    // exactly as "Ask about" does in the document viewers.
    setChatInput((prev) => (prev.trim() ? `${prev.replace(/\s+$/, '')}\n\n${prompt}` : prompt));
    closeMenu();
  }, [t, setChatInput, closeMenu]);

  const handleLookUpExcerpt = useCallback((m) => {
    closeMenu();
    openLookup(m?.selectedText, { x: m.x, y: m.y }, { surface: 'chat' });
  }, [closeMenu, openLookup]);

  const handleTranslateExcerpt = useCallback((m) => {
    closeMenu();
    runTranslation(m?.selectedText, { x: m.x, y: m.y });
  }, [closeMenu, runTranslation]);

  const handlePronounceExcerpt = useCallback((m) => {
    closeMenu();
    speak(m?.selectedText, { x: m.x, y: m.y });
  }, [closeMenu, speak]);


  useEffect(() => {
    if (!showHeartbeatMenu) return;
    const handleClickOutside = (e) => {
      if (heartbeatMenuRef.current && !heartbeatMenuRef.current.contains(e.target)) {
        setShowHeartbeatMenu(false);
      }
    };
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        setShowHeartbeatMenu(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [showHeartbeatMenu]);

  useEffect(() => {
    if (!showReasoningMenu) return;
    const handleClickOutside = (e) => {
      if (reasoningMenuRef.current && !reasoningMenuRef.current.contains(e.target)) {
        setShowReasoningMenu(false);
      }
    };
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        setShowReasoningMenu(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [showReasoningMenu]);

  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }, [chatInput]);

  // Something outside the chat asked for the composer (the PDF viewer's "Ask
  // about" staging a prompt, or Ctrl+T opening the chat): focus it and park
  // the caret at the end so the user can type straight away.
  useEffect(() => {
    if (!chatInputFocusSignal) return;
    const el = inputRef.current;
    if (!el) return;
    const focusAtEnd = requestAnimationFrame(() => {
      el.focus();
      const end = el.value.length;
      el.setSelectionRange(end, end);
      el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(focusAtEnd);
  }, [chatInputFocusSignal]);

  // Auto-scroll follows the bottom of the history only while the user is
  // parked there. Scrolling up (to read something while the agent streams)
  // unpins it; coming back to the bottom pins it again.
  const isPinnedToBottomRef = useRef(true);
  const programmaticScrollUntilRef = useRef(0);

  const isHistoryNearBottom = useCallback(() => {
    const el = historyRef.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_PIN_THRESHOLD_PX;
  }, []);

  const handleHistoryScroll = useCallback(() => {
    // Ignore the scroll events our own smooth scrolling emits, otherwise the
    // intermediate positions would look like the user scrolled up.
    if (Date.now() < programmaticScrollUntilRef.current) return;
    isPinnedToBottomRef.current = isHistoryNearBottom();
  }, [isHistoryNearBottom]);

  const scrollHistoryToBottom = useCallback((behavior = 'auto') => {
    const el = historyRef.current;
    if (!el) return;
    if (behavior === 'smooth') programmaticScrollUntilRef.current = Date.now() + 700;
    el.scrollTo({ top: el.scrollHeight, behavior });
  }, []);

  useEffect(() => {
    if (isAgentRunning && (chatThoughtStream || chatResponseStream) && isPinnedToBottomRef.current) {
      scrollHistoryToBottom();
    }
  }, [chatThoughtStream, chatResponseStream, isAgentRunning, scrollHistoryToBottom]);

  useEffect(() => {
    // A message the user just sent always brings the view back to the bottom.
    if (chatMessages[chatMessages.length - 1]?.role === 'user') {
      isPinnedToBottomRef.current = true;
    }
    if (!isPinnedToBottomRef.current) return;
    scrollHistoryToBottom('smooth');
  }, [chatMessages, scrollHistoryToBottom]);

  useEffect(() => {
    // Opening another chat starts at its bottom.
    isPinnedToBottomRef.current = true;
    scrollHistoryToBottom();
  }, [activeChatId, scrollHistoryToBottom]);


  useEffect(() => {
    setCompactPreferred(readChatCompact(compactLayout));
    setShowCompactControls(false);
  }, [compactLayout]);

  const setCompactView = useCallback((value) => {
    setCompactPreferred(value);
    setShowCompactControls(false);
    writeChatCompact(compactLayout, value);
  }, [compactLayout]);

  useEffect(() => {
    if (!isCompact || !showCompactControls) return;
    const handlePointerDown = (event) => {
      // The toggle closes the overlay itself; letting this handler close it
      // first would make the toggle's click reopen it.
      if (compactControlsToggleRef.current?.contains(event.target)) return;
      if (!compactControlsRef.current?.contains(event.target)) {
        setShowCompactControls(false);
      }
    };
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') setShowCompactControls(false);
    };
    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isCompact, showCompactControls]);

  useEffect(() => {
    if (!showChatActionsMenu) return;
    const handlePointerDown = (event) => {
      if (!chatActionsMenuRef.current?.contains(event.target)) {
        setShowChatActionsMenu(false);
      }
    };
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [showChatActionsMenu]);

  const handlePaste = useCallback(() => {
    readClipboard().then((text) => {
      if (!text) {
        // No text on the clipboard: the user may be pasting a copied image.
        attachClipboardImageRef.current?.();
        return;
      }
      const el = inputRef.current;
      if (el) {
        const start = el.selectionStart ?? chatInput.length;
        const end = el.selectionEnd ?? chatInput.length;
        const next = chatInput.slice(0, start) + text + chatInput.slice(end);
        setChatInput(next);
        requestAnimationFrame(() => {
          el.focus();
          const pos = start + text.length;
          el.setSelectionRange(pos, pos);
        });
      } else {
        setChatInput((prev) => prev + text);
      }
    });
    closeMenu();
  }, [chatInput, setChatInput, closeMenu]);

  const [hideThink, setHideThink] = useState(() => {
    return getPreference('chatHideThink') === 'true';
  });

  // Chat input history state
  const [inputHistory, setInputHistory] = useState(() => {
    try {
      const saved = getPreference('chatInputHistory');
      return saved ? JSON.parse(saved) : [];
    } catch (_) {
      return [];
    }
  });
  const [historyIndex, setHistoryIndex] = useState(-1);
  const [tempInput, setTempInput] = useState('');
  // MCP config panel state
  const [showMcpPanel, setShowMcpPanel] = useState(false);
  const [mcpUrlDraft, setMcpUrlDraft] = useState('');
  const [mcpToolDraft, setMcpToolDraft] = useState('web_search');
  const [mcpApiKeyDraft, setMcpApiKeyDraft] = useState('');
  const [useMcpDraft, setUseMcpDraft] = useState(false);
  const [chatZoom, setChatZoom] = useState(() => {
    const saved = getPreference('chatZoom');
    const parsed = saved !== null ? Number(saved) : NaN;
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
  });
  const [mcpTestStatus, setMcpTestStatus] = useState(''); // '', 'testing', 'ok', 'error:<msg>'

  // Font magnifier helpers
  const MIN_ZOOM = 0.5;
  const MAX_ZOOM = 2.5;
  const ZOOM_STEP = 0.1;
  const applyChatZoom = useCallback((next) => {
    const clamped = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(next * 10) / 10));
    setChatZoom(clamped);
    setPreference('chatZoom', String(clamped));
  }, []);
  const zoomIn = useCallback(() => applyChatZoom(chatZoom + ZOOM_STEP), [applyChatZoom, chatZoom]);
  const zoomOut = useCallback(() => applyChatZoom(chatZoom - ZOOM_STEP), [applyChatZoom, chatZoom]);
  const zoomReset = useCallback(() => applyChatZoom(1), [applyChatZoom]);

  // Custom prompt state for new chat
  const [showNewChatPrompt, setShowNewChatPrompt] = useState(false);
  const [newChatName, setNewChatName] = useState('');
  const [showSearchModal, setShowSearchModal] = useState(false);
  const [editingMessageIndex, setEditingMessageIndex] = useState(null);
  const [editingMessageDraft, setEditingMessageDraft] = useState('');
  // Why the last save of the open editor was refused; the editor stays open
  // with the text until the edit is accepted or cancelled.
  const [editingMessageError, setEditingMessageError] = useState('');
  const [isSubmittingEdit, setIsSubmittingEdit] = useState(false);
  const editSubmitInFlightRef = useRef(false);

  // Custom confirm state for deleting chat
  const [chatToDelete, setChatToDelete] = useState(null);

  const getPromptEvolutionErrorMessage = useCallback((error) => {
    const raw = error instanceof Error ? error.message : String(error || '');
    const normalized = raw.toLowerCase();

    if (normalized.includes('reasoning only')) {
      return t('chatPanel.evolveReasoningOnly');
    }
    if (normalized.includes('internal task wrapper')) {
      return t('chatPanel.evolveInternalWrapperResult');
    }
    if (normalized.includes('original prompt unchanged')) {
      return t('chatPanel.evolveOriginalUnchanged');
    }
    if (normalized.includes('internal instructions')) {
      return t('chatPanel.evolveInternalInstructionLeak');
    }
    if (normalized.includes('empty refined prompt')) {
      return t('chatPanel.evolveEmptyResult');
    }
    if (!raw || normalized === 'evolution failed') {
      return t('chatPanel.evolveGenericFailure');
    }
    return raw;
  }, [t]);
  const handleCancelEvolvePrompt = useCallback(async () => {
    if (evolveAbortControllerRef.current) {
      try {
        evolveAbortControllerRef.current.abort();
      } catch (_) {}
      evolveAbortControllerRef.current = null;
    }
    try {
      fetch('/api/chat/cancel-evolve-prompt', { method: 'POST' }).catch(() => {});
    } catch (_) {}
    if (originalPromptRef.current) {
      setChatInput(originalPromptRef.current);
    }
    setIsEvolvingPrompt(false);
    setEvolutionProgress(null);
  }, []);

  const handleEvolvePrompt = async () => {
    const rawPrompt = chatInput.trim();
    if (!rawPrompt || isAgentRunning || isEvolvingPrompt || !activeProject) return;

    originalPromptRef.current = rawPrompt;
    const controller = new AbortController();
    evolveAbortControllerRef.current = controller;

    setIsEvolvingPrompt(true);
    setEvolutionProgress({ active: true, iteration: 1, total: 1, complete: false });

    try {
      const res = await fetch('/api/chat/evolve-prompt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
        signal: controller.signal,
        body: JSON.stringify({
          prompt: rawPrompt,
          model: activeProject.model,
          project_path: activeProject?.path || activeProject?.project_name,
          stream: true,
        }),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.error || t('statusBar.networkError', { message: res.statusText }));
      }

      const contentType = res.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        const data = await res.json();
        if (data.prompt) {
          setChatInput(data.prompt);
        }
      } else if (res.body) {
        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let currentEvent = 'message';
        let isFirstChunkInIter = true;

        while (true) {
          if (controller.signal.aborted) break;
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;

            if (trimmed.startsWith('event:')) {
              currentEvent = trimmed.slice(6).trim();
            } else if (trimmed.startsWith('data:')) {
              const dataStr = trimmed.slice(5).trim();
              try {
                const payload = JSON.parse(dataStr);
                if (currentEvent === 'iteration_start') {
                  isFirstChunkInIter = true;
                  setEvolutionProgress({
                    active: true,
                    iteration: payload.iteration,
                    total: payload.total_iterations,
                    complete: false,
                  });
                } else if (currentEvent === 'chunk') {
                  if (payload.text) {
                    if (isFirstChunkInIter) {
                      setChatInput(payload.text);
                      isFirstChunkInIter = false;
                    } else {
                      setChatInput(prev => prev + payload.text);
                    }
                  }
                } else if (currentEvent === 'iteration_end') {
                  if (payload.prompt) {
                    setChatInput(payload.prompt);
                  }
                } else if (currentEvent === 'complete') {
                  if (payload.prompt) {
                    setChatInput(payload.prompt);
                  }
                } else if (currentEvent === 'error') {
                  throw new Error(payload.error || t('chatPanel.evolveGenericFailure'));
                }
              } catch (e) {
                if (currentEvent === 'error') throw e;
              }
            }
          }
        }
      }

      if (!controller.signal.aborted) {
        setEvolutionProgress(prev => ({ ...(prev || {}), active: false, complete: true }));
        setTimeout(() => setEvolutionProgress(null), 2200);
      }

    } catch (err) {
      if (err.name === 'AbortError' || controller.signal.aborted) {
        if (originalPromptRef.current) {
          setChatInput(originalPromptRef.current);
        }
        setEvolutionProgress(null);
      } else {
        showAlert(t('chatPanel.evolveError', { message: getPromptEvolutionErrorMessage(err) }));
        setEvolutionProgress(null);
      }
    } finally {
      setIsEvolvingPrompt(false);
      if (evolveAbortControllerRef.current === controller) {
        evolveAbortControllerRef.current = null;
      }
    }
  };


  const searchEnabled = webSearchConfig?.enabled ?? true;

  const handleToggleWebSearch = async () => {
    const newEnabled = !searchEnabled;
    const updated = { ...webSearchConfig, enabled: newEnabled };
    setWebSearchConfig(updated);
    try {
      await fetch('/api/settings/web-search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updated),
      });
    } catch (_) {}
  };

  const handleOpenMcp = () => {
    setMcpUrlDraft(webSearchConfig?.mcp_url || '');
    setMcpToolDraft(webSearchConfig?.mcp_tool || 'web_search');
    setMcpApiKeyDraft(webSearchConfig?.mcp_api_key || '');
    const provider = webSearchConfig?.provider;
    const isMcp = provider ? (provider === 'mcp') : !!(webSearchConfig?.mcp_url);
    setUseMcpDraft(isMcp);
    setMcpTestStatus('');
    setShowMcpPanel(p => !p);
  };

  const handleSaveMcp = async () => {
    const updated = {
      ...webSearchConfig,
      mcp_url: mcpUrlDraft.trim(),
      mcp_tool: mcpToolDraft.trim() || 'web_search',
      mcp_api_key: mcpApiKeyDraft.trim(),
      provider: useMcpDraft ? 'mcp' : 'duckduckgo',
    };
    setWebSearchConfig(updated);
    try {
      await fetch('/api/settings/web-search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updated),
      });
      setShowMcpPanel(false);
    } catch (_) {}
  };

  const handleTestMcp = async () => {
    setMcpTestStatus('testing');
    try {
      const res = await fetch('/api/settings/web-search/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mcp_url: mcpUrlDraft.trim(),
          mcp_tool: mcpToolDraft.trim() || 'web_search',
          mcp_api_key: mcpApiKeyDraft.trim(),
        }),
      });
      const data = await res.json();
      setMcpTestStatus(data.ok ? 'ok' : `error:${data.error || 'Unknown error'}`);
    } catch (e) {
      setMcpTestStatus(`error:${e.message}`);
    }
  };

  const handleFormSubmit = (e) => {
    if (e) e.preventDefault();
    if ((!chatInput.trim() && (!pendingAttachments || pendingAttachments.length === 0)) || !activeProject) return;
    const text = chatInput.trim();
    if (text) {
      setInputHistory(prev => {
        if (prev[prev.length - 1] === text) return prev;
        const newHist = [...prev, text].slice(-100);
        setPreference('chatInputHistory', JSON.stringify(newHist));
        return newHist;
      });
    }
    setHistoryIndex(-1);
    setTempInput('');
    handleSendMessage(e);
  };

  const runChatAction = useCallback(async (action) => {
    try {
      await action();
    } catch (err) {
      const message = err?.message || String(err);
      console.error('Chat action failed:', err);
      showAlert?.(t('chatPanel.actionFailed', 'Could not start this chat action: {{message}}', { message }));
    }
  }, [showAlert, t]);

  const handleKeyDown = (e) => {
    // Font zoom shortcuts: Ctrl+= / Ctrl+Plus, Ctrl+-, Ctrl+0
    if (e.ctrlKey && !e.altKey && !e.metaKey) {
      if (e.key === '=' || e.key === '+') {
        e.preventDefault();
        zoomIn();
        return;
      }
      if (e.key === '-') {
        e.preventDefault();
        zoomOut();
        return;
      }
      if (e.key === '0') {
        e.preventDefault();
        zoomReset();
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleFormSubmit(null);
    } else if (e.ctrlKey && !e.altKey && !e.metaKey && e.key === 'ArrowUp') {
      if (inputHistory.length === 0) return;
      e.preventDefault();
      const newIndex = historyIndex === -1
        ? inputHistory.length - 1
        : Math.max(0, historyIndex - 1);
      if (historyIndex === -1) setTempInput(chatInput);
      setHistoryIndex(newIndex);
      setChatInput(inputHistory[newIndex]);
    } else if (e.ctrlKey && !e.altKey && !e.metaKey && e.key === 'ArrowDown') {
      if (historyIndex === -1) return;
      e.preventDefault();
      if (historyIndex === inputHistory.length - 1) {
        setHistoryIndex(-1);
        setChatInput(tempInput);
      } else {
        const newIndex = historyIndex + 1;
        setHistoryIndex(newIndex);
        setChatInput(inputHistory[newIndex]);
      }
    } else if (e.key === 'Escape') {
      if (historyIndex !== -1) {
        e.preventDefault();
        setHistoryIndex(-1);
        setChatInput(tempInput);
      }
    }
  };

  const hasMcp = webSearchConfig?.provider === 'mcp' && !!(webSearchConfig?.mcp_url);

  // A local model runs on the user's own machine with a small context window,
  // and every attachment competes with the conversation for it. Cloud models
  // take much larger batches, so the warning is scoped to the local case.
  const usesLocalModel = isLocalModelId(activeProject?.model, activeProject?.api_base);
  const showLocalAttachmentWarning = usesLocalModel && (pendingAttachments?.length || 0) >= 2;

  // ---- Attachment helpers ----
  const supportedAttachmentMimes = new Set([
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  ]);
  const supportedAttachmentExtensions = new Set(['.pdf', '.docx', '.pptx']);

  const getFileExtension = (filename = '') => {
    const dot = filename.lastIndexOf('.');
    return dot >= 0 ? filename.slice(dot).toLowerCase() : '';
  };

  const isSupportedAttachment = (file) => {
    const mime = file.type || '';
    return mime.startsWith('image/')
      || supportedAttachmentMimes.has(mime)
      || supportedAttachmentExtensions.has(getFileExtension(file.name));
  };

  const getUploadMime = (file) => {
    if (file.type) return file.type;
    const ext = getFileExtension(file.name);
    if (ext === '.pdf') return 'application/pdf';
    if (ext === '.docx') return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
    if (ext === '.pptx') return 'application/vnd.openxmlformats-officedocument.presentationml.presentation';
    return 'application/octet-stream';
  };

  const uploadFile = async (file) => {
    const reader = new FileReader();
    return new Promise((resolve, reject) => {
      reader.onload = async (ev) => {
        const dataUrl = ev.target.result;
        const base64 = dataUrl.split(',')[1];
        try {
          const res = await fetch('/api/chat/upload', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              filename: file.name,
              data_b64: base64,
              mime: getUploadMime(file),
              project_name: activeProject?.name,
            }),
          });
          if (!res.ok) {
            let serverMsg = `Upload failed: ${res.status}`;
            try {
              const errBody = await res.json();
              if (errBody?.error) serverMsg = errBody.error;
            } catch (_) {}
            throw new Error(serverMsg);
          }
          const descriptor = await res.json();
          // keep original data URL for image preview in the UI
          descriptor._previewUrl = file.type?.startsWith('image/') ? dataUrl : null;
          resolve(descriptor);
        } catch (err) {
          reject(err);
        }
      };
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  };

  const processFiles = async (files) => {
    if (!files || files.length === 0) return;
    setUploadingFiles(true);
    const results = [];
    for (const f of Array.from(files)) {
      if (!isSupportedAttachment(f)) continue;
      try {
        const desc = await uploadFile(f);
        results.push(desc);
      } catch (err) {
        console.error('Attachment upload failed:', err);
        // Show the error in chat so the user knows why the attachment was not added
        setMessages(prev => [...(prev || []), {
          role: 'system',
          content: `⚠️ Attachment "${f.name}" could not be uploaded: ${err.message}`,
        }]);
      }
    }
    setPendingAttachments(prev => [...(prev || []), ...results]);
    setUploadingFiles(false);
  };

  const removeAttachment = (idx) => {
    setPendingAttachments(prev => prev.filter((_, i) => i !== idx));
  };

  const handleFileInputChange = (e) => {
    processFiles(e.target.files);
    e.target.value = '';
  };

  // Reads the system clipboard directly. Used when the paste event carried no
  // file, which is what happens in the embedded QtWebEngine shell for an image
  // copied outside the app. Returns true when an image was attached.
  const attachClipboardImage = async () => {
    if (!activeProject || isAgentRunning) return false;
    try {
      const image = await readClipboardImage();
      if (!image) return false;
      const file = image.blob
        ? new File([image.blob], pastedImageName(image.mime), { type: image.mime })
        : base64ImageToFile(image.data_b64, image.mime);
      await processFiles([file]);
      return true;
    } catch (err) {
      console.error('Clipboard image paste failed:', err);
      return false;
    }
  };

  useEffect(() => {
    attachClipboardImageRef.current = attachClipboardImage;
  });

  const handleInputPaste = (e) => {
    if (!activeProject || isAgentRunning) return;
    const files = extractClipboardFiles(e.clipboardData).filter(isSupportedAttachment);
    if (files.length > 0) {
      // An image copied from a page also carries a text/html twin; without this
      // the markup would be inserted into the box alongside the attachment.
      e.preventDefault();
      processFiles(files);
      return;
    }
    // clipboardData is unusable after the first await, so decide before it.
    if (clipboardHasText(e.clipboardData)) return;
    attachClipboardImage();
  };

  const handleDragOver = (e) => { e.preventDefault(); setIsDragOver(true); };
  const handleDragLeave = () => setIsDragOver(false);
  const handleDrop = (e) => {
    e.preventDefault();
    setIsDragOver(false);
    processFiles(e.dataTransfer.files);
  };

  const handleCreateChatClick = () => {
    if (!activeProject) return;
    setNewChatName(`Chat ${chats.length + 1}`);
    setShowNewChatPrompt(true);
  };

  const submitNewChat = async (e) => {
    e?.preventDefault();
    if (!activeProject || !newChatName.trim()) return;

    try {
      const res = await fetch('/api/chat/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_name: activeProject.name, chat_name: newChatName.trim() })
      });
      if (!res.ok) throw new Error('Failed to create chat');
      const data = await res.json();
      setChats(prev => [data, ...prev]);
      setActiveChatId(data.id);
      // A brand-new chat has no measured window yet: the previous chat's
      // measurement must not leak into this one's indicator.
      setChatContextUsage?.(null);

      const greeting = activeProject.project_name || activeProject.name;
      setChatMessages([{ role: 'assistant', content: t('app.greeting', { projectName: greeting }) }]);
      setShowNewChatPrompt(false);
    } catch (e) {
      console.error(e);
    }
  };

  const handleCreateChat = async () => {
    // Legacy fallback, replaced by handleCreateChatClick
  };

  const handleDeleteChatClick = (id, e) => {
    e.stopPropagation();
    if (!activeProject || !id) return;
    setChatToDelete(id);
  };

  const confirmDeleteChat = async () => {
    if (!activeProject || !chatToDelete) return;
    const id = chatToDelete;

    try {
      const res = await fetch('/api/chat/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_name: activeProject.name, chat_id: id })
      });
      if (!res.ok) throw new Error('Failed to delete chat');
      const newChats = chats.filter(c => c.id !== id);
      setChats(newChats);
      if (activeChatId === id) {
        // Open the most recent chat left (the list is newest first), or none.
        handleSwitchChat(newChats[0]?.id || '');
      }
      setChatToDelete(null);
    } catch (err) {
      console.error(err);
      setChatToDelete(null);
    }
  };

  // Clearing the chat is a server-side operation, not a UI reset: it erases the
  // stored conversation and the orchestrator's working memory. Hiding the
  // bubbles while the agent still remembers everything would misrepresent both
  // the conversation and the context indicator.
  const handleClearChat = async () => {
    if (!activeProject || isAgentRunning) return;

    setShowChatActionsMenu(false);
    const confirmed = await showConfirm(
      t('chatPanel.clearChatConfirmation', 'This will permanently delete this chat\'s history and the agent\'s memory of it. This cannot be undone.'),
      t('chatPanel.clearChatTitle', 'Clear chat')
    );
    if (!confirmed) return;

    try {
      const res = await fetch('/api/chat/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_name: activeProject.name, chat_id: activeChatId }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Request failed: ${res.status}`);

      setChatInput('');
      setPendingAttachments([]);
      setChatContextUsage?.(null);
      onClearChat?.();
    } catch (err) {
      console.error('Failed to clear chat:', err);
      showAlert(t('chatPanel.clearChatFailed', 'Could not clear the chat: {{message}}', {
        message: err?.message || String(err),
      }));
    }
  };

  const handleClearAllChats = async () => {
    if (!activeProject || isAgentRunning) return;

    setShowChatActionsMenu(false);
    const confirmed = await showConfirm(
      t('chatPanel.clearAllChatsConfirmation', 'This will permanently delete every chat and its history for this project. This cannot be undone.'),
      t('chatPanel.clearAllChatsTitle', 'Delete all chats')
    );
    if (!confirmed) return;

    try {
      const res = await fetch('/api/chat/clear-all', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_name: activeProject.name }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Request failed: ${res.status}`);

      // The project is left with no chat: nothing is open until the user
      // creates one or sends a message, which starts one.
      setChats([]);
      setChatInput('');
      setPendingAttachments([]);
      handleSwitchChat('');
    } catch (err) {
      console.error('Failed to clear all chats:', err);
      showAlert(t('chatPanel.clearAllChatsFailed', 'Could not delete all chats: {{message}}', {
        message: err?.message || String(err),
      }));
    }
  };

  const handleSwitchChat = onSwitchChat;

  const handleExportMarkdown = () => {
    if (!chatMessages || chatMessages.length === 0) return;
    let md = `# Chat Export - ${activeProject?.name || 'GilzaBots'}\n\n`;
    chatMessages.filter(msg => !isHiddenChatSystemMessage(msg)).forEach(msg => {
      const role = msg.role === 'user' ? 'User' : 'GilzaBots';
      md += `### ${role}\n\n${msg.content}\n\n---\n\n`;
    });
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `chat_export_${new Date().toISOString().split('T')[0]}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleBranchChat = async (messageIndex, message) => {
    const newChatName = await showPrompt(t('chatPanel.branchPrompt', 'Name for the new chat (branch):'));
    if (!newChatName) return;
 
    try {
      const projectName = activeProject?.name || '';
      const selectedMessageId = numericMessageId(message);
      const selectedClientMessageId = String(message?.client_message_id || '').trim();
      const sourceChatId = message?.chat_id || activeChatId;
      // Branch from the stored message itself. A UI position is not a usable
      // anchor: rendered messages and stored rows do not line up.
      if (selectedMessageId === null && !selectedClientMessageId) {
        await showAlert(t('chatPanel.branchAnchorMissing', 'This message cannot be branched: it has no stored identifier.'));
        return;
      }
      const payload = {
        project_name: projectName,
        source_chat_id: sourceChatId,
        new_chat_name: newChatName,
      };
      if (selectedMessageId !== null) {
        payload.message_id = selectedMessageId;
      } else {
        payload.client_message_id = selectedClientMessageId;
      }
      const res = await fetch('/api/chat/branch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      
      if (res.ok) {
        const data = await res.json();
        const listRes = await fetch(`/api/chat/list?project_name=${encodeURIComponent(projectName)}&t=${Date.now()}`);
        if (listRes.ok) {
          const listData = await listRes.json();
          setChats(listData.chats || []);
        }
        if (handleSwitchChat) {
          handleSwitchChat(data.new_chat_id);
        }
      } else {
        const err = await res.json();
        console.error('Failed to branch chat:', err.error);
        await showAlert(t('app.error', 'Erro:') + ' ' + (err.error || 'Failed to branch chat'));
      }
    } catch (e) {
      console.error(e);
      await showAlert(t('app.error', 'Erro:') + ' ' + e.message);
    }
  };

  const handleStartEditMessage = (messageIndex, message) => {
    setEditingMessageIndex(messageIndex);
    setEditingMessageDraft(message.content || '');
    setEditingMessageError('');
  };

  const handleCancelEditMessage = () => {
    setEditingMessageIndex(null);
    setEditingMessageDraft('');
    setEditingMessageError('');
  };

  // The editor closes only once the edit is accepted. A refused edit keeps the
  // text and shows why, so it is never lost and never has to be re-sent
  // another way (sending it from the composer appends it after the very turns
  // the edit was meant to replace).
  const handleSubmitEditMessage = async (messageIndex, message) => {
    if (editSubmitInFlightRef.current) return;
    const nextContent = editingMessageDraft.trim();
    if (!nextContent) return;
    if (nextContent === message.content) {
      handleCancelEditMessage();
      return;
    }
    editSubmitInFlightRef.current = true;
    setIsSubmittingEdit(true);
    setEditingMessageError('');
    let result;
    try {
      result = onEditUserMessage
        ? await onEditUserMessage(messageIndex, message, nextContent)
        : { accepted: false, error: t('chatPanel.editUnavailable', 'Editing is not available here.') };
    } catch (err) {
      result = { accepted: false, error: err?.message || String(err) };
    } finally {
      editSubmitInFlightRef.current = false;
      setIsSubmittingEdit(false);
    }
    if (result?.accepted) {
      handleCancelEditMessage();
      return;
    }
    setEditingMessageError(result?.error || t('chatPanel.editNotApplied', 'The edit was not applied.'));
  };

  const handleEditMessageKeyDown = (e, messageIndex, message) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      handleCancelEditMessage();
      return;
    }
    // Same keys as the composer: Enter saves, Shift+Enter starts a new line.
    // Not while an input method is still composing a character.
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent?.isComposing) {
      e.preventDefault();
      if (!isAgentRunning) handleSubmitEditMessage(messageIndex, message);
    }
  };

  const handleExportSingleMessage = (content) => {
    if (!content) return;
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    // Use ISO string but make it filename-safe
    const timeStr = new Date().toISOString().replace(/[:.]/g, '-');
    a.download = `agent_response_${timeStr}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };


  const handlePrintPDF = () => {
    document.body.classList.remove('printing-editor');
    window.print();
  };

  const contentWithoutThink = (content = '') => (
    stripInlineReasoning(content).trim()
  );

  const isInternalResumePrompt = (content = '') => {
    const text = String(content).trim();
    return (
      text.startsWith('Continue the task that was interrupted. Do not restart from scratch.') &&
      text.includes('## Captured agent activity before interruption')
    );
  };

  const modelSelectorGroupStyle = {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    flex: '1 1 260px',
    minWidth: 0,
    maxWidth: '100%',
    flexWrap: 'wrap',
  };
  const modelSelectorLabelStyle = {
    fontSize: '11px',
    color: 'var(--vscode-descriptionForeground)',
    flex: '0 0 auto',
    whiteSpace: 'nowrap',
  };
  const modelSelectorInputStyle = {
    flex: '1 1 150px',
    minWidth: 0,
    maxWidth: '100%',
    padding: '2px 4px',
    fontSize: '11px',
    height: '22px',
  };

  // Token battery calculation.
  //
  // The authoritative number is prompt_tokens from the provider, reported by the
  // backend after every LLM call: it is the only measurement that includes the
  // system prompt, the tool JSON schemas, the tool calls and the tool results —
  // and those, not the visible bubbles, are what fills an agent context window.
  //
  // The character estimate below is a fallback for the state before the first
  // measured call of a conversation. It is deliberately not mixed into the
  // measured value: adding a guess on top of a measurement would make the gauge
  // wrong in a way that looks precise.
  const measuredTokens = chatContextUsage?.promptTokens || 0;
  const isMeasuredContext = measuredTokens > 0;
  // A count of the restored working state, for a conversation whose last turn
  // predates the persisted measurement. It is a real token count of what the
  // agent will replay, but it omits the tool schemas the provider also charges
  // for, so it is labelled as its own thing rather than as the provider number.
  const isDerivedContext = isMeasuredContext && chatContextUsage?.source === 'state';
  // 0 means the window is unknown: the model is on "Auto" and its provider did
  // not report one, so GilzaBots applies no limit of its own. A measurement
  // carries the window it was taken against; without one the project's
  // resolved window is used. Never a guessed size -- a battery drawn against an
  // invented window reads as a real limit.
  const numCtx = chatContextUsage
    ? (chatContextUsage.contextWindow || 0)
    : (parseInt(activeProject?.model_params?.num_ctx || activeProject?.agent_params?.max_context_tokens || activeProject?.effective_num_ctx || 0, 10) || 0);
  const isWindowKnown = numCtx > 0;
  const historyTokens = chatMessages.reduce((acc, msg) => {
    const contentLen = msg.content?.length || 0;
    const thoughtLen = msg._thoughtStream?.length || 0;
    return acc + Math.ceil((contentLen + thoughtLen) / 4);
  }, 0);
  const liveStreamTokens = Math.ceil(((chatThoughtStream?.length || 0) + (chatResponseStream?.length || 0)) / 4);
  const estimatedTokens = historyTokens + liveStreamTokens;
  const usedTokens = isMeasuredContext ? measuredTokens : estimatedTokens;
  const availableTokens = isWindowKnown ? Math.max(0, numCtx - usedTokens) : 0;
  // The bar drains like a battery, and the number states the remaining charge
  // (available tokens) too — a battery reads its own level, not how much was used.
  const remainingPercentage = isWindowKnown ? Math.min(100, Math.max(0, (availableTokens / numCtx) * 100)) : 100;
  const isTokenExploded = isWindowKnown && availableTokens === 0;
  // Cheia (verde), perto do limite (amarela), explodiu (vermelha)
  const batteryColor = !isWindowKnown
    ? 'var(--vscode-descriptionForeground, #999999)'
    : isTokenExploded ? 'var(--battery-exploded)' : remainingPercentage <= 20 ? 'var(--battery-low)' : 'var(--battery-good)';
  const formatTokens = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K` : `${n}`);

  // Effort / heartbeat budget selector. It sits in its own toolbar above the
  // input in the full panel and inside the input row in the compact one.
  const heartbeatSelector = (() => {
    const projectHeartbeats = activeProject?.model_params?.max_heartbeats ?? 50;
    const heartbeatOptions = [
      {
        key: 'low',
        heartbeats: 20,
        label: t('chatPanel.heartbeatModeLow', 'Low'),
        desc: t('chatPanel.heartbeatModeLowDesc', 'Fast (max 20 heartbeats)'),
        color: 'var(--vscode-fg-teal, #4ec9b0)',
      },
      {
        key: 'medium',
        heartbeats: 50,
        label: t('chatPanel.heartbeatModeMedium', 'Medium'),
        desc: t('chatPanel.heartbeatModeMediumDesc', 'Standard (max 50 heartbeats)'),
        color: '#e5a84b',
      },
      {
        key: 'high',
        heartbeats: 100,
        label: t('chatPanel.heartbeatModeHigh', 'High'),
        desc: t('chatPanel.heartbeatModeHighDesc', 'Thorough (max 100 heartbeats)'),
        color: '#c586c0',
      },
      {
        key: 'custom',
        heartbeats: projectHeartbeats,
        label: t('chatPanel.heartbeatModeCustom', 'Custom'),
        desc: t('chatPanel.heartbeatModeCustomDesc', 'Project settings ({{count}} heartbeats)', { count: projectHeartbeats }),
        color: 'var(--vscode-descriptionForeground, #999999)',
      },
    ];
    const currentOption = heartbeatOptions.find(opt => opt.key === heartbeatMode) || heartbeatOptions[1];
    const currentButtonLabel = heartbeatMode === 'custom'
      ? `${currentOption.label} (${projectHeartbeats})`
      : `${currentOption.label} (${currentOption.heartbeats})`;

    return (
      <div className="vscode-chat-mode-selector-wrap" ref={heartbeatMenuRef}>
        <button
          type="button"
          className="vscode-chat-mode-btn"
          onClick={() => setShowHeartbeatMenu(prev => !prev)}
          aria-expanded={showHeartbeatMenu}
          aria-haspopup="menu"
          title={t('chatPanel.heartbeatModeTooltip', 'Execution heartbeat budget per turn')}
        >
          <Zap size={12} style={{ color: currentOption.color, flexShrink: 0 }} />
          <span className="vscode-chat-mode-btn-label">{currentButtonLabel}</span>
          <ChevronDown size={11} style={{ opacity: 0.7, flexShrink: 0 }} />
        </button>
        {showHeartbeatMenu && (
          <div className="vscode-chat-mode-menu" role="menu">
            <div className="vscode-chat-mode-menu-header">
              {t('chatPanel.heartbeatMode', 'Effort / Budget')}
            </div>
            {heartbeatOptions.map((opt) => {
              const isSelected = opt.key === heartbeatMode;
              return (
                <button
                  key={opt.key}
                  type="button"
                  className={`vscode-chat-mode-menu-item ${isSelected ? 'selected' : ''}`}
                  onClick={() => {
                    setHeartbeatMode?.(opt.key);
                    setShowHeartbeatMenu(false);
                  }}
                  role="menuitem"
                >
                  <div className="vscode-chat-mode-item-left">
                    <Zap size={13} style={{ color: opt.color, flexShrink: 0, marginTop: '2px' }} />
                    <div className="vscode-chat-mode-item-text">
                      <div className="vscode-chat-mode-item-title">
                        {opt.label} ({opt.heartbeats})
                      </div>
                      <div className="vscode-chat-mode-item-desc">{opt.desc}</div>
                    </div>
                  </div>
                  {isSelected && <Check size={14} className="vscode-chat-mode-item-check" />}
                </button>
              );
            })}
          </div>
        )}
      </div>
    );
  })();

  // Reasoning effort of this one chat. It is stored on the chat itself, so it
  // follows the chat across reloads and the backend reads it when the next
  // turn starts; nothing travels with the message.
  const handleChatReasoningEffortChange = async (effort) => {
    setShowReasoningMenu(false);
    if (!activeProject || !activeChatId) return;
    if (effort === chatReasoningEffort(chats, activeChatId)) return;
    try {
      const res = await fetch('/api/chat/reasoning-effort', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_name: activeProject.name, chat_id: activeChatId, reasoning_effort: effort }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      setChats(prev => withChatReasoningEffort(prev, data.chat_id, data.reasoning_effort));
    } catch (err) {
      showAlert(t('chatPanel.reasoningEffortSaveFailed', 'Could not save the reasoning effort: {{message}}', {
        message: err?.message || String(err),
      }));
    }
  };

  const reasoningSelector = (() => {
    const chatEffort = chatReasoningEffort(chats, activeChatId);
    const catalog = catalogReasoning(globalModels, activeProject?.model || '');
    // A worker without its own model runs on the orchestrator's (backend:
    // `project_worker = worker_model or model`).
    const workerCatalog = catalogReasoning(globalModels, activeProject?.worker_model || activeProject?.model || '');
    const anyModelReasons = catalog.supportsThinking || workerCatalog.supportsThinking;
    const levelLabels = {
      low: t('chatPanel.reasoningEffortLow', 'Low'),
      medium: t('chatPanel.reasoningEffortMedium', 'Medium'),
      high: t('chatPanel.reasoningEffortHigh', 'High'),
      xhigh: t('chatPanel.reasoningEffortXHigh', 'Extra high'),
    };
    const reasoningOptions = [
      {
        key: '',
        label: t('chatPanel.reasoningEffortDefault', 'Model default'),
        desc: levelLabels[catalog.effort]
          ? t('chatPanel.reasoningEffortDefaultDescValue', 'Follows the model catalog ({{level}})', { level: levelLabels[catalog.effort] })
          : t('chatPanel.reasoningEffortDefaultDesc', 'Follows the model catalog'),
        color: 'var(--vscode-descriptionForeground, #999999)',
      },
      {
        key: 'low',
        label: levelLabels.low,
        desc: t('chatPanel.reasoningEffortLowDesc', 'Quick answers, little thinking'),
        color: 'var(--vscode-fg-teal, #4ec9b0)',
      },
      {
        key: 'medium',
        label: levelLabels.medium,
        desc: t('chatPanel.reasoningEffortMediumDesc', 'Balanced'),
        color: '#e5a84b',
      },
      {
        key: 'high',
        label: levelLabels.high,
        desc: t('chatPanel.reasoningEffortHighDesc', 'Thinks longer before answering'),
        color: '#c586c0',
      },
      {
        key: 'xhigh',
        label: levelLabels.xhigh,
        desc: t('chatPanel.reasoningEffortXHighDesc', 'Deepest level the provider offers'),
        color: '#d16969',
      },
    ];
    const currentOption = reasoningOptions.find(opt => opt.key === chatEffort) || reasoningOptions[0];
    // The override applies to the orchestrator and its workers, but only
    // reaches a model whose catalog entry declares thinking; with neither model
    // reasoning, a control that silently does nothing would read as a broken one.
    const unavailable = !activeProject || !activeChatId || !anyModelReasons;
    const title = !activeProject || !activeChatId || anyModelReasons
      ? t('chatPanel.reasoningEffortTooltip', "Reasoning effort of this chat's orchestrator and workers. Applies from the next message.")
      : t('chatPanel.reasoningEffortUnsupported', 'Neither the orchestrator nor the worker model declares thinking support, so there is no reasoning effort to set. Enable it in Edit Models if a model reasons.');

    return (
      <div className="vscode-chat-mode-selector-wrap" ref={reasoningMenuRef}>
        <button
          type="button"
          className="vscode-chat-mode-btn"
          onClick={() => setShowReasoningMenu(prev => !prev)}
          disabled={unavailable}
          aria-expanded={showReasoningMenu}
          aria-haspopup="menu"
          aria-label={t('chatPanel.reasoningEffort', 'Reasoning effort (this chat)')}
          title={title}
        >
          <Brain size={12} style={{ color: currentOption.color, flexShrink: 0 }} />
          <span className="vscode-chat-mode-btn-label">{currentOption.label}</span>
          <ChevronDown size={11} style={{ opacity: 0.7, flexShrink: 0 }} />
        </button>
        {showReasoningMenu && !unavailable && (
          <div className="vscode-chat-mode-menu" role="menu">
            <div className="vscode-chat-mode-menu-header">
              {t('chatPanel.reasoningEffort', 'Reasoning effort (this chat)')}
            </div>
            {reasoningOptions.map((opt) => {
              const isSelected = opt.key === chatEffort;
              return (
                <button
                  key={opt.key || 'default'}
                  type="button"
                  className={`vscode-chat-mode-menu-item ${isSelected ? 'selected' : ''}`}
                  onClick={() => handleChatReasoningEffortChange(opt.key)}
                  role="menuitem"
                >
                  <div className="vscode-chat-mode-item-left">
                    <Brain size={13} style={{ color: opt.color, flexShrink: 0, marginTop: '2px' }} />
                    <div className="vscode-chat-mode-item-text">
                      <div className="vscode-chat-mode-item-title">{opt.label}</div>
                      <div className="vscode-chat-mode-item-desc">{opt.desc}</div>
                    </div>
                  </div>
                  {isSelected && <Check size={14} className="vscode-chat-mode-item-check" />}
                </button>
              );
            })}
          </div>
        )}
      </div>
    );
  })();

  if (!isChatVisible) return null;

  return (
    <aside 
      className={`vscode-chat${isCompact ? ' is-compact' : ''}`}
      style={isChatMode || fillContainer
        ? { flex: 1, minWidth: 0, borderLeft: 'none', width: '100%' }
        : { width: `${chatWidth}px` }}
    >
      <TextContextMenu
        menu={menu}
        onCopy={handleCopy}
        onPaste={handlePaste}
        onSelectAll={() => handleSelectAll(historyRef)}
        onAskAbout={handleQuoteExcerpt}
        onLookUp={handleLookUpExcerpt}
        onTranslate={handleTranslateExcerpt}
        onPronounce={handlePronounceExcerpt}
        canPronounce={speechAvailability.ready}
        pronounceUnavailableHint={speechAvailability.problem}
      />

      <TranslationPopup
        state={translation}
        onClose={closeTranslation}
        onRetry={retryTranslation}
        onCopy={copyTranslation}
        onSpeak={speechAvailability.ready ? speak : undefined}
      />

      <SnippetQueryPopup
        state={lookup}
        projectPath={activeProject?.project_path || ''}
        onClose={closeLookup}
        onQuestionChange={setLookupQuestion}
        onSubmit={submitLookup}
        onCompose={composeLookup}
        onCopy={copyLookup}
        onSaveMarkdown={saveLookupMarkdown}
      />

      <SpeechPopup
        state={speech}
        onClose={closeSpeech}
        onToggle={toggleSpeech}
        onReplay={replaySpeech}
        onRetry={retrySpeech}
      />
      {/* Header */}
      <div className={`vscode-chat-header${isCompact ? ' is-compact' : ''}`}>
        <span className="vscode-sidebar-title vscode-chat-header-title" style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <MessageSquare size={12} style={{ color: 'var(--vscode-accent)', flexShrink: 0 }} />
          {isCompact ? (
            // The chat's name is the title here, and picking another one is the
            // most frequent thing the full chat-selector row is used for.
            <select
              className="vscode-chat-compact-select"
              value={activeChatId}
              onChange={(e) => handleSwitchChat(e.target.value)}
              title={t('chatPanel.switchChat', 'Switch chat')}
              aria-label={t('chatPanel.switchChat', 'Switch chat')}
            >
              {chats.map(c => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
          ) : (
            <span className="vscode-chat-header-title-text">{t('chatPanel.header')}</span>
          )}
        </span>
        <div className="vscode-chat-header-actions" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {isAgentRunning && (
            <span
              className="chat-msg-badge chat-msg-badge-step-running"
              title={t('chatPanel.stepsExecutedTooltip', 'Steps executed in this turn: {{step}}{{max}}', {
                step: agentStepInfo?.step ?? 0,
                max: agentStepInfo?.maxSteps ? ` / ${agentStepInfo.maxSteps}` : '',
              })}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '4px',
                fontSize: '11px',
                padding: '2px 7px',
                borderRadius: '8px',
                flexShrink: 0,
              }}
            >
              <Activity size={11} className="spin-slow" />
              <span>
                {t('chatPanel.stepsCount', 'Steps: {{step}}{{max}}', {
                  step: agentStepInfo?.step ?? 0,
                  max: agentStepInfo?.maxSteps ? `/${agentStepInfo.maxSteps}` : '',
                })}
              </span>
            </span>
          )}
          <div
            className="vscode-chat-context-badge"
            title={t(
              !isWindowKnown
                ? 'chatPanel.contextUnknownWindow'
                : isDerivedContext
                  ? 'chatPanel.contextDerived'
                  : isMeasuredContext ? 'chatPanel.contextMeasured' : 'chatPanel.contextEstimated',
              { used: usedTokens, available: availableTokens, total: numCtx },
            )}
            style={{
              display: 'flex', alignItems: 'center', gap: '4px',
              marginRight: '8px', cursor: 'help',
              opacity: 0.9,
              flexShrink: 0,
              fontSize: '11px',
              color: batteryColor
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '1px' }}>
              <div style={{
                width: '16px', height: '9px', 
                border: `1px solid ${batteryColor}`, 
                borderRadius: '2px', 
                padding: '1px',
                display: 'flex'
              }}>
                <div style={{
                  width: `${Math.min(100, remainingPercentage)}%`,
                  backgroundColor: batteryColor,
                  height: '100%',
                  transition: 'width 0.3s'
                }} />
              </div>
              <div style={{
                width: '2px', height: '3px',
                backgroundColor: batteryColor,
                borderRadius: '0 1px 1px 0'
              }} />
            </div>
            <span style={{ whiteSpace: 'nowrap' }}>
              {isWindowKnown ? `${Math.round(remainingPercentage)}%` : formatTokens(usedTokens)}
              <span className="vscode-chat-context-detail" style={{ opacity: 0.75, marginLeft: '4px' }}>
                {isWindowKnown ? `${formatTokens(availableTokens)}/${formatTokens(numCtx)}` : '/ ?'}
              </span>
            </span>
          </div>
          {!isCompact && (<>
          <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginRight: '4px', flexShrink: 0 }}>
            <input 
              type="checkbox" 
              id="hide-think-cb" 
              checked={hideThink}
              onChange={e => {
                const val = e.target.checked;
                setHideThink(val);
                setPreference('chatHideThink', val);
              }}
              style={{ cursor: 'pointer', margin: 0 }}
              title={t('chatPanel.hideThinkTooltip', 'Ocultar blocos <think> do chat')}
            />
            <label htmlFor="hide-think-cb" className="vscode-chat-hide-think-label" style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)', cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap' }} title={t('chatPanel.hideThinkTooltip', 'Ocultar blocos <think> do chat')}>
              {t('chatPanel.hideThink', 'Hide Think')}
            </label>
          </div>
          <button
            onClick={zoomOut}
            disabled={chatZoom <= MIN_ZOOM + 1e-9}
            className="vscode-chat-header-btn"
            style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--vscode-text-fg)', opacity: chatZoom <= MIN_ZOOM + 1e-9 ? 0.4 : 1 }}
            title={t('chatPanel.zoomOut', 'Diminuir Zoom')}
          >
            <ZoomOut size={14} />
          </button>
          <button
            onClick={zoomReset}
            className="vscode-chat-header-btn"
            style={{
              background: 'transparent', border: '1px solid var(--vscode-border, #3c3c3c)',
              borderRadius: '3px', cursor: 'pointer', color: 'var(--vscode-text-fg)',
              fontSize: '11px', lineHeight: 1, padding: '2px 5px', minWidth: '34px',
            }}
            title={t('chatPanel.zoomReset', 'Restaurar Zoom')}
          >
            {Math.round(chatZoom * 100)}%
          </button>
          <button
            onClick={zoomIn}
            disabled={chatZoom >= MAX_ZOOM - 1e-9}
            className="vscode-chat-header-btn"
            style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--vscode-text-fg)', opacity: chatZoom >= MAX_ZOOM - 1e-9 ? 0.4 : 1 }}
            title={t('chatPanel.zoomIn', 'Aumentar Zoom')}
          >
            <ZoomIn size={14} />
          </button>
          </>)}
          {isCompact && (
            <button
              ref={compactControlsToggleRef}
              type="button"
              onClick={() => setShowCompactControls(prev => !prev)}
              className={`vscode-chat-header-btn vscode-chat-compact-toggle${showCompactControls ? ' active' : ''}`}
              title={showCompactControls
                ? t('chatPanel.hideControls', 'Hide chat controls')
                : t('chatPanel.showControls', 'Chats, models and web search')}
              aria-label={showCompactControls
                ? t('chatPanel.hideControls', 'Hide chat controls')
                : t('chatPanel.showControls', 'Chats, models and web search')}
              aria-expanded={showCompactControls}
              aria-controls="chat-compact-controls"
            >
              <SlidersHorizontal size={14} />
            </button>
          )}
          <div ref={chatActionsMenuRef} className="vscode-overflow-menu-wrap" style={{ flexShrink: 0 }}>
            <button
              onClick={() => setShowChatActionsMenu(prev => !prev)}
              className="vscode-bottom-panel-clear-btn"
              style={{ padding: '4px' }}
              title={t('chatPanel.moreActions', 'Mais acoes')}
              aria-label={t('chatPanel.moreActions', 'Mais acoes')}
              aria-expanded={showChatActionsMenu}
            >
              <MoreHorizontal size={14} />
            </button>
            {showChatActionsMenu && (
              <div className="vscode-overflow-menu" role="menu">
                {isCompact && (
                  <>
                    {/* The header's own copies of these are dropped in the
                        compact chat; they stay reachable from here. */}
                    <button
                      type="button"
                      className="vscode-overflow-menu-item"
                      onClick={() => {
                        const val = !hideThink;
                        setHideThink(val);
                        setPreference('chatHideThink', val);
                      }}
                      role="menuitemcheckbox"
                      aria-checked={hideThink}
                      title={t('chatPanel.hideThinkTooltip', 'Ocultar blocos <think> do chat')}
                    >
                      <Brain size={14} />
                      <span style={{ flex: 1 }}>{t('chatPanel.hideThink', 'Hide Think')}</span>
                      {hideThink && <Check size={14} />}
                    </button>
                    <div className="vscode-overflow-menu-item vscode-overflow-menu-zoom-row" role="group" aria-label={t('chatPanel.zoom', 'Zoom')}>
                      <ZoomIn size={14} style={{ visibility: 'hidden' }} aria-hidden="true" />
                      <span style={{ flex: 1 }}>{t('chatPanel.zoom', 'Zoom')}</span>
                      <button
                        type="button"
                        onClick={zoomOut}
                        disabled={chatZoom <= MIN_ZOOM + 1e-9}
                        className="vscode-chat-header-btn"
                        title={t('chatPanel.zoomOut', 'Diminuir Zoom')}
                        aria-label={t('chatPanel.zoomOut', 'Diminuir Zoom')}
                      >
                        <ZoomOut size={14} />
                      </button>
                      <button
                        type="button"
                        onClick={zoomReset}
                        className="vscode-chat-header-btn vscode-chat-zoom-reset"
                        title={t('chatPanel.zoomReset', 'Restaurar Zoom')}
                      >
                        {Math.round(chatZoom * 100)}%
                      </button>
                      <button
                        type="button"
                        onClick={zoomIn}
                        disabled={chatZoom >= MAX_ZOOM - 1e-9}
                        className="vscode-chat-header-btn"
                        title={t('chatPanel.zoomIn', 'Aumentar Zoom')}
                        aria-label={t('chatPanel.zoomIn', 'Aumentar Zoom')}
                      >
                        <ZoomIn size={14} />
                      </button>
                    </div>
                  </>
                )}
                {compactLayout && (
                  <button
                    type="button"
                    className="vscode-overflow-menu-item"
                    onClick={() => {
                      setCompactView(!isCompact);
                      setShowChatActionsMenu(false);
                    }}
                    role="menuitemcheckbox"
                    aria-checked={isCompact}
                  >
                    {isCompact ? <Maximize2 size={14} /> : <Minimize2 size={14} />}
                    <span style={{ flex: 1 }}>{t('chatPanel.compactView', 'Compact chat')}</span>
                    {isCompact && <Check size={14} />}
                  </button>
                )}
                <button
                  type="button"
                  className="vscode-overflow-menu-item"
                  onClick={() => {
                    handleExportMarkdown();
                    setShowChatActionsMenu(false);
                  }}
                  role="menuitem"
                >
                  <Download size={14} />
                  <span>{t('chatPanel.exportMarkdown', 'Exportar como Markdown')}</span>
                </button>
                <button
                  type="button"
                  className="vscode-overflow-menu-item"
                  onClick={() => {
                    handlePrintPDF();
                    setShowChatActionsMenu(false);
                  }}
                  role="menuitem"
                >
                  <Printer size={14} />
                  <span>{t('chatPanel.exportPDF', 'Exportar como PDF / Imprimir')}</span>
                </button>
                <button
                  type="button"
                  className="vscode-overflow-menu-item"
                  onClick={handleClearChat}
                  disabled={!activeProject || isAgentRunning}
                  role="menuitem"
                >
                  <Eraser size={14} />
                  <span>{t('chatPanel.clearChat')}</span>
                </button>
                <button
                  type="button"
                  className="vscode-overflow-menu-item"
                  onClick={handleClearAllChats}
                  disabled={!activeProject || isAgentRunning}
                  role="menuitem"
                >
                  <Trash2 size={14} />
                  <span>{t('chatPanel.clearAllChats')}</span>
                </button>
              </div>
            )}
          </div>
          {!isChatMode && (
            <button
              onClick={() => setIsChatVisible(false)}
              className="vscode-chat-header-btn"
              style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--vscode-text-fg)' }}
              title={t('chatPanel.closeChat', 'Close chat')}
              aria-label={t('chatPanel.closeChat', 'Close chat')}
            >
              <X size={14} />
            </button>
          )}
        </div>
      </div>
      
      {showSearchModal && (
        <SearchChatsModal
          onClose={() => setShowSearchModal(false)}
          activeProject={activeProject?.name}
          onSwitchChat={handleSwitchChat}
        />
      )}

      {/* Chat controls: inline rows in the full panel (the wrapper is
          `display: contents`), an overlay under the header in the compact one.
          One tree for both, so switching never remounts the model selectors or
          loses a half-typed MCP draft. */}
      <div
        ref={compactControlsRef}
        id="chat-compact-controls"
        className={isCompact ? 'vscode-chat-compact-controls' : 'vscode-chat-controls-inline'}
        style={isCompact && !showCompactControls ? { display: 'none' } : undefined}
      >
      {/* Chat Selector Toolbar - Only shown in IDE mode */}
      {!isChatMode && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '4px 10px', borderBottom: '1px solid var(--vscode-border)', background: 'var(--vscode-sidebar-bg)', minHeight: '28px', gap: '6px' }}>
          <select 
            className="vscode-settings-input" 
            value={activeChatId} 
            onChange={(e) => handleSwitchChat(e.target.value)}
            style={{ flex: 1, padding: '2px 4px', fontSize: '11px', height: '22px' }}
          >
            {chats.map(c => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
          <div style={{ display: 'flex', gap: '4px' }}>
            <button onClick={() => setShowSearchModal(true)} title={t('chat.searchChats', 'Search Chats')} style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--vscode-text-fg)', display: 'flex', alignItems: 'center', padding: '2px' }}>
              <Search size={14} />
            </button>
            <button onClick={handleCreateChatClick} title={t('chatSidebar.newChat', 'Novo Chat')} style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--vscode-fg-teal)', display: 'flex', alignItems: 'center', padding: '2px' }}>
              <Plus size={14} />
            </button>
            {activeChatId && (
              <button onClick={(e) => handleDeleteChatClick(activeChatId, e)} title={t('chatPanel.deleteCurrentChat', 'Deletar Chat Atual')} style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--vscode-fg-danger)', display: 'flex', alignItems: 'center', padding: '2px' }}>
                <Trash2 size={14} />
              </button>
            )}
          </div>
        </div>
      )}

      {chatToDelete && (
        <div style={{ padding: '8px', borderBottom: '1px solid var(--vscode-border)', background: 'var(--vscode-sidebar-bg)', display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <span style={{ fontSize: '11px', color: 'var(--vscode-text-fg)' }}>{t('chatSidebar.deletePrompt', 'Deletar este chat e todo o seu histórico?')}</span>
          <div style={{ display: 'flex', gap: '6px' }}>
            <button onClick={confirmDeleteChat} className="vscode-button" style={{ height: '24px', padding: '0 8px', fontSize: '11px', background: 'var(--vscode-fg-danger)', color: '#fff', border: 'none' }}>
              {t('chatSidebar.delete', 'Deletar')}
            </button>
            <button onClick={() => setChatToDelete(null)} className="vscode-button" style={{ height: '24px', padding: '0 8px', fontSize: '11px', background: 'transparent', color: 'var(--vscode-text-fg)', border: '1px solid var(--vscode-border)' }}>
              {t('chatSidebar.cancel', 'Cancelar')}
            </button>
          </div>
        </div>
      )}

      {showNewChatPrompt && (
        <div style={{ padding: '8px', borderBottom: '1px solid var(--vscode-border)', background: 'var(--vscode-sidebar-bg)' }}>
          <form onSubmit={submitNewChat} style={{ display: 'flex', gap: '6px' }}>
            <input 
              autoFocus
              className="vscode-settings-input"
              value={newChatName}
              onChange={e => setNewChatName(e.target.value)}
              placeholder={t('chatSidebar.chatName', 'Nome do chat')}
              style={{ flex: 1, height: '24px', fontSize: '11px' }}
            />
            <button type="submit" className="vscode-button" style={{ height: '24px', padding: '0 8px', fontSize: '11px' }}>
              {t('chatSidebar.create', 'Criar')}
            </button>
            <button type="button" onClick={() => setShowNewChatPrompt(false)} className="vscode-button" style={{ height: '24px', padding: '0 8px', fontSize: '11px', background: 'transparent', color: 'var(--vscode-text-fg)', border: '1px solid var(--vscode-border)' }}>
              {t('chatSidebar.cancel', 'Cancelar')}
            </button>
          </form>
        </div>
      )}

      {/* Model selectors toolbar */}
      <div className="vscode-chat-toolbar" style={{ display: 'flex', gap: '8px', padding: '6px 10px', flexWrap: 'wrap' }}>
        <div style={modelSelectorGroupStyle}>
          <Settings2 size={12} style={{ color: 'var(--vscode-descriptionForeground)' }} />
          <span style={modelSelectorLabelStyle}>{t('chatPanel.orchestrator', 'Orchestrator')}:</span>
          <ModelSelect
            style={modelSelectorInputStyle}
            value={activeProject?.model || ''}
            onChange={(val) => onModelChange?.('model', val)}
            globalModels={globalModels}
            onEditModels={onEditModels}
            onRefreshModels={onRefreshModels}
            disabled={!activeProject}
          />
        </div>
        <div style={modelSelectorGroupStyle}>
          <Cpu size={12} style={{ color: 'var(--vscode-descriptionForeground)' }} />
          <span style={modelSelectorLabelStyle}>{t('chatPanel.worker', 'Worker')}:</span>
          <ModelSelect
            style={modelSelectorInputStyle}
            value={activeProject?.worker_model || ''}
            onChange={(val) => onModelChange?.('worker_model', val)}
            globalModels={globalModels}
            onEditModels={onEditModels}
            onRefreshModels={onRefreshModels}
            disabled={!activeProject}
          />
        </div>
      </div>

      {/* Web Search toggle bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '4px 10px',
          borderBottom: '1px solid var(--vscode-border)',
          background: 'var(--vscode-sidebar-bg)',
          minHeight: '28px',
          gap: '6px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <Globe size={12} style={{ color: searchEnabled ? 'var(--vscode-fg-teal)' : '#888' }} />
          <span style={{ fontSize: '11px', color: searchEnabled ? 'var(--vscode-text-fg)' : '#888', userSelect: 'none' }}>
            {t('chatPanel.webSearch')}
            {hasMcp && searchEnabled && (
              <span style={{ marginLeft: '4px', fontSize: '11px', color: 'var(--vscode-text-muted)' }}>{t('chatPanel.mcpIndicator')}</span>
            )}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          {/* Settings / MCP gear button */}
          <button
            onClick={handleOpenMcp}
            title={t('chatPanel.configureMcp')}
            style={{
              background: 'transparent',
              border: 'none',
              cursor: 'pointer',
              color: showMcpPanel ? 'var(--vscode-fg-teal)' : '#888',
              padding: '1px',
              display: 'flex',
              alignItems: 'center',
            }}
          >
            <Settings size={11} />
          </button>
          {/* Toggle switch */}
          <button
            id="web-search-toggle"
            onClick={handleToggleWebSearch}
            title={searchEnabled ? t('chatPanel.disableWebSearch') : t('chatPanel.enableWebSearch')}
            style={{
              background: 'transparent',
              border: 'none',
              cursor: 'pointer',
              padding: '0',
              display: 'flex',
              alignItems: 'center',
            }}
          >
            <div
              style={{
                width: '28px',
                height: '14px',
                borderRadius: '7px',
                background: searchEnabled ? 'var(--vscode-fg-teal)' : 'var(--vscode-text-subtle)',
                position: 'relative',
                transition: 'background 0.2s',
              }}
            >
              <div
                style={{
                  width: '10px',
                  height: '10px',
                  borderRadius: '50%',
                  background: '#fff',
                  position: 'absolute',
                  top: '2px',
                  left: searchEnabled ? '16px' : '2px',
                  transition: 'left 0.2s',
                }}
              />
            </div>
          </button>
        </div>
      </div>

      {/* MCP config panel (inline, collapsible) */}
      {showMcpPanel && (
        <div
          style={{
            padding: '8px 10px',
            borderBottom: '1px solid var(--vscode-border)',
            background: 'var(--vscode-sidebar-bg)',
            display: 'flex',
            flexDirection: 'column',
            gap: '6px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
            <input
              id="use-mcp-checkbox"
              type="checkbox"
              checked={useMcpDraft}
              onChange={e => setUseMcpDraft(e.target.checked)}
              style={{ cursor: 'pointer' }}
            />
            <label htmlFor="use-mcp-checkbox" style={{ fontSize: '11px', color: 'var(--vscode-text-fg)', cursor: 'pointer', userSelect: 'none' }}>
              {t('chatPanel.useMcpServer')}
            </label>
          </div>

          <label style={{ fontSize: '11px', color: useMcpDraft ? 'var(--vscode-text-fg)' : 'var(--vscode-text-muted)' }}>{t('chatPanel.serverUrl')}</label>
          <input
            id="mcp-url-input"
            type="text"
            value={mcpUrlDraft}
            disabled={!useMcpDraft}
            onChange={e => { setMcpUrlDraft(e.target.value); setMcpTestStatus(''); }}
            placeholder={t('chatPanel.mcpUrlPlaceholder')}
            className="vscode-settings-input"
            style={{
              fontSize: '11px',
              padding: '3px 6px',
            }}
          />

          <label style={{ fontSize: '11px', color: useMcpDraft ? 'var(--vscode-text-fg)' : 'var(--vscode-text-muted)' }}>{t('chatPanel.toolName')}</label>
          <input
            id="mcp-tool-input"
            type="text"
            value={mcpToolDraft}
            disabled={!useMcpDraft}
            onChange={e => { setMcpToolDraft(e.target.value); setMcpTestStatus(''); }}
            placeholder={t('chatPanel.mcpToolPlaceholder')}
            className="vscode-settings-input"
            style={{
              fontSize: '11px',
              padding: '3px 6px',
            }}
          />

          <label style={{ fontSize: '11px', color: useMcpDraft ? 'var(--vscode-text-fg)' : 'var(--vscode-text-muted)' }}>{t('chatPanel.apiKeyOptional')}</label>
          <input
            id="mcp-api-key-input"
            type="password"
            value={mcpApiKeyDraft}
            disabled={!useMcpDraft}
            onChange={e => { setMcpApiKeyDraft(e.target.value); setMcpTestStatus(''); }}
            placeholder={t('chatPanel.mcpApiKeyPlaceholder')}
            className="vscode-settings-input"
            style={{
              fontSize: '11px',
              padding: '3px 6px',
            }}
          />

          <div style={{ display: 'flex', gap: '6px', alignItems: 'center', marginTop: '2px' }}>
            <button
              id="mcp-test-btn"
              onClick={handleTestMcp}
              disabled={!useMcpDraft || !mcpUrlDraft.trim() || mcpTestStatus === 'testing'}
              className="vscode-button"
              style={{
                fontSize: '11px',
                padding: '3px 8px',
                background: 'transparent',
                border: '1px solid var(--vscode-border)',
                color: 'var(--vscode-text-fg)',
              }}
            >
              {mcpTestStatus === 'testing' ? '...' : t('chatPanel.test')}
            </button>
            <button
              id="mcp-save-btn"
              onClick={handleSaveMcp}
              className="vscode-button"
              style={{
                fontSize: '11px',
                padding: '3px 8px',
              }}
            >
              {t('chatPanel.save')}
            </button>
            <button
              onClick={() => { setShowMcpPanel(false); setMcpTestStatus(''); }}
              className="vscode-button"
              style={{
                fontSize: '11px',
                padding: '3px 6px',
                background: 'transparent',
                border: 'none',
                color: 'var(--vscode-text-fg)',
              }}
            >
              {t('chatPanel.cancel')}
            </button>
          </div>

          {/* Test result */}
          {mcpTestStatus && mcpTestStatus !== 'testing' && (
            <div
              style={{
                fontSize: '11px',
                color: mcpTestStatus === 'ok' ? 'var(--vscode-fg-teal)' : 'var(--vscode-errorForeground)',
                marginTop: '2px',
              }}
            >
              {mcpTestStatus === 'ok'
                ? t('chatPanel.connectionOk')
                : t('chatPanel.connectionError', { error: mcpTestStatus.replace('error:', '') })}
            </div>
          )}
        </div>
      )}
      </div>

      {/* Message history */}
      <div className="vscode-chat-history" ref={historyRef} onScroll={handleHistoryScroll} onContextMenu={handleChatContextMenu} style={{ zoom: chatZoom }}>
        {activeProject && !activeChatId && chatMessages.length === 0 && (
          <div style={{ padding: '16px', fontSize: '12px', color: 'var(--vscode-text-muted)', textAlign: 'center' }}>
            {t('chatPanel.noChatOpen', 'No chat is open. Send a message to start a new chat.')}
          </div>
        )}
        {chatMessages.map((msg, i) => {
          if (isHiddenChatSystemMessage(msg)) {
            return null;
          }

          const isSystem = msg.role === 'system';
          if (isSystem) {
            const isLastSystemAfterUserOnly = i === chatMessages.length - 1 && !chatMessages.slice(i + 1).some(m => m.role === 'user' || m.role === 'assistant');
            let previousUserMsg = null;
            if (isLastSystemAfterUserOnly) {
              for (let j = i - 1; j >= 0; j--) {
                if (chatMessages[j].role === 'assistant') break;
                if (chatMessages[j].role === 'user') {
                  previousUserMsg = chatMessages[j];
                  break;
                }
              }
            }
            return (
              <div key={i} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '8px', margin: '12px 0', clear: 'both' }}>
                <div style={{
                  fontSize: '11px',
                  color: 'var(--vscode-descriptionForeground, var(--vscode-text-subtle))',
                  background: 'var(--vscode-badge-background, rgba(255, 255, 255, 0.06))',
                  border: '1px solid var(--vscode-border, rgba(255, 255, 255, 0.12))',
                  borderRadius: '12px',
                  padding: '4px 14px',
                  maxWidth: '85%',
                  wordBreak: 'break-word',
                  textAlign: 'center',
                  lineHeight: '1.4'
                }}>
                  {msg.content}
                </div>
                {previousUserMsg && !isAgentRunning && (
                  <button
                    type="button"
                    onClick={() => runChatAction(() => onGenerateResponseForUserMessage?.(chatMessages.indexOf(previousUserMsg), previousUserMsg))}
                    style={{
                      padding: '7px 11px',
                      background: 'var(--vscode-button-background, #0e639c)',
                      color: 'var(--vscode-button-foreground, #ffffff)',
                      border: '1px solid rgba(255,255,255,0.12)',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: '12px',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '7px',
                      width: 'fit-content',
                      boxShadow: '0 1px 0 rgba(255,255,255,0.08) inset',
                    }}
                    title={t('chatPanel.tryAgain', 'Tentar Novamente')}
                  >
                    <RefreshCw size={14} />
                    {t('chatPanel.tryAgain', 'Tentar Novamente')}
                  </button>
                )}
              </div>
            );
          }

          const isUser = msg.role === 'user';
          const isLastUserOrAssistantMessage = !chatMessages.slice(i + 1).some(m => m.role === 'user' || m.role === 'assistant');
          // A user turn without a following assistant turn is resumable.  System
          // messages are deliberately ignored because they are implementation
          // metadata and must not hide the continuation action after reload.
          const canContinueUserMessage = isUser && isLastUserOrAssistantMessage && !isAgentRunning && editingMessageIndex !== i;
          const atts = msg._attachments || [];
          const persistedThoughtStream = !isUser ? String(msg._thoughtStream || '').trim() : '';
          // Reasoning kept in the store for this message: counted when the chat
          // was opened and read only if the user opens the Agent Thinking
          // panel for it. The chat never renders it inline -- a bubble holding
          // tens of thousands of tokens is what froze the window.
          const thoughtWindow = !isUser ? msg._thoughtWindow : null;
          
          const interruptionProbe = contentWithoutThink(msg.content);
          // Interrupted, stopped by the runaway guardrail, or killed by an
          // error: three ways a turn ends without an answer, and one rule for
          // all of them. The backend appends a stable, unlocalised marker after
          // the work the model did produce -- that text is real and is never
          // dropped -- so the marker is split off here and shown as the
          // localised notice instead, next to the continue action. Matching the
          // translated prose would break for anyone who changed language after
          // the turn ran.
          const turnEnd = !isUser ? turnEndFromContent(msg.content, interruptionProbe) : null;
          const isInterrupted = Boolean(turnEnd?.interrupted);
          const isTurnCutShort = Boolean(turnEnd?.cutShort);
          const isTurnFailed = Boolean(turnEnd?.failed);
          const isTurnNoAnswer = Boolean(turnEnd?.noAnswer);
          let displayContent = isUser && isInternalResumePrompt(msg.content)
            ? t('chatPanel.continue', 'Continue')
            : msg.content;
          if (isTurnCutShort || isTurnFailed || isTurnNoAnswer || turnEnd?.interruptedByMarker) {
            displayContent = turnEnd.text;
          } else if (turnEnd?.legacyInterruption) {
            // Recorded before the marker existed: the prose *is* the notice, and
            // there is no partial answer underneath it to preserve.
            displayContent = t('app.interruptionNotice');
          }
          if (hideThink && !isUser && displayContent) {
            displayContent = stripInlineReasoning(displayContent);
          }

          const isError = !isUser && displayContent && (
            displayContent.includes('🔴') ||
            displayContent.includes('err_connection_failed')
          );


          const legacyErrorProbe = contentWithoutThink(displayContent);
          const shouldShowTryAgain = !isUser && (
            isRetryableAssistantErrorMessage(msg, displayContent) ||
            msg.is_error === true ||
            /^\s*(🔴|ðŸ”´|\u{1F534})\s*(Agent Error|Erro do Agente|Erro:|Error:|Falha|Failure|Failed|Falha na execução)/i.test(legacyErrorProbe) ||
            /Erro do Agente|Agent Error|cota de uso|créditos suficientes|Insufficient AI credits|CRITICAL WORKER CRASH/i.test(legacyErrorProbe)
          );

          let lastUserMsgBeforeThis = null;
          if (shouldShowTryAgain || isInterrupted) {
            for (let j = i - 1; j >= 0; j--) {
              if (chatMessages[j].role === 'user') {
                lastUserMsgBeforeThis = chatMessages[j];
                break;
              }
            }
          }

          // Sent while the agent was working. Three states, all stated
          // explicitly: still being handed over, accepted and waiting for the
          // agent's next step, or actually read by it. "Read" is the answer the
          // user is waiting for, so it gets a badge of its own instead of being
          // signalled by the waiting badge disappearing.
          const deliveryState = isUser ? msg._deliveryState : undefined;
          const isQueued = deliveryState === 'sending' || deliveryState === 'queued';
          const queuePosition = deliveryState === 'queued'
            ? queuedMessages.findIndex(m => m.clientMessageId === msg.client_message_id) + 1
            : 0;

          return (
            <div
              key={i}
              style={{ display: 'flex', flexDirection: 'column', gap: '4px', opacity: isQueued ? 0.65 : 1 }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <span
                    className={`vscode-chat-msg-header ${isUser ? 'chat-header-user' : 'chat-header-agent'}`}
                    style={{ margin: 0 }}
                  >
                    {isUser ? t('chatPanel.you') : t('chatPanel.gilzabots')}
                  </span>
                  {msg.timestamp && (
                    <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground, #717171)' }}>
                      {new Date(msg.timestamp).toLocaleString()}
                    </span>
                  )}
                  {deliveryState === 'sending' && (
                    <span className="chat-msg-badge chat-msg-badge-sending">
                      <RefreshCw size={11} className="spin" />
                      {t('chatPanel.messageSending', 'Sending...')}
                    </span>
                  )}
                  {deliveryState === 'queued' && (
                    <span
                      className="chat-msg-badge chat-msg-badge-waiting"
                      title={t('chatPanel.queuedMessageHint', 'The agent receives this message at its next step.')}
                    >
                      <Clock size={11} />
                      {queuePosition > 1
                        ? t('chatPanel.queuedMessagePosition', 'Waiting for the agent (#{{position}})', { position: queuePosition })
                        : t('chatPanel.queuedMessage', 'Waiting for the agent')}
                      {onCancelQueuedMessage && (
                        <button
                          type="button"
                          className="chat-msg-badge-cancel"
                          onClick={() => onCancelQueuedMessage(msg.client_message_id)}
                          title={t('chatPanel.cancelQueuedMessage', 'Cancel this message')}
                        >
                          <X size={11} />
                        </button>
                      )}
                    </span>
                  )}
                  {deliveryState === 'delivered' && (
                    <span
                      className="chat-msg-badge chat-msg-badge-delivered chat-msg-delivered"
                      title={t('chatPanel.deliveredMessageHint', 'The agent read this message and is taking it into account.')}
                    >
                      <Check size={11} />
                      {t('chatPanel.deliveredMessage', 'Read by the agent')}
                    </span>
                  )}
                </div>
                {isUser && (
                  <button
                    onClick={() => handleStartEditMessage(i, msg)}
                    disabled={isAgentRunning}
                    style={{
                      background: 'transparent',
                      border: 'none',
                      cursor: isAgentRunning ? 'not-allowed' : 'pointer',
                      color: 'var(--vscode-descriptionForeground, #717171)',
                      display: 'flex',
                      alignItems: 'center',
                      padding: '2px',
                      borderRadius: '3px',
                      transition: 'background-color 0.1s, color 0.1s',
                      opacity: isAgentRunning ? 0.5 : 1,
                    }}
                    title={t('chatPanel.editMessage', 'Edit message')}
                    className="msg-action-btn"
                  >
                    <Pencil size={12} />
                  </button>
                )}
                {/* A queued message is not part of the stored conversation yet,
                    so there is nothing here to branch from. */}
                {!isQueued && (
                <button
                  onClick={() => handleBranchChat(i, msg)}
                  style={{
                    background: 'transparent',
                    border: 'none',
                    cursor: 'pointer',
                    color: 'var(--vscode-descriptionForeground, #717171)',
                    display: 'flex',
                    alignItems: 'center',
                    padding: '2px',
                    borderRadius: '3px',
                    transition: 'background-color 0.1s, color 0.1s',
                  }}
                  title={t('chatPanel.branchMessage', 'Criar branch a partir daqui')}
                  className="msg-action-btn"
                >
                  <GitBranch size={12} />
                </button>
                )}
                {!isUser && (
                  <button
                    onClick={() => handleExportSingleMessage(msg.content)}
                    style={{
                      background: 'transparent',
                      border: 'none',
                      cursor: 'pointer',
                      color: 'var(--vscode-descriptionForeground, #717171)',
                      display: 'flex',
                      alignItems: 'center',
                      padding: '2px',
                      borderRadius: '3px',
                      transition: 'background-color 0.1s, color 0.1s',
                    }}
                    title={t('chatPanel.exportMessage', 'Exportar esta resposta')}
                    className="msg-action-btn"
                  >
                    <Download size={12} />
                  </button>
                )}
              </div>
              {/* Attachment previews */}
              {atts.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', margin: '2px 0' }}>
                  {atts.map((att, ai) => (
                    <div key={ai} style={{
                      display: 'flex', alignItems: 'center', gap: '4px',
                      background: 'var(--vscode-input-bg)', borderRadius: '4px', padding: '3px 7px',
                      fontSize: '11px', color: 'var(--vscode-text-subtle)',
                    }}>
                      {att._previewUrl
                        ? <img src={att._previewUrl} alt={att.name} style={{ height: '40px', borderRadius: '3px', objectFit: 'cover' }} />
                        : <FileText size={14} style={{ color: 'var(--vscode-fg-teal)' }} />}
                      <span>{att.name}</span>
                    </div>
                  ))}
                </div>
              )}
              <div className="vscode-chat-msg-content">
                {(persistedThoughtStream || thoughtWindow) && !hideThink && (
                  <button
                    type="button"
                    onClick={() => onShowThinking?.(i, thoughtWindow)}
                    title={t('chatPanel.aiThoughtsOpenHint', 'Open the full reasoning in the Agent Thinking panel')}
                    style={{
                      display: 'flex', alignItems: 'center', gap: '6px', margin: '0 0 8px 0',
                      padding: '6px 10px', width: '100%', textAlign: 'left', cursor: 'pointer',
                      fontSize: '11px', color: 'var(--vscode-descriptionForeground, #717171)',
                      background: 'var(--titlebar-bg, #252526)',
                      border: '1px solid var(--vscode-widget-border, #3c3c3c)', borderRadius: '4px',
                    }}
                  >
                    <Brain size={12} />
                    {t('chatPanel.aiThoughts', 'Pensamentos da IA')}
                    {thoughtWindow?.count > 0 && (
                      <span style={{ opacity: 0.7 }}>
                        {t('chatPanel.aiThoughtsPieces', '({{count}} pieces)', { count: thoughtWindow.count })}
                      </span>
                    )}
                  </button>
                )}
                {editingMessageIndex === i && isUser ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    <textarea
                      value={editingMessageDraft}
                      onChange={(e) => setEditingMessageDraft(e.target.value)}
                      onKeyDown={(e) => handleEditMessageKeyDown(e, i, msg)}
                      className="vscode-chat-textarea"
                      rows={Math.min(8, Math.max(2, editingMessageDraft.split('\n').length))}
                      autoFocus
                      disabled={isAgentRunning}
                      aria-invalid={editingMessageError ? true : undefined}
                      style={{
                        width: '100%',
                        resize: 'vertical',
                        minHeight: '72px',
                        boxSizing: 'border-box',
                      }}
                    />
                    {editingMessageError && (
                      <div
                        role="alert"
                        className="chat-edit-error"
                        style={{ color: 'var(--vscode-errorForeground)', fontSize: '0.9em', whiteSpace: 'pre-wrap' }}
                      >
                        {editingMessageError}
                      </div>
                    )}
                    <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
                      <button
                        type="button"
                        onClick={() => handleSubmitEditMessage(i, msg)}
                        disabled={isAgentRunning || isSubmittingEdit || !editingMessageDraft.trim()}
                        className="vscode-button"
                        title={t('chatPanel.saveEdit', 'Save edit')}
                        style={{ padding: '5px 7px', display: 'flex', alignItems: 'center' }}
                      >
                        <Check size={13} />
                      </button>
                      <button
                        type="button"
                        onClick={handleCancelEditMessage}
                        className="vscode-button"
                        title={t('chatPanel.cancelEdit', 'Cancel edit')}
                        style={{
                          padding: '5px 7px',
                          display: 'flex',
                          alignItems: 'center',
                          background: 'transparent',
                          border: '1px solid var(--vscode-border)',
                          color: 'var(--vscode-text-fg)',
                        }}
                      >
                        <X size={13} />
                      </button>
                    </div>
                  </div>
                ) : (
                  <FormattedMessage content={displayContent} projectPath={activeProject?.project_path} />
                )}
                {shouldShowTryAgain && isLastUserOrAssistantMessage && !isAgentRunning && lastUserMsgBeforeThis && (
                  <button
                    type="button"
                    onClick={() => runChatAction(() => handleSendMessage(null, lastUserMsgBeforeThis))}
                    style={{
                      marginTop: '8px',
                      padding: '6px 10px',
                      background: 'var(--vscode-button-background, #0e639c)',
                      color: 'var(--vscode-button-foreground, #ffffff)',
                      border: 'none',
                      borderRadius: '3px',
                      cursor: 'pointer',
                      fontSize: '12px',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '6px',
                      width: 'fit-content'
                    }}
                  >
                    <RefreshCw size={14} /> {t('chatPanel.tryAgain', 'Tentar Novamente')}
                  </button>
                )}
                {(isTurnCutShort || isTurnFailed || isTurnNoAnswer) && isLastUserOrAssistantMessage && !isAgentRunning && (
                  <div style={{ marginTop: '10px' }}>
                    <div style={{ fontSize: '12px', color: 'var(--vscode-descriptionForeground)', marginBottom: '6px' }}>
                      <AlertTriangle size={12} aria-hidden="true" style={{ verticalAlign: '-2px', marginRight: '4px' }} />{isTurnFailed
                        ? t('app.turnFailedNotice')
                        : isTurnNoAnswer
                          ? t('app.turnNoAnswerNotice')
                          : t('app.turnCutShortNotice', {
                            // Named exactly as the composer labels the control,
                            // read from the catalogue so a rename cannot leave the
                            // notice pointing at a control that no longer exists.
                            selector: t('chatPanel.heartbeatMode'),
                            custom: t('chatPanel.heartbeatModeCustom'),
                            field: t('editProjectModal.maxHeartbeats'),
                          })}
                    </div>
                    <button
                      type="button"
                      onClick={() => runChatAction(() => handleSendMessage(null, null, {
                        resumeInterrupted: true,
                        displayText: t('chatPanel.continue', 'Continue'),
                      }))}
                      style={{
                        padding: '6px 10px',
                        background: 'var(--vscode-button-background)',
                        color: 'var(--vscode-button-foreground)',
                        border: 'none',
                        borderRadius: '3px',
                        cursor: 'pointer',
                        fontSize: '12px',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '6px',
                        width: 'fit-content'
                      }}
                    >
                      <ArrowRight size={14} /> {t('chatPanel.continue', 'Continue')}
                    </button>
                  </div>
                )}
                {isInterrupted && isLastUserOrAssistantMessage && !isAgentRunning && (
                  <div style={{ marginTop: '8px' }}>
                    {/* The notice is what the raw marker used to say, in the
                        user's language. A legacy interruption has it as its
                        content already, so it is not repeated here. */}
                    {turnEnd?.interruptedByMarker && (
                      <div style={{ fontSize: '12px', color: 'var(--vscode-descriptionForeground)', marginBottom: '6px' }}>
                        <AlertTriangle size={12} aria-hidden="true" style={{ verticalAlign: '-2px', marginRight: '4px' }} />{t('app.interruptionNotice')}
                      </div>
                    )}
                    <button
                      type="button"
                      onClick={() => runChatAction(() => handleSendMessage(null, null, {
                        resumeInterrupted: true,
                        displayText: t('chatPanel.continue', 'Continuar'),
                      }))}
                      style={{
                        padding: '6px 10px',
                        background: 'var(--vscode-button-background, #0e639c)',
                        color: 'var(--vscode-button-foreground, #ffffff)',
                        border: 'none',
                        borderRadius: '3px',
                        cursor: 'pointer',
                        fontSize: '12px',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '6px',
                        width: 'fit-content'
                      }}
                    >
                      <ArrowRight size={14} /> {t('chatPanel.continue', 'Continuar')}
                    </button>
                  </div>
                )}
                {canContinueUserMessage && (
                  <button
                    type="button"
                    onClick={() => runChatAction(() => handleSendMessage(null, null, {
                      resumeInterrupted: true,
                      displayText: t('chatPanel.continue', 'Continue'),
                    }))}
                    style={{
                      marginTop: '10px',
                      padding: '7px 11px',
                      background: 'var(--vscode-button-background, #0e639c)',
                      color: 'var(--vscode-button-foreground, #ffffff)',
                      border: '1px solid rgba(255,255,255,0.12)',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: '12px',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '7px',
                      width: 'fit-content',
                      boxShadow: '0 1px 0 rgba(255,255,255,0.08) inset',
                    }}
                    title={t('chatPanel.continue', 'Continue')}
                  >
                    <ArrowRight size={14} />
                    {t('chatPanel.continue', 'Continue')}
                  </button>
                )}
              </div>
            </div>
          );
        })}

        {isAgentRunning && isLiveTurnOnScreen && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span className="vscode-chat-msg-header chat-header-agent" style={{ margin: 0 }}>
                {t('chatPanel.gilzabots')}
              </span>
              <span
                className="chat-msg-badge chat-msg-badge-step-running"
                title={t('chatPanel.stepsExecutedTooltip', 'Steps executed in this turn: {{step}}{{max}}', {
                  step: agentStepInfo?.step ?? 0,
                  max: agentStepInfo?.maxSteps ? ` / ${agentStepInfo.maxSteps}` : '',
                })}
              >
                <Activity size={12} className="spin-slow" />
                <span>
                  {t('chatPanel.stepsExecuted', 'Steps executed: {{step}}{{max}}', {
                    step: agentStepInfo?.step ?? 0,
                    max: agentStepInfo?.maxSteps ? ` / ${agentStepInfo.maxSteps}` : '',
                  })}
                </span>
              </span>
            </div>
            {providerWait && <ProviderWaitNotice wait={providerWait} t={t} />}
            <div className="vscode-chat-msg-content">
              {(chatThoughtStream && !hideThink) ? (
                <details open style={{ margin: '8px 0', border: '1px solid var(--vscode-widget-border, #3c3c3c)', borderRadius: '4px', background: 'var(--titlebar-bg, #252526)' }}>
                  <summary style={{ padding: '6px 10px', fontSize: '11px', cursor: 'pointer', userSelect: 'none', color: 'var(--vscode-descriptionForeground, #717171)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }}>
                    <span>{t('chatPanel.aiThoughts', 'Pensamentos da IA')}</span>
                    {/* The chat keeps the last lines in view; the whole of this
                        turn's reasoning is read in the Agent Thinking panel. */}
                    <span
                      role="button"
                      tabIndex={0}
                      onClick={e => { e.preventDefault(); e.stopPropagation(); onShowThinking?.(null, null); }}
                      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onShowThinking?.(null, null); } }}
                      title={t('chatPanel.aiThoughtsOpenHint', 'Open the full reasoning in the Agent Thinking panel')}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', cursor: 'pointer', opacity: 0.8 }}
                    >
                      <Brain size={12} />
                      {t('chatPanel.aiThoughtsOpenPanel', 'Full reasoning')}
                    </span>
                  </summary>
                  <pre style={{ margin: 0, padding: '10px', background: 'var(--editor-bg, #1e1e1e)', overflowX: 'auto', fontSize: '11px', color: 'var(--vscode-textPreformat-foreground, #d7ba7d)', borderTop: '1px solid var(--vscode-widget-border, #3c3c3c)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {chatThoughtStream}
                  </pre>
                </details>
              ) : chatThoughtStream ? (
                <div className="thinking-indicator">
                  <span className="dot" />
                  <span className="dot" />
                  <span className="dot" />
                </div>
              ) : !chatResponseStream ? (
                <div className="thinking-indicator" style={{ display: 'inline-flex', alignItems: 'center', gap: '8px' }}>
                  <div style={{ display: 'inline-flex', gap: '3px' }}>
                    <span className="dot" />
                    <span className="dot" />
                    <span className="dot" />
                  </div>
                  <span style={{ fontSize: '11px', color: 'var(--vscode-descriptionForeground)', marginLeft: '4px' }}>
                    {t('chatPanel.stepsExecuted', 'Steps executed: {{step}}{{max}}', {
                      step: agentStepInfo?.step ?? 0,
                      max: agentStepInfo?.maxSteps ? ` / ${agentStepInfo.maxSteps}` : '',
                    })}
                  </span>
                </div>
              ) : null}
              {chatResponseStream && (
                <pre
                  className="chat-stream-raw"
                  style={{
                    margin: chatThoughtStream && !hideThink ? '8px 0 0' : 0,
                    color: 'var(--chat-text, var(--vscode-text-fg))',
                    fontFamily: 'var(--vscode-editor-font-family, monospace)',
                    fontSize: '13px',
                    lineHeight: '1.5',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                  }}
                >{chatResponseStream}</pre>
              )}
            </div>
          </div>
        )}
        <div ref={chatEndRef} />
      </div>

      {/* Tutorial question menu (built-in tutorial chat only) */}
      {isTutorialChat && (
        <TutorialMenu
          topics={tutorialTopics}
          disabled={isAgentRunning}
          onSelectTopic={(topicId) => runChatAction(() => onTutorialTopic?.(topicId))}
        />
      )}

      {/* Input form */}
      <form
        onSubmit={handleFormSubmit}
        className={`vscode-chat-form${isCompact ? ' is-compact' : ''}`}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        style={
          isDragOver
            ? { outline: '2px dashed var(--vscode-fg-teal)', outlineOffset: '-2px' }
            : isEvolvingPrompt
            ? {
                border: '1px solid var(--vscode-fg-teal)',
                boxShadow: '0 0 12px rgba(78, 201, 176, 0.4)',
                transition: 'border-color 0.3s ease, box-shadow 0.3s ease',
              }
            : {}
        }
      >
        {/* Evolution Visual Progress Banner */}
        {evolutionProgress && (
          <div
            style={{
              padding: '6px 12px',
              margin: '6px 10px 4px 10px',
              borderRadius: '6px',
              background: 'linear-gradient(135deg, rgba(78, 201, 176, 0.15), rgba(197, 134, 192, 0.15))',
              border: '1px solid rgba(78, 201, 176, 0.4)',
              boxShadow: '0 0 10px rgba(78, 201, 176, 0.2)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              fontSize: '12px',
              color: 'var(--vscode-text-fg)',
              animation: 'opc-fade-in 0.2s ease',
            }}
          >
            <div className="flex items-center" style={{ gap: '8px' }}>
              {evolutionProgress.complete ? (
                <Check size={14} style={{ color: 'var(--vscode-fg-teal)' }} />
              ) : (
                <Sparkles size={14} className="spin" style={{ color: 'var(--vscode-fg-teal)' }} />
              )}
              <span style={{ fontWeight: 500 }}>
                {evolutionProgress.complete
                  ? t('chatPanel.evolvedSuccess')
                  : t('chatPanel.evolvingIteration', {
                      iteration: evolutionProgress.iteration || 1,
                      total: evolutionProgress.total || 1,
                    })}
              </span>
            </div>
            {!evolutionProgress.complete && (
              <div className="flex items-center" style={{ gap: '8px' }}>
                <div style={{ fontSize: '11px', opacity: 0.85, background: 'rgba(0,0,0,0.25)', padding: '2px 6px', borderRadius: '4px', border: '1px solid rgba(78, 201, 176, 0.3)' }}>
                  {Math.round((((evolutionProgress.iteration || 1) - 1) / (evolutionProgress.total || 1)) * 100)}%
                </div>
                <button
                  type="button"
                  onClick={handleCancelEvolvePrompt}
                  title={t('chatPanel.cancelEvolution', 'Cancel prompt evolution')}
                  className="vscode-button"
                  style={{
                    background: 'rgba(244, 135, 113, 0.15)',
                    border: '1px solid rgba(244, 135, 113, 0.4)',
                    color: 'var(--vscode-errorForeground)',
                    borderRadius: '4px',
                    padding: '2px 6px',
                    fontSize: '11px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                  }}
                >
                  <X size={12} />
                  <span>{t('chatPanel.cancel', 'Cancel')}</span>
                </button>
              </div>
            )}
          </div>
        )}
        {/* Pending attachment preview strip */}
        {pendingAttachments && pendingAttachments.length > 0 && (
          <div style={{
            display: 'flex', flexWrap: 'wrap', gap: '6px',
            padding: '6px 10px', borderBottom: '1px solid var(--vscode-border)',
          }}>
            {pendingAttachments.map((att, idx) => (
              <div key={idx} style={{
                display: 'flex', alignItems: 'center', gap: '4px',
                background: 'var(--vscode-input-bg)', border: '1px solid var(--vscode-border)',
                borderRadius: '4px', padding: '3px 6px', fontSize: '11px', color: 'var(--vscode-text-fg)',
              }}>
                {att._previewUrl
                  ? <img src={att._previewUrl} alt={att.name} style={{ height: '32px', borderRadius: '2px', objectFit: 'cover' }} />
                  : <FileText size={13} style={{ color: 'var(--vscode-fg-teal)' }} />}
                <span style={{ maxWidth: '100px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{att.name}</span>
                <button
                  type="button"
                  onClick={() => removeAttachment(idx)}
                  style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--vscode-text-muted)', padding: '0 2px', lineHeight: 1 }}
                >
                  <X size={11} />
                </button>
              </div>
            ))}
          </div>
        )}
        {/* Local models pay for every attachment out of a small context window */}
        {showLocalAttachmentWarning && (
          <div className="chat-attachment-warning">
            <AlertTriangle size={12} style={{ flexShrink: 0 }} />
            <span>
              {t(
                'chatPanel.localAttachmentWarning',
                'Local model: attachments compete with the conversation for a small context window, so attach few files at a time. Cloud models handle larger batches.',
              )}
            </span>
          </div>
        )}
        {/* Delivery status for messages sent into a running turn. The bubble
            badges alone are not enough: when the user presses Enter their eyes
            are on the composer, and the bubble may have already scrolled out of
            view behind the agent's streaming output. */}
        {queuedMessages.length > 0 && (
          <div className="chat-queue-strip">
            <Clock size={12} style={{ flexShrink: 0 }} />
            <span style={{ flex: 1 }}>
              {queuedMessages.length === 1
                ? t('chatPanel.queueStatusOne', 'Message sent. The agent reads it at its next step.')
                : t('chatPanel.queueStatusMany', '{{count}} messages sent. The agent reads them at its next steps.', { count: queuedMessages.length })}
            </span>
            {onCancelAllQueuedMessages && (
              <button
                type="button"
                className="chat-queue-strip-action"
                onClick={onCancelAllQueuedMessages}
              >
                {queuedMessages.length === 1
                  ? t('chatPanel.cancelQueuedMessage', 'Cancel this message')
                  : t('chatPanel.cancelAllQueuedMessages', 'Cancel all')}
              </button>
            )}
          </div>
        )}
        {/* Upload status */}
        {uploadingFiles && (
          <div style={{ padding: '4px 10px', fontSize: '11px', color: 'var(--vscode-text-muted)' }}>
            {t('chatPanel.uploadingFiles', 'Processing attachment...')}
          </div>
        )}
        {/* Composer toolbar with expandable heartbeat / effort mode selector */}
        {!isCompact && (
          <div className="vscode-chat-composer-toolbar">
            {heartbeatSelector}
            {reasoningSelector}
          </div>
        )}
        {dictation.state === 'error' && (
          <div className="vscode-chat-dictation-error" role="status">
            <AlertTriangle size={12} style={{ flexShrink: 0 }} />
            <span>
              {dictation.error === 'denied'
                ? t('dictation.denied', 'Microphone access was refused. Allow it and try again.')
                : dictation.error === 'silent'
                  ? t('dictation.silent', 'Nothing was recorded — check the microphone and try again.')
                  : dictation.error === 'unsupported'
                    ? t('dictation.unsupported', 'This window cannot reach a microphone.')
                    : t('dictation.failed', 'Transcription failed: {{error}}', { error: dictation.error })}
            </span>
            <button type="button" onClick={dictation.dismissError} aria-label={t('common.close', 'Close')}>
              <X size={12} />
            </button>
          </div>
        )}
        <div className="vscode-chat-input-row">
          {/* Hidden file input */}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*,application/pdf,.docx,.pptx,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.presentationml.presentation"
            multiple
            style={{ display: 'none' }}
            onChange={handleFileInputChange}
            id="chat-file-input"
          />
          {isCompact && heartbeatSelector}
          {isCompact && reasoningSelector}
          {/* Paperclip button */}
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={!activeProject}
            title={t('chatPanel.attachFile', 'Attach images, PDFs, DOCX or PPTX (you can also paste images)')}
            style={{
              background: 'transparent', border: 'none', cursor: 'pointer',
              color: (pendingAttachments && pendingAttachments.length > 0) ? 'var(--vscode-fg-teal)' : '#666',
              padding: '4px', display: 'flex', alignItems: 'center',
            }}
          >
            <Paperclip size={15} />
          </button>
          {/* Microphone. Shown whenever the user asked for it in Settings, and
              disabled *with its reason* when it cannot run yet. Hiding it when
              not ready meant ticking the box appeared to do nothing, which is
              indistinguishable from a broken build — the failure this replaced.
              A model still downloading is exactly that state. */}
          {dictation.availability.offered && (
            <button
              type="button"
              onClick={dictation.toggle}
              disabled={!activeProject || !dictation.availability.ready
                        || dictation.state === 'transcribing'}
              title={
                !dictation.availability.ready
                  ? (dictation.availability.problem
                     || t('dictation.notReady', 'Dictation is not ready yet.'))
                  : dictation.state === 'recording'
                    ? t('dictation.stop', 'Stop recording and transcribe')
                    : dictation.state === 'transcribing'
                      ? t('dictation.transcribing', 'Transcribing…')
                      : t('dictation.start', 'Dictate a message')
              }
              aria-label={t('dictation.start', 'Dictate a message')}
              className={`vscode-chat-mic-btn${dictation.state === 'recording' ? ' is-recording' : ''}`}
            >
              {dictation.state === 'recording' ? (
                <Square size={13} />
              ) : dictation.state === 'transcribing' ? (
                <RefreshCw size={14} className="animate-spin" />
              ) : (
                <Mic size={15} />
              )}
            </button>
          )}
          <textarea
            ref={inputRef}
            rows={1}
            value={chatInput}
            onChange={(e) => setChatInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handleInputPaste}
            disabled={!activeProject || isEvolvingPrompt}
            placeholder={
              !activeProject ? t('chatPanel.setProjectFirst') :
              isEvolvingPrompt ? t('chatPanel.evolving', 'Evolving...') :
              isAgentRunning ? t('chatPanel.sendWhileRunning', 'Send a message to the running agent...') :
              t('chatPanel.askGilzaBots')
            }
            className="vscode-chat-textarea"
          />
          {/* Evolve Prompt Button */}
          <button
            type="button"
            onClick={isEvolvingPrompt ? handleCancelEvolvePrompt : handleEvolvePrompt}
            disabled={!activeProject || (!isEvolvingPrompt && !chatInput.trim()) || isAgentRunning}
            title={
              isEvolvingPrompt
                ? t('chatPanel.cancelEvolution', 'Cancel prompt evolution')
                : t('chatPanel.evolvePrompt', 'Evolve prompt')
            }
            className="vscode-button"
            style={{
              padding: '6px',
              backgroundColor: isEvolvingPrompt ? 'rgba(244, 135, 113, 0.15)' : 'transparent',
              border: isEvolvingPrompt ? '1px solid rgba(244, 135, 113, 0.4)' : 'none',
              color: isEvolvingPrompt ? 'var(--vscode-errorForeground)' : 'var(--vscode-text-fg)',
              cursor: (!activeProject || (!isEvolvingPrompt && !chatInput.trim()) || isAgentRunning) ? 'not-allowed' : 'pointer',
              opacity: (!activeProject || (!isEvolvingPrompt && !chatInput.trim()) || isAgentRunning) ? 0.4 : 1,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              borderRadius: isEvolvingPrompt ? '4px' : undefined,
            }}
          >
            {isEvolvingPrompt ? (
              <X size={14} style={{ color: 'var(--vscode-errorForeground)' }} />
            ) : (
              <Sparkles size={14} style={{ color: (!activeProject || !chatInput.trim() || isAgentRunning) ? 'inherit' : 'var(--vscode-fg-teal)' }} />
            )}
          </button>
          {/* Stop and Send coexist during a turn: stopping and adding to it are
              different intentions, and hiding Send is what forced a user to
              interrupt the agent just to say one more thing. */}
          {isAgentRunning && (
            <button
              type="button"
              onClick={handleInterruptAgent}
              className="vscode-button"
              disabled={isInterruptPending}
              style={{ padding: '6px', backgroundColor: 'var(--vscode-errorForeground)', color: 'var(--vscode-bg)', opacity: isInterruptPending ? 0.7 : 1 }}
              title={t('chatPanel.interruptAgent')}
            >
              {isInterruptPending ? <RefreshCw size={14} className="spin" /> : <X size={14} />}
            </button>
          )}
          <button
            type="submit"
            disabled={!activeProject || (!chatInput.trim() && (!pendingAttachments || pendingAttachments.length === 0))}
            className="vscode-button"
            style={{ padding: '6px' }}
            title={isAgentRunning
              ? t('chatPanel.sendToRunningAgent', 'Send to the running agent (delivered at its next step)')
              : undefined}
          >
            <ArrowRight size={14} />
          </button>
        </div>
      </form>
    </aside>
  );
}
