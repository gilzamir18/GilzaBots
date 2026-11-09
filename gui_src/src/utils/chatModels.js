// The models one chat runs on, resolved the way the backend resolves them
// (`resolve_chat_models` in gilzabots/project.py):
//
//   orchestrator: the chat's own model, else the project's default model
//                 (`project.model`), else the catalog's global default;
//   worker:       the chat's own worker model, else the project's worker model,
//                 else the orchestrator's model.
//
// Each chat carries `model` / `worker_model` as `/api/chat/list` reports them;
// an empty value means the chat follows the default for that role.

export const CHAT_MODEL_ROLES = ['model', 'worker_model'];

// The id of the model the catalog marks as the global default ('' if none).
export function globalDefaultModelId(globalModels) {
  const entry = (globalModels || []).find(model => model?.is_default);
  return entry?.id || '';
}

export function chatModelChoice(chats, chatId, role) {
  const chat = (chats || []).find(candidate => candidate?.id === chatId);
  return String(chat?.[role] || '').trim();
}

export function withChatModel(chats, chatId, role, modelId) {
  const stored = String(modelId || '').trim();
  return (chats || []).map(chat => (
    chat?.id === chatId ? { ...chat, [role]: stored } : chat
  ));
}

// The `{ chatId, role }` pairs whose own choice names `modelId`, e.g. to follow
// a model renamed in the catalog.
export function chatsUsingModel(chats, modelId) {
  const uses = [];
  for (const chat of chats || []) {
    for (const role of CHAT_MODEL_ROLES) {
      if (modelId && chat?.[role] === modelId) uses.push({ chatId: chat.id, role });
    }
  }
  return uses;
}

/**
 * Resolve both roles for one chat.
 *
 * Returns `{ model, modelSource, defaultModel, defaultSource, workerModel,
 * workerSource, workerDefault, workerDefaultSource }`. Sources are 'chat',
 * 'project', 'global', or '' (orchestrator: nothing resolves) /
 * 'orchestrator' (worker: follows the orchestrator). `defaultModel` and
 * `defaultSource` describe what the chat falls back to when it names no model,
 * so a selector can say which default it is using.
 *
 * `chatChoice` (`{ model, worker_model }`) stands in for the chat's own
 * models when there is no chat yet: the choice made in the toolbar before the
 * first message creates the chat, which then stores it.
 */
export function resolveChatModels({ chats, chatId, project, globalModels, chatChoice }) {
  const choiceFor = role => (chatChoice
    ? String(chatChoice[role] || '').trim()
    : chatModelChoice(chats, chatId, role));
  const chatModel = choiceFor('model');
  const projectModel = String(project?.model || '').trim();
  const globalModel = globalDefaultModelId(globalModels);

  const defaultModel = projectModel || globalModel;
  const defaultSource = projectModel ? 'project' : (globalModel ? 'global' : '');
  const model = chatModel || defaultModel;
  const modelSource = chatModel ? 'chat' : defaultSource;

  const chatWorker = choiceFor('worker_model');
  const projectWorker = String(project?.worker_model || '').trim();
  const workerDefault = projectWorker;
  const workerDefaultSource = projectWorker ? 'project' : 'orchestrator';
  const workerModel = chatWorker || workerDefault;
  const workerSource = chatWorker ? 'chat' : workerDefaultSource;

  return {
    model,
    modelSource,
    defaultModel,
    defaultSource,
    workerModel,
    workerSource,
    workerDefault,
    workerDefaultSource,
  };
}
