import { createFileSaveQueue } from './utils/fileSave.js';
import React, { useState, useEffect, useMemo, useRef, useCallback, useTransition } from 'react';
import '@xterm/xterm/css/xterm.css';
import { useTranslation } from 'react-i18next';
import i18n from './i18n/index.js';

// Utils
import { getPreference, setPreference } from './utils/preferences';
import { UI_SCALE_DEFAULT, UI_SCALE_KEY_STEP, clampUiScale, roundUiScale, viewportPointToApp, viewportPxToApp } from './utils/uiScale';
import { enterLayout, layoutAfterOpeningFile, layoutAllowsChatToggle, layoutHasChatSidebar, layoutShowsEditor } from './utils/layoutModes';
import { compactChatLayoutFor } from './utils/chatCompact';
import { clearAnsweredRequest, confirmRequestDialog, dialogRequestKey, normalizeInputRequest } from './utils/askQuestion';
import { readRunConflict, RUN_CONFLICT_TURN_STOPPING } from './utils/agentRunConflict';
import { interruptedTurnContent } from './utils/turnMarkers.js';
import { settleAtHandoff } from './utils/editHandoff.js';
import { formatWaitDuration, providerWaitFromEvent } from './utils/providerWait.js';
import { consumeLinesByFrame } from './utils/frameBatchedLines.js';
import {
  appendThoughtChunk,
  clampThoughtContextTokens,
  createThoughtTail,
  DEFAULT_THOUGHT_CONTEXT_TOKENS,
  thoughtTailText,
} from './utils/thoughtTail';
import {
  clampLogMessage,
  fitLogMessage as fitMergedLogMessage,
  joinMergedLogMessage,
} from './utils/logMerge';
import { resolveInlineCreatePath } from './utils/inlineCreate';
import {
  markerPathKey,
  removeAgentChangedFiles,
  renameAgentChangedFiles,
  unsavedFilePaths,
  withAgentChangedFiles,
  withoutAgentChangedFile,
} from './utils/fileMarkers';
import { createAgentFileSync } from './utils/agentFileSync';
import { createIdeCommandRunner, sameProjectPath } from './utils/ideCommands.js';

// Hooks
import { useResizing } from './hooks/useResizing';
import { useModelCatalog } from './contexts/ModelCatalogProvider.jsx';

// Layout components
import ActivityBar from './components/ActivityBar';
import StatusBar from './components/StatusBar';
import ExplorerSidebar from './components/ExplorerSidebar';
import GitSidebar from './components/GitSidebar';
import EditorPanel from './components/EditorPanel';
import ChatPanel from './components/ChatPanel';
import ChatSidebar from './components/ChatSidebar';
import {
  STUDIO_BOTTOM_HEIGHT_DEFAULT,
  STUDIO_CHAT_WIDTH_DEFAULT,
  studioGridTemplate,
} from './utils/studioLayout';
import BottomPanel from './components/BottomPanel';
import ContextMenu from './components/ContextMenu';
import { buildAskAboutDocumentPrompt } from './utils/askAboutPrompt';
import MoveToModal from './components/MoveToModal';

// Modals
import InstallDepsPrompt from './components/modals/InstallDepsPrompt';
import NewProjectModal from './components/modals/NewProjectModal';
import EditProjectModal from './components/modals/EditProjectModal';
import SettingsModal from './components/modals/SettingsModal';
import ConfirmModal from './components/modals/ConfirmModal';
import PlanReviewWindow from './components/PlanReviewWindow';
import AlertModal from './components/modals/AlertModal';
import InteractiveTerminalModal from './components/modals/InteractiveTerminalModal';
import AskModal from './components/modals/AskModal';
import HardwareModal from './components/modals/HardwareModal';
import MonitorsModal from './components/modals/MonitorsModal';
import ConnectorsModal from './components/modals/ConnectorsModal';
import useMonitorBridge from './hooks/useMonitorBridge';
import AssetStoreModal from './components/modals/AssetStoreModal';
import CloudSyncModal from './components/modals/CloudSyncModal';
import CloudDownloadModal from './components/modals/CloudDownloadModal';
import OnboardingModal from './components/modals/OnboardingModal';
import DirPickerModal from './components/modals/DirPickerModal';
import DeleteProjectModal from './components/modals/DeleteProjectModal';
import { useCustomDialog } from './components/modals/CustomDialogProvider';

import EditModelsModal from './components/modals/EditModelsModal';
import AddModelModal from './components/modals/AddModelModal';
import EditProvidersModal from './components/modals/EditProvidersModal';
import AddConnectionModal from './components/modals/AddConnectionModal';

const numericMessageId = (message) => {
  if (message?.id === undefined || message?.id === null || message.id === '') return null;
  const parsed = Number(message.id);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
};

const AGENT_WRITE_TOOLS = new Set(['write_file', 'write_content_pos', 'replace_content_range', 'edit_file']);

// How long the editor panel gets to show an agent's reveal_in_pdf, loading the
// PDF included; below the backend's IDE_COMMAND_TIMEOUT, so the agent hears
// this reason rather than "the window did not answer".
const PDF_REVEAL_TIMEOUT_MS = 12000;

const normalizeEditorPathKey = (filePath, caseInsensitive = false) => {
  if (!filePath) return '';
  let normalized = String(filePath)
    .replace(/\\/g, '/')
    .replace(/\/+/g, '/')
    .replace(/^\.\//, '');
  const parts = [];
  for (const part of normalized.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..' && parts.length > 0 && parts[parts.length - 1] !== '..') {
      parts.pop();
    } else {
      parts.push(part);
    }
  }
  normalized = parts.join('/');
  return caseInsensitive ? normalized.toLowerCase() : normalized;
};

const normalizeInlineReplacementSpacing = (replacementText, originalText = '', eol = '\n') => {
  const toLf = (value) => String(value || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const replacement = toLf(replacementText);
  const original = toLf(originalText);
  const replacementLines = replacement.split('\n');
  const originalLines = original.split('\n');
  const isBlank = (line) => line.trim() === '';
  const replacementNonBlank = replacementLines.filter(line => !isBlank(line));
  const originalNonBlank = originalLines.filter(line => !isBlank(line));

  if (
    originalNonBlank.length > 0 &&
    replacementNonBlank.length === originalNonBlank.length &&
    replacementLines.length > originalLines.length + Math.max(4, Math.ceil(originalLines.length * 0.2))
  ) {
    const gapsFor = (lines) => {
      const gaps = [];
      let pendingBlankCount = 0;
      let seenContent = false;
      let leading = 0;
      const content = [];

      for (const line of lines) {
        if (isBlank(line)) {
          pendingBlankCount += 1;
          continue;
        }
        if (!seenContent) {
          leading = pendingBlankCount;
        } else {
          gaps.push(pendingBlankCount);
        }
        seenContent = true;
        pendingBlankCount = 0;
        content.push(line);
      }

      return { leading, gaps, trailing: pendingBlankCount, content };
    };

    const originalGaps = gapsFor(originalLines);
    const replacementGaps = gapsFor(replacementLines);
    const rebuilt = [];
    const pushBlankLines = (count) => {
      for (let i = 0; i < count; i += 1) rebuilt.push('');
    };

    pushBlankLines(Math.min(replacementGaps.leading, originalGaps.leading));
    replacementGaps.content.forEach((line, index) => {
      if (index > 0) {
        pushBlankLines(Math.min(
          replacementGaps.gaps[index - 1] || 0,
          originalGaps.gaps[index - 1] || 0,
        ));
      }
      rebuilt.push(line);
    });
    pushBlankLines(Math.min(replacementGaps.trailing, originalGaps.trailing));
    return rebuilt.join('\n').replace(/\n/g, eol);
  }

  const originalBlankCount = originalLines.length - originalNonBlank.length;
  const replacementBlankCount = replacementLines.length - replacementNonBlank.length;
  if (
    originalNonBlank.length > 0 &&
    replacementLines.length > originalLines.length + Math.max(4, Math.ceil(originalLines.length * 0.2)) &&
    replacementBlankCount > originalBlankCount + Math.max(4, Math.ceil(originalLines.length * 0.15))
  ) {
    return replacementLines
      .filter((line, index, lines) => {
        if (!isBlank(line)) return true;
        const previous = lines[index - 1];
        const next = lines[index + 1];
        return !(previous && next && !isBlank(previous) && !isBlank(next));
      })
      .join('\n')
      .replace(/\n/g, eol);
  }

  return replacement.replace(/\n/g, eol);
};

const extractInlineReplacementBlock = (text) => {
  const source = String(text || '').trim();
  const lines = source.split('\n');
  const blocks = [];

  for (let index = 0; index < lines.length; index += 1) {
    const opening = lines[index].match(/^( {0,3})(`{3,}|~{3,})([^\r\n]*)$/);
    if (!opening) continue;

    const fence = opening[2];
    const fenceChar = fence[0];
    const fenceLength = fence.length;
    const info = opening[3].trim().split(/\s+/)[0].toLowerCase();
    const bodyLines = [];

    index += 1;
    for (; index < lines.length; index += 1) {
      const closingPattern = fenceChar === '`'
        ? /^( {0,3})(`{3,})(\s*)$/
        : /^( {0,3})(~{3,})(\s*)$/;
      const closing = lines[index].match(closingPattern);
      if (closing && closing[2].length >= fenceLength) {
        blocks.push({
          fenceLength,
          info,
          content: bodyLines.join('\n').replace(/\n+$/, ''),
        });
        break;
      }
      bodyLines.push(lines[index]);
    }
  }

  if (!blocks.length) return null;
  const contentBlock = blocks.find(block => block.info === 'content');
  if (contentBlock) return contentBlock.content;
  const fourTickBlock = blocks.find(block => block.fenceLength >= 4);
  if (fourTickBlock) return fourTickBlock.content;
  return blocks[blocks.length - 1].content;
};

const isBinaryEditorFile = (filePath) => {
  if (!filePath) return false;
  return /\.pdf$/i.test(String(filePath));
};

const SYSTEM_APP_EXTENSIONS = new Set([
  // Images
  'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'ico', 'tiff', 'tif', 'psd', 'ai', 'raw', 'cr2', 'nef', 'heic', 'heif',
  // Audio
  'mp3', 'wav', 'ogg', 'flac', 'aac', 'm4a', 'wma', 'opus', 'mid', 'midi',
  // Video
  'mp4', 'webm', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'm4v', '3gp',
  // Archives
  'zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'tgz', 'iso', 'cab', 'dmg', 'pkg',
  // Non-supported Office / Documents
  'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'xlsm', 'odt', 'ods', 'odp', 'epub', 'pages', 'numbers', 'key',
  // Executables / Binaries / Compiled / Database / Libraries
  'exe', 'dll', 'so', 'dylib', 'bin', 'dat', 'db', 'sqlite', 'sqlite3', 'pyc', 'pyo', 'pyd', 'o', 'obj', 'a', 'lib', 'class', 'jar', 'war', 'ear', 'apk', 'msi', 'deb', 'rpm'
]);

const isUnsupportedSystemFile = (filePath) => {
  if (!filePath) return false;
  const parts = String(filePath).split('.');
  if (parts.length <= 1) return false;
  const ext = parts.pop().toLowerCase();
  return SYSTEM_APP_EXTENSIONS.has(ext);
};

// ─────────────────────────────────────────────────────────────────────────────
// App
// ─────────────────────────────────────────────────────────────────────────────
export default function App() {
  const { t } = useTranslation();
  const { showAlert, showConfirm } = useCustomDialog();

  // ── Projects / files ──────────────────────────────────────────────────────
  const [projects, setProjects] = useState([]);
  const [activeProject, setActiveProject] = useState(null);
  const isCaseInsensitiveProjectPath = Boolean(
    activeProject?.project_path?.includes('\\') ||
    /^[a-z]:/i.test(activeProject?.project_path || '') ||
    navigator.userAgent.toLowerCase().includes('windows')
  );
  const filePathKey = (filePath) => normalizeEditorPathKey(filePath, isCaseInsensitiveProjectPath);
  const sameFilePath = (left, right) => filePathKey(left) === filePathKey(right);
  const isFileInsidePath = (filePath, parentPath) => {
    const parentKey = filePathKey(parentPath);
    const childKey = filePathKey(filePath);
    return childKey === parentKey || childKey.startsWith(`${parentKey}/`);
  };
  const replaceFilePathPrefix = (filePath, oldPath, newPath) => {
    if (!isFileInsidePath(filePath, oldPath)) return filePath;
    const normalizedFile = String(filePath).replace(/\\/g, '/');
    const normalizedOld = String(oldPath).replace(/\\/g, '/').replace(/\/+$/g, '');
    const oldSegments = normalizedOld.split('/').filter(Boolean).length;
    const suffix = normalizedFile.split('/').filter(Boolean).slice(oldSegments).join('/');
    return suffix ? `${newPath}/${suffix}` : newPath;
  };
  // Keep the unsaved marker's disk baseline under the paths the buffers move
  // to. Mutated in place: saveFile treats a new object as a project switch.
  const moveDiskContents = (oldPath, newPath) => {
    const cache = diskFileContentsRef.current;
    const moves = Object.keys(cache)
      .filter(key => isFileInsidePath(key, oldPath))
      .map(key => [key, replaceFilePathPrefix(key, oldPath, newPath), cache[key]]);
    for (const [key] of moves) delete cache[key];
    for (const [, moved, content] of moves) cache[moved] = content;
  };
  const dropDiskContents = (path) => {
    const cache = diskFileContentsRef.current;
    for (const key of Object.keys(cache)) if (isFileInsidePath(key, path)) delete cache[key];
  };
  const dedupeOpenFileList = (files, preferredPath = null) => {
    const preferredKey = preferredPath ? filePathKey(preferredPath) : null;
    const seen = new Set();
    return files.reduce((deduped, file) => {
      const key = filePathKey(file);
      if (!key || seen.has(key)) return deduped;
      seen.add(key);
      deduped.push(preferredKey === key ? preferredPath : file);
      return deduped;
    }, []);
  };
  const [files, setFiles] = useState([]);
  const [selectedFile, setSelectedFile] = useState(null);
  // Word/character statistics reported by the editor panel (mode-aware:
  // LaTeX, Markdown or plain text), rendered in the status bar.
  const [editorTextStats, setEditorTextStats] = useState(null);
  const [selectedNodes, setSelectedNodes] = useState(new Set());
  const [renamingNodePath, setRenamingNodePath] = useState(null);
  // Provisional tree row for creating a file or directory in place.
  const [pendingCreate, setPendingCreate] = useState(null);
  const inlineCreateIdRef = useRef(0);
  useEffect(() => { setPendingCreate(null); }, [activeProject?.project_path]);
  const [fileContent, setFileContentState] = useState('');
  // Every editor surface updates the ref synchronously, so an HTTP response
  // cannot observe the previous render's text between typing and an effect.
  const fileContentRef = useRef('');
  const setFileContent = useCallback(value => {
    const next = typeof value === 'function' ? value(fileContentRef.current) : value;
    fileContentRef.current = next;
    setFileContentState(next);
  }, []);
  const [openFiles, setOpenFiles] = useState([]);
  const [fileContents, setFileContents] = useState({});

  // Baseline of the diff view (the file at git HEAD), not of the unsaved
  // marker: that one compares with the disk (see utils/fileMarkers.js).
  const [originalFileContents, setOriginalFileContents] = useState({});
  // Project-relative paths an agent turn changed and the user has not opened
  // since, from `checkpoint_finalized.changed_files`.
  const [agentChangedFiles, setAgentChangedFiles] = useState(() => new Set());
  const [rightClickedNode, setRightClickedNode] = useState(null);
  const [isSaving, setIsSaving] = useState(false);

  // ── Chat / agent ──────────────────────────────────────────────────────────
  const [chatMessages, setChatMessages] = useState([]);
  const [chatThoughtTail, setChatThoughtTail] = useState(createThoughtTail);
  const chatThoughtStream = useMemo(() => thoughtTailText(chatThoughtTail), [chatThoughtTail]);
  // The live reasoning on screen: the most recent chunks within the configured
  // size (editor settings). Everything is still stored by the backend.
  const chatThoughtTailRef = useRef(createThoughtTail());
  const [thoughtContextTokens, setThoughtContextTokens] = useState(DEFAULT_THOUGHT_CONTEXT_TOKENS);
  // Read by the stream handler, which a running turn captured before a change.
  const thoughtContextTokensRef = useRef(DEFAULT_THOUGHT_CONTEXT_TOKENS);
  const applyThoughtContextTokens = (value) => {
    const tokens = clampThoughtContextTokens(value);
    thoughtContextTokensRef.current = tokens;
    setThoughtContextTokens(tokens);
  };
  // What the *chat* shows of the running reasoning: a preview of the last
  // lines, so the user sees the model thinking without a bubble holding tens
  // of thousands of tokens, which is what froze the window on a long turn.
  // Expanding it opens the Agent Thinking panel, which holds the whole thing.
  const [chatThoughtPreviewTokens, setChatThoughtPreviewTokens] = useState(1000);
  // What the Agent Thinking panel is showing: the live turn (null) or the
  // stored reasoning of one past message, fetched in full when it is opened.
  const [thinkingFocus, setThinkingFocus] = useState(null);
  const chatThoughtPreviewTokensRef = useRef(1000);
  const applyChatThoughtPreviewTokens = (value) => {
    const tokens = Math.max(100, Math.min(100000, Math.floor(Number(value)) || 1000));
    chatThoughtPreviewTokensRef.current = tokens;
    setChatThoughtPreviewTokens(tokens);
  };
  useEffect(() => {
    fetch('/api/settings/thoughts')
      .then(r => (r.ok ? r.json() : null))
      .then(cfg => {
        if (cfg?.thought_context_tokens !== undefined) applyThoughtContextTokens(cfg.thought_context_tokens);
        if (cfg?.chat_thought_preview_tokens !== undefined) applyChatThoughtPreviewTokens(cfg.chat_thought_preview_tokens);
      })
      .catch(() => { });
  }, []);
  const [chatResponseStream, setChatResponseStream] = useState('');
  const chatResponseStreamRef = useRef('');
  // Provider-reported occupancy of the orchestrator context window, emitted by
  // the backend after every LLM call. Null until the first measured call of a
  // conversation, when the panel falls back to a character estimate.
  const [chatContextUsage, setChatContextUsage] = useState(null);
  // Shape the backend's measured usage for the panel. A payload without
  // prompt_tokens carries no measurement, so the panel keeps estimating.
  // `source` says where the number came from: the provider, a local count of the
  // assembled request, or a count of the restored working state for a chat whose
  // last turn predates the persisted measurement. The panel labels them
  // differently instead of presenting all three as the provider's number.
  const contextUsageFromPayload = (payload) => (
    payload && payload.prompt_tokens > 0
      ? {
        promptTokens: payload.prompt_tokens,
        completionTokens: payload.completion_tokens || 0,
        totalTokens: payload.total_tokens || 0,
        contextWindow: payload.context_window || 0,
        source: payload.source || '',
      }
      : null
  );
  const [chatInput, setChatInput] = useState('');
  // Bumped when something outside the chat (e.g. the PDF viewer's "Ask about")
  // drops text into the composer and the caret should land there.
  const [chatInputFocusSignal, setChatInputFocusSignal] = useState(0);
  // Bumped whenever a speech or dictation setting is saved. The surfaces that
  // gate a *control* on readiness (the chat's microphone above all) have no
  // "on open" moment to re-read at, so they watch this instead — see
  // hooks/useDictation.js.
  const [speechSettingsSignal, setSpeechSettingsSignal] = useState(0);
  const bumpSpeechSettings = useCallback(() => setSpeechSettingsSignal((n) => n + 1), []);
  const [pendingAttachments, setPendingAttachments] = useState([]);
  const [isAgentRunning, setIsAgentRunning] = useState(false);
  // Messages typed while a turn is running, waiting to be handed to it. An entry
  // leaves this list when the backend reports it delivered, when the user
  // cancels it, or when the turn ends without delivering it — in which case it
  // is sent as an ordinary next turn.
  const [queuedMessages, setQueuedMessages] = useState([]);
  const [isInterruptPending, setIsInterruptPending] = useState(false);
  const [agentStepInfo, setAgentStepInfo] = useState({ step: 0, maxSteps: null });
  // The provider has sent nothing past the backend's threshold (`provider_wait`).
  const [providerWait, setProviderWait] = useState(null);
  // Whatever ended the turn (answer, error, interruption) also ended the wait.
  useEffect(() => { if (!isAgentRunning) setProviderWait(null); }, [isAgentRunning]);
  const [heartbeatMode, setHeartbeatModeState] = useState(() => getPreference('chatHeartbeatMode') || 'medium');
  const setHeartbeatMode = (mode) => {
    setHeartbeatModeState(mode);
    setPreference('chatHeartbeatMode', mode);
  };
  const resolveEffectiveMaxHeartbeats = (modeOverride = null) => {
    const mode = modeOverride || heartbeatMode;
    if (mode === 'low') return 20;
    if (mode === 'medium') return 50;
    if (mode === 'high') return 100;
    if (mode === 'custom') {
      const projHb = activeProject?.model_params?.max_heartbeats;
      return projHb !== undefined && projHb !== null && projHb !== '' ? parseInt(projHb, 10) : null;
    }
    return 50;
  };
  const [isInlineRunning, setIsInlineRunning] = useState(false);
  // Refreshes an already-open Review panel after the backend has safely closed
  // an agent checkpoint, including a turn whose cancellation was acknowledged
  // before its Git cleanup completed.
  const [checkpointRevision, setCheckpointRevision] = useState(0);

  // ── Bottom panel ──────────────────────────────────────────────────────────
  const [terminalLogs, setTerminalLogs] = useState([]);
  const [problems, setProblems] = useState([]);
  const [achievementsMemory, setAchievementsMemory] = useState('');
  const [isTerminalCollapsed, setIsTerminalCollapsed] = useState(true);
  const [activeBottomTab, setActiveBottomTab] = useState('thinking');
  const [panelMaxLines, setPanelMaxLines] = useState(() => {
    const stored = getPreference('panelMaxLines');
    const parsed = stored !== null ? Number(stored) : NaN;
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 1000;
  });

  // ── UI state ──────────────────────────────────────────────────────────────
  const [layoutMode, setLayoutMode] = useState('ide');
  const isChatLayout = layoutHasChatSidebar(layoutMode);
  // The studio: editor + preview across the top, chat and terminal side by side
  // beneath, workspace explorer docked left. See utils/studioLayout.js.
  const isStudioLayout = layoutMode === 'studio';
  // The document layout: the open .tex/.md and its own preview (the PDF for
  // LaTeX, the rendered document for Markdown/HTML) across the full width, with
  // neither chat nor terminal on screen. It renders the same EditorPanel as the
  // IDE layout and only seeds the panel's preview flags, so every toolbar
  // toggle keeps working once the user is inside it.
  const isDocumentLayout = layoutMode === 'document';
  // Layouts that show the editor and its preview, and therefore share the IDE
  // layout's docked sidebar and resize handle. See utils/layoutModes.js.
  const isEditorLayout = layoutShowsEditor(layoutMode);
  const [isChatVisible, setIsChatVisible] = useState(false);
  const [activeSidebarTab, setActiveSidebarTab] = useState('explorer');
  // The chat-first layouts' own sidebar (chat list + explorer). It has no tab to
  // clear, so its visibility is a flag of its own, toggled by the Activity Bar's
  // Explorer button while one of those layouts is active.
  const [isChatSidebarVisible, setIsChatSidebarVisible] = useState(true);
  const [contextMenu, setContextMenu] = useState(null);
  const [clipboardNode, setClipboardNode] = useState(null);
  const [jumpToLine, setJumpToLine] = useState(null);
  // The last compile an agent ran (`latex_compiled`); `seq` makes each one an
  // event for the editor panel, even when two in a row have the same outcome.
  const [agentCompile, setAgentCompile] = useState(null);
  // The agent's reveal_in_pdf, carried out by the editor panel's forward search
  // ({ seq, file, line, resolve }; see requestPdfReveal).
  const [pdfRevealRequest, setPdfRevealRequest] = useState(null);
  const pdfRevealSeqRef = useRef(0);
  const [showAdvancedParams, setShowAdvancedParams] = useState(false);
  const [dirPicker, setDirPicker] = useState(null);
  const [alertMessage, setAlertMessage] = useState('');

  useEffect(() => {
    const nativeAlert = window.alert;
    window.alert = (message) => setAlertMessage(String(message ?? ''));
    return () => { window.alert = nativeAlert; };
  }, []);


  useEffect(() => {
    if (isChatLayout) {
      setIsChatVisible(true);
    }
  }, [isChatLayout]);

  // ── Git ───────────────────────────────────────────────────────────────────
  const [gitChanges, setGitChanges] = useState([]);
  const [commitMessage, setCommitMessage] = useState('');
  const [isCommitting, setIsCommitting] = useState(false);
  const [useShadowGit, setUseShadowGit] = useState(false);
  const currentGitRootPath = activeProject?.git_root_path || '';
  const gitQuerySuffix = () => {
    if (!activeProject) return '';
    const params = new URLSearchParams({
      projectPath: activeProject.project_path,
      shadow: String(useShadowGit),
    });
    if (!useShadowGit && currentGitRootPath) params.set('gitRootPath', currentGitRootPath);
    return params.toString();
  };
  const gitRequestPayload = (extra = {}) => ({
    projectPath: activeProject?.project_path,
    shadow: useShadowGit,
    ...(!useShadowGit && currentGitRootPath ? { gitRootPath: currentGitRootPath } : {}),
    ...extra,
  });

  // ── Drag-and-drop ─────────────────────────────────────────────────────────
  const [draggedNode, setDraggedNode] = useState(null);
  const [dragOverPath, setDragOverPath] = useState(null);
  const [moveModal, setMoveModal] = useState(null);

  // ── Panel sizing ──────────────────────────────────────────────────────────
  const [sidebarWidth, setSidebarWidth] = useState(330);
  const [chatWidth, setChatWidth] = useState(400);
  const [bottomPanelHeight, setBottomPanelHeight] = useState(240);
  // The studio arranges the same panels in a different geometry, so it keeps
  // its own sizes: sharing them would mean a chat widened for the studio's
  // bottom row also widening the IDE's right-hand dock, and a bottom row sized
  // to hold a conversation reopening as a 700px terminal in the IDE.
  const [studioChatWidth, setStudioChatWidth] = useState(STUDIO_CHAT_WIDTH_DEFAULT);
  const [studioBottomHeight, setStudioBottomHeight] = useState(STUDIO_BOTTOM_HEIGHT_DEFAULT);
  const [isEditorMaximized, setIsEditorMaximized] = useState(false);
  const [isBottomMaximized, setIsBottomMaximized] = useState(false);
  // The studio's whole geometry is this one grid template: the panels below are
  // the IDE's own components kept at their existing positions in the tree — so
  // switching layouts never remounts the editor or tears down a live terminal —
  // and only their CSS placement changes.
  const studioGrid = useMemo(() => studioGridTemplate({
    chatWidth: studioChatWidth,
    bottomHeight: studioBottomHeight,
    isChatVisible,
    isTerminalVisible: !isTerminalCollapsed,
    isEditorMaximized,
    isBottomMaximized,
  }), [studioChatWidth, studioBottomHeight, isChatVisible, isTerminalCollapsed, isEditorMaximized, isBottomMaximized]);

  // ── Modals ─────────────────────────────────────────────────────────────────
  const [showNewProjectModal, setShowNewProjectModal] = useState(false);
  const [importError, setImportError] = useState('');
  const [newProjName, setNewProjName] = useState('');
  const [newProjPath, setNewProjPath] = useState('');
  const [newProjDesc, setNewProjDesc] = useState('');
  // A new project starts with no model configured; the user picks one from the
  // global model store (chat toolbar or Project Settings).
  const [newProjModel, setNewProjModel] = useState('');
  const [newProjWorkerModel, setNewProjWorkerModel] = useState('');
  const [newProjMode, setNewProjMode] = useState('auto');
  const [newProjModelParams, setNewProjModelParams] = useState({});
  const [newProjWorkerModelParams, setNewProjWorkerModelParams] = useState({});
  const [newProjApiKey, setNewProjApiKey] = useState('');
  const [newProjApiBase, setNewProjApiBase] = useState('http://localhost:11434/v1');
  const [newProjWorkerApiKey, setNewProjWorkerApiKey] = useState('');
  const [newProjWorkerApiBase, setNewProjWorkerApiBase] = useState('');
  const [newProjError, setNewProjError] = useState('');

  const [editingProject, setEditingProject] = useState(null);
  const [editProjError, setEditProjError] = useState('');
  const [projectToDelete, setProjectToDelete] = useState(null);
  const [deleteCloudLink, setDeleteCloudLink] = useState(null);
  const [confirmRequest, setConfirmRequest] = useState(null);
  // Non-modal agent requests need slots that front-end prompts cannot overwrite.
  // The user can open new-file/delete/etc. confirmations while either floating
  // window is pending; losing its request id would strand the backend future.
  const [askRequest, setAskRequest] = useState(null);
  const [planRequest, setPlanRequest] = useState(null);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [isHardwareModalOpen, setIsHardwareModalOpen] = useState(false);
  const [isMonitorsModalOpen, setIsMonitorsModalOpen] = useState(false);
  const [isConnectorsModalOpen, setIsConnectorsModalOpen] = useState(false);
  const [isAssetStoreOpen, setIsAssetStoreOpen] = useState(false);
  const [isCloudSyncOpen, setIsCloudSyncOpen] = useState(false);
  const [isCloudDownloadOpen, setIsCloudDownloadOpen] = useState(false);
  // Folder the downloaded project is created in, chosen with the directory
  // picker, which lives here because the picker is a sibling modal.
  const [cloudDownloadParent, setCloudDownloadParent] = useState('');
  // Mirrors the project's cloud status so the activity bar and status bar can
  // show it without either of them polling the backend on its own.
  const [cloudStatus, setCloudStatus] = useState(null);
  // rel path -> synced | pending | syncing | conflict, for the tree badges.
  const [cloudFileStates, setCloudFileStates] = useState(null);
  const [webSearchConfig, setWebSearchConfig] = useState({ enabled: true, mcp_url: '', mcp_tool: 'web_search' });
  const [showInstallPrompt, setShowInstallPrompt] = useState(false);
  const [showOnboarding, setShowOnboarding] = useState(false);

  // ── Global Models ─────────────────────────────────────────────────────────
  // The catalog lives in ModelCatalogProvider so every surface (chat toolbar,
  // project dialogs, Edit Models, onboarding) reads the same list and sees
  // registrations made anywhere else without a reload.
  const {
    models: globalModels,
    refresh: fetchGlobalModels,
    saveModel: saveGlobalModel,
    deleteModel: deleteGlobalModel,
    loadLocalOllamaModels: loadLocalOllamaCatalogModels,
    connections: providerConnections,
    saveConnection: saveProviderConnection,
    deleteConnection: deleteProviderConnection,
  } = useModelCatalog();
  const [showEditModelsModal, setShowEditModelsModal] = useState(false);
  const [showAddModelModal, setShowAddModelModal] = useState(false);
  const [editingModelModalData, setEditingModelModalData] = useState(null);
  const [showEditProvidersModal, setShowEditProvidersModal] = useState(false);
  const [showAddConnectionModal, setShowAddConnectionModal] = useState(false);
  const [editingConnectionModalData, setEditingConnectionModalData] = useState(null);

  // ── IDE settings ──────────────────────────────────────────────────────────
  const [settingsTab, setSettingsTab] = useState('general');
  // Empty until the chat list loads. There is no chat whose id is the literal
  // "main": the default chat is stored as `main_<project>`, so a sentinel here
  // would render a selector value that matches no option while the body showed
  // whatever chat the server fell back to.
  const [activeChatId, setActiveChatId] = useState('');
  // Read by asynchronous loads to discard a result that arrives after the user
  // has already moved to another chat.
  const activeChatIdRef = useRef('');
  useEffect(() => { activeChatIdRef.current = activeChatId; }, [activeChatId]);
  // The chat the agent turn in flight belongs to ('' when none is running).
  // The user may move to another chat while it runs: its live bubble,
  // reasoning, answer and context measurement belong to that chat, never to
  // the one on screen. The backend persists all of them in the turn's own
  // chat, so going back to it reloads what was missed.
  const runChatIdRef = useRef('');
  const [runChatId, setRunChatId] = useState('');
  const bindRunChat = (chatId) => {
    runChatIdRef.current = chatId || '';
    setRunChatId(chatId || '');
  };
  const isLiveTurnOnScreen = !runChatId || runChatId === activeChatId;
  // Set synchronously when this window starts a turn and cleared where that
  // start ends. `isAgentRunning` cannot guard a start on its own: it is state,
  // so two calls issued before the next render (a double click on "save edit")
  // both read `false`. The second one then reached /run, was refused with 409,
  // and its cleanup reset `isAgentRunning` and the run's chat binding while
  // the first turn kept streaming -- a live agent behind an idle UI.
  const localTurnRef = useRef(false);
  const isTurnBusy = () => isAgentRunning || localTurnRef.current;
  // Same reason for the edit-branch flow, whose first await (creating the
  // branch) happens before any turn starts: without it a double submit made
  // two branches of one message.
  const editBranchInFlightRef = useRef(false);
  const [chats, setChats] = useState([]);
  // The built-in tutorial lives in a reserved chat (`tutorial_<project>`). Its id and
  // question menu come from the backend so the front-end never has to guess either.
  const [tutorialChatId, setTutorialChatId] = useState('');
  const [tutorialTopics, setTutorialTopics] = useState([]);
  const [theme, setTheme] = useState(() => getPreference('theme', 'dark'));
  // Accessibility interface scale. Unlike the other appearance settings this
  // one lives in ui_settings.json rather than localStorage: it is the setting a
  // user with low vision cannot work around if it silently resets, and the
  // backend store is the one whose stated purpose is surviving restarts.
  const [uiScale, setUiScale] = useState(UI_SCALE_DEFAULT);
  const [editorFontSize, setEditorFontSize] = useState(() => Number(getPreference('editorFontSize', 13)));
  const [editorTabSize, setEditorTabSize] = useState(() => Number(getPreference('editorTabSize', 4)));
  const [editorWordWrap, setEditorWordWrap] = useState(() => getPreference('editorWordWrap', 'on'));
  const [editorMinimap, setEditorMinimap] = useState(() => getPreference('editorMinimap', 'on'));
  const [showHiddenWorkspaceFiles, setShowHiddenWorkspaceFiles] = useState(false);

  // ── Accessibility interface scale ───────────────────────────────────────
  // The factor is published as a CSS custom property on the document element;
  // `.vscode-app` turns it into a `zoom` over the whole interface. Monaco
  // (automaticLayout) and xterm (a ResizeObserver on its container) both see
  // their container change size in the zoomed coordinate space and re-fit on
  // their own, so no manual relayout is needed here.
  useEffect(() => {
    document.documentElement.style.setProperty('--ui-scale', String(uiScale));
  }, [uiScale]);

  // Restore the stored scale on startup (localStorage is not reliable in the
  // webview, so this setting is owned by the backend).
  useEffect(() => {
    fetch('/api/settings/appearance')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => { if (cfg) setUiScale(clampUiScale(cfg.ui_scale)); })
      .catch(() => { });
  }, []);

  // The scale is applied immediately but written back on a trailing delay:
  // dragging the settings slider walks through every step in between, and each
  // one would otherwise rewrite ui_settings.json.
  const uiScaleSaveTimerRef = useRef(null);
  useEffect(() => () => clearTimeout(uiScaleSaveTimerRef.current), []);

  const applyUiScale = useCallback((value) => {
    const next = roundUiScale(value);
    setUiScale(next);
    clearTimeout(uiScaleSaveTimerRef.current);
    uiScaleSaveTimerRef.current = setTimeout(() => {
      fetch('/api/settings/appearance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ui_scale: next }),
      }).catch(() => { });
    }, 300);
    return next;
  }, []);

  useEffect(() => {
    fetch('/api/settings/workspace')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        if (cfg?.show_hidden_workspace_files !== undefined) {
          setShowHiddenWorkspaceFiles(Boolean(cfg.show_hidden_workspace_files));
        }
      })
      .catch(() => { });
  }, []);

  const [isPending, startTransition] = useTransition();
  const [isLoadingChat, setIsLoadingChat] = useState(false);

  // ── Optional dependencies ─────────────────────────────────────────────────
  const [isInstallingDeps, setIsInstallingDeps] = useState(false);
  const [installDepsStatus, setInstallDepsStatus] = useState('');
  const [installDepsLog, setInstallDepsLog] = useState('');

  // ── Ephemeral Agent Params ────────────────────────────────────────────────
  const [ephemeralParams, setEphemeralParams] = useState(() => {
    try {
      const stored = JSON.parse(getPreference('ephemeralParams')) || {};
      // `think` was a per-run override before thinking became a model-catalog
      // capability. Drop a stored value so it cannot be sent as a request
      // parameter that nothing reads back.
      delete stored.think;
      return stored;
    } catch { return {}; }
  });

  const [triggerCompileRequest, setTriggerCompileRequest] = useState(null);

  // ── Inline prompt (editor Ctrl+L / context-menu actions) ────────────────
  const [inlinePrompt, setInlinePrompt] = useState(null);
  // Stores the Monaco range that should be replaced after an inline agent reply
  const pendingInlineRangeRef = useRef(null);
  // Paths the turn's write tools named so far, reconciled with open buffers
  // after each write and once more when the turn ends.
  const turnWrittenPathsRef = useRef(new Set());

  // ── Refs ──────────────────────────────────────────────────────────────────
  const terminalRef = useRef(null);
  const terminalInstanceRef = useRef(null);
  const fitAddonRef = useRef(null);
  const eventSourceRef = useRef(null);
  const chatEndRef = useRef(null);
  const logEndRef = useRef(null);
  const editorRef = useRef(null);
  const monacoRef = useRef(null);
  const saveFileRef = useRef(null);
  const diskFileContentsRef = useRef({});
  const fileContentsRef = useRef(fileContents);
  fileContentsRef.current = fileContents;
  // Recomputed on every render rather than memoized: the disk baseline is a
  // ref, and each write to it comes with a buffer state update (fileContent /
  // fileContents), which is what brings the render.
  const unsavedFiles = new Set(unsavedFilePaths({
    openFiles,
    fileContents,
    activeFile: selectedFile,
    activeContent: fileContent,
    diskContents: diskFileContentsRef.current,
    isBinary: isBinaryEditorFile,
  }).map(markerPathKey));
  const fileSaveQueueRef = useRef(createFileSaveQueue());
  const pendingSaveCountRef = useRef(0);
  const documentContextRef = useRef(null);
  documentContextRef.current = {
    projectPath: activeProject?.project_path, selectedFile,
    getContent: getCurrentTextFileContent,
  };
  const gitStatusRequestRef = useRef(null);
  const lastEditorInputAtRef = useRef(0);
  const importFileInputRef = useRef(null);
  const importTargetPathRef = useRef('');

  function getCurrentTextFileContent() {
    if (selectedFile && !isBinaryEditorFile(selectedFile)) {
      const editorValue = editorRef.current?.getModel?.()?.getValue?.();
      if (typeof editorValue === 'string') return editorValue;
    }
    return fileContentRef.current;
  }

  async function refreshSelectedFileFromDiskIfUnmodified() {
    if (!activeProject?.project_path || !selectedFile) return;
    if (isBinaryEditorFile(selectedFile)) return;
    if (editorRef.current?.hasTextFocus?.()) return;
    if (Date.now() - lastEditorInputAtRef.current < 1500) return;
    const lastDiskContent = diskFileContentsRef.current[selectedFile];
    if (lastDiskContent === undefined || fileContentRef.current !== lastDiskContent) return;
    try {
      const res = await fetch(`/api/file/read?projectPath=${encodeURIComponent(activeProject.project_path)}&filePath=${encodeURIComponent(selectedFile)}&t=${Date.now()}`);
      if (!res.ok) return;
      const data = await res.json();
      if (data.content === lastDiskContent) return;
      diskFileContentsRef.current[selectedFile] = data.content;
      setFileContent(data.content);
      setFileContents(prev => ({ ...prev, [selectedFile]: data.content }));
      setOriginalFileContents(prev => ({ ...prev, [selectedFile]: data.content }));
      addLog('info', t('app.reloadedFromDisk', { path: selectedFile }));
    } catch {
      // Ignore transient file reads; the next refresh/focus will try again.
    }
  }

  // ── Hooks ─────────────────────────────────────────────────────────────────
  const { startResizing } = useResizing({
    setSidebarWidth,
    setChatWidth,
    setBottomPanelHeight,
    sidebarWidth,
    chatWidth,
    bottomPanelHeight,
    studioChatWidth,
    setStudioChatWidth,
    studioBottomHeight,
    setStudioBottomHeight,
  });

  // ── Effects ───────────────────────────────────────────────────────────────
  useEffect(() => {
    fetch('/api/onboarding/status')
      .then(res => res.json())
      .then(data => {
        if (!data.completed) {
          setShowOnboarding(true);
        } else {
          fetchProjects();
        }
      })
      .catch(() => {
        // Fallback if endpoint fails
        fetchProjects();
      });
  }, []);

  const handleOnboardingComplete = () => {
    setShowOnboarding(false);
    fetchProjects();
  };

  const handleGlobalModelSave = async (modelData) => {
    try {
      const previousModelId = modelData.previous_id || modelData.id;
      const result = await saveGlobalModel(modelData);
      if (result.ok) {
        const projectUsesMainModel = activeProject?.model === previousModelId || activeProject?.model === modelData.id;
        const projectUsesWorkerModel = activeProject?.worker_model === previousModelId || activeProject?.worker_model === modelData.id;
        if (activeProject && (projectUsesMainModel || projectUsesWorkerModel)) {
          const payload = {
            project_name: activeProject.name,
            chat_id: activeChatId
          };
          if (projectUsesMainModel) {
            payload.model = modelData.id;
          }
          if (projectUsesWorkerModel) {
            payload.worker_model = modelData.id;
          }

          const projectRes = await fetch('/api/gilzabots/update-project', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
          if (projectRes.ok) {
            const updated = await projectRes.json();
            setActiveProject(prev => prev ? ({ ...prev, ...updated }) : prev);
            setProjects(prev => prev.map(p => (p.name === updated.name) ? { ...p, ...updated } : p));
            setEditingProject(prev => {
              if (!prev || prev.name !== updated.name) return prev;
              return { ...prev, ...updated };
            });
          }
        }
        setShowAddModelModal(false);
      }
      return result;
    } catch (e) {
      console.error(e);
      return { ok: false, error: 'model_save_failed' };
    }
  };

  const handleConnectionSave = async (connectionData) => {
    const result = await saveProviderConnection(connectionData);
    if (result.ok) {
      setShowAddConnectionModal(false);
    }
    return result;
  };

  const handleConnectionDelete = (connectionId) => deleteProviderConnection(connectionId);

  const handleGlobalModelDelete = (modelId) => deleteGlobalModel(modelId);

  const handleLoadLocalOllamaModels = () => loadLocalOllamaCatalogModels();
  const handleProjectModelChange = async (field, value) => {
    if (!activeProject) return;
    try {
      const payload = {
        project_name: activeProject.name,
        display_name: activeProject.project_name || activeProject.name,
        project_path: activeProject.project_path,
        main_file: activeProject.main_file || '',
        model: activeProject.model,
        worker_model: activeProject.worker_model,
        mode: activeProject.mode,
        description: activeProject.description,
        model_params: activeProject.model_params,
        worker_model_params: activeProject.worker_model_params,
        use_shared_memory: activeProject.use_shared_memory,
        chat_id: activeChatId
      };

      // Update specific field (orchestrator or worker)
      if (field === 'model') {
        payload.model = value;
      } else if (field === 'worker_model') {
        payload.worker_model = value;
      }

      const res = await fetch('/api/gilzabots/update-project', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        const updated = await res.json();
        setActiveProject(prev => ({ ...prev, ...updated }));
        setProjects(prev => prev.map(p => (p.name === updated.name) ? { ...p, ...updated } : p));
      }
    } catch (err) {
      console.error('Failed to update project model', err);
    }
  };

  useEffect(() => {
    fetch('/api/settings/web-search')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => { if (cfg) setWebSearchConfig(cfg); })
      .catch(() => { });
  }, []);

  // Restore language from backend on startup (localStorage not reliable in webview)
  useEffect(() => {
    fetch('/api/settings/language')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        if (cfg?.lang) {
          i18n.changeLanguage(cfg.lang);
        } else {
          // No saved preference — push current detected language to backend
          const detected = i18n.language?.startsWith('pt') ? 'pt' : 'en';
          fetch('/api/settings/language', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lang: detected }),
          }).catch(() => { });
        }
      })
      .catch(() => { });
  }, []);

  useEffect(() => {
    if (!editingProject) setShowAdvancedParams(false);
  }, [editingProject]);

  useEffect(() => {
    if (!showNewProjectModal) setNewProjError('');
  }, [showNewProjectModal]);

  // Track the name of the last project for which chat was initialised, so that
  // re-rendering the same project (e.g. after saving its settings) does NOT wipe
  // the chat history. Messages are only reset when switching to a DIFFERENT project.
  const prevProjectNameRef = useRef(null);

  useEffect(() => {
    if (activeProject) {
      fetchFiles();
      fetchProblems();
      if (prevProjectNameRef.current !== activeProject.name) {
        prevProjectNameRef.current = activeProject.name;

        // Show a blank state while we load — avoid flash of stale greeting
        setChatMessages([]);
        // The measurement described the previous project's window.
        setChatContextUsage(null);
        setIsLoadingChat(true);

        // Fetch chats
        fetch(`/api/chat/list?project_name=${encodeURIComponent(activeProject.name)}&t=${Date.now()}`)
          .then(res => res.json())
          .then(data => {
            const loadedChats = data.chats || [];
            setChats(loadedChats);
            // Published by the server (empty when the project has no tutorial chat) so
            // the question menu reappears when the tutorial is reopened from the chat
            // sidebar rather than from the ActivityBar button.
            setTutorialChatId(data.tutorial_chat_id || '');

            // Opening a project lands on its most recently created chat. The
            // server lists chats newest first, so that is the first entry;
            // taking it from the list (not from browser storage) keeps the
            // choice the same in every distribution and always names a chat
            // the project still has. A project with no chat opens none.
            const currentChatId = loadedChats.length > 0 ? loadedChats[0].id : '';
            setActiveChatId(currentChatId);
            if (activeProject.current_chat_id !== currentChatId) {
              setActiveProject(prev => prev ? { ...prev, current_chat_id: currentChatId } : null);
            }
            if (!currentChatId) {
              setTerminalLogs([]);
              setIsLoadingChat(false);
              return;
            }

            // Now fetch history for this chat
            fetch(`/api/chat/history?project_name=${encodeURIComponent(activeProject.name)}&chat_id=${encodeURIComponent(currentChatId)}&t=${Date.now()}`)
              .then(async res => {
                const payload = await res.json();
                if (!res.ok) throw new Error(payload?.error || `history request failed: ${res.status}`);
                // The server names the chat it answered for; anything else would
                // put another conversation under this selector.
                if (payload.chat_id && payload.chat_id !== currentChatId) {
                  throw new Error(`history returned chat ${payload.chat_id}, expected ${currentChatId}`);
                }
                return payload;
              })
              .then(histData => {
                startTransition(() => {
                  // The server still holds the measured window for this chat.
                  setChatContextUsage(contextUsageFromPayload(histData.context_usage));
                  if (histData.history && histData.history.length > 0) {
                    // Restore previous conversation
                    setChatMessages(attachThoughtActivityToMessages(histData.history, histData.activity || [], histData.thought_windows || []));
                  } else {
                    // First time opening this project/chat → show greeting
                    const greeting = activeProject.project_name || activeProject.name;
                    setChatMessages([{ role: 'assistant', content: t('app.greeting', { projectName: greeting }) }]);
                  }
                  setTerminalLogs(activityToTerminalLogs(histData.activity || []));
                  seedThinkingPanelFromChat(activeProject.name, currentChatId, histData.thought_windows || []);
                });
              })
              .catch(err => {
                console.error("Failed to fetch chat history:", err);
                const greeting = activeProject.project_name || activeProject.name;
                startTransition(() => {
                  setChatMessages([{ role: 'assistant', content: t('app.greeting', { projectName: greeting }) }]);
                });
              })
              .finally(() => {
                setIsLoadingChat(false);
              });
          })
          .catch(err => {
            console.error("Failed to fetch chat list:", err);
            const greeting = activeProject.project_name || activeProject.name;
            startTransition(() => {
              setChatMessages([{ role: 'assistant', content: t('app.greeting', { projectName: greeting }) }]);
            });
            setIsLoadingChat(false);
          });

        setOpenFiles([]);
        setSelectedFile(null);
        setFileContent('');
        setFileContents({});
        setOriginalFileContents({});
        setGitChanges([]);
        setTerminalLogs([]);
        setAchievementsMemory('');
        setCommitMessage('');
      }
    } else {
      setFiles([]);
      setSelectedFile(null);
      setFileContent('');
      setOpenFiles([]);
      setFileContents({});
      setOriginalFileContents({});
      setProblems([]);
      setChats([]);
      setChatMessages([]);
      setChatContextUsage(null);
      setActiveChatId('');
      setGitChanges([]);
      setTerminalLogs([]);
      setAchievementsMemory('');
      setCommitMessage('');
      prevProjectNameRef.current = null;
    }
  }, [activeProject]);

  const handleSwitchChat = async (id) => {
    if (!activeProject || id === activeChatId) return;
    if (!id) {
      // No chat left to show (the last one was deleted): the panel stays empty
      // until the user creates a chat or sends a message, which starts one.
      setChatContextUsage(null);
      setActiveChatId('');
      setActiveProject(prev => prev ? { ...prev, current_chat_id: '' } : null);
      setChatMessages([]);
      setTerminalLogs([]);
      return;
    }
    setIsLoadingChat(true);
    // Another chat is another history: the measured window no longer applies.
    setChatContextUsage(null);
    setActiveChatId(id);
    setActiveProject(prev => prev ? { ...prev, current_chat_id: id } : null);

    setTimeout(async () => {
      try {
        const res = await fetch(`/api/chat/history?project_name=${encodeURIComponent(activeProject.name)}&chat_id=${encodeURIComponent(id)}&t=${Date.now()}`);
        if (res.ok) {
          const data = await res.json();
          if (data.chat_id && data.chat_id !== id) {
            throw new Error(`history returned chat ${data.chat_id}, expected ${id}`);
          }
          // The user already moved on: this history belongs to a chat that is
          // no longer on screen.
          if (activeChatIdRef.current !== id) return;
          const greeting = activeProject.project_name || activeProject.name;
          startTransition(() => {
            setChatContextUsage(contextUsageFromPayload(data.context_usage));
            if (data.history && data.history.length > 0) {
              setChatMessages(attachThoughtActivityToMessages(data.history, data.activity || [], data.thought_windows || []));
            } else {
              setChatMessages([{ role: 'assistant', content: t('app.greeting', { projectName: greeting }) }]);
            }
            setTerminalLogs(activityToTerminalLogs(data.activity || []));
            seedThinkingPanelFromChat(activeProject.name, id, data.thought_windows || []);
          });
        }
      } catch (err) {
        console.error(err);
      } finally {
        setIsLoadingChat(false);
      }
    }, 10);
  };

  const createChatForTask = async (chatName) => {
    if (!activeProject) return null;
    const res = await fetch('/api/chat/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: activeProject.name, chat_name: chatName }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || t('app.createChatFailed', 'Failed to create chat'));
    }
    const data = await res.json();
    const chatId = data.id;
    setChats(prev => prev.some(chat => chat.id === chatId) ? prev : [data, ...prev]);
    setActiveChatId(chatId);
    setActiveProject(prev => prev ? { ...prev, current_chat_id: chatId } : null);
    // A brand-new chat has no measured window yet: the previous chat's
    // measurement must not leak into this one's indicator.
    setChatContextUsage(null);
    const greeting = activeProject.project_name || activeProject.name;
    const baseMessages = [{ role: 'assistant', content: t('app.greeting', { projectName: greeting }) }];
    setChatMessages(baseMessages);
    return { chatId, baseMessages };
  };

  // Open (or reopen) the built-in tutorial chat. The backend owns the reserved chat id,
  // the welcome text, and the question menu, so clicking the button twice returns to the
  // same conversation instead of stacking a new welcome on top of it.
  const handleOpenTutorial = async () => {
    if (!activeProject) {
      setAlertMessage(t('tutorial.needProject', 'Open or create a project first — the tutorial runs inside a project chat.'));
      return;
    }
    try {
      const res = await fetch('/api/tutorial/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_name: activeProject.name, lang: i18n.language }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || t('tutorial.openFailed', 'Failed to open the tutorial'));
      }
      const data = await res.json();
      const chatId = data.chat_id;

      setTutorialChatId(chatId);
      setTutorialTopics(data.topics || []);
      setChats(prev => prev.some(c => c.id === chatId) ? prev : [{ id: chatId, name: data.name }, ...prev]);

      // Another chat is another history: the previous chat's measurement no
      // longer applies, and this one's comes back with its transcript.
      setChatContextUsage(contextUsageFromPayload(data.context_usage));
      setActiveChatId(chatId);
      setActiveProject(prev => prev ? { ...prev, current_chat_id: chatId } : null);
      setChatMessages(data.history || []);

      if (layoutMode === 'ide') setIsChatVisible(true);
    } catch (err) {
      setAlertMessage(err.message);
    }
  };

  // The menu must also appear when the tutorial chat is reopened from the chat sidebar,
  // where nothing went through /api/tutorial/open and so no topics were returned.
  useEffect(() => {
    if (!tutorialChatId || activeChatId !== tutorialChatId || tutorialTopics.length) return;
    let cancelled = false;
    fetch(`/api/tutorial/topics?lang=${encodeURIComponent(i18n.language || 'en')}`)
      .then(res => res.ok ? res.json() : null)
      .then(data => { if (!cancelled && data?.topics) setTutorialTopics(data.topics); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [tutorialChatId, activeChatId, tutorialTopics.length]);

  // Answer a menu topic. The server appends the question and its pre-written answer from
  // the guide — no agent run — so the tutorial works before any model is registered.
  const handleTutorialTopic = async (topicId) => {
    if (!activeProject) return;
    const res = await fetch('/api/tutorial/answer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: activeProject.name, topic_id: topicId, lang: i18n.language }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || t('tutorial.answerFailed', 'Failed to load the tutorial answer'));
    }
    const data = await res.json();
    setChatMessages(prev => [...prev, ...(data.messages || [])]);
  };

  useEffect(() => {
    if (!activeProject || !selectedFile) return;
    if (isBinaryEditorFile(selectedFile)) {
      setOriginalFileContents(prev => ({ ...prev, [selectedFile]: '' }));
      return;
    }
    fetch(`/api/git/file-at-head?${gitQuerySuffix()}&filePath=${encodeURIComponent(selectedFile)}&t=${Date.now()}`)
      .then(r => r.ok ? r.json() : null)
      .then(gitData => {
        if (gitData && gitData.content !== undefined) {
          setOriginalFileContents(prev => ({ ...prev, [selectedFile]: gitData.content }));
        } else {
          setOriginalFileContents(prev => ({ ...prev, [selectedFile]: '' }));
        }
      })
      .catch(() => {
        setOriginalFileContents(prev => ({ ...prev, [selectedFile]: '' }));
      });
  }, [useShadowGit, currentGitRootPath, selectedFile, activeProject]);

  useEffect(() => {
    const disableContextMenu = (e) => e.preventDefault();
    const closeMenu = () => setContextMenu(null);
    document.addEventListener('contextmenu', disableContextMenu);
    document.addEventListener('click', closeMenu);
    return () => {
      document.removeEventListener('contextmenu', disableContextMenu);
      document.removeEventListener('click', closeMenu);
    };
  }, []);

  // Chat auto-scroll lives in ChatPanel, which owns the scroll container and
  // only follows new content while the user is parked at the bottom.

  // Global keyboard shortcuts (Ctrl+S, Ctrl+J, Ctrl+T, Ctrl+/- zoom)
  useEffect(() => {
    // Match the physical key alongside the character. `e.key` is resolved by the
    // platform's keyboard/compose layer, which is not always available in a
    // packaged runtime; `e.code` names the physical key and stays correct there.
    const handleKeyDown = (e) => {
      const isCtrl = e.ctrlKey || e.metaKey;
      if (isCtrl && (e.key === 's' || e.code === 'KeyS')) { e.preventDefault(); saveFileRef.current?.(); }
      else if (isCtrl && (e.key === 'j' || e.code === 'KeyJ')) {
        e.preventDefault();
        if (isBottomMaximized) {
          setIsBottomMaximized(false);
        } else if (isTerminalCollapsed) {
          setIsTerminalCollapsed(false);
        } else {
          setIsTerminalCollapsed(true);
        }
      }
      // Ctrl+T does what the Activity Bar chat button does, and is a no-op in
      // the layouts where that button is disabled. Opening the chat also puts
      // the caret in its composer, so the shortcut leads straight to typing.
      else if (isCtrl && !e.shiftKey && !e.altKey && (e.key === 't' || e.code === 'KeyT')) {
        e.preventDefault();
        if (!layoutAllowsChatToggle(layoutMode)) return;
        const nextVisible = !isChatVisible;
        setIsChatVisible(nextVisible);
        if (isEditorMaximized) setIsEditorMaximized(false);
        if (nextVisible) setChatInputFocusSignal((prev) => prev + 1);
      }
      // Interface scale — Shift distinguishes these from the editor's own
      // Ctrl +/- font size, which stays bound to the focused editor. With Shift
      // held, `e.key` reports the shifted character ('+', '_', ')'), so the
      // physical `e.code` is matched alongside it.
      else if (isCtrl && e.shiftKey && (e.key === '+' || e.key === '=' || e.code === 'Equal' || e.code === 'NumpadAdd')) {
        e.preventDefault();
        applyUiScale(uiScale + UI_SCALE_KEY_STEP);
      }
      else if (isCtrl && e.shiftKey && (e.key === '_' || e.key === '-' || e.code === 'Minus' || e.code === 'NumpadSubtract')) {
        e.preventDefault();
        applyUiScale(uiScale - UI_SCALE_KEY_STEP);
      }
      else if (isCtrl && e.shiftKey && (e.key === ')' || e.key === '0' || e.code === 'Digit0' || e.code === 'Numpad0')) {
        e.preventDefault();
        applyUiScale(UI_SCALE_DEFAULT);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isBottomMaximized, isTerminalCollapsed, uiScale, applyUiScale, layoutMode, isChatVisible, isEditorMaximized]);

  useEffect(() => {
    setPreference('theme', theme);
    if (theme === 'light') document.body.classList.add('light-theme');
    else document.body.classList.remove('light-theme');
    // `color-scheme` is declared on :root, so the html element keeps the dark
    // palette (native scrollbars, form controls and the UA selection color)
    // unless it is switched here as well.
    document.documentElement.style.colorScheme = theme === 'light' ? 'light' : 'dark';
  }, [theme]);

  useEffect(() => {
    if (activeProject) fetchGitStatus();
    else setGitChanges([]);
  }, [activeProject]);

  useEffect(() => {
    diskFileContentsRef.current = {};
    turnWrittenPathsRef.current = new Set();
    setAgentChangedFiles(new Set());
  }, [activeProject?.project_path]);

  useEffect(() => {
    if (!activeProject) return;
    // Each refresh walks the project tree and runs `git status` under the
    // server's exclusive-work lock, which every other request waits on. A
    // hidden window shows neither, so it skips them and catches up when it is
    // shown again. Coming back fires both `focus` and `visibilitychange`; the
    // second one, a moment after the first, is the same refresh twice.
    let lastRefresh = 0;
    const refreshWorkspace = () => {
      if (document.hidden) return;
      const now = Date.now();
      if (now - lastRefresh < 1000) return;
      lastRefresh = now;
      fetchFiles();
      fetchGitStatus();
      refreshSelectedFileFromDiskIfUnmodified();
    };
    const interval = setInterval(refreshWorkspace, 10000);
    window.addEventListener('focus', refreshWorkspace);
    document.addEventListener('visibilitychange', refreshWorkspace);
    return () => {
      clearInterval(interval);
      window.removeEventListener('focus', refreshWorkspace);
      document.removeEventListener('visibilitychange', refreshWorkspace);
    };
  // fileContent intentionally excluded: the guard in refreshSelectedFileFromDiskIfUnmodified
  // now reads fileContentRef.current (always current) instead of a closure-captured value,
  // so there is no need to recreate this effect on every keystroke.
  // showHiddenWorkspaceFiles is included so the polling closure's fetchFiles() default
  // parameter always matches the current toggle (otherwise a stale closure would
  // periodically overwrite the file list with the toggle value from when the effect
  // was last created, making hidden files flicker on/off).
  }, [activeProject, useShadowGit, currentGitRootPath, selectedFile, showHiddenWorkspaceFiles]);

  useEffect(() => {
    if (activeSidebarTab === 'git' && activeProject) fetchGitStatus();
  }, [activeSidebarTab, activeProject, currentGitRootPath]);

  // Un-maximize editor if all files are closed
  useEffect(() => {
    if (openFiles.length === 0 && isEditorMaximized) {
      setIsEditorMaximized(false);
    }
  }, [openFiles, isEditorMaximized]);

  useEffect(() => {
    setOpenFiles(prev => {
      const deduped = dedupeOpenFileList(prev, selectedFile);
      return deduped.length === prev.length && deduped.every((file, index) => file === prev[index])
        ? prev
        : deduped;
    });
  }, [isCaseInsensitiveProjectPath, selectedFile]);

  // ── Helpers ───────────────────────────────────────────────────────────────
  const trimToLimit = (arr, limit) => arr.length > limit ? arr.slice(arr.length - limit) : arr;

  const collectRecentChatAttachments = (messages, limit = 3) => {
    const seen = new Set();
    const collected = [];
    for (let i = (messages || []).length - 1; i >= 0; i--) {
      const atts = messages[i]?._attachments || [];
      for (let j = atts.length - 1; j >= 0; j--) {
        const att = atts[j];
        const key = `${att.type || ''}:${att.name || ''}:${att.mime || ''}`;
        if (seen.has(key)) continue;
        seen.add(key);
        collected.push(att);
        if (collected.length >= limit) return collected.reverse();
      }
    }
    return collected.reverse();
  };

  const normalizeLogAgent = (agent) => String(agent || '').trim().replace(/^@+/, '');
  const sanitizeVisibleStreamChunk = (value) => String(value ?? '').replace(/<\/?think>/gi, '');
  const mergeableLogTypes = new Set(['thought', 'reflection', 'stream_chunk', 'stdout', 'stderr']);
  const thinkingLogTypes = new Set(['thought', 'reflection', 'stream_chunk']);
  const shouldMergeLog = (left, right) => (
    left
    && right
    && left.type === right.type
    && mergeableLogTypes.has(left.type)
    && normalizeLogAgent(left.agent) === normalizeLogAgent(right.agent)
  );
  // Reasoning keeps its most recent part, within the configured size; other
  // entries keep their beginning (gilzabots gui_src/src/utils/logMerge.js).
  const fitLogMessage = (message, type) => (
    fitMergedLogMessage(message, type, thoughtContextTokensRef.current)
  );
  const mergeLogEntries = (logs) => (
    (logs || []).reduce((merged, log) => {
      if (!log) return merged;
      const cleanLog = {
        ...log,
        agent: normalizeLogAgent(log.agent),
        message: fitLogMessage(log.message, log.type),
      };
      const last = merged[merged.length - 1];
      if (shouldMergeLog(last, cleanLog)) {
        merged[merged.length - 1] = {
          ...last,
          message: fitLogMessage(joinMergedLogMessage(last.message, cleanLog.message, cleanLog.type), cleanLog.type),
        };
        return merged;
      }
      merged.push(cleanLog);
      return merged;
    }, [])
  );

  const activityToTerminalLogs = (activity = []) => (
    mergeLogEntries((activity || [])
      .filter(item => item && ['thought', 'reflection', 'stream_chunk'].includes(item.event))
      .map(item => ({
        type: item.event,
        message: item.event === 'stream_chunk'
          ? sanitizeVisibleStreamChunk(item.content || item.payload?.content || '')
          : fitLogMessage(item.content || item.payload?.content || '', item.event),
        agent: normalizeLogAgent(item.agent || item.payload?.agent || ''),
        timestamp: item.timestamp
          ? new Date(item.timestamp).toLocaleTimeString()
          : new Date().toLocaleTimeString(),
      }))
      .filter(item => item.message)
    )
  );

  const activityTimestampMs = (value) => {
    const ms = Date.parse(value || '');
    return Number.isFinite(ms) ? ms : null;
  };

  // Agent errors are stored as activity, not as chat messages: the model must never
  // read its own failures back as conversation. They are re-inserted here, in
  // timestamp order, so a failed turn still shows its error bubble after a reload.
  const mergeErrorActivityIntoMessages = (messages = [], activity = []) => {
    const errors = (activity || [])
      .filter(item => item?.event === 'error' && String(item.content || item.payload?.message || '').trim())
      .map(item => ({
        role: 'assistant',
        content: t('app.agentError', '🔴 Erro do Agente: {{message}}', {
          message: String(item.content || item.payload?.message || ''),
        }),
        is_error: true,
        timestamp: item.timestamp,
        _fromActivity: true,
      }));
    if (!errors.length) return messages;

    const merged = [...messages, ...errors];
    merged.sort((left, right) => {
      const leftTime = activityTimestampMs(left.timestamp);
      const rightTime = activityTimestampMs(right.timestamp);
      if (leftTime === null || rightTime === null || leftTime === rightTime) return 0;
      return leftTime - rightTime;
    });
    return merged;
  };

  // Stored reasoning is not part of the transcript payload any more: it is
  // written once per streamed token, so a long chat carried hundreds of
  // thousands of rows (24 MB on one measured chat) for text that opens
  // collapsed. The server counts it per message (`thought_windows`) and the
  // panel fetches a window from /api/chat/thoughts when the user expands it.
  const attachThoughtActivityToMessages = (history = [], activity = [], thoughtWindows = []) => {
    const messages = (history || []).map(message => ({ ...message }));
    for (const window of thoughtWindows || []) {
      const message = messages[window?.index];
      if (!message || !(window.count > 0)) continue;
      message._thoughtWindow = {
        count: window.count,
        fromId: window.from_id,
        toId: window.to_id,
      };
    }
    return mergeErrorActivityIntoMessages(messages, activity);
  };

  const addLog = (type, message, agent) =>
    setTerminalLogs(prev => {
      const cleanMessage = type === 'stream_chunk'
        ? sanitizeVisibleStreamChunk(message)
        : String(message ?? '');
      if (!cleanMessage) return prev;
      const cleanAgent = normalizeLogAgent(agent);
      let next;
      if (mergeableLogTypes.has(type)) {
        const candidate = { type, agent: cleanAgent };
        for (let index = prev.length - 1; index >= 0; index -= 1) {
          const existing = prev[index];
          if (shouldMergeLog(existing, candidate)) {
            next = [...prev];
            next[index] = {
              ...existing,
              agent: normalizeLogAgent(existing.agent),
              // Reasoning keeps its *recent* part, as the reloaded history does
              // (`fitLogMessage`). Clamping to the first characters instead
              // froze the Agent Thinking panel mid-turn: past the cap every new
              // chunk was appended and cut away again, so the panel sat on the
              // opening lines while the chat preview moved with the stream.
              message: fitLogMessage(joinMergedLogMessage(existing.message, cleanMessage, type), type),
            };
            break;
          }
          if (
            thinkingLogTypes.has(type)
            && thinkingLogTypes.has(existing.type)
            && !shouldMergeLog(existing, candidate)
          ) {
            break;
          }
        }
      }
      if (!next) next = [...prev, { type, message: clampLogMessage(cleanMessage), agent: cleanAgent, timestamp: new Date().toLocaleTimeString() }];
      return trimToLimit(next, panelMaxLines);
    });

  // The Agent Thinking panel belongs to the chat on screen: switching chats has
  // to bring that chat's reasoning with it, or the panel keeps showing another
  // conversation's. The transcript no longer carries reasoning, so it is read
  // here -- the recent part, within the configured reasoning size, since this
  // is the chat's history rather than one message's full window.
  const seedThinkingPanelFromChat = async (projectName, chatId, windows) => {
    setThinkingFocus(null);
    const ranges = (windows || []).filter(w => Number(w?.count) > 0);
    if (!ranges.length || !projectName || !chatId) return;
    try {
      const params = new URLSearchParams({
        project_name: projectName,
        chat_id: chatId,
        from_id: String(Math.min(...ranges.map(w => w.from_id))),
        to_id: String(Math.max(...ranges.map(w => w.to_id))),
        max_tokens: String(thoughtContextTokensRef.current),
      });
      const res = await fetch(`/api/chat/thoughts?${params}`);
      const data = res.ok ? await res.json() : {};
      const content = String(data.content || '');
      if (!content || activeChatIdRef.current !== chatId) return;
      setTerminalLogs(prev => [{
        type: 'thought',
        message: content,
        agent: 'chat_orchestrator',
        timestamp: new Date().toLocaleTimeString(),
      }, ...prev]);
    } catch (err) { /* the panel simply starts empty for this chat */ }
  };

  // Every way of changing the active chat -- the selector, a new chat from the
  // sidebar or the chat header, a task chat, an edited-message branch, the
  // tutorial -- ends in `setActiveChatId`, and not all of them reload a
  // history. Emptying the panel here is what keeps a new chat from inheriting
  // the previous chat's reasoning. It runs on commit, before any history
  // request can resolve, so the reasoning a switch loads afterwards stays; only
  // the reasoning types are removed, so the Output log is left alone.
  useEffect(() => {
    setThinkingFocus(null);
    setTerminalLogs(prev => {
      const kept = prev.filter(log => !thinkingLogTypes.has(log.type));
      return kept.length === prev.length ? prev : kept;
    });
  }, [activeChatId]);

  const isRunOnScreen = () => (
    !runChatIdRef.current || runChatIdRef.current === activeChatIdRef.current
  );

  // Opening the reasoning of a message: the chat only ever previews it, and the
  // panel is where the whole thing is read (the user's design). A live turn
  // needs no fetch -- the panel is already receiving its events.
  const showThinkingPanel = async (messageIndex, window) => {
    setActiveBottomTab('thinking');
    setIsTerminalCollapsed(false);
    if (!window || !activeProject || !activeChatId) {
      setThinkingFocus(null);
      return;
    }
    setThinkingFocus({ index: messageIndex, loading: true, content: '' });
    try {
      const params = new URLSearchParams({
        project_name: activeProject.name,
        chat_id: activeChatId,
        from_id: String(window.fromId),
        to_id: String(window.toId),
        max_tokens: '0',
      });
      const res = await fetch(`/api/chat/thoughts?${params}`);
      const data = res.ok ? await res.json() : {};
      setThinkingFocus({ index: messageIndex, loading: false, content: String(data.content || '') });
    } catch (err) {
      setThinkingFocus({ index: messageIndex, loading: false, error: true, content: '' });
    }
  };

  const addProblem = ({ tool = t('app.agentTool', 'Agent'), message, severity = 'error', ...metadata }) => {
    if (!message) return;
    setProblems(prev => trimToLimit([
      ...prev,
      {
        id: Math.random().toString(),
        tool,
        message,
        severity,
        timestamp: new Date().toLocaleTimeString(),
        ...metadata,
      },
    ], panelMaxLines));
  };

  // Sub-folders with their own .git are gitlinks for the shadow repository, so
  // agent edits inside them never produce a turn checkpoint and the Review panel
  // stays empty for work that really happened. Said once per project path when
  // the project is opened, instead of leaving the gap silent.
  const warnedNestedReposRef = useRef(new Set());
  useEffect(() => {
    const projectPath = activeProject?.project_path;
    if (!projectPath || warnedNestedReposRef.current.has(projectPath)) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/git/nested-repos?${new URLSearchParams({ projectPath })}`);
        if (!res.ok) return;
        const data = await res.json();
        const nested = data.nested_repos || [];
        if (cancelled || nested.length === 0) return;
        warnedNestedReposRef.current.add(projectPath);
        addLog('warning', t('app.shadowGitNestedRepos', { paths: nested.join(', ') }));
      } catch { /* ignore */ }
    })();
    return () => { cancelled = true; };
  }, [activeProject?.project_path]);

  // A native crash — the embedded browser faulting, most often inside the
  // graphics driver QtWebEngine loads into the GilzaBots process itself — kills
  // the application without raising a Python exception: the window disappears
  // and nothing is printed anywhere. The backend detects it on the next launch
  // from the run marker the dead process left behind, and this is where the
  // user finally hears about it, with the log that explains it.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/diagnostics/startup-report');
        if (!res.ok) return;
        const data = await res.json();
        const failures = data.previous_run_failures || [];
        if (cancelled || failures.length === 0) return;
        for (const failure of failures) {
          const when = failure.started ? new Date(failure.started).toLocaleString() : '';
          addLog('error', failure.native_fault
            ? t('app.previousRunCrashed', { when, path: failure.log_path })
            : t('app.previousRunEndedAbruptly', { when, path: failure.log_path }));
        }
        // Only offered while the GPU is actually in use: suggesting a switch
        // that is already off would send the user after the wrong cause.
        if (failures.some(f => f.native_fault) && data.webengine_gpu !== 'off') {
          addLog('warning', t('app.previousRunCrashedGpuHint'));
        }
      } catch { /* ignore */ }
    })();
    return () => { cancelled = true; };
  }, []);

  const compactForLatexFixPrompt = (value, limit) => {
    const text = String(value || '');
    if (text.length <= limit) return text;
    return `${text.slice(0, limit)}\n[truncated ${text.length - limit} characters]`;
  };

  const summarizeCompileTarget = (compileDebug = {}) => {
    const target = compileDebug.selected_main_rel || compileDebug.selected_main_file || '';
    const input = compileDebug.compiler_input_tex || '';
    const cwd = compileDebug.compiler_cwd || '';
    const returncode = compileDebug.compiler_returncode;
    return [
      target ? `Selected main file: ${target}` : '',
      input ? `Compiler input: ${input}` : '',
      cwd ? `Compiler working directory: ${cwd}` : '',
      returncode !== undefined && returncode !== '' ? `Compiler return code: ${returncode}` : '',
    ].filter(Boolean).join('\n');
  };

  const buildLatexFixPrompt = (problem) => {
    const filePath = problem.filePath || selectedFile || '';
    const compiledMainFile = problem.compiledMainFile || activeProject?.main_file || '';
    const compileDebug = problem.compileDebug || {};
    const compileTarget = summarizeCompileTarget(compileDebug);
    const articleContent = problem.fileContent ?? fileContent ?? '';
    const log = problem.log || problem.message || '';
    const compileMode = problem.partial ? 'partial compilation' : 'full compilation';
    const draftMode = problem.draft ? 'draft mode enabled' : 'draft mode disabled';

    return [
      'Task: Fix the LaTeX compilation error in this GilzaBots project.',
      '',
      'Use the chat_orchestrator workflow: diagnose the compiler error, inspect project files when needed, edit the relevant file(s) with the available tools, and finish with a concise summary of what changed.',
      'Do not guess silently. If the log points to a different file than the selected file, prioritize the compiler location and read that file before editing.',
      '',
      'Project context:',
      `- Project: ${activeProject?.project_name || activeProject?.name || problem.projectName || '(unknown)'}`,
      `- Project path: ${activeProject?.project_path || problem.projectPath || ''}`,
      `- Selected article/file: ${filePath || '(none)'}`,
      `- Configured main file: ${activeProject?.main_file || problem.mainFile || '(none)'}`,
      `- Compiled main file: ${compiledMainFile || '(unresolved)'}`,
      `- Mode: ${compileMode}; ${draftMode}`,
      compileTarget ? `\nCompiler target details:\n${compileTarget}` : '',
      '',
      'Compiler error log:',
      '````text',
      compactForLatexFixPrompt(log, 20000),
      '````',
      '',
      'Selected article/file content at the time of compilation:',
      '````latex',
      compactForLatexFixPrompt(articleContent, 60000),
      '````',
      '',
      'Please correct the project files based on the error above. After editing, explain which file(s) changed and whether the user should compile again.',
    ].filter(part => part !== '').join('\n');
  };

  const buildLatexFixDisplayText = (problem) => {
    const log = problem?.log || problem?.message || t('app.latexCompileFailed', 'LaTeX compilation failed.');
    const filePath = problem?.filePath || selectedFile || '';
    const compiledMainFile = problem?.compiledMainFile || activeProject?.main_file || problem?.mainFile || '';
    return [
      t('app.fixLatexDisplayPrompt', 'Fix LaTeX compilation error'),
      '',
      filePath ? `${t('app.selectedFileLabel', 'Selected file')}: ${filePath}` : '',
      compiledMainFile ? `${t('app.compiledMainFileLabel', 'Compiled main file')}: ${compiledMainFile}` : '',
      '',
      `${t('app.compilerErrorLogLabel', 'Compiler error log')}:`,
      '```text',
      compactForLatexFixPrompt(log, 4000),
      '```',
    ].filter(part => part !== '').join('\n');
  };

  const handleLatexCompileSuccess = () => {
    setProblems(prev => prev.filter(problem => problem.source !== 'latex_compile'));
  };

  const handleLatexCompileError = (details) => {
    setProblems(prev => prev.filter(problem => problem.source !== 'latex_compile'));
    addProblem({
      tool: t('app.latexCompilerTool', 'LaTeX compiler'),
      message: details.log || t('app.latexCompileFailed', 'LaTeX compilation failed.'),
      severity: 'error',
      source: 'latex_compile',
      filePath: details.filePath || '',
      fileContent: details.fileContent || '',
      projectPath: details.projectPath || '',
      projectName: details.projectName || '',
      mainFile: details.mainFile || '',
      compiledMainFile: details.compiledMainFile || '',
      log: details.log || '',
      compileDebug: details.compileDebug || {},
      partial: Boolean(details.partial),
      draft: Boolean(details.draft),
    });
  };

  // ── API calls ─────────────────────────────────────────────────────────────
  const fetchProjects = async (forceSelectName) => {
    try {
      const res = await fetch('/api/gilzabots/list-projects');
      if (res.ok) {
        const data = await res.json();
        setProjects(data.projects || []);
        if (data.projects?.length > 0) {
          if (forceSelectName) {
            const forced = data.projects.find(p => p.name === forceSelectName && p.exists);
            if (forced) { handleSelectProject(forced); return; }
          }
          if (!activeProject) {
            // Owned by the backend (ui_settings.json): localStorage is per
            // origin, and the origin changes with the server port.
            const lastActiveProjectName = data.last_active_project || '';
            let projToSelect = null;
            if (lastActiveProjectName) {
              projToSelect = data.projects.find(p => p.name === lastActiveProjectName && p.exists);
            }
            if (!projToSelect) {
              projToSelect = data.projects.find(p => p.exists);
            }
            if (projToSelect) handleSelectProject(projToSelect);
          }
        }
      }
    } catch (err) { addLog('error', t('app.failedToLoadProjects', { error: err.message })); }
  };

  const fetchFiles = async (showHiddenOverride = showHiddenWorkspaceFiles) => {
    if (!activeProject) return;
    try {
      const res = await fetch(`/api/files?projectPath=${encodeURIComponent(activeProject.project_path)}&showHiddenFiles=${showHiddenOverride ? 'true' : 'false'}`);
      if (res.ok) { const data = await res.json(); setFiles(data.files || []); }
      else { const e = await res.json(); addLog('error', t('app.failedToListFiles', { error: e.error })); }
    } catch (err) { addLog('error', t('app.fileCallError', { error: err.message })); }
  };

  const handleShowHiddenWorkspaceFilesChange = (checked) => {
    setShowHiddenWorkspaceFiles(checked);
    fetch('/api/settings/workspace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ show_hidden_workspace_files: checked }),
    }).catch(() => { });
    fetchFiles(checked);
  };

  useEffect(() => {
    if (activeProject) fetchFiles(showHiddenWorkspaceFiles);
  }, [showHiddenWorkspaceFiles]);

  const fetchProblems = async () => {
    if (!activeProject) return;
    try {
      const res = await fetch(`/api/gilzabots/problems?projectPath=${encodeURIComponent(activeProject.project_path)}`);
      if (res.ok) {
        const data = await res.json();
        setProblems(prev => {
          const nonLinter = prev.filter(p => p.tool !== 'python-linter');
          return trimToLimit([...nonLinter, ...(data.problems || [])], panelMaxLines);
        });
      }
    } catch (err) { console.error('Failed to fetch problems', err); }
  };

  const fetchGitStatus = async () => {
    if (!activeProject) return;
    if (gitStatusRequestRef.current) return gitStatusRequestRef.current;

    const request = (async () => {
      try {
        const res = await fetch(`/api/git/status?${gitQuerySuffix()}&t=${Date.now()}`);
        if (res.ok) {
          const data = await res.json();
          setGitChanges(data.files || []);
        }
      } catch (err) {
        console.error('Failed to fetch git status', err);
      } finally {
        gitStatusRequestRef.current = null;
      }
    })();
    gitStatusRequestRef.current = request;
    return request;
  };

  // Anything that writes into the project from outside the editor (an Asset
  // Store install, for one) has to re-read the workspace right away: otherwise
  // the new files only show up on the next polling tick, seconds later.
  const refreshWorkspaceFiles = async () => {
    await Promise.all([fetchFiles(), fetchGitStatus()]);
  };

  // Cloud sync status for the active project. Fetching it is also what
  // registers the project with the backend's sync scheduler, so this runs
  // whenever a project becomes active — not only when the panel is open.
  const refreshCloudStatus = useCallback(async () => {
    if (!activeProject?.project_path) { setCloudStatus(null); return; }
    try {
      const params = new URLSearchParams({
        projectPath: activeProject.project_path,
        project: activeProject.name || '',
      });
      const res = await fetch(`/api/cloud/status?${params}`);
      const payload = await res.json();
      setCloudStatus(payload.error ? null : payload);
    } catch (_) {
      setCloudStatus(null);
    }
  }, [activeProject?.project_path, activeProject?.name]);

  // Which files are synced, waiting, moving or conflicted — the explorer marks
  // each one, the way a desktop Drive client does.
  const refreshCloudFileStates = useCallback(async () => {
    if (!activeProject?.project_path) { setCloudFileStates(null); return; }
    try {
      const params = new URLSearchParams({ projectPath: activeProject.project_path });
      const res = await fetch(`/api/cloud/file-states?${params}`);
      const payload = await res.json();
      setCloudFileStates(payload?.enabled ? payload.states : null);
    } catch (_) {
      setCloudFileStates(null);
    }
  }, [activeProject?.project_path]);

  useEffect(() => { refreshCloudStatus(); }, [refreshCloudStatus]);

  const cloudPassRunning = !!cloudStatus?.syncing || !!cloudStatus?.progress?.active;

  useEffect(() => {
    // A background pass finishes without telling the front-end, so the status
    // line is refreshed on a tick. Polling only while sync is on keeps a
    // project that never uses the feature from paying for it. While a pass is
    // actually running the tick speeds up: that is when the footer is showing
    // which file is moving, and a 20 s refresh would make it a slideshow.
    if (!cloudStatus?.settings?.enabled) { setCloudFileStates(null); return undefined; }
    const tick = () => { refreshCloudStatus(); refreshCloudFileStates(); };
    tick();
    const timer = setInterval(tick, cloudPassRunning ? 1000 : 20000);
    return () => clearInterval(timer);
  }, [cloudStatus?.settings?.enabled, cloudPassRunning, refreshCloudStatus, refreshCloudFileStates]);

  const rememberLastActiveProject = (projectName) => {
    fetch('/api/settings/last-project', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name: projectName }),
    }).then(res => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    }).catch(err => addLog('error', t('app.failedToRememberProject', { error: err.message })));
  };

  const handleSelectProject = (proj) => {
    if (proj.exists === false) {
      addLog('error', t('app.projectDirMissing', { path: proj.project_path }));
      return;
    }

    let currentContents = { ...fileContents };
    if (selectedFile) {
      currentContents[selectedFile] = fileContent;
    }

    const dirtyFiles = unsavedFilePaths({
      openFiles,
      fileContents: currentContents,
      diskContents: diskFileContentsRef.current,
      isBinary: isBinaryEditorFile,
    });

    if (dirtyFiles.length > 0) {
      setConfirmRequest({
        prompt: t('app.unsavedProjectSwitchPrompt', { count: dirtyFiles.length }),
        options: ['yes', 'no', 'cancel'],
        callback: async (val) => {
          if (val === 'cancel') return;
          if (val === 'yes') {
            for (const filePath of dirtyFiles) {
              try {
                await fetch('/api/file/write', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ projectPath: activeProject.project_path, filePath, content: currentContents[filePath] })
                });
              } catch (e) { }
            }
          }
          setActiveProject(proj);
          rememberLastActiveProject(proj.name);
          addLog('info', t('app.projectSelected', { name: proj.project_name || proj.name }));
        }
      });
      return;
    }

    setActiveProject(proj);
    rememberLastActiveProject(proj.name);
    addLog('info', t('app.projectSelected', { name: proj.project_name || proj.name }));
  };

  const handleGitCommit = async (e) => {
    if (e) e.preventDefault();
    if (!activeProject || !commitMessage.trim() || isCommitting) return;
    setIsCommitting(true);
    addLog('info', t('app.commitCreating', { message: commitMessage }));
    try {
      const res = await fetch('/api/git/commit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(gitRequestPayload({ message: commitMessage })),
      });
      const data = await res.json();
      if (res.ok) { addLog('info', t('app.commitSuccess')); setCommitMessage(''); fetchGitStatus(); }
      else { addLog('error', t('app.commitFailed', { error: data.error || t('app.unknownError') })); }
    } catch (err) { addLog('error', t('app.commitError', { error: err.message })); }
    finally { setIsCommitting(false); }
  };

  const handleStageFile = async (filePath) => {
    if (!activeProject) return false;
    try {
      const res = await fetch('/api/git/stage', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(gitRequestPayload({ filePath, action: 'stage' })),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || t('app.unknownError'));
      }
      fetchGitStatus();
      return true;
    } catch (err) {
      addLog('error', t('app.stageError', { error: err.message }));
      return false;
    }
  };

  const handleStageAllFiles = async () => {
    if (!activeProject) return false;
    try {
      const res = await fetch('/api/git/stage', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(gitRequestPayload({ filePath: '__all__', action: 'stage' })),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || t('app.unknownError'));
      }
      fetchGitStatus();
      return true;
    } catch (err) {
      addLog('error', t('app.stageError', { error: err.message }));
      return false;
    }
  };

  const handleUnstageFile = async (filePath) => {
    if (!activeProject) return false;
    try {
      const res = await fetch('/api/git/stage', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(gitRequestPayload({ filePath, action: 'unstage' })),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || t('app.unknownError'));
      }
      fetchGitStatus();
      return true;
    } catch (err) {
      addLog('error', t('app.unstageError', { error: err.message }));
      return false;
    }
  };

  const handleDiscardFile = async (filePath) => {
    if (!activeProject) return;
    try {
      const res = await fetch('/api/git/discard', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(gitRequestPayload({ filePath })),
      });
      if (res.ok) {
        addLog('info', t('app.changesDiscarded', { path: filePath }));
        setFileContents(prev => {
          const next = { ...prev };
          delete next[filePath];
          return next;
        });
        if (sameFilePath(selectedFile, filePath)) {
          const readRes = await fetch(`/api/file/read?projectPath=${encodeURIComponent(activeProject.project_path)}&filePath=${encodeURIComponent(filePath)}&t=${Date.now()}`);
          if (readRes.ok) {
            const data = await readRes.json();
            diskFileContentsRef.current[filePath] = data.content;
            setFileContent(data.content);
            setFileContents(prev => ({ ...prev, [filePath]: data.content }));
            setOriginalFileContents(prev => ({ ...prev, [filePath]: data.content }));
          } else {
            delete diskFileContentsRef.current[filePath];
            setOpenFiles(prev => prev.filter(f => !sameFilePath(f, filePath)));
            setSelectedFile(null);
            setFileContent('');
            setOriginalFileContents(prev => {
              const next = { ...prev };
              delete next[filePath];
              return next;
            });
          }
        }
        fetchGitStatus();
        fetchFiles();
        return;
      }
      if (res.ok) { addLog('info', t('app.changesDiscarded', { path: filePath })); fetchGitStatus(); fetchFiles(); }
      else { const d = await res.json(); addLog('error', t('app.discardFailed', { error: d.error })); }
    } catch (err) { addLog('error', t('app.discardChangesError', { error: err.message })); }
  };

  const handleCheckpointRestored = async (commit) => {
    diskFileContentsRef.current = {};
    setOpenFiles([]);
    setSelectedFile(null);
    setSelectedNodes(new Set());
    setFileContent('');
    setFileContents({});
    setOriginalFileContents({});
    setAgentChangedFiles(new Set());
    setJumpToLine(null);
    await fetchFiles();
    await fetchGitStatus();
    addLog('info', t('app.checkpointRestored', { short: commit.short }));
  };

  const handleInstallOptionalDeps = async () => {
    if (isInstallingDeps) return;
    setIsInstallingDeps(true);
    setInstallDepsStatus('Instalando...');
    setInstallDepsLog('Iniciando pip install...\n');
    try {
      const response = await fetch('/api/settings/install-dependencies', { method: 'POST' });
      if (!response.ok) throw new Error(t('app.installStartFailed'));
      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const data = JSON.parse(line.trim());
            if (data.output) setInstallDepsLog(prev => prev + data.output);
            if (data.status === 'success') setInstallDepsStatus('Instalado com Sucesso!');
            else if (data.status === 'error') setInstallDepsStatus('Erro na Instalação');
          } catch (e) { /* ignore chunk parsing errors */ }
        }
      }
    } catch (err) {
      setInstallDepsStatus('Falha ao conectar');
      setInstallDepsLog(prev => prev + `\nErro: ${err.message}\n`);
    } finally { setIsInstallingDeps(false); }
  };

  // ── File operations ────────────────────────────────────────────────────────
  const handleNodeSelect = (nodePath, isDirectory, e) => {
    if (e.ctrlKey || e.metaKey) {
      setSelectedNodes(prev => {
        const next = new Set(prev);
        if (next.has(nodePath)) next.delete(nodePath);
        else next.add(nodePath);
        return next;
      });
      return;
    }

    // Normal click clears multi-selection
    setSelectedNodes(new Set());
    if (!isDirectory) {
      handleFileSelect(nodePath);
    }
  };

  // Opening a file has to put the editor on screen, but only the layouts that
  // hide it need leaving: forcing 'ide' from the studio or the document layout
  // would drop the user out of a layout that is already showing the file they
  // just picked.
  const revealEditorLayout = useCallback(() => {
    setLayoutMode(layoutAfterOpeningFile);
  }, []);

  // The diff view's baseline for a freshly read file: its content at git HEAD,
  // or empty when it has none.
  const loadHeadBaseline = (filePath) => {
    fetch(`/api/git/file-at-head?${gitQuerySuffix()}&filePath=${encodeURIComponent(filePath)}&t=${Date.now()}`)
      .then(r => r.ok ? r.json() : null)
      .then(gitData => {
        if (gitData && gitData.content !== undefined) {
          setOriginalFileContents(prev => ({ ...prev, [filePath]: gitData.content }));
        } else {
          setOriginalFileContents(prev => ({ ...prev, [filePath]: '' }));
        }
      })
      .catch(() => {
        setOriginalFileContents(prev => ({ ...prev, [filePath]: '' }));
      });
  };

  // `focusEditor: false` scrolls to `jumpLine` without moving the keyboard
  // focus: an agent showing the user a file must not pull them out of the chat
  // composer they may be typing in.
  const handleFileSelect = async (filePath, jumpLine = null, { focusEditor = true } = {}) => {
    if (!activeProject) return;
    setIsBottomMaximized(false);
    // Opening the file is what acknowledges the agent's change to it.
    setAgentChangedFiles(prev => withoutAgentChangedFile(prev, filePath));
    if (selectedFile) {
      const currentContent = getCurrentTextFileContent();
      setFileContents(prev => ({ ...prev, [selectedFile]: currentContent }));
      fileContentRef.current = currentContent;
    }
    if (isUnsupportedSystemFile(filePath)) {
      if (await handleOpenInSystem(filePath)) {
        addLog('info', t('app.openedInSystemApp', { path: filePath }));
      }
      return;
    }
    if (isBinaryEditorFile(filePath)) {
      setOpenFiles(prev => {
        const deduped = dedupeOpenFileList(prev, filePath);
        return deduped.some(openFile => sameFilePath(openFile, filePath))
          ? deduped.map(openFile => sameFilePath(openFile, filePath) ? filePath : openFile)
          : [...deduped, filePath];
      });
      setFileContent('');
      setSelectedFile(filePath);
      revealEditorLayout();
      return;
    }
    const cachedFilePath = Object.keys(fileContents).find(path => sameFilePath(path, filePath)) || filePath;
    setOpenFiles(prev => {
      const deduped = dedupeOpenFileList(prev, filePath);
      return deduped.some(openFile => sameFilePath(openFile, filePath))
        ? deduped.map(openFile => sameFilePath(openFile, filePath) ? filePath : openFile)
        : [...deduped, filePath];
    });
    if (cachedFilePath !== filePath) {
      setFileContents(prev => ({ ...prev, [filePath]: prev[cachedFilePath] }));
      if (diskFileContentsRef.current[cachedFilePath] !== undefined) {
        diskFileContentsRef.current[filePath] = diskFileContentsRef.current[cachedFilePath];
      }
      setOriginalFileContents(prev => (
        prev[cachedFilePath] === undefined ? prev : { ...prev, [filePath]: prev[cachedFilePath] }
      ));
    }
    if (fileContents[cachedFilePath] !== undefined) {
      setFileContent(fileContents[cachedFilePath]);
      setSelectedFile(filePath);
      revealEditorLayout(); // Put the editor on screen if the layout hides it
      if (jumpLine !== null) {
        setJumpToLine({ file: filePath, line: jumpLine, focusEditor });
      }
      return;
    }
    try {
      const res = await fetch(`/api/file/read?projectPath=${encodeURIComponent(activeProject.project_path)}&filePath=${encodeURIComponent(filePath)}`);
      if (res.ok) {
        const data = await res.json();
        diskFileContentsRef.current[filePath] = data.content;
        setFileContent(data.content);
        setFileContents(prev => ({ ...prev, [filePath]: data.content }));
        setOriginalFileContents(prev => ({ ...prev, [filePath]: data.content }));
        setSelectedFile(filePath);
        revealEditorLayout(); // Put the editor on screen if the layout hides it
        if (jumpLine !== null) {
          setJumpToLine({ file: filePath, line: jumpLine, focusEditor });
        }

        loadHeadBaseline(filePath);
      }
      else {
        addLog('error', t('app.fileReadFailed', { path: filePath }));
        setSelectedFile(filePath);
        setFileContent('');
        revealEditorLayout();
      }
    } catch (err) {
      addLog('error', t('app.readError', { error: err.message }));
      setSelectedFile(filePath);
      setFileContent('');
      revealEditorLayout();
    }
  };

  const saveFile = async ({ suppressCompile = false } = {}) => {
    if (!activeProject || !selectedFile) return false;
    // The PDF panel is a read-only viewer: there is nothing to write back.
    if (isBinaryEditorFile(selectedFile)) return false;
    const currentContent = getCurrentTextFileContent();
    fileContentRef.current = currentContent;
    if (currentContent !== fileContent) {
      setFileContent(currentContent);
    }
    const projectPath = activeProject.project_path;
    const filePath = selectedFile;
    const diskCache = diskFileContentsRef.current;
    const sameProject = () => documentContextRef.current?.projectPath === projectPath
      && diskFileContentsRef.current === diskCache;
    setFileContents(prev => ({ ...prev, [filePath]: currentContent }));
    pendingSaveCountRef.current += 1;
    setIsSaving(true);
    return fileSaveQueueRef.current.run(projectPath, filePathKey(filePath), async () => {
      try {
        // Check only once preceding saves have completed. A queued older write
        // may otherwise overwrite the version this save thought was on disk.
        if (diskCache[filePath] === currentContent) return true;
        const res = await fetch('/api/file/write', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ projectPath: activeProject.project_path, filePath: selectedFile, content: currentContent }),
        });
        if (res.ok) {
          if (!sameProject()) return true;
          addLog('info', t('app.fileSaved', { path: filePath }));
          diskCache[filePath] = currentContent;
          // A save acknowledges a snapshot: typing that arrived while it was in
          // flight stays the live buffer.
          const live = documentContextRef.current;
          const activeDocument = sameFilePath(live.selectedFile, filePath);
          const liveContent = activeDocument ? live.getContent() : undefined;
          setFileContents(prev => {
            // Closing/renaming a tab must not resurrect it when a save returns.
            if (!Object.hasOwn(prev, filePath)) return prev;
            return { ...prev, [filePath]: activeDocument ? liveContent : prev[filePath] };
          });

          fetch(`/api/git/file-at-head?${gitQuerySuffix()}&filePath=${encodeURIComponent(selectedFile)}&t=${Date.now()}`)
            .then(r => r.ok ? r.json() : null)
            .then(gitData => {
              if (!sameProject()) return;
              if (gitData && gitData.content !== undefined) {
                setOriginalFileContents(prev => ({ ...prev, [selectedFile]: gitData.content }));
              } else {
                setOriginalFileContents(prev => ({ ...prev, [selectedFile]: '' }));
              }
            })
            .catch(() => {
              if (!sameProject()) return;
              // Do not overwrite originalFileContents on error or 404 if it's unwanted, but actually we should set to empty if untracked.
              setOriginalFileContents(prev => ({ ...prev, [selectedFile]: '' }));
            });
          fetchGitStatus();
          fetchProblems();
          if (activeDocument && !suppressCompile && selectedFile && selectedFile.toLowerCase().match(/\.(tex|cls|sty|bib)$/)) {
            const compileFull = activeProject.compile_on_save_full === true;
            const compilePartial = activeProject.compile_on_save_partial !== false;
            if (compileFull || compilePartial) {
              setTriggerCompileRequest({
                id: Date.now(),
                partial: !compileFull && compilePartial,
              });
            }
          }
          return true;
        }
        else {
          addLog('error', t('app.fileSaveFailedPath', { path: selectedFile }));
          return false;
        }
      } catch (err) {
        addLog('error', t('app.writeError', { error: err.message }));
        return false;
      }
      finally {
        pendingSaveCountRef.current -= 1;
        setIsSaving(pendingSaveCountRef.current > 0);
      }
    });
  };

  useEffect(() => { saveFileRef.current = saveFile; }, [saveFile]);

  // ── Buffers vs. files the agent wrote ─────────────────────────────────────
  // The rule and its orchestration live in utils/agentFileSync.js. The agent
  // event handler may be a closure from when the turn's stream started, so the
  // reconciliation is created once and reads every value through refs; the
  // deps object is refreshed each render for the helpers that close over state.
  const applyAgentDiskContent = (filePath, content) => {
    const live = documentContextRef.current;
    if (live?.selectedFile && sameFilePath(live.selectedFile, filePath)) {
      setFileContent(content);
      setFileContents(prev => ({ ...prev, [filePath]: content }));
      fetch(`/api/git/file-at-head?${gitQuerySuffix()}&filePath=${encodeURIComponent(filePath)}&t=${Date.now()}`)
        .then(r => r.ok ? r.json() : null)
        .then(gitData => {
          setOriginalFileContents(prev => ({ ...prev, [filePath]: gitData?.content ?? '' }));
        })
        .catch(() => {
          setOriginalFileContents(prev => ({ ...prev, [filePath]: '' }));
        });
    } else {
      setFileContents(prev => (Object.hasOwn(prev, filePath) ? { ...prev, [filePath]: content } : prev));
    }
  };
  const agentFileSyncDepsRef = useRef(null);
  agentFileSyncDepsRef.current = {
    getProjectPath: () => documentContextRef.current?.projectPath || '',
    caseInsensitive: () => isCaseInsensitiveProjectPath,
    bufferedPaths: () => {
      const live = documentContextRef.current?.selectedFile;
      return [...Object.keys(fileContentsRef.current), ...(live ? [live] : [])];
    },
    bufferOf: (filePath) => {
      const live = documentContextRef.current;
      if (live?.selectedFile && sameFilePath(live.selectedFile, filePath)) return live.getContent();
      return fileContentsRef.current[filePath];
    },
    baselineOf: (filePath) => diskFileContentsRef.current[filePath],
    setBaseline: (filePath, content) => { diskFileContentsRef.current[filePath] = content; },
    samePath: sameFilePath,
    pathKey: filePathKey,
    isBinary: isBinaryEditorFile,
    readDisk: async (projectPath, filePath) => {
      try {
        const res = await fetch(`/api/file/read?projectPath=${encodeURIComponent(projectPath)}&filePath=${encodeURIComponent(filePath)}&t=${Date.now()}`);
        if (!res.ok) return undefined;
        const data = await res.json();
        return typeof data.content === 'string' ? data.content : undefined;
      } catch {
        return undefined;
      }
    },
    apply: applyAgentDiskContent,
    confirmLoad: (filePath) => showConfirm(
      t('app.agentChangedUnsavedPrompt', { path: filePath }),
      t('app.agentChangedUnsavedTitle', 'File changed by the agent'),
      {
        okLabel: t('app.loadAgentVersion', "Load the agent's version"),
        cancelLabel: t('app.keepMyEdits', 'Keep my edits'),
        destructive: true,
      },
    ),
    log: (kind, filePath) => addLog('info', kind === 'kept'
      ? t('app.keptUnsavedEdits', { path: filePath })
      : t('app.reloadedFromDisk', { path: filePath })),
  };
  const agentFileSyncRef = useRef(null);
  if (!agentFileSyncRef.current) {
    const deps = new Proxy({}, { get: (_, name) => agentFileSyncDepsRef.current[name] });
    agentFileSyncRef.current = createAgentFileSync(deps).reconcile;
  }

  const handleCloseTab = (filePath, e) => {
    if (e) { e.stopPropagation(); e.preventDefault(); }
    if (sameFilePath(selectedFile, filePath)) {
      const currentContent = getCurrentTextFileContent();
      fileContentRef.current = currentContent;
      setFileContents(prev => ({ ...prev, [filePath]: currentContent }));
    }
    setOpenFiles(prev => {
      const remaining = prev.filter(f => !sameFilePath(f, filePath));
      if (sameFilePath(selectedFile, filePath)) {
        if (remaining.length > 0) {
          const next = remaining[remaining.length - 1];
          setSelectedFile(next);
          setFileContent(isBinaryEditorFile(next) ? '' : (fileContents[next] || ''));
        }
        else { setSelectedFile(null); setFileContent(''); }
      }
      return remaining;
    });
  };

  // A tab an agent opens without focusing it. Its content is read now, as
  // handleFileSelect reads a focused one: closing the focused tab shows its
  // neighbour from the buffer cache, and a tab with nothing cached there would
  // show an empty editor whose save would empty the file.
  const openBackgroundTab = async (filePath) => {
    if (!activeProject) return;
    if (!isBinaryEditorFile(filePath) && fileContentsRef.current[filePath] === undefined) {
      const res = await fetch(`/api/file/read?projectPath=${encodeURIComponent(activeProject.project_path)}&filePath=${encodeURIComponent(filePath)}`);
      if (!res.ok) throw new Error(`could not read ${filePath} (HTTP ${res.status})`);
      const data = await res.json();
      diskFileContentsRef.current[filePath] = data.content;
      setFileContents(prev => (prev[filePath] === undefined ? { ...prev, [filePath]: data.content } : prev));
      setOriginalFileContents(prev => ({ ...prev, [filePath]: data.content }));
      loadHeadBaseline(filePath);
    }
    setOpenFiles(prev => (prev.some(f => sameFilePath(f, filePath)) ? prev : [...prev, filePath]));
  };

  // Hands reveal_in_pdf to the editor panel, which answers through the request's
  // resolver. An answered request is cleared, so a panel that remounts later
  // never replays it, and one no panel takes up (the editor is not on screen)
  // is answered with that rather than left to the backend's timeout.
  const requestPdfReveal = (filePath, line) => new Promise((resolve) => {
    let timer = null;
    const finish = (answer) => {
      if (timer === null) return;
      clearTimeout(timer);
      timer = null;
      setPdfRevealRequest(prev => (prev?.resolve === finish ? null : prev));
      resolve(answer);
    };
    timer = setTimeout(() => finish({
      ok: false,
      error: `The editor did not show ${filePath} in the PDF pane within ${PDF_REVEAL_TIMEOUT_MS / 1000} s.`,
    }), PDF_REVEAL_TIMEOUT_MS);
    pdfRevealSeqRef.current += 1;
    setPdfRevealRequest({ seq: pdfRevealSeqRef.current, file: filePath, line, resolve: finish });
  });

  // Commands an agent sends to this window (open_file, close_file,
  // reveal_in_pdf, set_layout, get_problems; gilzabots/ide_control.py). As with the file sync above, every piece of
  // state is read through a ref: the stream handler that receives a command
  // may be a closure from when the turn started.
  const ideCommandDepsRef = useRef(null);
  ideCommandDepsRef.current = {
    getProjectPath: () => documentContextRef.current?.projectPath || '',
    caseInsensitive: () => isCaseInsensitiveProjectPath,
    samePath: sameFilePath,
    openFiles: () => openFiles,
    selectedFile: () => selectedFile,
    opensOutside: isUnsupportedSystemFile,
    hasNoLines: isBinaryEditorFile,
    isUnsaved: (filePath) => {
      const live = documentContextRef.current;
      return unsavedFilePaths({
        openFiles: [filePath],
        fileContents: fileContentsRef.current,
        activeFile: live?.selectedFile,
        activeContent: live?.getContent(),
        diskContents: diskFileContentsRef.current,
        isBinary: isBinaryEditorFile,
      }).length > 0;
    },
    focusTab: (filePath, line) => handleFileSelect(filePath, line, { focusEditor: false }),
    addBackgroundTab: openBackgroundTab,
    closeTab: (filePath) => handleCloseTab(filePath),
    revealInPdf: requestPdfReveal,
    setLayout: (mode) => enterLayout(mode, {
      hasOpenDocument: !!selectedFile,
      setLayoutMode,
      setActiveSidebarTab,
      setIsChatVisible,
      setIsTerminalCollapsed,
      setActiveBottomTab,
    }),
    problems: () => problems,
  };
  const ideCommandRunnerRef = useRef(null);
  if (!ideCommandRunnerRef.current) {
    const deps = new Proxy({}, { get: (_, name) => ideCommandDepsRef.current[name] });
    ideCommandRunnerRef.current = createIdeCommandRunner(deps);
  }

  // Keeps only `filePath` open. When it is not the active tab we go through
  // handleFileSelect first, so its content is loaded/persisted exactly like a
  // regular tab switch before the other tabs disappear.
  const handleCloseOtherTabs = async (filePath) => {
    if (!sameFilePath(selectedFile, filePath)) {
      await handleFileSelect(filePath);
    } else {
      const currentContent = getCurrentTextFileContent();
      fileContentRef.current = currentContent;
      setFileContents(prev => ({ ...prev, [filePath]: currentContent }));
    }
    setOpenFiles(prev => prev.filter(f => sameFilePath(f, filePath)));
  };

  const handleCloseAllTabs = () => {
    if (selectedFile) {
      const currentContent = getCurrentTextFileContent();
      fileContentRef.current = currentContent;
      setFileContents(prev => ({ ...prev, [selectedFile]: currentContent }));
    }
    setOpenFiles([]);
    setSelectedFile(null);
    setFileContent('');
  };

  // New files and new directories are created in place: a provisional row
  // appears in the target directory of the workspace tree and the user types
  // the name there (`InlineCreateNode`).
  const beginInlineCreate = (kind, parentPath) => {
    if (!activeProject) return;
    setContextMenu(null);
    setRenamingNodePath(null);
    inlineCreateIdRef.current += 1;
    setPendingCreate({ id: inlineCreateIdRef.current, kind, parentPath: parentPath || '' });
  };

  const handleCreateNewFile = (parentPath) => beginInlineCreate('file', parentPath);
  const handleCreateNewDir = (parentPath) => beginInlineCreate('dir', parentPath);

  const cancelInlineCreate = () => setPendingCreate(null);

  // Resolves to a message shown under the provisional row (which stays open for
  // correction), or to null once the entry exists. Creation is exclusive: the
  // server refuses an existing path instead of overwriting it.
  const submitInlineCreate = async (name) => {
    const request = pendingCreate;
    if (!request || !activeProject) return null;
    const resolved = resolveInlineCreatePath({ parentPath: request.parentPath, name });
    if (resolved.cancelled) {
      setPendingCreate(null);
      return null;
    }
    if (resolved.error) {
      return t('explorerSidebar.inlineCreateInvalidName', 'Use a relative name without empty, "." or ".." segments.');
    }

    const targetPath = resolved.path;
    const isDir = request.kind === 'dir';
    try {
      let res;
      if (isDir) {
        res = await fetch('/api/file/mkdir', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ projectPath: activeProject.project_path, dirPath: targetPath, exclusive: true }),
        });
      } else {
        res = await fetch('/api/file/write', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ projectPath: activeProject.project_path, filePath: targetPath, content: '', exclusive: true }),
        });
      }

      if (!res.ok) {
        if (res.status === 409) {
          return t('explorerSidebar.inlineCreateExists', '"{{name}}" already exists at this location. Choose a different name.', { name: targetPath.split('/').pop() });
        }
        const body = await res.json().catch(() => ({}));
        const message = body.error || `HTTP ${res.status}`;
        addLog('error', isDir ? t('app.dirCreateError', { error: message }) : t('app.fileCreateError', { error: message }));
        return message;
      }

      setPendingCreate(null);
      addLog('info', isDir ? t('app.dirCreated', { path: targetPath }) : t('app.fileCreated', { path: targetPath }));
      await fetchFiles();
      if (!isDir) await handleFileSelect(targetPath);
      return null;
    } catch (err) {
      addLog('error', isDir ? t('app.dirCreateCallError', { error: err.message }) : t('app.fileCreateCallError', { error: err.message }));
      return err.message;
    }
  };

  const handleImportFile = (parentPath = '') => {
    if (!activeProject) return;
    importTargetPathRef.current = parentPath || '';
    setContextMenu(null);
    importFileInputRef.current?.click();
  };

  const handleImportFileSelected = async (event) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (!files.length || !activeProject) return;

    const targetDir = importTargetPathRef.current || '';
    let lastImportedPath = null;

    for (const file of files) {
      const formData = new FormData();
      formData.append('projectPath', activeProject.project_path);
      formData.append('targetDir', targetDir);
      formData.append('file', file, file.name);

      try {
        const res = await fetch('/api/file/import', {
          method: 'POST',
          body: formData,
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || t('app.importFailed'));
        addLog('info', t('app.fileImported', { path: data.filePath || file.name }));
        if (data.filePath) lastImportedPath = data.filePath;
      } catch (err) {
        addLog('error', t('app.importFileError', { error: err.message }));
      }
    }

    await fetchFiles();
    if (lastImportedPath) await handleFileSelect(lastImportedPath);
  };

  const handleRenameNode = (node) => {
    if (!activeProject || !node) return;
    setPendingCreate(null);
    setRenamingNodePath(node.path);
  };

  const executeRenameNode = async (node, newPath) => {
    setRenamingNodePath(null);
    if (!activeProject || !node || !newPath || newPath === node.path) return;
    try {
      const res = await fetch('/api/file/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectPath: activeProject.project_path, oldPath: node.path, newPath })
      });
      if (res.ok) {
        addLog('info', t('app.itemRenamed', { itemType: t(node.isDirectory ? 'app.itemTypeDirectory' : 'app.itemTypeFile'), oldPath: node.path, newPath }));
        if (!node.isDirectory) {
          setOpenFiles(prev => dedupeOpenFileList(prev.map(f => sameFilePath(f, node.path) ? newPath : f), newPath));
          setFileContents(prev => {
            const n = { ...prev };
            const oldKey = Object.keys(n).find(k => sameFilePath(k, node.path));
            if (oldKey !== undefined) { n[newPath] = n[oldKey]; delete n[oldKey]; }
            return n;
          });
          setOriginalFileContents(prev => {
            const n = { ...prev };
            const oldKey = Object.keys(n).find(k => sameFilePath(k, node.path));
            if (oldKey !== undefined) { n[newPath] = n[oldKey]; delete n[oldKey]; }
            return n;
          });
          if (sameFilePath(selectedFile, node.path)) setSelectedFile(newPath);
        } else {
          setOpenFiles(prev => dedupeOpenFileList(prev.map(f => replaceFilePathPrefix(f, node.path, newPath)), newPath));
          setFileContents(prev => { const n = {}; for (const [k, v] of Object.entries(prev)) n[replaceFilePathPrefix(k, node.path, newPath)] = v; return n; });
          setOriginalFileContents(prev => { const n = {}; for (const [k, v] of Object.entries(prev)) n[replaceFilePathPrefix(k, node.path, newPath)] = v; return n; });
          if (isFileInsidePath(selectedFile, node.path)) setSelectedFile(prev => replaceFilePathPrefix(prev, node.path, newPath));
        }
        moveDiskContents(node.path, newPath);
        setAgentChangedFiles(prev => renameAgentChangedFiles(prev, node.path, newPath));
        await fetchFiles();
      } else {
        const e = await res.json();
        addLog('error', t('app.fileRenameError', { error: e.error }));
      }
    } catch (err) {
      addLog('error', t('app.renameError', { error: err.message }));
    }
  };

  const handleSetMainFile = async (node) => {
    if (!activeProject || !node || node.isDirectory) return;
    try {
      // Calculate relative path from project root
      let relPath = node.path;
      if (activeProject.project_path && node.path.startsWith(activeProject.project_path)) {
        relPath = node.path.substring(activeProject.project_path.length);
        if (relPath.startsWith('/') || relPath.startsWith('\\')) {
          relPath = relPath.substring(1);
        }
      }

      const payload = {
        project_name: activeProject.name,
        main_file: relPath,
        chat_id: activeChatId
      };
      const res = await fetch('/api/gilzabots/update-project', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        const updated = await res.json();
        setActiveProject(prev => ({ ...prev, ...updated }));
        setProjects(prev => prev.map(p => (p.name === updated.name) ? { ...p, ...updated } : p));
        addLog('info', t('app.mainFileSet', { path: relPath }));
      } else {
        const err = await res.json();
        addLog('error', t('app.mainFileSetFailed', { error: err.error }));
      }
    } catch (err) {
      addLog('error', t('app.mainFileSetError', { error: err.message }));
    }
  };

  const handleDeleteNode = async (node) => {
    if (!activeProject || !node) return;

    // Se o nó clicado faz parte da seleção múltipla, apagamos todos da seleção
    const nodesToDelete = (selectedNodes && selectedNodes.has(node.path))
      ? Array.from(selectedNodes)
      : [node.path];

    const isMulti = nodesToDelete.length > 1;
    let msg = '';
    if (isMulti) {
      msg = t('app.deleteMultiPrompt', 'Are you sure you want to delete {{count}} selected items? All internal files in directories will also be removed!', { count: nodesToDelete.length });
    } else {
      if (node.isDirectory) {
        msg = t('app.deleteSingleDir', 'Are you sure you want to delete the directory "{{name}}"? All internal files will also be removed!', { name: node.path });
      } else {
        msg = t('app.deleteSingleFile', 'Are you sure you want to delete the file "{{name}}"?', { name: node.path });
      }
    }

    setConfirmRequest({
      prompt: msg,
      options: ['yes', 'no'],
      default: 'no',
      callback: async (value) => {
        if (value !== 'yes') return;
        try {
          let successCount = 0;
          for (const pathToDelete of nodesToDelete) {
            const res = await fetch('/api/file/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ projectPath: activeProject.project_path, filePath: pathToDelete }) });
            if (res.ok) {
              successCount++;
              setOpenFiles(prev => prev.filter(f => !isFileInsidePath(f, pathToDelete)));
              setFileContents(prev => { const n = {}; for (const [k, v] of Object.entries(prev)) if (!isFileInsidePath(k, pathToDelete)) n[k] = v; return n; });
              setOriginalFileContents(prev => { const n = {}; for (const [k, v] of Object.entries(prev)) if (!isFileInsidePath(k, pathToDelete)) n[k] = v; return n; });
              dropDiskContents(pathToDelete);
              setAgentChangedFiles(prev => removeAgentChangedFiles(prev, pathToDelete));
              if (isFileInsidePath(selectedFile, pathToDelete)) {
                // If it's the current file, we can't reliably pick the 'last' open file easily in a loop, so we just clear it.
                setSelectedFile(null); setFileContent('');
              }
            } else {
              const e = await res.json(); addLog('error', t('app.deletePathFailed', { path: pathToDelete, error: e.error }));
            }
          }
          if (successCount > 0) {
            addLog('info', t('app.itemsDeleted', { count: successCount }));
            setSelectedNodes(new Set()); // clear multi-selection
            await fetchFiles();
          }
        } catch (err) { addLog('error', t('app.deleteError', { error: err.message })); }
      }
    });
  };

  // Moves one or more nodes (files/directories) into targetDirPath ('' = project root).
  const handleMoveNode = async (paths, targetDirPath) => {
    if (!activeProject || !paths || paths.length === 0) return;
    const uniquePaths = Array.from(new Set(paths));
    let successCount = 0;
    for (const oldPath of uniquePaths) {
      const nodeName = oldPath.replace(/\\/g, '/').split('/').pop();
      const newPath = targetDirPath ? `${targetDirPath}/${nodeName}` : nodeName;
      if (sameFilePath(oldPath, newPath)) continue;
      if (isFileInsidePath(targetDirPath || '', oldPath)) { addLog('error', t('app.moveDirectoryIntoItself')); continue; }
      try {
        const res = await fetch('/api/file/rename', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ projectPath: activeProject.project_path, oldPath, newPath }) });
        if (res.ok) {
          successCount++;
          setOpenFiles(prev => dedupeOpenFileList(prev.map(f => isFileInsidePath(f, oldPath) ? replaceFilePathPrefix(f, oldPath, newPath) : f), newPath));
          setFileContents(prev => { const n = {}; for (const [k, v] of Object.entries(prev)) n[isFileInsidePath(k, oldPath) ? replaceFilePathPrefix(k, oldPath, newPath) : k] = v; return n; });
          setOriginalFileContents(prev => { const n = {}; for (const [k, v] of Object.entries(prev)) n[isFileInsidePath(k, oldPath) ? replaceFilePathPrefix(k, oldPath, newPath) : k] = v; return n; });
          moveDiskContents(oldPath, newPath);
          setAgentChangedFiles(prev => renameAgentChangedFiles(prev, oldPath, newPath));
          if (isFileInsidePath(selectedFile, oldPath)) setSelectedFile(prev => replaceFilePathPrefix(prev, oldPath, newPath));
        } else {
          const e = await res.json();
          addLog('error', t('app.movePathFailed', { path: oldPath, error: e.error }));
        }
      } catch (err) { addLog('error', t('app.moveError', { error: err.message })); }
    }
    if (successCount > 0) {
      addLog('info', t('app.itemsMoved', { count: successCount }));
      setSelectedNodes(new Set());
      await fetchFiles();
    }
  };

  const handleOpenMoveModal = (node) => {
    setContextMenu(null);
    if (!activeProject || !node) return;
    const paths = (selectedNodes && selectedNodes.has(node.path) && selectedNodes.size > 1)
      ? Array.from(selectedNodes)
      : [node.path];
    setMoveModal({ paths });
  };

  const confirmMoveModal = async (targetDirPath) => {
    if (!moveModal) return;
    const paths = moveModal.paths;
    setMoveModal(null);
    await handleMoveNode(paths, targetDirPath);
  };

  // ── Project CRUD ──────────────────────────────────────────────────────────
  const handleCreateProject = async (e) => {
    e.preventDefault();
    if (!newProjName || !newProjPath) return;
    setNewProjError('');

    const sep = navigator.userAgent.toLowerCase().includes('windows') ? '\\' : '/';
    let basePath = newProjPath;
    if (basePath.endsWith(sep)) basePath = basePath.slice(0, -1);
    const finalProjectPath = `${basePath}${sep}${newProjName}`;

    try {
      const res = await fetch('/api/gilzabots/create-project', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_name: newProjName, project_path: finalProjectPath, description: newProjDesc, model: newProjModel, worker_model: newProjWorkerModel, mode: newProjMode, model_params: Object.keys(newProjModelParams).length ? newProjModelParams : undefined, worker_model_params: Object.keys(newProjWorkerModelParams).length ? newProjWorkerModelParams : undefined }),
      });
      if (res.ok) {
        const created = await res.json();
        addLog('info', t('app.projectRegistered', { name: newProjName }));
        setShowNewProjectModal(false); setNewProjName(''); setNewProjPath(''); setNewProjDesc(''); setNewProjModel(''); setNewProjWorkerModel(''); setNewProjApiKey(''); setNewProjApiBase('http://localhost:11434/v1'); setNewProjWorkerApiKey(''); setNewProjWorkerApiBase(''); setNewProjModelParams({}); setNewProjWorkerModelParams({});
        fetchProjects(created.name);
      } else { const err = await res.json(); setNewProjError(err.error || t('app.projectCreateError')); addLog('error', t('app.projectCreateFailed', { error: err.error })); }
    } catch (err) { setNewProjError(err.message || t('app.projectCreateError')); addLog('error', t('app.projectCreateFailed', { error: err.message })); }
  };

  const handleDeleteProject = (projName) => {
    setDeleteCloudLink(null);
    setProjectToDelete(projName);
    // Whether the dialog can offer to delete the cloud copy too. Local state
    // only, so the dialog does not wait on the network.
    const projectPath = projects.find(p => p.name === projName)?.project_path
      || (activeProject?.name === projName ? activeProject.project_path : '');
    if (!projectPath) return;
    fetch(`/api/cloud/link?${new URLSearchParams({ projectPath })}`)
      .then(res => res.json())
      .then(link => setDeleteCloudLink(link && !link.error ? link : null))
      .catch(() => setDeleteCloudLink(null));
  };

  const confirmDeleteProject = async (deleteDir, deleteCloud = false) => {
    if (!projectToDelete) return;
    const projName = projectToDelete;
    setProjectToDelete(null);
    setDeleteCloudLink(null);
    try {
      const res = await fetch('/api/gilzabots/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_name: projName, delete_dir: deleteDir, delete_cloud: deleteCloud }) });
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        addLog('info', t('app.projectRemoved', { name: projName }));
        if (data?.cloud?.deleted) addLog('info', t('app.projectCloudCopyRemoved', { defaultValue: 'Cloud copy of {{name}} deleted.', name: projName }));
        if (activeProject?.name === projName) setActiveProject(null);
        fetchProjects();
      }
      else {
        const data = await res.json();
        addLog('error', t('app.projectDeleteError', { error: data.error }));
        // A failed cloud deletion leaves the project fully in place; the log
        // alone is easy to miss once the dialog has closed.
        if (deleteCloud) showAlert(t('app.projectDeleteError', { error: data.error }));
      }
    } catch (err) { addLog('error', t('app.projectDeleteError', { error: err.message })); }
  };

  const openEditModal = async (e, proj) => {
    e.stopPropagation();
    let fresh = proj;
    try {
      const res = await fetch('/api/gilzabots/list-projects');
      if (res.ok) {
        const { projects: list } = await res.json();
        const found = list.find(p => p.name === proj.name);
        if (found) fresh = found;
      }
    } catch (_) { }
    setEditProjError('');
    const compileOnSaveFull = fresh.compile_on_save_full === true;
    const compileOnSavePartial = !compileOnSaveFull && fresh.compile_on_save_partial !== false;
    const newState = { user_prompt_prefix: fresh.user_prompt_prefix || '', name: fresh.name, project_name: fresh.project_name || fresh.name, project_path: fresh.project_path || '', main_file: fresh.main_file || '', git_root_path: fresh.git_root_path || '', compile_on_save_partial: compileOnSavePartial, compile_on_save_full: compileOnSaveFull, model: fresh.model || '', worker_model: fresh.worker_model || '', mode: fresh.mode || 'auto', description: fresh.description || '', model_params: fresh.model_params || {}, worker_model_params: fresh.worker_model_params || {}, api_key: fresh.api_key || '', api_base: fresh.api_base || '', worker_api_key: fresh.worker_api_key || '', worker_api_base: fresh.worker_api_base || '', use_shared_memory: fresh.use_shared_memory ?? false };
    setEditingProject(newState);
  };

  const handleUpdateProject = async (e) => {
    e.preventDefault();
    if (!editingProject) return;
    setEditProjError('');

    try {
      const res = await fetch('/api/gilzabots/update-project', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_name: editingProject.name, user_prompt_prefix: editingProject.user_prompt_prefix, display_name: editingProject.project_name, project_path: editingProject.project_path, main_file: editingProject.main_file, git_root_path: editingProject.git_root_path || '', compile_on_save_partial: editingProject.compile_on_save_partial === true && editingProject.compile_on_save_full !== true, compile_on_save_full: editingProject.compile_on_save_full === true, model: editingProject.model, worker_model: editingProject.worker_model, mode: editingProject.mode, description: editingProject.description, model_params: editingProject.model_params, worker_model_params: editingProject.worker_model_params, use_shared_memory: editingProject.use_shared_memory, chat_id: activeChatId }),
      });
      if (res.ok) {
        const updated = await res.json();
        addLog('info', t('app.projectUpdated', { name: updated.project_name }));
        // The saved git root was kept as stored but no longer resolves on this
        // machine; the save is not blocked, so say so instead of failing silently.
        if (updated.git_root_warning) addLog('warning', t('app.gitRootStale', { path: updated.git_root_path }));
        setEditingProject(null);
        // Auto-selects the project once its path is fixed, so repairing a
        // project whose folder had moved (e.g. after switching OS) takes effect immediately.
        await fetchProjects(updated.name);
        if (activeProject?.name === updated.name) setActiveProject(prev => ({ ...prev, ...updated }));
      } else { const err = await res.json(); setEditProjError(err.error || t('app.projectUpdateError')); addLog('error', t('app.projectUpdateFailed', { error: err.error })); }
    } catch (err) { setEditProjError(err.message || t('app.projectUpdateError')); addLog('error', t('app.projectUpdateFailed', { error: err.message })); }
  };

  // ── Dir picker ────────────────────────────────────────────────────────────
  const openDirPicker = async (target, startPath) => {
    const path = startPath || '~';
    try {
      const res = await fetch('/api/fs/dirs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) });
      const data = await res.json();
      setDirPicker({ target, current: data.current, dirs: data.dirs || [] });
    } catch (e) { setDirPicker({ target, current: path, dirs: [] }); }
  };

  const navigateDirPicker = async (path) => {
    try {
      const res = await fetch('/api/fs/dirs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) });
      const data = await res.json();
      setDirPicker(prev => ({ ...prev, current: data.current, dirs: data.dirs || [] }));
    } catch (e) { }
  };

  const updateActiveGitRoot = async (gitRootPath) => {
    if (!activeProject) return;
    try {
      const res = await fetch('/api/gilzabots/update-project', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_name: activeProject.name,
          git_root_path: gitRootPath || '',
          chat_id: activeChatId,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        setActiveProject(prev => prev ? { ...prev, git_root_path: data.git_root_path || '' } : prev);
        setProjects(prev => prev.map(p => p.name === activeProject.name ? { ...p, git_root_path: data.git_root_path || '' } : p));
        addLog('info', gitRootPath ? t('app.gitRootSet', { path: data.git_root_path }) : t('app.gitRootReset'));
        fetchGitStatus();
      } else {
        addLog('error', t('app.gitRootSetError', { error: data.error || t('app.unknownError') }));
      }
    } catch (err) {
      addLog('error', t('app.gitRootSetError', { error: err.message }));
    }
  };

  // Cloud download: opening the panel resolves a sensible default parent folder
  // so the common case is "press Download", not "go find the home directory".
  const openCloudDownload = async () => {
    setIsCloudDownloadOpen(true);
    if (cloudDownloadParent) return;
    try {
      const res = await fetch('/api/fs/dirs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: '~' })
      });
      const data = await res.json();
      if (data.current) setCloudDownloadParent(data.current);
    } catch (e) { /* the user can still pick a folder by hand */ }
  };

  const handleProjectDownloaded = (project) => {
    setIsCloudDownloadOpen(false);
    const downloaded = (project?.report?.downloaded || []).length;
    addLog('info', t('app.projectDownloaded', {
      name: project.project_name || project.name,
      count: downloaded,
      defaultValue: '{{name}} downloaded from the cloud ({{count}} file(s)).',
    }));
    fetchProjects(project.name);
  };

  const confirmDirPicker = async () => {
    if (!dirPicker) return;
    if (dirPicker.target === 'new') setNewProjPath(dirPicker.current);
    else if (dirPicker.target === 'git-root') {
      await updateActiveGitRoot(dirPicker.current);
    }
    else if (dirPicker.target === 'edit-git-root') {
      setEditingProject(p => ({ ...p, git_root_path: dirPicker.current }));
    }
    else if (dirPicker.target === 'cloud-download') {
      setCloudDownloadParent(dirPicker.current);
    }
    else if (dirPicker.target === 'import') {
      // Call import-project API
      try {
        const res = await fetch('/api/gilzabots/import-project', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_path: dirPicker.current })
        });
        const data = await res.json();
        if (res.ok) {
          addLog('info', t('app.projectImported', { name: data.project_name }));
          setImportError('');
          fetchProjects(data.name);
        } else {
          setImportError(data.error || t('app.importProjectError'));
          addLog('error', t('app.importProjectErrorDetail', { error: data.error }));
        }
      } catch (err) {
        setImportError(t('app.errorWithMessage', { error: err.message }));
        addLog('error', t('app.importFileError', { error: err.message }));
      }
    }
    else setEditingProject(p => ({ ...p, project_path: dirPicker.current }));
    setDirPicker(null);
  };

  // ── Context menu ──────────────────────────────────────────────────────────
  const handleWorkspaceContextMenu = (e) => {
    if (!activeProject) return;
    e.preventDefault(); e.stopPropagation();
    setRightClickedNode(null);
    setContextMenu(viewportPointToApp(e.clientX, e.clientY));
  };

  const handleNodeContextMenu = (e, node) => {
    if (!activeProject) return;
    e.preventDefault(); e.stopPropagation();
    setRightClickedNode(node);
    setContextMenu(viewportPointToApp(e.clientX, e.clientY));
  };

  // Resolves to whether the desktop actually took the file, so a caller never
  // announces an open that the system opener refused.
  const handleOpenInSystem = async (node) => {
    setContextMenu(null);
    if (!activeProject || !node) return false;
    const targetPath = typeof node === 'string' ? node : (node.path || node);
    try {
      const res = await fetch('/api/file/open-explorer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectPath: activeProject.project_path, filePath: targetPath })
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.error || `HTTP ${res.status}`);
      }
      return true;
    } catch (err) {
      console.error('Failed to open in system:', err);
      addLog('error', t('app.openSystemError', { error: err.message }));
      return false;
    }
  };

  const handleCopyNode = (node) => {
    setClipboardNode(node);
    setContextMenu(null);
  };

  const handlePasteNode = async (parentPath) => {
    setContextMenu(null);
    if (!clipboardNode || !activeProject) return;
    try {
      const targetName = clipboardNode.name;
      const targetPath = parentPath ? `${parentPath}/${targetName}` : targetName;
      const res = await fetch('/api/file/copy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectPath: activeProject.project_path,
          sourcePath: clipboardNode.path,
          targetPath: targetPath,
        }),
      });
      if (!res.ok) {
        const errorData = await res.json();
        throw new Error(errorData.error || t('app.pasteFailed'));
      }
      fetchFiles();
    } catch (err) {
      addLog('error', t('app.pasteError', { error: err.message }));
    }
  };

  // ── Agent ─────────────────────────────────────────────────────────────────
  const handleInterruptAgent = async () => {
    if (isInterruptPending) return;
    setIsInterruptPending(true);
    try {
      const res = await fetch('/api/gilzabots/interrupt', { method: 'POST' });
      if (res.ok) {
        addLog('info', t('app.interruptSent'));
        setConfirmRequest(null);
        setAskRequest(null);
        setPlanRequest(null);
      } else {
        addLog('error', t('app.interruptFailed'));
        setIsInterruptPending(false);
      }
    } catch (err) {
      addLog('error', t('app.interruptError', { error: err.message }));
      setIsInterruptPending(false);
    }
  };

  // The backend refused to start a turn because another one is still alive. The
  // request was not run, so this reports it; stopping the other turn is offered,
  // never done on the user's behalf.
  const reportRunConflict = (conflict) => {
    if (conflict.reason === RUN_CONFLICT_TURN_STOPPING) {
      addLog('error', t('app.turnStillStopping', 'The message was not sent: the previous agent turn is still stopping. Try again in a moment.'));
      return;
    }
    addLog('error', t('app.turnStillRunning', 'The message was not sent: another agent turn is still running without a connection to this window.'));
    setConfirmRequest({
      prompt: t('app.turnStillRunningPrompt', 'An agent turn is still running, but this window lost its connection to it (for example, after the computer went to sleep). Stop that turn so a new one can start?'),
      options: ['yes', 'no'],
      default: 'no',
      callback: async (value) => {
        if (value !== 'yes') return;
        try {
          const res = await fetch('/api/gilzabots/interrupt', { method: 'POST' });
          addLog(res.ok ? 'info' : 'error', res.ok ? t('app.interruptSent') : t('app.interruptFailed'));
        } catch (err) {
          addLog('error', t('app.interruptError', { error: err.message }));
        }
      },
    });
  };

  const handleAgentEvent = (eventObj) => {
    const { event, ...data } = eventObj;
    switch (event) {
      case 'server_ready': addLog('info', t('app.agentReady'), data.agent); break;
      case 'agent_started':
        setAgentStepInfo({ step: 0, maxSteps: null });
        setProviderWait(null);
        // A new turn is what the panel should be showing: if the user had
        // opened a past message's reasoning, hand the panel back to the live
        // stream instead of leaving it on text that no longer moves.
        setThinkingFocus(null);
        addLog('info', t('app.agentStarted', { agent: data.agent }), data.agent);
        break;
      case 'agent_step':
        setAgentStepInfo({
          step: typeof data.step === 'number' ? data.step : 0,
          maxSteps: typeof data.max_steps === 'number' ? data.max_steps : null,
        });
        break;
      case 'thought': {
        if (isRunOnScreen()) addLog('thought', data.content, data.agent);
        chatThoughtTailRef.current = appendThoughtChunk(
          chatThoughtTailRef.current,
          data.content,
          Number(data.thought_tokens),
          chatThoughtPreviewTokensRef.current,
        );
        // The text is joined once per render (`chatThoughtStream` below), not
        // once per chunk: a batch of a frame's chunks lands in one render.
        setChatThoughtTail(chatThoughtTailRef.current);
        break;
      }
      case 'reflection':
        if (isRunOnScreen()) addLog('reflection', data.content, data.agent);
        break;
      case 'achievements_update':
        setAchievementsMemory(data.content);
        break;
      case 'token_usage':
        // prompt_tokens is the size of the request the provider just billed, so
        // it already accounts for the system prompt, the tool schemas, the tool
        // calls and the tool results — none of which reach chatMessages.
        if (isRunOnScreen()) setChatContextUsage(prev => contextUsageFromPayload(data) || prev);
        break;
      case 'stream_retract': {
        // An orphan </think> proved this text was reasoning, not the answer, after
        // it had already been streamed. It arrives again as a 'thought' event, so
        // it only has to leave the live response here. The Output log keeps the
        // raw provider stream on purpose.
        const retracted = sanitizeVisibleStreamChunk(data.content);
        if (!retracted) break;
        setChatResponseStream(prev => {
          const next = prev.endsWith(retracted) ? prev.slice(0, -retracted.length) : '';
          chatResponseStreamRef.current = next;
          return next;
        });
        break;
      }
      case 'stream_chunk':
        const visibleStreamChunk = sanitizeVisibleStreamChunk(data.content);
        if (isRunOnScreen()) addLog('stream_chunk', visibleStreamChunk, data.agent);
        if (!visibleStreamChunk) break;
        setChatResponseStream(prev => {
          const next = prev + visibleStreamChunk;
          chatResponseStreamRef.current = next;
          return next;
        });
        break;
      case 'cancelled': {
        addLog('warning', data.message || t('app.executionCancelled'), data.agent);
        // What the turn had already said is part of the interruption, not
        // collateral: the backend persists it above the marker, so the bubble
        // shown here has to be the same message, or reloading the chat would
        // produce a second, longer account of one event — and the partial
        // answer would vanish from the screen in the meantime.
        const partialAnswer = chatResponseStreamRef.current;
        chatThoughtTailRef.current = createThoughtTail();
        setChatThoughtTail(chatThoughtTailRef.current);
        chatResponseStreamRef.current = '';
        setChatResponseStream('');
        if (isRunOnScreen()) setChatMessages(prev => [...prev, {
          role: 'assistant',
          content: interruptedTurnContent(partialAnswer),
          timestamp: new Date().toISOString(),
        }]);
        setConfirmRequest(null);
        setAskRequest(null);
        setPlanRequest(null);
        break;
      }
      case 'tool_call':
        addLog('tool_call', t('app.callingTool', { tool: data.tool, arguments: JSON.stringify(data.arguments) }), data.agent);
        if (AGENT_WRITE_TOOLS.has(data.tool) && data.arguments?.path) {
          turnWrittenPathsRef.current.add(data.arguments.path);
        }
        break;
      case 'tool_result':
        if (data.is_error) {
          addLog('error', t('app.toolFailed', { tool: data.tool }), data.agent);
        } else {
          addLog('tool_result', t('app.toolSucceeded', { tool: data.tool }), data.agent);
        }
        // Tool events carry no call id, so a result cannot be paired with its
        // call: every path written so far is checked, and the ones whose disk
        // is unchanged cost a read and nothing else.
        if (AGENT_WRITE_TOOLS.has(data.tool)) {
          agentFileSyncRef.current?.([...turnWrittenPathsRef.current]);
        }
        break;
      case 'agent_response':
        addLog('info', t('app.responseReceived'));
        const finalThoughtStream = thoughtTailText(chatThoughtTailRef.current);
        const responseText = (data.response && data.response.trim() !== '')
          ? data.response
          : "⚠️ *O agente concluiu o processamento, mas não emitiu nenhuma resposta textual ou chamada de ferramenta. Isso geralmente acontece quando o modelo de IA sofre uma falha de geração (ex: esqueceu de usar o formato correto após pensar).*";

        chatThoughtTailRef.current = createThoughtTail();
        setChatThoughtTail(chatThoughtTailRef.current);
        chatResponseStreamRef.current = '';
        setChatResponseStream('');

        if (isRunOnScreen()) setChatMessages(prev => {
          const last = prev[prev.length - 1];
          const baseContent = responseText;
          const finalContent = baseContent;
          if (last?.role === 'assistant' && last.content === finalContent) return prev;
          return [...prev, {
            id: data.message_id,
            role: 'assistant',
            content: finalContent,
            _thoughtStream: finalThoughtStream || undefined,
            timestamp: new Date().toISOString(),
            chat_id: runChatIdRef.current || activeChatId,
          }];
        });

        // ── Auto-replace: if there is a pending inline selection range, extract
        //    the first fenced code block from the response and apply it.
        if (pendingInlineRangeRef.current && editorRef.current && monacoRef.current) {
          const range = pendingInlineRangeRef.current;
          pendingInlineRangeRef.current = null;
          try {
            const newCode = extractInlineReplacementBlock(data.response);
            if (newCode !== null) {
              const monacoRange = new monacoRef.current.Range(
                range.startLineNumber,
                range.startColumn,
                range.endLineNumber,
                range.endColumn,
              );
              const model = editorRef.current.getModel();
              const originalText = model ? model.getValueInRange(monacoRange) : '';
              const normalizedCode = normalizeInlineReplacementSpacing(
                newCode,
                originalText,
                model?.getEOL?.() || '\n',
              );
              editorRef.current.executeEdits('gilzabots-inline', [{
                range: monacoRange,
                text: normalizedCode,
                forceMoveMarkers: true,
              }]);
              addLog('info', t('app.inlineReplacementApplied', { start: range.startLineNumber, end: range.endLineNumber }));
            }
          } catch (replaceErr) {
            addLog('error', t('app.inlineReplacementFailed', { error: replaceErr.message }));
          }
        }
        break;
      case 'user_message_saved':
        // Stamp the stored id on the optimistic user message so editing, retrying
        // and branching have a stable anchor during this session.
        setChatMessages(prev => prev.map(msg => (
          msg.role === 'user'
            && msg.client_message_id === data.client_message_id
            && msg.id === undefined
            ? { ...msg, id: data.message_id }
            : msg
        )));
        break;
      case 'user_message_delivered':
        // The agent has taken the message off the queue and read it. This is the
        // one moment the user is waiting to see, so it is reported as its own
        // state rather than by dropping the waiting badge.
        setQueuedMessages(prev => prev.filter(m => m.clientMessageId !== data.client_message_id));
        setChatMessages(prev => prev.map(msg => (
          msg.client_message_id === data.client_message_id
            ? { ...msg, _deliveryState: 'delivered' }
            : msg
        )));
        addLog('info', t('app.messageDelivered', 'Message delivered to the running agent.'));
        break;
      case 'user_message_backlog':
        // Reported, not acted on here: these entries are still in queuedMessages
        // and the flush effect sends them as the next turn.
        addLog('info', t('app.messageBacklog', 'The turn ended before {{count}} queued message(s) were delivered. Sending them as a new turn.', { count: (data.items || []).length }));
        break;
      case 'agent_finished':
        addLog('info', t('app.processingCompleted', 'Processamento concluído.'));
        // Without a checkpoint (git strategy "none") this is the turn's last
        // chance to reconcile what its write tools named.
        agentFileSyncRef.current?.([...turnWrittenPathsRef.current]);
        turnWrittenPathsRef.current = new Set();
        break;
      case 'checkpoint_finalized': {
        setCheckpointRevision(value => value + 1);
        // A turn of a project the user has since left must not mark files of
        // the one on screen. The ref is read because this handler may be a
        // closure from when the turn's stream started.
        const turnProject = String(data.project_path || '').replace(/[\\/]+$/, '');
        const shownProject = String(documentContextRef.current?.projectPath || '').replace(/[\\/]+$/, '');
        if (data.success && turnProject && turnProject === shownProject && data.changed_files?.length) {
          setAgentChangedFiles(prev => withAgentChangedFiles(prev, data.changed_files));
        }
        // The checkpoint also sees what no tool event reports: workers' writes,
        // run_command, scripts. Open buffers of all of them are brought up to date.
        if (turnProject && turnProject === shownProject) {
          agentFileSyncRef.current?.([...(data.changed_files || []), ...turnWrittenPathsRef.current]);
          turnWrittenPathsRef.current = new Set();
        }
        break;
      }
      case 'ide_command': {
        // The agent waits for this answer (gilzabots/ide_control.py); a refusal
        // is posted too, so it fails with the reason instead of timing out.
        const commandId = data.id;
        Promise.resolve(ideCommandRunnerRef.current(data))
          .then((result) => {
            if (result.ok) {
              addLog('info', t('app.ideCommandDone', 'Agent: {{action}} {{path}}', { action: data.action, path: data.args?.path || data.args?.layout || '' }), data.agent);
            } else {
              addLog('error', t('app.ideCommandRefused', 'Agent command {{action}} not carried out: {{error}}', { action: data.action, error: result.error }));
            }
            return fetch('/api/gilzabots/ide_command_result', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ ...result, id: commandId }),
            });
          })
          .catch((err) => addLog('error', t('app.ideCommandFailed', 'Could not answer the agent\'s IDE command: {{error}}', { error: err.message })));
        break;
      }
      case 'latex_compiled': {
        // compile_latex is the editor's own Compile, so its outcome is shown the
        // same way: the PDF pane reloads and a failure fills the error card and
        // Problems. A compile of a project the user has left changes nothing.
        const shownProject = documentContextRef.current?.projectPath || '';
        const caseInsensitive = ideCommandDepsRef.current.caseInsensitive();
        if (!sameProjectPath(data.project_path, shownProject, { caseInsensitive })) break;
        const details = {
          filePath: data.main_file || '',
          fileContent: '',
          projectPath: data.project_path || '',
          mainFile: data.main_file || '',
          compiledMainFile: data.main_file || '',
          log: data.log || '',
          compileDebug: {},
          partial: false,
          draft: Boolean(data.draft),
        };
        if (data.success) handleLatexCompileSuccess();
        else handleLatexCompileError(details);
        setAgentCompile(prev => ({ ...details, success: Boolean(data.success), seq: (prev?.seq || 0) + 1 }));
        break;
      }
      case 'input_request_closed':
        setPlanRequest(prev => prev?.id === data.id ? null : prev);
        setAskRequest(prev => prev?.id === data.id ? null : prev);
        setConfirmRequest(prev => prev?.id === data.id ? null : prev);
        break;
      case 'input_request': {
        // Plans and questions float over a usable workbench, each in its own
        // state slot. Unsafe-tool confirmations remain blocking modals because
        // changing the underlying files could invalidate what is being approved.
        const request = normalizeInputRequest(data);
        if (data.markdown_content) setPlanRequest(request);
        else if (request.type === 'ask') setAskRequest(request);
        else setConfirmRequest(request);
        addLog('info', t('app.waitingConfirmation', '🔔 Aguardando confirmação: {{prompt}}', { prompt: data.prompt }));
        break;
      }
      case 'error':
        addLog('error', data.message);
        addProblem({ tool: data.agent || t('app.agentTool', 'Agent'), message: data.message, severity: 'error' });
        if (isRunOnScreen()) setChatMessages(prev => [...prev, { role: 'assistant', content: t('app.agentError', '🔴 Erro do Agente: {{message}}', { message: data.message }), is_error: true, timestamp: new Date().toISOString() }]);
        break;
      case 'problem':
        addLog('error', t('app.toolProblem', { tool: data.tool, message: data.message }));
        addProblem({ tool: data.tool, message: data.message, severity: data.severity || 'error' });
        break;
      case 'provider_wait': {
        // Information only: the request, its timeout and its retries are unchanged.
        const wait = providerWaitFromEvent(data);
        setProviderWait(wait);
        if (isRunOnScreen()) {
          addLog('info', wait
            ? t('app.providerWaitLog', { elapsed: formatWaitDuration(data.elapsed_seconds), model: data.model || '' })
            : t('app.providerWaitOverLog', { elapsed: formatWaitDuration(data.elapsed_seconds) }), data.agent);
        }
        break;
      }
      default: addLog('info', t('app.eventReceived', { event }));
    }
  };

  const makeClientMessageId = () => {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
      return crypto.randomUUID();
    }
    return `msg_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  };

  const setMessageDeliveryState = (clientMessageId, deliveryState) => {
    setChatMessages(prev => prev.map(msg => (
      msg.client_message_id === clientMessageId ? { ...msg, _deliveryState: deliveryState } : msg
    )));
  };

  // Hand a message to the turn already running. The agent picks it up at its next
  // boundary — after the tool call in flight, before the next model call — so the
  // message passes through three reported states: 'sending' while the request is
  // in flight, 'queued' once the backend accepted it, and 'delivered' once the
  // agent has actually read it. Delivery is never instant, and the difference
  // between "waiting" and "read" is the whole question the user is asking, so
  // both are stated rather than implied by the absence of a badge.
  const queueMessageForRunningAgent = async (text, attachments, chatId) => {
    const clientMessageId = makeClientMessageId();
    setChatMessages(prev => [...prev, {
      role: 'user',
      content: text || '📎 Attachment',
      client_message_id: clientMessageId,
      _attachments: attachments,
      _deliveryState: 'sending',
      timestamp: new Date().toISOString(),
      chat_id: chatId,
    }]);
    setQueuedMessages(prev => [...prev, { clientMessageId, itemId: '', text, attachments, chatId }]);

    let result = {};
    try {
      const res = await fetch('/api/gilzabots/message', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          content: text,
          display_text: text,
          attachments,
          project_path: activeProject?.project_path || '',
          chat_id: chatId,
          client_message_id: clientMessageId,
        }),
      });
      result = await res.json().catch(() => ({}));
      if (res.ok) {
        setQueuedMessages(prev => prev.map(m => (
          m.clientMessageId === clientMessageId ? { ...m, itemId: result.item_id || '' } : m
        )));
        setMessageDeliveryState(clientMessageId, 'queued');
        addLog('info', t('app.messageQueued', 'Message queued for the running agent.'));
        return;
      }
      if (result.reason === 'no_active_run') {
        // The turn ended between typing and this request. The entry stays queued
        // and the flush effect below sends it as an ordinary next turn — so it is
        // waiting, not still being sent.
        setMessageDeliveryState(clientMessageId, 'queued');
        return;
      }
      throw new Error(result.error || t('app.messageQueueFailed', 'The message could not be queued.'));
    } catch (err) {
      // Refused: drop the bubble and give the text back to the composer instead
      // of showing a message that will never reach the agent.
      setChatMessages(prev => prev.filter(m => m.client_message_id !== clientMessageId));
      setQueuedMessages(prev => prev.filter(m => m.clientMessageId !== clientMessageId));
      setChatInput(prev => (prev.trim() ? prev : text));
      setPendingAttachments(prev => (prev.length ? prev : attachments));
      addLog('error', t('app.messageQueueError', 'Could not queue the message: {{error}}', { error: err.message }));
    }
  };

  const handleCancelQueuedMessage = async (clientMessageId) => {
    const entry = queuedMessages.find(m => m.clientMessageId === clientMessageId);
    if (!entry) return;
    if (entry.itemId) {
      try {
        const res = await fetch('/api/gilzabots/message/cancel', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_id: entry.itemId }),
        });
        // 404 means the agent already took it: it is part of the conversation
        // now, and removing the bubble would hide a message the model has read.
        if (!res.ok) return;
      } catch (err) {
        addLog('error', t('app.messageCancelError', 'Could not cancel the message: {{error}}', { error: err.message }));
        return;
      }
    }
    setQueuedMessages(prev => prev.filter(m => m.clientMessageId !== clientMessageId));
    setChatMessages(prev => prev.filter(m => m.client_message_id !== clientMessageId));
  };

  const handleCancelAllQueuedMessages = async () => {
    const pending = [...queuedMessages];
    for (const entry of pending) {
      await handleCancelQueuedMessage(entry.clientMessageId);
    }
  };

  const handleSendMessage = async (e, retryMsg = null, options = {}) => {
    if (e && e.preventDefault) e.preventDefault();
    let targetChatId = options.chatIdOverride || activeChatId;

    let userText = '';
    let displayText = '';
    let attachmentsSnapshot = [];
    let clientMessageId = options.clientMessageId || retryMsg?.client_message_id || retryMsg?.clientMessageId || '';

    if (options.resumeInterrupted) {
      if (!activeProject || isTurnBusy()) return;
      const historySnapshot = options.historyOverride || chatMessages;
      // The backend builds the resumed turn from what it stored for the
      // interrupted one (resume_interrupted below); only the label is sent.
      // The conversation is deliberately not sent either: the orchestrator
      // restores its own working memory for this chat, tool results included,
      // and replacing it with the visible bubbles (truncated, and without any
      // tool call) made every resume re-read and re-run what the stopped turn
      // had already done.
      userText = options.displayText || t('chatPanel.continue', 'Continue');
      displayText = options.displayText || t('chatPanel.continue', 'Continue');
      attachmentsSnapshot = options.overrideAttachments || collectRecentChatAttachments(historySnapshot, 3);
    } else if (options.overrideText !== undefined) {
      if (!activeProject || isTurnBusy()) return;
      userText = options.overrideText;
      displayText = options.displayText || userText;
      attachmentsSnapshot = options.overrideAttachments || [];
    } else if (retryMsg) {
      if (!activeProject || isTurnBusy()) return;
      userText = retryMsg.content === '📎 Attachment' ? '' : retryMsg.content;
      displayText = userText;
      attachmentsSnapshot = retryMsg._attachments || [];
    } else {
      if ((!chatInput.trim() && pendingAttachments.length === 0) || !activeProject) return;
      userText = chatInput;
      displayText = userText;
      attachmentsSnapshot = [...pendingAttachments];
      setChatInput('');
      setPendingAttachments([]);
      if (isTurnBusy()) {
        // A slash command is a client-side operation on the chat the agent is
        // using (/clear erases the history it is answering from), not something
        // to hand the model as text. Refuse it while a turn runs instead of
        // silently sending it as a message.
        if (userText.trim().startsWith('/')) {
          setChatInput(userText);
          setPendingAttachments(attachmentsSnapshot);
          addLog('error', t('app.slashCommandWhileRunning', 'Slash commands cannot run while the agent is working. Stop the agent first.'));
          return;
        }
        // With no chat open there is no conversation to join: handing it to
        // the running turn would put it in another chat.
        if (!targetChatId) {
          setChatInput(userText);
          setPendingAttachments(attachmentsSnapshot);
          addLog('error', t('app.noChatWhileRunning', 'No chat is open. Wait for the agent to finish, then send the message to start a new chat.'));
          return;
        }
        // The composer stays live during a turn. This message joins the turn in
        // flight instead of waiting for it to end, so it does not start a run of
        // its own here.
        await queueMessageForRunningAgent(userText, attachmentsSnapshot, targetChatId);
        return;
      }
    }
    // Taken before any request below is made; every exit from here on releases it.
    localTurnRef.current = true;
    // A project may have no chat: the first message is what starts one. Slash
    // commands act on the project or on an existing chat and never create one.
    if (!targetChatId && !options.resumeInterrupted && !userText.trim().startsWith('/')) {
      try {
        const created = await createChatForTask(t('chatPanel.defaultChatName', 'New Chat'));
        targetChatId = created.chatId;
        if (!options.baseMessages) options = { ...options, baseMessages: created.baseMessages };
      } catch (err) {
        if (options.overrideText === undefined && !retryMsg) {
          setChatInput(userText);
          setPendingAttachments(attachmentsSnapshot);
        }
        addLog('error', t('app.autoCreateChatFailed', 'Could not create a chat for this message: {{error}}', { error: err.message }));
        localTurnRef.current = false;
        return;
      }
    }
    if (!clientMessageId) {
      clientMessageId = makeClientMessageId();
    }
    if (options.supersedeFrom) {
      try {
        // Anchor the cut on the stored message id (or its client id), never on a
        // UI position: the rendered list and the stored rows do not line up, and a
        // wrong position would supersede unrelated turns.
        const truncateRes = await fetch('/api/chat/truncate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            project_name: activeProject.name,
            chat_id: targetChatId,
            message_id: options.supersedeFrom.messageId ?? undefined,
            client_message_id: options.supersedeFrom.clientMessageId || '',
            superseded_by: clientMessageId,
          }),
        });
        if (!truncateRes.ok) {
          const err = await truncateRes.json().catch(() => ({}));
          throw new Error(err.error || t('app.truncateChatFailed'));
        }
      } catch (err) {
        const message = t('app.editPreparationFailed', { error: err.message });
        addLog('error', message);
        options.onRefused?.(message);
        localTurnRef.current = false;
        return;
      }
    }
    // From here on the message is on its way: a caller holding the text
    // elsewhere (the message editor) can let go of it.
    options.onHandoff?.();
    const requestPrompt = userText;
    if (displayText) userText = displayText;
    // Show attachment previews alongside the user message in the chat history
    const userMsg = {
      role: 'user',
      content: userText || '📎 Attachment',
      client_message_id: clientMessageId,
      _attachments: attachmentsSnapshot,
      timestamp: new Date().toISOString(),
      chat_id: targetChatId,
    };
    if (!options.skipUserMessageAppend) {
      setChatMessages(prev => {
        if (options.baseMessages) {
          return [...options.baseMessages, userMsg];
        }
        if (options.replaceUiIndex !== undefined) {
          return [...prev.slice(0, options.replaceUiIndex), userMsg];
        }
        if (retryMsg) {
          const idx = prev.indexOf(retryMsg);
          if (idx !== -1) {
            return [...prev.slice(0, idx), userMsg];
          }
        }
        return [...prev, userMsg];
      });
    }
    setIsAgentRunning(true);
    setIsInterruptPending(false);
    setAgentStepInfo({ step: 0, maxSteps: null });
    setProblems([]);
    setAchievementsMemory('');
    // The live stream buffers are deliberately NOT cleared here. They hold the
    // only copy of a partial answer: a turn that died mid-stream (a provider
    // network error, a standby) persists no assistant message, just activity
    // fragments. Clearing them before knowing the new turn will actually start
    // destroyed that text on a run the backend then refused with 409 -- the
    // screen emptied and there was nothing left to continue from. They are
    // cleared below, once the request is accepted.
    addLog('info', t('app.starting', { text: userText }));

    if (userText.trim().startsWith('/')) {
      try {
        const res = await fetch('/api/gilzabots/slash-command', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt: userText.trim(), project_name: activeProject.name, project_path: activeProject.project_path, chat_id: targetChatId }),
        });
        const result = await res.json();
        if (result.status === 'confirm') {
          setConfirmRequest({ id: result.id, prompt: result.prompt, options: result.options || ['yes', 'no'], default: result.default || 'yes', type: result.type || 'confirm', isSlashCommand: true });
          addLog('info', t('app.waitingConfirmation', { prompt: result.prompt }));
        } else if (result.status === 'done') {
          // /clear and /clear_chat erase the context the indicator described.
          setChatContextUsage(contextUsageFromPayload(result.context_usage));
          setChatMessages(prev => [...prev, { role: 'assistant', content: (result.messages || []).join('\n') || 'Comando executado.', timestamp: new Date().toISOString() }]);
        } else {
          setChatMessages(prev => [...prev, { role: 'assistant', content: `🔴 Erro: ${result.error || 'desconhecido'}`, is_error: true, timestamp: new Date().toISOString() }]);
        }
      } catch (err) {
        addLog('error', t('app.commandFailed', { error: err.message }));
        setChatMessages(prev => [...prev, { role: 'assistant', content: `🔴 Falha: ${err.message}`, is_error: true, timestamp: new Date().toISOString() }]);
      } finally { localTurnRef.current = false; setIsAgentRunning(false); fetchFiles(); }
      return;
    }

    let selectedText = '';
    if (editorRef.current) {
      try { const model = editorRef.current.getModel(); const sel = editorRef.current.getSelection(); if (model && sel) selectedText = model.getValueInRange(sel); } catch (e) { }
    }

    bindRunChat(targetChatId);
    // Reported to callers that must know whether the turn ran, such as a
    // monitor's wake-up (useMonitorBridge), which returns a refused wake-up to
    // the queue instead of marking it done.
    let runOutcome = 'not_started';
    try {
      const res = await fetch('/api/gilzabots/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          command: 'run', agent: 'chat_orchestrator', prompt: requestPrompt,
          display_prompt: displayText || userText || '',
          resume_interrupted: Boolean(options.resumeInterrupted),
          project_name: activeProject.name, project_path: activeProject.project_path,
          model: activeProject.model,
          worker_model: activeProject.worker_model,
          model_params: activeProject.model_params,
          worker_model_params: activeProject.worker_model_params,
          max_heartbeats: options.maxHeartbeats !== undefined ? options.maxHeartbeats : resolveEffectiveMaxHeartbeats(),
          current_file: selectedFile || '',
          open_files: openFiles,
          editor_content: fileContent || '', selected_text: selectedText || '',
          lang: i18n.language || 'en',
          chat_id: targetChatId,
          client_message_id: clientMessageId,
          attachments: attachmentsSnapshot,
          ...(options.monitorWake ? { monitor_wake: options.monitorWake } : {}),
          ...(options.monitorWakeId ? { monitor_wake_id: options.monitorWakeId } : {}),
        }),
      });
      const runConflict = await readRunConflict(res);
      if (runConflict) {
        // Nothing was sent: take the optimistic bubble back and, for a message
        // typed in the composer, return its text and attachments.
        setChatMessages(prev => prev.filter(m => m.client_message_id !== clientMessageId));
        if (
          (!options.resumeInterrupted && options.overrideText === undefined && !retryMsg)
          || options.returnTextOnConflict
        ) {
          setChatInput(prev => (prev.trim() ? prev : userText));
          setPendingAttachments(prev => (prev.length ? prev : attachmentsSnapshot));
        }
        reportRunConflict(runConflict);
        return 'conflict';
      }
      // The turn was accepted, so the previous turn's partial output is safe to
      // drop: this one is about to produce its own. See the note above.
      chatThoughtTailRef.current = createThoughtTail();
      setChatThoughtTail(chatThoughtTailRef.current);
      chatResponseStreamRef.current = '';
      setChatResponseStream('');
      if (!res.body) { addLog('error', t('app.streamUnsupportedBackend')); setIsAgentRunning(false); return; }
      await consumeLinesByFrame(res.body.getReader(), (line) => {
        try { handleAgentEvent(JSON.parse(line)); } catch (e) { addLog('stdout', line); }
      });
      runOutcome = 'completed';
    } catch (err) {
      runOutcome = 'failed';
      addLog('error', t('app.executionFailed', { error: err.message }));
      setChatMessages(prev => [...prev, { role: 'assistant', content: `🔴 Falha na execução: ${err.message}`, is_error: true, timestamp: new Date().toISOString() }]);
    } finally { localTurnRef.current = false; bindRunChat(''); setIsAgentRunning(false); setIsInterruptPending(false); fetchFiles(); fetchProblems(); }
    return runOutcome;
  };

  // ── Monitors (PROJECT_DESIGN 2.24) ─────────────────────────────────────
  // A wake-up runs as an ordinary turn of its chat, marked `monitor_wake`: the
  // backend then refuses every question outside the chat (tool approval, plan
  // approval, ask_question), and the agent reports in its reply instead.
  const pendingMonitorOpenRef = useRef(null);
  const lastMonitorEventRef = useRef(null);
  // The chats of the open project, reloaded when a monitor created one.
  const refreshChatList = async () => {
    if (!activeProject) return [];
    try {
      const res = await fetch(`/api/chat/list?project_name=${encodeURIComponent(activeProject.name)}&t=${Date.now()}`);
      const data = res.ok ? await res.json() : null;
      if (data?.chats) {
        setChats(data.chats);
        return data.chats;
      }
    } catch (err) {
      console.error(err);
    }
    return [];
  };
  const [monitorUnseenEvents, setMonitorUnseenEvents] = useState(0);
  // Reload the history of the chat on screen, when something other than this
  // window wrote to it (a monitor's background turn).
  const reloadChatOnScreen = async (chatId) => {
    if (!activeProject || !chatId) return;
    try {
      const res = await fetch(`/api/chat/history?project_name=${encodeURIComponent(activeProject.name)}&chat_id=${encodeURIComponent(chatId)}&t=${Date.now()}`);
      if (!res.ok) return;
      const data = await res.json();
      if (activeChatIdRef.current !== chatId || isTurnBusy()) return;
      startTransition(() => {
        setChatContextUsage(contextUsageFromPayload(data.context_usage));
        if (data.history && data.history.length > 0) {
          setChatMessages(attachThoughtActivityToMessages(data.history, data.activity || [], data.thought_windows || []));
        }
        setTerminalLogs(activityToTerminalLogs(data.activity || []));
      });
    } catch (err) {
      console.error(err);
    }
  };
  const showMonitorChat = (target) => {
    if (!target?.project) return;
    setIsChatVisible(true);
    if (activeProject?.name === target.project) {
      if (!target.chat_id || target.chat_id === activeChatIdRef.current) return;
      // A chat a monitor created may not be in the list yet; the selector
      // cannot show a chat it does not list.
      if (chats.some(chat => chat.id === target.chat_id)) {
        handleSwitchChat(target.chat_id);
      } else {
        refreshChatList().then(list => {
          if (list.some(chat => chat.id === target.chat_id)) handleSwitchChat(target.chat_id);
          else addLog('error', t('app.monitorChatMissing', 'The chat of this monitor event no longer exists.'));
        });
      }
      return;
    }
    const proj = projects.find(p => p.name === target.project);
    if (!proj) {
      addLog('error', t('app.monitorProjectMissing', 'The monitor\'s project "{{project}}" is not in the project list.', { project: target.project }));
      return;
    }
    pendingMonitorOpenRef.current = target;
    handleSelectProject(proj);
  };
  useMonitorBridge({
    activeProject,
    isTurnBusy,
    // A wake-up of the chat a monitor was registered from is an ordinary turn
    // of that chat: streamed there, and shown only while it is on screen. A
    // wake-up in a new chat never reaches the window (the daemon runs it, out
    // of the user's way); it arrives as an event (onEvents below).
    runWake: (wake) => {
      // Never fall back to the chat on screen: handleSendMessage reads an
      // empty chatIdOverride as "the active chat".
      if (!wake.chat_id) {
        addLog('error', t('app.monitorWakeWithoutChat', 'A monitor wake-up named no chat and was not run.'));
        return Promise.resolve('failed');
      }
      return handleSendMessage(null, null, {
        overrideText: wake.prompt,
        displayText: wake.display || wake.prompt,
        chatIdOverride: wake.chat_id,
        skipUserMessageAppend: wake.chat_id !== activeChatIdRef.current,
        monitorWake: wake.monitor_name || 'monitor',
        monitorWakeId: wake.monitor_id || '',
      });
    },
    openChat: showMonitorChat,
    onEvents: (events, unseen) => {
      setMonitorUnseenEvents(unseen);
      const newest = events.reduce((max, event) => Math.max(max, event.id), 0);
      const known = lastMonitorEventRef.current;
      lastMonitorEventRef.current = Math.max(known, newest);
      // The first poll only learns where the list stands (null until then;
      // an empty list stands at 0, so its first event is news).
      if (known === null) return;
      const mine = events.filter(event => event.id > known && event.project === activeProject?.name);
      if (!mine.length) return;
      if (mine.some(event => !chats.some(chat => chat.id === event.chat_id))) refreshChatList();
      // The user opened the chat while the monitor was writing to it.
      if (mine.some(event => event.chat_id === activeChatIdRef.current)) {
        reloadChatOnScreen(activeChatIdRef.current);
      }
    },
  });
  useEffect(() => {
    const target = pendingMonitorOpenRef.current;
    if (!target || activeProject?.name !== target.project) return;
    pendingMonitorOpenRef.current = null;
    if (target.chat_id && target.chat_id !== activeChatIdRef.current) handleSwitchChat(target.chat_id);
  }, [activeProject?.name]);

  // A message the turn ended without delivering becomes an ordinary next turn.
  // It goes back through handleSendMessage rather than through a shortcut, so it
  // keeps its attachments, its turn checkpoint and its stored identity: a queued
  // message must not end up as a lesser kind of message. One per pass — starting
  // a turn re-runs this effect for whatever is still queued.
  useEffect(() => {
    if (isTurnBusy() || !activeProject || queuedMessages.length === 0) return;
    const next = queuedMessages[0];
    setQueuedMessages(prev => prev.filter(m => m.clientMessageId !== next.clientMessageId));
    setChatMessages(prev => prev.filter(m => m.client_message_id !== next.clientMessageId));
    handleSendMessage(null, null, {
      overrideText: next.text,
      overrideAttachments: next.attachments,
      clientMessageId: next.clientMessageId,
      chatIdOverride: next.chatId,
    });
  }, [isAgentRunning, queuedMessages, activeProject]);

  const handleFixLatexProblem = async (problem) => {
    if (!activeProject || isAgentRunning || !problem) return;
    setIsChatVisible(true);
    try {
      const prompt = buildLatexFixPrompt(problem);
      const timestamp = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const chatName = `${t('app.fixLatexChatName', 'Fix LaTeX error')} ${timestamp}`;
      const { chatId, baseMessages } = await createChatForTask(chatName);
      await handleSendMessage(null, null, {
        overrideText: prompt,
        displayText: buildLatexFixDisplayText(problem),
        chatIdOverride: chatId,
        baseMessages,
      });
    } catch (err) {
      addLog('error', t('app.fixLatexChatCreateFailed', 'Failed to create a new chat for the LaTeX fix: {{error}}', { error: err.message }));
    }
  };

  // "Ask about" only stages the question: it opens the chat and pre-fills the
  // composer so the user completes the prompt and sends it themselves. It never
  // starts a turn, and it never replaces text the user has already typed.
  //
  // The staged text has one shape for every document (utils/askAboutPrompt.js);
  // only the locator differs, because a PDF has pages and a rendered Markdown
  // preview has sections.
  const stageAskAboutPrompt = (prompt) => {
    setIsChatVisible(true);
    setChatInput((prev) => (prev.trim() ? `${prev.replace(/\s+$/, '')}\n\n${prompt}` : prompt));
    setChatInputFocusSignal((prev) => prev + 1);
  };

  const handleAskAboutPdf = ({ pdfPath, sourceFile, page, totalPages, selectedText } = {}) => {
    if (!activeProject || !pdfPath) return;
    const locator = totalPages
      ? `${t('app.pdfPageLabel', 'Page')} ${page}/${totalPages}`
      : `${t('app.pdfPageLabel', 'Page')} ${page}`;
    stageAskAboutPrompt(buildAskAboutDocumentPrompt(t, {
      documentPath: pdfPath,
      projectPath: activeProject?.project_path || '',
      sourceFile,
      sourceFileLabel: t('app.pdfSourceFileLabel', 'LaTeX source open in the editor'),
      locator,
      selectedText,
      excerptLabel: t('app.pdfSelectedExcerptLabel', 'Excerpt selected in the PDF viewer'),
    }));
  };

  const handleAskAboutMarkdown = ({ documentPath, heading, selectedText } = {}) => {
    if (!activeProject || !documentPath) return;
    // A rendered preview has no page numbers and no dependable mapping back to
    // source lines, so the section the excerpt was read in is the locator. It
    // is omitted rather than guessed when the excerpt sits above the first
    // heading.
    stageAskAboutPrompt(buildAskAboutDocumentPrompt(t, {
      documentPath,
      projectPath: activeProject?.project_path || '',
      locator: heading ? `${t('app.markdownSectionLabel', 'Section')}: ${heading}` : '',
      selectedText,
      excerptLabel: t('app.markdownSelectedExcerptLabel', 'Excerpt selected in the Markdown preview'),
    }));
  };

  // Sends an edited message and resolves as soon as the editor's copy of the
  // text is no longer needed (see utils/editHandoff.js): the turn itself may
  // run for minutes, and an edit that went nowhere must not close the editor.
  const sendEditedMessage = (sendOptions) => settleAtHandoff(
    ({ onHandoff, onRefused }) => handleSendMessage(null, null, { ...sendOptions, onHandoff, onRefused }),
    t('app.editNotSent', 'The edited message was not sent.'),
  );

  // Returns { accepted, error }. A refused edit keeps the message editor open
  // with the text and the reason (ChatPanel): an edit must never disappear
  // without a word, leaving the user to send the text some other way.
  const handleEditUserMessage = async (messageIndex, originalMessage, editedContent) => {
    const refuse = (error) => {
      addLog('error', error);
      return { accepted: false, error };
    };
    if (!activeProject) return refuse(t('app.editNoProject', 'No project is open.'));
    if (!originalMessage || originalMessage.role !== 'user') {
      return refuse(t('app.editNotUserMessage', 'Only your own messages can be edited.'));
    }
    if (isTurnBusy()) {
      return refuse(t('app.editWhileRunning', 'The agent is still working. Wait for it to finish, or stop it, then save the edit again.'));
    }
    if (editBranchInFlightRef.current) {
      return refuse(t('app.editAlreadyInFlight', 'Another edit is still being applied. Try again in a moment.'));
    }
    const nextContent = (editedContent || '').trim();
    if (!nextContent) return refuse(t('app.editEmpty', 'The edited message is empty.'));
    if (nextContent === originalMessage.content) return { accepted: true };

    let lastUserIndex = -1;
    for (let i = chatMessages.length - 1; i >= 0; i--) {
      if (chatMessages[i]?.role === 'user') {
        lastUserIndex = i;
        break;
      }
    }

    const attachments = originalMessage._attachments || [];
    const messageAnchor = {
      messageId: numericMessageId(originalMessage),
      clientMessageId: String(originalMessage.client_message_id || '').trim(),
    };
    if (!messageAnchor.messageId && !messageAnchor.clientMessageId) {
      return refuse(t('app.editAnchorMissing', 'This message cannot be edited: it has no stored identifier.'));
    }
    if (messageIndex === lastUserIndex) {
      return sendEditedMessage({
        overrideText: nextContent,
        overrideAttachments: attachments,
        supersedeFrom: messageAnchor,
        replaceUiIndex: messageIndex,
        // The old answer is already superseded when a run is refused, so the
        // composer of this chat is where the edit can still be sent from.
        returnTextOnConflict: true,
      });
    }

    const sourceChat = chats.find(c => c.id === activeChatId);
    const newChatName = `${sourceChat?.name || 'Chat'} (edited)`;
    editBranchInFlightRef.current = true;
    let data;
    try {
      const res = await fetch('/api/chat/branch-edit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_name: activeProject.name,
          source_chat_id: activeChatId,
          new_chat_name: newChatName,
          message_id: messageAnchor.messageId ?? undefined,
          client_message_id: messageAnchor.clientMessageId || '',
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || t('app.editedBranchCreateFailed'));
      }
      data = await res.json();
    } catch (err) {
      editBranchInFlightRef.current = false;
      return refuse(t('app.editedBranchCreateError', { error: err.message }));
    }

    const branchHistory = data.history || [];
    const newChatId = data.new_chat_id;
    const branchName = data.name || newChatName;
    setActiveChatId(newChatId);
    setActiveProject(prev => prev ? { ...prev, current_chat_id: newChatId } : null);
    setChats(prev => [{ id: newChatId, name: branchName }, ...prev]);
    // The branched chat has its own (shorter) history: the source chat's
    // measured window does not describe it.
    setChatContextUsage(null);
    setChatMessages(branchHistory);
    try {
      const result = await sendEditedMessage({
        overrideText: nextContent,
        overrideAttachments: attachments,
        chatIdOverride: newChatId,
        baseMessages: branchHistory,
        returnTextOnConflict: true,
      });
      if (!result.accepted) {
        // The branch exists and holds exactly the conversation before the
        // edited message, so sending from its composer is the edit. The text
        // goes there instead of staying in an editor that belongs to the
        // chat that was just left.
        setChatInput(prev => (prev.trim() ? prev : nextContent));
        setPendingAttachments(prev => (prev.length ? prev : attachments));
        addLog('error', t('app.editBranchNotSent', 'The branch "{{name}}" was created, but the edited message was not sent ({{error}}). It is in the message box: send it from there.', { name: branchName, error: result.error }));
      }
    } finally {
      editBranchInFlightRef.current = false;
      fetch(`/api/chat/list?project_name=${encodeURIComponent(activeProject.name)}&t=${Date.now()}`)
        .then(r => r.ok ? r.json() : null)
        .then(d => { if (d?.chats) setChats(d.chats); })
        .catch(() => { });
    }
    // Accepted either way: the editor's chat was left, and the text now lives
    // in the branch (sent, or waiting in its composer).
    return { accepted: true };
  };

  const handleGenerateResponseForUserMessage = async (messageIndex, message) => {
    if (!activeProject || isTurnBusy() || !message || message.role !== 'user') return;
    const content = message.content === '📎 Attachment' ? '' : (message.content || '');
    const attachments = message._attachments || [];
    if (!content.trim() && attachments.length === 0) return;
    if (message.client_message_id) {
      // The stored row is reused (append_message dedupes on client_message_id),
      // so the user turn is answered again without being rewritten.
      await handleSendMessage(null, null, {
        overrideText: content,
        overrideAttachments: attachments,
        clientMessageId: message.client_message_id,
        skipUserMessageAppend: true,
      });
      return;
    }
    const messageId = numericMessageId(message);
    if (messageId === null) {
      addLog('error', t('app.retryAnchorMissing', 'This message cannot be retried: it has no stored identifier.'));
      return;
    }
    await handleSendMessage(null, null, {
      overrideText: content,
      overrideAttachments: attachments,
      supersedeFrom: { messageId, clientMessageId: '' },
      replaceUiIndex: messageIndex,
    });
  };

  /**
   * Answer a plan review. Unlike `sendConfirmResponse` this clears the request
   * only once the backend has taken the answer: the window is the sole place
   * the decision can be made, and it carries the user's edits to the plan, so
   * hiding it optimistically would throw those away on a failed POST. The
   * window disables its own buttons while this is in flight.
   */
  const sendPlanResponse = async (value) => {
    const current = planRequest;
    if (!current) return;
    addLog('info', t('app.confirmationValue', { prompt: current.prompt, value }));
    try {
      const res = await fetch('/api/gilzabots/input_response', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: current.id, value }) });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(text || `HTTP ${res.status}`);
      }
      setPlanRequest(clearAnsweredRequest(current.id));
    } catch (err) {
      addLog('error', t('app.confirmationSendError', { error: err.message }));
      addProblem({ tool: t('app.agentTool', 'Agent'), message: t('app.confirmationRejectedByBackend', { error: err.message }), severity: 'error' });
    }
  };

  // Like plan review, an ask window is the sole route to a backend future. Keep
  // it visible until the backend accepts the answer so a failed POST cannot
  // turn into an invisible 24-hour wait.
  const sendAskResponse = async (value) => {
    const current = askRequest;
    if (!current) return;
    addLog('info', t('app.confirmationValue', { prompt: current.prompt, value }));
    try {
      const res = await fetch('/api/gilzabots/input_response', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: current.id, value }),
      });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(text || `HTTP ${res.status}`);
      }
      // The answer releases the agent at once, so its next question can reach
      // this window before this POST returns; clear only the answered one.
      setAskRequest(clearAnsweredRequest(current.id));
    } catch (err) {
      addLog('error', t('app.confirmationSendError', { error: err.message }));
      addProblem({ tool: t('app.agentTool', 'Agent'), message: t('app.confirmationRejectedByBackend', { error: err.message }), severity: 'error' });
    }
  };

  const sendConfirmResponse = async (value) => {
    if (!confirmRequest) return;
    const currentRequest = confirmRequest;
    const { id, prompt, isSlashCommand, callback } = currentRequest;
    setConfirmRequest(null);

    if (callback) {
      callback(value);
      return;
    }

    addLog('info', t('app.confirmationValue', { prompt, value }));
    try {
      if (isSlashCommand) {
        const res = await fetch('/api/gilzabots/slash-command/continue', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, value }) });
        if (!res.ok) {
          const text = await res.text().catch(() => '');
          throw new Error(text || `HTTP ${res.status}`);
        }
        const result = await res.json();
        if (result.status === 'done') {
          setChatMessages(prev => [...prev, { role: 'assistant', content: (result.messages || []).join('\n') || 'Comando executado.', timestamp: new Date().toISOString() }]);
          fetchFiles();
          fetchGitStatus();
        }
      } else {
        const res = await fetch('/api/gilzabots/input_response', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, value }) });
        if (!res.ok) {
          const text = await res.text().catch(() => '');
          throw new Error(text || `HTTP ${res.status}`);
        }
      }
    } catch (err) {
      setConfirmRequest(currentRequest);
      addLog('error', t('app.confirmationSendError', { error: err.message }));
      addProblem({ tool: t('app.agentTool', 'Agent'), message: t('app.confirmationRejectedByBackend', { error: err.message }), severity: 'error' });
    }
  };

  // ── Editor mount ──────────────────────────────────────────────────────────
  const handleEditorDidMount = (editor, monaco) => {
    editorRef.current = editor;
    if (monaco) monacoRef.current = monaco;

    editor.onDidChangeModelContent(() => {
      lastEditorInputAtRef.current = Date.now();
      fileContentRef.current = editor.getValue();
    });

    editor.onKeyDown((e) => {
      const ev = e.browserEvent;
      const isCtrl = ev.ctrlKey || ev.metaKey;
      if (isCtrl && (ev.key === '+' || ev.key === '=' || ev.code === 'Equal' || ev.code === 'NumpadAdd')) {
        ev.preventDefault(); ev.stopPropagation();
        setEditorFontSize(prev => { const v = Math.min(30, prev + 1); setPreference('editorFontSize', v); return v; });
      } else if (isCtrl && (ev.key === '-' || ev.code === 'Minus' || ev.code === 'NumpadSubtract')) {
        ev.preventDefault(); ev.stopPropagation();
        setEditorFontSize(prev => { const v = Math.max(10, prev - 1); setPreference('editorFontSize', v); return v; });
      }
    });

    // Ctrl+S is registered as a Monaco command rather than matched on
    // browserEvent.key: the keybinding service resolves the physical key, so the
    // shortcut survives runtimes where the character cannot be resolved. This is
    // how every other editor shortcut (Ctrl+J, Ctrl+F, Ctrl+L) is already bound.
    if (monaco) {
      editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
        saveFileRef.current?.();
      });
    }
  };

  // ── Inline submit — called by InlinePromptOverlay ─────────────────────────
  /**
   * Builds a context-prefixed prompt and sends it to the agent.
   * Also stores the current selection range so that auto-replace can fire
   * when the agent returns the modified code.
   */
  const handleInlineSubmit = async (instruction) => {
    if (!inlinePrompt || !activeProject) return;
    const { startLine, endLine, selectedText, mode } = inlinePrompt;
    const startColumn = inlinePrompt.startColumn ?? 1;
    const endColumn = inlinePrompt.endColumn ?? inlinePrompt.cursorCol ?? 1;

    const editorModel = editorRef.current?.getModel?.();
    const currentEditorContent = editorModel?.getValue?.() ?? fileContent ?? '';
    const cursorColumn = inlinePrompt.cursorCol ?? startColumn;
    const hasSelection = selectedText && selectedText.trim().length > 0;

    let systemPrompt, fullPrompt;
    if (mode === 'createIllustration') {
      systemPrompt = "You are a professional designer and SVG illustrator. " +
        "Your task is to generate a beautiful, clean, modern, and valid SVG illustration that visually represents the text selected by the user. " +
        "CRITICAL:\n" +
        "1. Return ONLY the raw SVG content.\n" +
        "2. The SVG MUST be valid, self-contained, and have appropriate viewBox, width, and height attributes so it renders beautifully.\n" +
        "3. Do NOT wrap the SVG in markdown code blocks like ```xml or ```svg. Return the raw SVG directly starting with <svg> and ending with </svg>.\n" +
        "4. Use a modern, appealing color scheme (like a dark theme or clean blue/purple/grey professional gradients).\n" +
        "5. Do not include any explanations, greetings, or text other than the SVG itself.\n" +
        "6. Do NOT write comments before or after the SVG.";

      fullPrompt = `Selected Text to Illustrate:\n"""\n${selectedText}\n"""\n\nUser Instruction:\n${instruction}`;
    } else {
      let verb;
      if (mode === 'refine') verb = instruction;
      else if (mode === 'fix') verb = instruction;
      else verb = instruction;

      const ext = (selectedFile || '').split('.').pop() || '';
      const fence = ext ? `\`\`\`${ext}` : '\`\`\`';

      if (hasSelection && mode !== 'generate') {
        fullPrompt = `Task: ${verb}\n\nFile Context:\n${fence}\n${currentEditorContent}\n\`\`\`\n\nTarget Selection to Replace:\n${fence}\n${selectedText}\n\`\`\``;
      } else {
        const cursorOffset = editorModel?.getOffsetAt?.({ lineNumber: startLine, column: cursorColumn });
        const beforeCursor = currentEditorContent.slice(0, cursorOffset ?? 0);
        const afterCursor = currentEditorContent.slice(cursorOffset ?? 0);
        fullPrompt = `Task: ${instruction}\n\nThe following is read-only context. The <cursor> tag is application metadata, not source content. It marks the exact insertion point; do not count lines, locate a position, reconcile line numbers, or reproduce the file. Generate only the requested snippet for that point.\n\n<file_context file=${JSON.stringify(selectedFile || '')}>\n<before_cursor>\n${beforeCursor}\n</before_cursor>\n<cursor line="${startLine}" column="${cursorColumn}" />\n<after_cursor>\n${afterCursor}\n</after_cursor>\n</file_context>`;
      }

      systemPrompt = "You are a precise inline content editor for any selected content: text, Markdown, LaTeX, code, config files, JSON, YAML, tables, or structured data. " +
        "CRITICAL: Do NOT create, modify, or save files. " +
        "Tools and function calls are unavailable for this task; do not request or attempt them. " +
        "All necessary context is already included in the user prompt; complete the task by returning the replacement content directly. " +
        "Return ONLY the final replacement source, with no Markdown fence or other envelope around it, even when it is code or LaTeX. " +
        "The one exception: when the replacement itself contains a fenced code block (lines starting with ```), wrap the entire replacement in an outer four-backtick fence labelled content, so the application can tell the content's fences from the envelope. " +
        "Do NOT include greetings, explanations, comments, summaries, tool calls, or any surrounding text. " +
        "Preserve the original language, format, structure, and intent unless the requested edit requires changes. " +
        "For insertion tasks, the host application owns the cursor position. The <cursor> tag is authoritative metadata: use surrounding content only for context, and never count lines, choose another insertion point, or explain your placement. " +
        "Be objective, concise, and direct. " +
        // The examples show exactly what to return, and follow the same rule as
        // the instructions. They used to wrap every replacement in a ````content
        // fence right after a sentence forbidding fences, and a model imitates
        // examples over instructions. The envelope still exists for the one case
        // that needs it: `_normalize_inline_fenced_replacement` extracts a fenced
        // block, so a bare reply containing a code block would lose the text
        // around it, while an outer ````content fence keeps it whole.
        "Examples (the whole output is the text after the arrow):\n" +
        "Original: 'The system is good.' → The system is functional.\n" +
        "Original: 'flowchart LR\\n  A --> B' → flowchart LR\\n  A[Start] --> B[End]\n" +
        "Original: '$$ E = m c ^ 2 $$' → $$\\nE = mc^2\\n$$\n" +
        "Original: 'Run:\\n```\\nnpm test\\n```' → ````content\\nRun the test suite:\\n```bash\\nnpm test\\n```\\n````\n" +
        "Your entire output must contain only the replacement source.";
    }

    setChatInput('');
    setIsInlineRunning(true);
    addLog('info', t('app.inlineEditStarting', { instruction }));

    try {
      const res = await fetch('/api/gilzabots/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          command: 'run',
          agent: 'inline_editor',
          model: activeProject.model,
          project_name: activeProject.name,
          project_path: activeProject.project_path,
          system_prompt: systemPrompt,
          tools: [],
          prompt: fullPrompt,
          inline_response_contract: mode === 'createIllustration' ? undefined : 'replacement_only',
          current_file: selectedFile || '',
          open_files: openFiles,
          editor_content: currentEditorContent,
          selected_text: selectedText || '',
          lang: i18n.language || 'en',
          // Inline runs preserve all user-selected parameters, except streaming:
          // this agent returns one final replacement and must not emit partial output.
          model_params: { ...ephemeralParams, stream: false }
        }),
      });

      const runConflict = await readRunConflict(res);
      if (runConflict) {
        reportRunConflict(runConflict);
        return;
      }

      if (!res.body) {
        addLog('error', t('app.streamUnsupportedBackground'));
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let agentResponse = '';
      let inlineErrorMessage = '';
      const inlineAgentTool = t('app.inlineAgentTool', 'Inline agent');

      const reportInlineFailure = (message, tool = inlineAgentTool, severity = 'error') => {
        const failureMessage = message || t('app.inlineAgentNoOutput', 'Inline agent finished without producing content.');
        if (!inlineErrorMessage) {
          inlineErrorMessage = failureMessage;
          setAlertMessage(t('app.inlineAgentErrorAlert', 'GilzaBots could not generate inline content: {{message}}', { message: failureMessage }));
        }
        addProblem({ tool, message: failureMessage, severity });
        setActiveBottomTab('problems');
        setIsTerminalCollapsed(false);
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const data = JSON.parse(line);
            if (data.event === 'agent_response' && data.response) {
              agentResponse = data.response;
            } else if (data.event === 'error') {
              addLog('error', t('app.inlineAgentErrorLog', { message: data.message }));
              reportInlineFailure(data.message);
            } else if (data.event === 'problem') {
              addLog('error', t('app.toolProblem', { tool: data.tool || inlineAgentTool, message: data.message }));
              reportInlineFailure(data.message, data.tool || inlineAgentTool, data.severity || 'error');
            } else if (data.event === 'thought' || data.event === 'reflection' || data.event === 'stream_chunk') {
              // Inline generation has no live-output contract. Only its final
              // agent_response may be applied to the editor.
              continue;
            } else if (data.event === 'tool_call') {
              addLog('tool_call', t('app.inlineCallingTool', { tool: data.tool, arguments: JSON.stringify(data.arguments) }), data.agent);
            } else if (data.event === 'tool_result') {
              addLog('tool_result', t('app.inlineToolResult', { tool: data.tool }), data.agent);
            }
          } catch (e) {
            // ignore non-json logs
          }
        }
      }

      if (buffer.trim()) {
        try {
          const data = JSON.parse(buffer);
          if (data.event === 'agent_response' && data.response) agentResponse = data.response;
          else if (data.event === 'error') {
            addLog('error', t('app.inlineAgentErrorLog', { message: data.message }));
            reportInlineFailure(data.message);
          } else if (data.event === 'problem') {
            addLog('error', t('app.toolProblem', { tool: data.tool || inlineAgentTool, message: data.message }));
            reportInlineFailure(data.message, data.tool || inlineAgentTool, data.severity || 'error');
          }
        } catch (e) { }
      }

      if (!agentResponse) {
        reportInlineFailure(inlineErrorMessage || t('app.inlineAgentNoOutput', 'Inline agent finished without producing content.'));
        return;
      }

      if (agentResponse && editorRef.current && monacoRef.current) {
        let rawResponse = agentResponse.trim();
        rawResponse = rawResponse.replace(/\{"result"\s*:\s*"[^"]*"\}\s*/g, '');
        rawResponse = rawResponse.replace(/\{"error"\s*:\s*"[^"]*"\}\s*/g, '');

        try {
          const parsed = JSON.parse(rawResponse);
          if (parsed && parsed.name && parsed.arguments && parsed.arguments.content) {
            rawResponse = parsed.arguments.content;
          }
        } catch (e) {
          const contentMatch = rawResponse.match(/"content"\s*:\s*"([\s\S]*)/);
          if (contentMatch) {
            let str = contentMatch[1];
            str = str.replace(/\"\s*\}?\s*\}?\s*$/, '');
            str = str.replace(/\\n/g, '\n').replace(/\\"/g, '"').replace(/\\\\/g, '\\').replace(/\\t/g, '\t');
            rawResponse = str;
          }
        }

        // ── Strip thought/reasoning blocks ────────────────────────────────────
        // Handles both 4-backtick (new) and 3-backtick (legacy) thought fences.
        // No `m` flag: ^ anchors to the START of the full string, not any line.
        // Uses greedy [\s\S]* to consume the entire block even if it contains
        // nested ``` inside — avoids the non-greedy stop-at-first-``` bug.
        rawResponse = rawResponse
          .replace(/^````(?:thought|reasoning)[\s\S]*?````\s*/, '')
          .replace(/^```(?:thought|reasoning)[\s\S]*?```\s*/, '')
          .trim();

        // ── Depth-aware code block extractor ─────────────────────────────────
        // Correctly handles content that contains nested ``` fences (mermaid,
        // latex, code examples, etc.) by tracking fence depth instead of using
        // a simple non-greedy regex that stops at the first ``` found.
        function extractOutermostCodeBlock(text) {
          const lines = text.split('\n');
          let outerFence = null;   // e.g. '````' or '```'
          let depth = 0;
          const bodyLines = [];
          let insideOuter = false;

          for (const line of lines) {
            // A fence line starts with optional spaces (≤3) then 3+ backticks/tildes
            const fm = line.match(/^( {0,3})(```+|~~~+)(\w*)/);

            if (!insideOuter) {
              if (fm) {
                outerFence = fm[2];  // the opening fence chars, e.g. '````'
                depth = 1;
                insideOuter = true;
                // Do NOT push this line — it's the opening fence delimiter
              }
              continue;
            }

            // Inside the outer block: track nested fences
            if (fm) {
              const thisFence = fm[2];
              const hasLabel = fm[3].length > 0;

              if (!hasLabel && thisFence.startsWith(outerFence)) {
                // Potential closing fence (same or more backticks, no language label)
                depth--;
                if (depth === 0) break; // real closing fence — stop
              } else if (hasLabel) {
                // Opening of a nested block (has a language label)
                depth++;
              }
              // A closing fence for a nested block (no label, fewer backticks):
              // depth-- only when it matches outerFence length, so inner ``` inside
              // a ```` outer are just regular lines.
            }

            bodyLines.push(line);
          }

          if (!insideOuter || depth !== 0) return null; // no valid block found
          // Strip trailing blank lines that were before the closing fence
          while (bodyLines.length > 0 && bodyLines[bodyLines.length - 1].trim() === '') {
            bodyLines.pop();
          }
          return bodyLines.join('\n');
        }

        if (mode === 'createIllustration') {
          let svgCode = rawResponse;

          // Try to extract content inside code blocks if present
          const svgMatch = /```(?:xml|svg)?\s*([\s\S]*?)```/i.exec(svgCode) || /````(?:xml|svg)?\s*([\s\S]*?)````/i.exec(svgCode);
          if (svgMatch) {
            svgCode = svgMatch[1].trim();
          } else {
            const tagMatch = /<svg[\s\S]*<\/svg>/i.exec(svgCode);
            if (tagMatch) {
              svgCode = tagMatch[0].trim();
            }
          }

          if (!svgCode.startsWith('<svg')) {
            const svgStart = svgCode.indexOf('<svg');
            if (svgStart !== -1) {
              svgCode = svgCode.substring(svgStart);
            }
          }

          if (!svgCode.includes('<svg') || !svgCode.includes('</svg>')) {
            throw new Error(t('app.invalidSvgReturned'));
          }

          const timestamp = Math.floor(Date.now() / 1000);
          const filename = `illustrations/illustration_${timestamp}.svg`;

          // ── Robust project-relative path normalizer ─────────────────────────
          // Handles all formats of selectedFile: relative ("d1/A.tex"),
          // backslash ("d1\\A.tex"), or absolute Windows ("C:\\Users\\...\\d1\\A.tex").
          // Returns a clean forward-slash relative path like "d1/A.tex".
          function toProjectRelativePath(filePath, projectPath) {
            if (!filePath) return '';
            let norm = filePath.replace(/\\/g, '/');

            // Strip project root prefix if selectedFile is absolute
            if (projectPath) {
              const projNorm = projectPath.replace(/\\/g, '/').replace(/\/+$/, '') + '/';
              // Case-insensitive comparison needed on Windows
              if (norm.toLowerCase().startsWith(projNorm.toLowerCase())) {
                norm = norm.substring(projNorm.length);
              }
            }

            // Remove any remaining drive letter (e.g., "C:/") or leading slashes
            norm = norm.replace(/^[A-Za-z]:\//, '').replace(/^\/+/, '');
            return norm;
          }

          const relSelectedFile = toProjectRelativePath(selectedFile, activeProject?.project_path);
          const dirSegments = relSelectedFile.split('/').filter(Boolean).slice(0, -1); // directory parts only
          const depth = dirSegments.length;
          const relPrefix = depth > 0 ? '../'.repeat(depth) : '';
          const relIllustrationPath = `${relPrefix}illustrations/illustration_${timestamp}`;

          addLog('info', t('app.savingSvg', { path: filename }));
          const writeRes = await fetch('/api/file/write', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              projectPath: activeProject.project_path,
              filePath: filename,
              content: svgCode,
            }),
          });
          if (!writeRes.ok) {
            const errData = await writeRes.json().catch(() => ({}));
            throw new Error(t('app.svgSaveFailed', { error: errData.error || writeRes.statusText }));
          }

          const model = editorRef.current.getModel();
          const endCol = model ? model.getLineMaxColumn(endLine) : 1;
          const range = new monacoRef.current.Range(startLine, 1, endLine, endCol);
          const fullLinesText = model.getValueInRange(range);

          const ext = (selectedFile || '').split('.').pop().toLowerCase();
          let refText = '';
          let adjustedEndLine = endLine;
          if (ext === 'tex') {
            // Ensure \usepackage{graphicx} is in the preamble
            const fullContent = model.getValue();
            if (!fullContent.includes('usepackage{graphicx}')) {
              const docIdx = fullContent.indexOf('\\begin{document}');
              if (docIdx !== -1) {
                const insertPos = model.getPositionAt(docIdx);
                editorRef.current.executeEdits('gilzabots_graphicx', [{
                  range: new monacoRef.current.Range(insertPos.lineNumber, 1, insertPos.lineNumber, 1),
                  text: '\\usepackage{graphicx}\n\n',
                  forceMoveMarkers: true,
                }]);
                // Shift endLine after preamble insertion
                adjustedEndLine += 2;
              }
            }
            refText = `\\begin{figure}[htbp]\n\\centering\n\\includegraphics[width=0.8\\textwidth]{${relIllustrationPath}.pdf}\n\\end{figure}`;
          } else if (ext === 'md' || ext === 'markdown') {
            // Markdown preview in GilzaBots resolves paths from the project root,
            // so always use root-relative path regardless of file depth
            refText = `![Ilustração](illustrations/illustration_${timestamp}.svg)`;
          } else if (ext === 'html' || ext === 'htm') {
            refText = `<img src="${relIllustrationPath}.svg" alt="Ilustração" />`;
          } else {
            refText = `${relIllustrationPath}.svg`;
          }

          // Re-read range after potential preamble insertion shift
          const updatedEndCol = model.getLineMaxColumn(adjustedEndLine);
          const updatedRange = new monacoRef.current.Range(startLine, 1, adjustedEndLine, updatedEndCol);
          const updatedText = model.getValueInRange(updatedRange);

          const separator = updatedText.endsWith('\n') ? '' : '\n';
          const codeToInsert = updatedText + separator + '\n' + refText + '\n';

          editorRef.current.executeEdits('gilzabots_inline', [{
            range: updatedRange,
            text: codeToInsert,
            forceMoveMarkers: true,
          }]);
          addLog('info', t('app.svgCreatedInserted'));
        } else {
          // ── Extraction pipeline ───────────────────────────────────────────────
          let codeToInsert;

          // 1) PRIMARY: 4-backtick outer fence (simple regex is safe because inner
          //    ``` cannot close a ```` fence — CommonMark spec §4.5).
          const match4Content = extractInlineReplacementBlock(rawResponse);
          const match4 = match4Content !== null ? [null, null, match4Content] : null;
          if (match4) {
            codeToInsert = match4[2];

            // 2) FALLBACK: depth-aware parser handles any 3-backtick fence whose body
            //    may contain nested ``` blocks (model ignored the 4-backtick instruction).
          } else {
            const extracted = extractOutermostCodeBlock(rawResponse);
            if (extracted !== null) {
              codeToInsert = extracted;

              // 3) LAST RESORT: strip leading/trailing fence markers manually so at
              //    least the raw fence delimiters don't end up in the editor.
            } else {
              codeToInsert = rawResponse
                .replace(/^````?\w*\n?/, '')  // strip opening fence line
                .replace(/\n?````?\s*$/, '')  // strip closing fence line
                .trim();
            }
          }

          if (mode === 'generate') {
            const range = new monacoRef.current.Range(startLine, inlinePrompt.cursorCol, startLine, inlinePrompt.cursorCol);
            const model = editorRef.current.getModel();
            editorRef.current.executeEdits('gilzabots_inline', [{
              range: range,
              text: normalizeInlineReplacementSpacing(codeToInsert, '', model?.getEOL?.() || '\n'),
              forceMoveMarkers: true,
            }]);
            addLog('info', t('app.inlineGenerationApplied'));
          } else if (hasSelection) {
            const model = editorRef.current.getModel();
            const range = new monacoRef.current.Range(startLine, startColumn, endLine, endColumn);
            const originalText = model ? model.getValueInRange(range) : selectedText;
            const normalizedCode = normalizeInlineReplacementSpacing(
              codeToInsert,
              originalText,
              model?.getEOL?.() || '\n',
            );
            editorRef.current.executeEdits('gilzabots_inline', [{
              range: range,
              text: normalizedCode,
              forceMoveMarkers: true,
            }]);
            addLog('info', t('app.inlineEditApplied'));
          }
        }
      }

    } catch (err) {
      addLog('error', t('app.inlineEditFailed', { error: err.message }));
      addProblem({ tool: t('app.inlineAgentTool', 'Inline agent'), message: err.message, severity: 'error' });
      setActiveBottomTab('problems');
      setIsTerminalCollapsed(false);
      setAlertMessage(t('app.inlineAgentErrorAlert', 'GilzaBots could not generate inline content: {{message}}', { message: err.message }));
    } finally {
      setInlinePrompt(null);
      setIsInlineRunning(false);
      fetchFiles();
      fetchProblems();
    }
  };

  /**
   * Variant of handleSendMessage that accepts an explicit prompt string.
   * Used by handleInlineSubmit to bypass the chatInput state timing.
   * @param {string} userText   - The full prompt to send to the agent.
   * @param {string} [capturedSelectedText] - The text captured at submit time
   *   (avoids re-reading Monaco selection which may be gone by now).
   */
  const handleSendMessageWithPrompt = async (userText, capturedSelectedText) => {
    if (!userText.trim() || !activeProject || isTurnBusy()) return;
    localTurnRef.current = true;
    setChatInput('');
    const promptMessage = { role: 'user', content: userText, timestamp: new Date().toISOString() };
    setChatMessages(prev => [...prev, promptMessage]);
    setIsAgentRunning(true);
    setIsInterruptPending(false);
    setAgentStepInfo({ step: 0, maxSteps: null });
    setProblems([]);
    // Cleared only once the turn is accepted -- these buffers hold the only copy
    // of a partial answer from a turn that died mid-stream. Same reason as in
    // handleSendMessage.
    addLog('info', t('app.starting', { text: `${userText.slice(0, 80)}${userText.length > 80 ? '...' : ''}` }))

    // Use the captured text if provided; otherwise try reading Monaco
    let selectedText = capturedSelectedText ?? '';
    if (!selectedText && editorRef.current) {
      try {
        const model = editorRef.current.getModel();
        const sel = editorRef.current.getSelection();
        if (model && sel) selectedText = model.getValueInRange(sel);
      } catch (e) { }
    }

    bindRunChat(activeChatId);
    try {
      const res = await fetch('/api/gilzabots/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'run', agent: 'chat_orchestrator', prompt: userText, project_name: activeProject.name, project_path: activeProject.project_path, model: activeProject.model, max_heartbeats: resolveEffectiveMaxHeartbeats(), current_file: selectedFile || '', open_files: openFiles, editor_content: fileContent || '', selected_text: selectedText || '', lang: i18n.language || 'en', chat_id: activeChatId, model_params: ephemeralParams }),
      });
      const runConflict = await readRunConflict(res);
      if (runConflict) {
        setChatMessages(prev => prev.filter(m => m !== promptMessage));
        reportRunConflict(runConflict);
        return;
      }
      chatThoughtTailRef.current = createThoughtTail();
      setChatThoughtTail(chatThoughtTailRef.current);
      chatResponseStreamRef.current = '';
      setChatResponseStream('');
      if (!res.body) { addLog('error', t('app.streamUnsupportedBackend')); setIsAgentRunning(false); return; }
      await consumeLinesByFrame(res.body.getReader(), (line) => {
        try { handleAgentEvent(JSON.parse(line)); } catch (e) { addLog('stdout', line); }
      });
    } catch (err) {
      addLog('error', t('app.executionFailed', { error: err.message }));
      setChatMessages(prev => [...prev, { role: 'assistant', content: `🔴 Falha na execução: ${err.message}`, is_error: true, timestamp: new Date().toISOString() }]);
    } finally { localTurnRef.current = false; bindRunChat(''); setIsAgentRunning(false); setIsInterruptPending(false); fetchFiles(); fetchProblems(); }
  };

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="vscode-app">
      <input
        ref={importFileInputRef}
        type="file"
        multiple
        style={{ display: 'none' }}
        onChange={handleImportFileSelected}
      />
      <div className="vscode-main">

        {/* Activity Bar */}
        <ActivityBar
          activeSidebarTab={activeSidebarTab}
          setActiveSidebarTab={(tab) => {
            setActiveSidebarTab(tab);
            if (isEditorMaximized) setIsEditorMaximized(false);
          }}
          isChatVisible={isChatVisible}
          setIsChatVisible={(val) => {
            setIsChatVisible(val);
            if (isEditorMaximized) setIsEditorMaximized(false);
          }}
          gitChangesCount={gitChanges.length}
          onOpenSettings={() => setIsSettingsOpen(true)}
          onEditModels={() => setShowEditModelsModal(true)}
          onOpenHardware={() => setIsHardwareModalOpen(true)}
          onOpenMonitors={() => setIsMonitorsModalOpen(true)}
          onOpenConnectors={() => setIsConnectorsModalOpen(true)}
          monitorsUnseenEvents={monitorUnseenEvents}
          onOpenAssetStore={() => setIsAssetStoreOpen(true)}
          onOpenCloudSync={() => setIsCloudSyncOpen(true)}
          cloudEnabled={!!cloudStatus?.settings?.enabled}
          onOpenTutorial={handleOpenTutorial}
          layoutMode={layoutMode}
          setLayoutMode={setLayoutMode}
          isChatSidebarVisible={isChatSidebarVisible}
          setIsChatSidebarVisible={(val) => {
            setIsChatSidebarVisible(val);
            if (isEditorMaximized) setIsEditorMaximized(false);
          }}
          hasOpenDocument={!!selectedFile}
          isTerminalCollapsed={isTerminalCollapsed}
          setIsTerminalCollapsed={setIsTerminalCollapsed}
          setActiveBottomTab={setActiveBottomTab}
          onOpenProjectSettings={(e) => { if (activeProject) openEditModal(e, activeProject); }}
          hasActiveProject={!!activeProject}
        />

        {/* Left Sidebar */}
        {!isEditorMaximized && activeSidebarTab && isEditorLayout && (
          <aside className="vscode-sidebar" style={{ width: `${sidebarWidth}px` }}>
            {activeSidebarTab === 'explorer' ? (
              <ExplorerSidebar
                projects={projects}
                activeProject={activeProject}
                handleSelectProject={handleSelectProject}
                onNewProject={() => { setShowNewProjectModal(true); setNewProjModelParams({}); }}
                onImportProject={() => { setImportError(''); openDirPicker('import', '~'); }}
                onDownloadFromCloud={openCloudDownload}
                importError={importError}
                onClearImportError={() => setImportError('')}
                files={files}
                selectedFile={selectedFile}
                selectedNodes={selectedNodes}
                unsavedFiles={unsavedFiles}
                agentChangedFiles={agentChangedFiles}
                handleNodeSelect={handleNodeSelect}
                handleFileSelect={handleFileSelect}
                handleNodeContextMenu={handleNodeContextMenu}
                handleWorkspaceContextMenu={handleWorkspaceContextMenu}
                draggedNode={draggedNode}
                setDraggedNode={setDraggedNode}
                dragOverPath={dragOverPath}
                setDragOverPath={setDragOverPath}
                handleMoveNode={handleMoveNode}
                fetchFiles={fetchFiles}
                showHiddenWorkspaceFiles={showHiddenWorkspaceFiles}
                onShowHiddenWorkspaceFilesChange={handleShowHiddenWorkspaceFilesChange}
                openEditModal={openEditModal}
                handleDeleteProject={handleDeleteProject}
                renamingNodePath={renamingNodePath}
                setRenamingNodePath={setRenamingNodePath}
                executeRenameNode={executeRenameNode}
                cloudFileStates={cloudFileStates}
                pendingCreate={pendingCreate}
                onInlineCreateSubmit={submitInlineCreate}
                onInlineCreateCancel={cancelInlineCreate}
              />
            ) : (
              <GitSidebar
                activeProject={activeProject}
                gitChanges={gitChanges}
                fetchGitStatus={fetchGitStatus}
                commitMessage={commitMessage}
                setCommitMessage={setCommitMessage}
                isCommitting={isCommitting}
                handleGitCommit={handleGitCommit}
                onStageFile={handleStageFile}
                onStageAllFiles={handleStageAllFiles}
                onUnstageFile={handleUnstageFile}
                onDiscardFile={handleDiscardFile}
                useShadowGit={useShadowGit}
                setUseShadowGit={setUseShadowGit}
                gitRootPath={currentGitRootPath}
                onPickGitRoot={() => openDirPicker('git-root', currentGitRootPath || activeProject?.project_path || '~')}
                onClearGitRoot={() => updateActiveGitRoot('')}
                checkpointRevision={checkpointRevision}
              />
            )}
          </aside>
        )}

        {/* Chat Sidebar (Only in Chat Mode) */}
        {!isEditorMaximized && isChatLayout && isChatSidebarVisible && (
          <aside className="vscode-sidebar" style={{ width: `${sidebarWidth}px`, display: 'flex', flexDirection: 'column' }}>
            <div className="vscode-chat-sidebar-history-pane">
              <ChatSidebar
                chats={chats}
                activeChatId={activeChatId}
                setActiveChatId={setActiveChatId}
                setChats={setChats}
                activeProject={activeProject}
                setChatMessages={setChatMessages}
                onSwitchChat={handleSwitchChat}
              />
            </div>

            <div className="vscode-chat-sidebar-divider" />

            <div className="vscode-chat-sidebar-explorer-pane">
              <ExplorerSidebar
                projects={projects}
                activeProject={activeProject}
                handleSelectProject={handleSelectProject}
                onNewProject={() => { setShowNewProjectModal(true); setNewProjModelParams({}); }}
                onImportProject={() => { setImportError(''); openDirPicker('import', '~'); }}
                onDownloadFromCloud={openCloudDownload}
                importError={importError}
                onClearImportError={() => setImportError('')}
                files={files}
                selectedFile={selectedFile}
                selectedNodes={selectedNodes}
                unsavedFiles={unsavedFiles}
                agentChangedFiles={agentChangedFiles}
                handleNodeSelect={handleNodeSelect}
                handleFileSelect={handleFileSelect}
                handleNodeContextMenu={handleNodeContextMenu}
                handleWorkspaceContextMenu={handleWorkspaceContextMenu}
                draggedNode={draggedNode}
                setDraggedNode={setDraggedNode}
                dragOverPath={dragOverPath}
                setDragOverPath={setDragOverPath}
                handleMoveNode={handleMoveNode}
                fetchFiles={fetchFiles}
                openEditModal={openEditModal}
                handleDeleteProject={handleDeleteProject}
                renamingNodePath={renamingNodePath}
                setRenamingNodePath={setRenamingNodePath}
                executeRenameNode={executeRenameNode}
                cloudFileStates={cloudFileStates}
                pendingCreate={pendingCreate}
                onInlineCreateSubmit={submitInlineCreate}
                onInlineCreateCancel={cancelInlineCreate}
              />
            </div>
          </aside>
        )}

        {/* Left resize handle */}
        {!isEditorMaximized && ((activeSidebarTab && isEditorLayout) || (isChatLayout && isChatSidebarVisible)) && (
          <div className="vscode-resizer-horizontal" onMouseDown={(e) => startResizing(e, 'left')} />
        )}

        {/* Center — Editor + Bottom Panel */}
        <main
          className={`vscode-editor-panel ${layoutMode === 'chat-bottom' ? 'vscode-chat-bottom-layout' : ''} ${isStudioLayout ? 'vscode-studio-layout' : ''}`}
          style={{
            flex: layoutMode === 'chat' ? 0 : 1,
            display: layoutMode === 'chat' ? 'none' : isStudioLayout ? 'grid' : 'flex',
            // Inline, because the flex/grid switch above is inline too and a
            // class-level template would lose to it.
            ...(isStudioLayout ? { gridTemplateColumns: studioGrid.gridTemplateColumns, gridTemplateRows: studioGrid.gridTemplateRows } : {}),
          }}
        >
          {!isBottomMaximized && layoutMode === 'review' && (
            <GitSidebar
              activeProject={activeProject}
              gitChanges={gitChanges}
              fetchGitStatus={fetchGitStatus}
              commitMessage={commitMessage}
              setCommitMessage={setCommitMessage}
              isCommitting={isCommitting}
              handleGitCommit={handleGitCommit}
              onStageFile={handleStageFile}
              onUnstageFile={handleUnstageFile}
              onDiscardFile={handleDiscardFile}
              useShadowGit={true}
              setUseShadowGit={() => {}}
              gitRootPath=""
              onPickGitRoot={() => {}}
              onClearGitRoot={() => {}}
              reviewMode
              onAfterRestore={handleCheckpointRestored}
              checkpointRevision={checkpointRevision}
            />
          )}

          {!isBottomMaximized && isEditorLayout && (
            <EditorPanel
              selectedFile={selectedFile}
              openFiles={openFiles}
              fileContent={fileContent}
              originalFileContents={originalFileContents}
              unsavedFiles={unsavedFiles}
              agentChangedFiles={agentChangedFiles}
              isSaving={isSaving}
              theme={theme}
              uiScale={uiScale}
              editorFontSize={editorFontSize}
              setEditorFontSize={setEditorFontSize}
              editorTabSize={editorTabSize}
              editorWordWrap={editorWordWrap}
              editorMinimap={editorMinimap}
              handleFileSelect={handleFileSelect}
              handleCloseTab={handleCloseTab}
              handleCloseOtherTabs={handleCloseOtherTabs}
              handleCloseAllTabs={handleCloseAllTabs}
              saveFile={saveFile}
              handleEditorDidMount={handleEditorDidMount}
              setFileContent={setFileContent}
              jumpToLine={jumpToLine}
              setJumpToLine={setJumpToLine}
              isMaximized={isEditorMaximized}
              onToggleMaximize={() => setIsEditorMaximized(!isEditorMaximized)}
              inlinePrompt={inlinePrompt}
              setInlinePrompt={setInlinePrompt}
              onInlineSubmit={handleInlineSubmit}
              isInlineRunning={isInlineRunning}
              onInlineCancel={() => {
                fetch('/api/gilzabots/interrupt', { method: 'POST' }).catch(() => { });
                setIsInlineRunning(false);
              }}
              onToggleTerminal={() => {
                if (isEditorMaximized) {
                  setIsEditorMaximized(false);
                  setIsTerminalCollapsed(false);
                  setActiveBottomTab('terminal');
                  setBottomPanelHeight(Math.floor(viewportPxToApp(window.innerHeight) / 2));
                  return;
                }
                if (isTerminalCollapsed) {
                  setIsTerminalCollapsed(false);
                  setActiveBottomTab('terminal');
                } else if (activeBottomTab === 'terminal') {
                  setIsTerminalCollapsed(true);
                } else {
                  setActiveBottomTab('terminal');
                }
              }}
              activeProject={activeProject}
              triggerCompileRequest={triggerCompileRequest}
              onCompileRequestHandled={(requestId) => {
                setTriggerCompileRequest(prev => (
                  prev?.id === requestId ? null : prev
                ));
              }}
              onLatexCompileError={handleLatexCompileError}
              onLatexCompileSuccess={handleLatexCompileSuccess}
              agentCompile={agentCompile}
              pdfRevealRequest={pdfRevealRequest}
              onFixLatexProblem={handleFixLatexProblem}
              onAskAboutPdf={handleAskAboutPdf}
              onAskAboutMarkdown={handleAskAboutMarkdown}
              speechSettingsSignal={speechSettingsSignal}
              isAgentRunning={isAgentRunning}
              onTextStatsChange={setEditorTextStats}
              openPreviewByDefault={isStudioLayout || isDocumentLayout}
            />
          )}

          {/* Studio drag handles. They are grid items like the panels, so they
              sit between the cells without wrapping anything in a container —
              which is what keeps the panels' tree positions untouched. */}
          {isStudioLayout && studioGrid.showRowResizer && (
            <div
              className="vscode-resizer-vertical vscode-studio-row-resizer"
              onMouseDown={(e) => startResizing(e, 'studio-bottom')}
            />
          )}
          {isStudioLayout && studioGrid.showColumnResizer && (
            <div
              className="vscode-resizer-horizontal vscode-studio-col-resizer"
              onMouseDown={(e) => startResizing(e, 'studio-chat')}
            />
          )}

          {((layoutMode === 'chat-bottom' && !isBottomMaximized)
            || (isStudioLayout && isChatVisible && !isEditorMaximized)) && (
            <ChatPanel
              isChatMode={!isStudioLayout}
              fillContainer={isStudioLayout}
              compactLayout={compactChatLayoutFor(layoutMode)}
              isTutorialChat={Boolean(tutorialChatId) && activeChatId === tutorialChatId}
              tutorialTopics={tutorialTopics}
              onTutorialTopic={handleTutorialTopic}
              chatMessages={chatMessages}
              chatInput={chatInput}
              setChatInput={setChatInput}
              chatInputFocusSignal={chatInputFocusSignal}
              speechSettingsSignal={speechSettingsSignal}
              isAgentRunning={isAgentRunning}
              isLiveTurnOnScreen={isLiveTurnOnScreen}
              agentStepInfo={agentStepInfo}
              providerWait={providerWait}
              heartbeatMode={heartbeatMode}
              setHeartbeatMode={setHeartbeatMode}
              queuedMessages={queuedMessages}
              onCancelQueuedMessage={handleCancelQueuedMessage}
              onCancelAllQueuedMessages={handleCancelAllQueuedMessages}
              isInterruptPending={isInterruptPending}
              chatThoughtStream={isLiveTurnOnScreen ? chatThoughtStream : ''}
              onShowThinking={showThinkingPanel}
              chatResponseStream={isLiveTurnOnScreen ? chatResponseStream : ''}
              chatContextUsage={chatContextUsage}
              setChatContextUsage={setChatContextUsage}
              activeProject={activeProject}
              isChatVisible={isChatVisible}
              setIsChatVisible={setIsChatVisible}
              chatWidth={chatWidth}
              handleSendMessage={handleSendMessage}
              onEditUserMessage={handleEditUserMessage}
              onGenerateResponseForUserMessage={handleGenerateResponseForUserMessage}
              handleInterruptAgent={handleInterruptAgent}
              onClearChat={() => {
                const currentProjName = activeProject ? (activeProject.project_name || activeProject.name) : '';
                setChatMessages(currentProjName ? [{ role: 'assistant', content: t('app.greeting', { projectName: currentProjName }) }] : []);
              }}
              chatEndRef={chatEndRef}
              webSearchConfig={webSearchConfig}
              setWebSearchConfig={setWebSearchConfig}
              activeChatId={activeChatId}
              setActiveChatId={setActiveChatId}
              chats={chats}
              setChats={setChats}
              setChatMessages={setChatMessages}
              onSwitchChat={handleSwitchChat}
              pendingAttachments={pendingAttachments}
              setPendingAttachments={setPendingAttachments}
              globalModels={globalModels}
              onRefreshModels={fetchGlobalModels}
              onEditModels={() => setShowEditModelsModal(true)}
              onModelChange={handleProjectModelChange}
            />
          )}
          <div
            className={isStudioLayout ? 'vscode-studio-terminal-cell' : undefined}
            style={{
              display: isStudioLayout
                ? (isEditorMaximized || isTerminalCollapsed ? 'none' : 'flex')
                : (isEditorMaximized || (layoutMode !== 'ide' && layoutMode !== 'chat-bottom') ? 'none' : 'contents'),
            }}
          >
            <BottomPanel
              activeBottomTab={activeBottomTab}
              setActiveBottomTab={setActiveBottomTab}
              isTerminalCollapsed={isTerminalCollapsed}
              setIsTerminalCollapsed={setIsTerminalCollapsed}
              terminalLogs={terminalLogs}
              thinkingFocus={thinkingFocus}
              onClearThinkingFocus={() => setThinkingFocus(null)}
              setTerminalLogs={setTerminalLogs}
              problems={problems}
              setProblems={setProblems}
              achievementsMemory={achievementsMemory}
              bottomPanelHeight={bottomPanelHeight}
              activeProject={activeProject}
              terminalRef={terminalRef}
              terminalInstanceRef={terminalInstanceRef}
              logEndRef={logEndRef}
              startResizing={startResizing}
              isBottomMaximized={isBottomMaximized}
              onToggleMaximizeBottom={() => setIsBottomMaximized(!isBottomMaximized)}
              theme={theme}
              uiScale={uiScale}
              fillContainer={isStudioLayout}
            />
          </div>
        </main>

        {/* Right resize handle */}
        {!isEditorMaximized && isChatVisible && layoutMode === 'ide' && (
          <div className="vscode-resizer-horizontal" onMouseDown={(e) => startResizing(e, 'right')} />
        )}

        {/* Chat Panel */}
        {(!isEditorMaximized && layoutMode !== 'review' && layoutMode !== 'chat-bottom' && !isDocumentLayout && !isStudioLayout && (isChatVisible || layoutMode === 'chat')) && (
          <>
            <ChatPanel
              isChatMode={layoutMode === 'chat'}
              isTutorialChat={Boolean(tutorialChatId) && activeChatId === tutorialChatId}
              tutorialTopics={tutorialTopics}
              onTutorialTopic={handleTutorialTopic}
              chatMessages={chatMessages}
              chatInput={chatInput}
              setChatInput={setChatInput}
              chatInputFocusSignal={chatInputFocusSignal}
              speechSettingsSignal={speechSettingsSignal}
              isAgentRunning={isAgentRunning}
              isLiveTurnOnScreen={isLiveTurnOnScreen}
              agentStepInfo={agentStepInfo}
              providerWait={providerWait}
              heartbeatMode={heartbeatMode}
              setHeartbeatMode={setHeartbeatMode}
              queuedMessages={queuedMessages}
              onCancelQueuedMessage={handleCancelQueuedMessage}
              onCancelAllQueuedMessages={handleCancelAllQueuedMessages}
              isInterruptPending={isInterruptPending}
              chatThoughtStream={isLiveTurnOnScreen ? chatThoughtStream : ''}
              onShowThinking={showThinkingPanel}
              chatResponseStream={isLiveTurnOnScreen ? chatResponseStream : ''}
              chatContextUsage={chatContextUsage}
              setChatContextUsage={setChatContextUsage}
              activeProject={activeProject}
              isChatVisible={isChatVisible}
              setIsChatVisible={setIsChatVisible}
              chatWidth={chatWidth}
              handleSendMessage={handleSendMessage}
              onEditUserMessage={handleEditUserMessage}
              onGenerateResponseForUserMessage={handleGenerateResponseForUserMessage}
              handleInterruptAgent={handleInterruptAgent}
              onClearChat={() => {
                const currentProjName = activeProject ? (activeProject.project_name || activeProject.name) : '';
                setChatMessages(currentProjName ? [
                  { role: 'assistant', content: t('app.greeting', { projectName: currentProjName }) }
                ] : []);
              }}
              chatEndRef={chatEndRef}
              webSearchConfig={webSearchConfig}
              setWebSearchConfig={setWebSearchConfig}
              activeChatId={activeChatId}
              setActiveChatId={setActiveChatId}
              chats={chats}
              setChats={setChats}
              setChatMessages={setChatMessages}
              onSwitchChat={handleSwitchChat}
              pendingAttachments={pendingAttachments}
              setPendingAttachments={setPendingAttachments}
              globalModels={globalModels}
              onRefreshModels={fetchGlobalModels}
              onEditModels={() => setShowEditModelsModal(true)}
              onModelChange={handleProjectModelChange}
            />

            {(isLoadingChat || isPending) && (
              <div style={{
                position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
                backgroundColor: 'rgba(0,0,0,0.5)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                zIndex: 50, color: 'var(--vscode-editor-foreground)',
                flexDirection: 'column', gap: '10px'
              }}>
                <div className="spinner" style={{
                  width: '30px', height: '30px', border: '3px solid rgba(255,255,255,0.3)',
                  borderRadius: '50%', borderTopColor: '#fff', animation: 'spin 1s ease-in-out infinite'
                }}></div>
                <style>{`
                @keyframes spin { to { transform: rotate(360deg); } }
              `}</style>
                <span>{t('app.loadingChat')}</span>
              </div>
            )}
          </>
        )}
      </div>

      {/* Status Bar */}
      <StatusBar
        activeProject={activeProject}
        isAgentRunning={isAgentRunning}
        textStats={editorTextStats}
        cloudStatus={cloudStatus}
        onOpenCloudSync={() => setIsCloudSyncOpen(true)}
      />

      {/* ── Overlays / Modals ── */}

      {/* Floating agent windows are first in this group on purpose. They carry the same
          z-index as `.vscode-modal-overlay`, so paint order between them is DOM
          order: coming after the workbench above, it covers the split gutters
          (which sit at that same z-index); coming before every modal below, a
          modal opened while an agent request is still pending is drawn on top
          of it. Each key remounts its window per request, so form state never
          leaks from one request into the next. */}
      {planRequest && (
        <PlanReviewWindow key={planRequest.id} planRequest={planRequest} onConfirm={sendPlanResponse} />
      )}
      {askRequest && (
        <AskModal key={askRequest.id} askRequest={askRequest} onConfirm={sendAskResponse} />
      )}

      <AlertModal message={alertMessage} onClose={() => setAlertMessage('')} />

      {showInstallPrompt && (
        <InstallDepsPrompt
          onClose={() => setShowInstallPrompt(false)}
          onInstall={() => { setShowInstallPrompt(false); setIsSettingsOpen(true); setSettingsTab('preferences'); handleInstallOptionalDeps(); }}
        />
      )}

      {showNewProjectModal && (
        <NewProjectModal
          globalModels={globalModels}
          onClose={() => setShowNewProjectModal(false)}
          onSubmit={handleCreateProject}
          newProjName={newProjName} setNewProjName={setNewProjName}
          newProjPath={newProjPath} setNewProjPath={setNewProjPath}
          newProjDesc={newProjDesc} setNewProjDesc={setNewProjDesc}
          newProjModel={newProjModel} setNewProjModel={setNewProjModel}
          newProjWorkerModel={newProjWorkerModel} setNewProjWorkerModel={setNewProjWorkerModel}
          newProjMode={newProjMode} setNewProjMode={setNewProjMode}
          newProjApiKey={newProjApiKey} setNewProjApiKey={setNewProjApiKey}
          newProjApiBase={newProjApiBase} setNewProjApiBase={setNewProjApiBase}
          newProjWorkerApiKey={newProjWorkerApiKey} setNewProjWorkerApiKey={setNewProjWorkerApiKey}
          newProjWorkerApiBase={newProjWorkerApiBase} setNewProjWorkerApiBase={setNewProjWorkerApiBase}
          newProjModelParams={newProjModelParams} setNewProjModelParams={setNewProjModelParams}
          newProjWorkerModelParams={newProjWorkerModelParams} setNewProjWorkerModelParams={setNewProjWorkerModelParams}
          newProjError={newProjError}
          onOpenDirPicker={openDirPicker}
        />
      )}

      {editingProject && (
        <EditProjectModal
          globalModels={globalModels}
          editingProject={editingProject}
          setEditingProject={setEditingProject}
          onClose={() => setEditingProject(null)}
          onSubmit={handleUpdateProject}
          editProjError={editProjError}
          showAdvancedParams={showAdvancedParams}
          setShowAdvancedParams={setShowAdvancedParams}
          onOpenDirPicker={openDirPicker}
        />
      )}

      {isSettingsOpen && (
        <SettingsModal
          onSpeechSettingsChange={bumpSpeechSettings}
          onClose={() => setIsSettingsOpen(false)}
          settingsTab={settingsTab}
          setSettingsTab={setSettingsTab}
          theme={theme} setTheme={setTheme}
          uiScale={uiScale} applyUiScale={applyUiScale}
          editorFontSize={editorFontSize} setEditorFontSize={setEditorFontSize}
          editorTabSize={editorTabSize} setEditorTabSize={setEditorTabSize}
          editorWordWrap={editorWordWrap} setEditorWordWrap={setEditorWordWrap}
          editorMinimap={editorMinimap} setEditorMinimap={setEditorMinimap}
          isInstallingDeps={isInstallingDeps}
          installDepsStatus={installDepsStatus}
          installDepsLog={installDepsLog}
          onInstallDeps={handleInstallOptionalDeps}
          ephemeralParams={ephemeralParams}
          setEphemeralParams={setEphemeralParams}
          panelMaxLines={panelMaxLines}
          setPanelMaxLines={(val) => { setPanelMaxLines(val); setPreference('panelMaxLines', val); }}
          thoughtContextTokens={thoughtContextTokens}
          onThoughtContextTokensChange={applyThoughtContextTokens}
          chatThoughtPreviewTokens={chatThoughtPreviewTokens}
          onChatThoughtPreviewTokensChange={applyChatThoughtPreviewTokens}
          onLanguageChange={(lang) => {
            fetch('/api/settings/language', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ lang }),
            }).catch(() => { });
          }}
        />
      )}

      {isMonitorsModalOpen && (
        <MonitorsModal
          onClose={() => setIsMonitorsModalOpen(false)}
          activeProject={activeProject}
          activeChatId={activeChatId}
          onEventsSeen={() => setMonitorUnseenEvents(0)}
          onOpenChat={(event) => {
            setIsMonitorsModalOpen(false);
            showMonitorChat({ project: event.project, chat_id: event.chat_id });
          }}
        />
      )}
      {isConnectorsModalOpen && (
        <ConnectorsModal onClose={() => setIsConnectorsModalOpen(false)} />
      )}
      {isHardwareModalOpen && (
        <HardwareModal onClose={() => setIsHardwareModalOpen(false)} />
      )}

      {isAssetStoreOpen && (
        <AssetStoreModal
          onClose={() => setIsAssetStoreOpen(false)}
          projectPath={activeProject?.project_path}
          onWorkspaceChanged={refreshWorkspaceFiles}
        />
      )}

      {isCloudSyncOpen && (
        <CloudSyncModal
          activeProject={activeProject}
          onClose={() => { setIsCloudSyncOpen(false); refreshCloudStatus(); }}
          onWorkspaceChanged={refreshWorkspaceFiles}
        />
      )}

      {isCloudDownloadOpen && (
        <CloudDownloadModal
          parentPath={cloudDownloadParent}
          onPickParentPath={() => openDirPicker('cloud-download', cloudDownloadParent || '~')}
          onClose={() => setIsCloudDownloadOpen(false)}
          onDownloaded={handleProjectDownloaded}
        />
      )}

      {showOnboarding && <OnboardingModal onComplete={handleOnboardingComplete} />}

      {confirmRequestDialog(confirmRequest) === 'interactive_terminal' ? (
        <InteractiveTerminalModal request={confirmRequest} onConfirm={sendConfirmResponse} activeProject={activeProject} uiScale={uiScale} />
      ) : confirmRequestDialog(confirmRequest) === 'ask' ? (
        <AskModal key={dialogRequestKey(confirmRequest)} askRequest={confirmRequest} onConfirm={sendConfirmResponse} />
      ) : confirmRequest ? (
        <ConfirmModal confirmRequest={confirmRequest} onConfirm={sendConfirmResponse} />
      ) : null}

      <DeleteProjectModal
        projectToDelete={projectToDelete}
        cloudLink={deleteCloudLink}
        onCancel={() => { setProjectToDelete(null); setDeleteCloudLink(null); }}
        onConfirm={confirmDeleteProject}
      />

      <DirPickerModal
        dirPicker={dirPicker}
        onNavigate={navigateDirPicker}
        onConfirm={confirmDirPicker}
        onClose={() => setDirPicker(null)}
      />

      <MoveToModal
        moveModal={moveModal}
        files={files}
        isFileInsidePath={isFileInsidePath}
        onConfirm={confirmMoveModal}
        onClose={() => setMoveModal(null)}
      />

      <ContextMenu
        contextMenu={contextMenu}
        rightClickedNode={rightClickedNode}
        handleCreateNewFile={handleCreateNewFile}
        handleCreateNewDir={handleCreateNewDir}
        handleImportFile={handleImportFile}
        handleRenameNode={handleRenameNode}
        handleDeleteNode={handleDeleteNode}
        handleCopyNode={handleCopyNode}
        handlePasteNode={handlePasteNode}
        handleOpenInSystem={handleOpenInSystem}
        handleSetMainFile={handleSetMainFile}
        handleMoveToNode={handleOpenMoveModal}
        clipboardNode={clipboardNode}
      />

      {showEditModelsModal && (
        <EditModelsModal
          globalModels={globalModels}
          onClose={() => setShowEditModelsModal(false)}
          onDeleteModel={handleGlobalModelDelete}
          onEditModel={(model) => {
            setEditingModelModalData(model);
            setShowAddModelModal(true);
          }}
          onAddModel={() => {
            setEditingModelModalData(null);
            setShowAddModelModal(true);
          }}
          onManageConnections={() => setShowEditProvidersModal(true)}
          onLoadLocalOllama={handleLoadLocalOllamaModels}
        />
      )}

      {showAddModelModal && (
        <AddModelModal
          editingModel={editingModelModalData}
          existingModels={globalModels}
          connections={providerConnections}
          onSaveConnection={handleConnectionSave}
          onClose={() => setShowAddModelModal(false)}
          onSave={handleGlobalModelSave}
        />
      )}

      {showEditProvidersModal && (
        <EditProvidersModal
          connections={providerConnections}
          models={globalModels}
          onClose={() => setShowEditProvidersModal(false)}
          onDeleteConnection={handleConnectionDelete}
          onEditConnection={(connection) => {
            setEditingConnectionModalData(connection);
            setShowAddConnectionModal(true);
          }}
          onAddConnection={() => {
            setEditingConnectionModalData(null);
            setShowAddConnectionModal(true);
          }}
        />
      )}

      {showAddConnectionModal && (
        <AddConnectionModal
          editingConnection={editingConnectionModalData}
          existingConnections={providerConnections}
          onClose={() => setShowAddConnectionModal(false)}
          onSave={handleConnectionSave}
        />
      )}
    </div>
  );
}
