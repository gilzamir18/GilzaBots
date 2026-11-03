// The reasoning effort one chat asks for in place of its model's catalog value.
//
// The levels mirror CHAT_REASONING_EFFORTS in gilzabots/project.py, which is
// where a value is validated and stored. The empty string is the absence of an
// override: the chat follows the model's catalog entry.

export const CHAT_REASONING_EFFORTS = ['low', 'medium', 'high', 'xhigh'];

export function normalizeChatReasoningEffort(value) {
  const effort = String(value || '').trim().toLowerCase();
  return CHAT_REASONING_EFFORTS.includes(effort) ? effort : '';
}

// The effort stored for a chat, as `/api/chat/list` reports it on each chat.
export function chatReasoningEffort(chats, chatId) {
  const chat = (chats || []).find(candidate => candidate?.id === chatId);
  return normalizeChatReasoningEffort(chat?.reasoning_effort);
}

export function withChatReasoningEffort(chats, chatId, effort) {
  const stored = normalizeChatReasoningEffort(effort);
  return (chats || []).map(chat => (
    chat?.id === chatId ? { ...chat, reasoning_effort: stored } : chat
  ));
}

// What the catalog says about a model's reasoning: whether it reasons at all
// (the override only reaches a model that declares it) and the effort the chat
// falls back to when it sets none.
export function catalogReasoning(globalModels, modelId) {
  const model = (globalModels || []).find(candidate => candidate?.id === modelId);
  return {
    supportsThinking: Boolean(model?.supports_thinking),
    effort: String(model?.reasoning_effort || '').trim().toLowerCase(),
  };
}
