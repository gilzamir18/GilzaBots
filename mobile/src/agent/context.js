/**
 * What the phone's agent knows when a turn starts.
 *
 * The desktop orchestrator keeps a MemGPT working state per chat (an evicted-
 * history summary plus recent turns). The phone does not run that agent; it
 * borrows what transfers: the chat's recursive summary from `agent_state`, the
 * project's (or chat's) core memory, and the most recent visible turns, under
 * a character budget. Tool calls in the desktop state are not replayed — they
 * name tools the phone does not have.
 */

import { stripProgressFence } from '../project/conversation.js';

export const DEFAULT_RECURSIVE_SUMMARY = 'No history has been evicted yet.';
export const HISTORY_BUDGET_CHARS = 40000;

export function recursiveSummaryOf(chat) {
  try {
    const state = JSON.parse(chat?.agent_state || '{}');
    const summary = String(state?.recursive_summary || '').trim();
    return summary && summary !== DEFAULT_RECURSIVE_SUMMARY ? summary : '';
  } catch {
    return '';
  }
}

export function coreMemoryOf(settings, chat) {
  const shared = String(settings?.use_shared_memory ?? '0') === '1';
  return String((shared ? settings?.core_memory : chat?.core_memory) || '').trim();
}

export function buildSystemPrompt({ projectName, settings = {}, chat, today, language }) {
  const parts = [
    'You are the GilzaBots assistant, running in the GilzaBots mobile app.',
    'GilzaBots is an AI-assisted LaTeX and document editor. This conversation is shared with the desktop app through Google Drive, so the user may continue it on a computer.',
    `Project: ${projectName}.`,
  ];
  if (settings.description) parts.push(`Project description: ${settings.description}`);
  if (settings.main_file) parts.push(`Main file: ${settings.main_file}`);
  parts.push(
    'You can list, read and search the project files, and propose edits or new files. The user approves each change before it is written. You cannot compile LaTeX or run commands here; say so when a task needs the desktop.',
    'Write mathematics in LaTeX with $…$ or $$…$$.',
    `Today is ${today}.`,
  );
  if (language) parts.push(`Answer in the user's language (${language}) unless asked otherwise.`);
  const memory = coreMemoryOf(settings, chat);
  if (memory) parts.push(`Core memory:\n${memory}`);
  const summary = recursiveSummaryOf(chat);
  if (summary) parts.push(`Summary of earlier conversation:\n${summary}`);
  return parts.join('\n\n');
}

/** The newest visible turns that fit the budget, oldest first. */
export function buildHistory(messages, budget = HISTORY_BUDGET_CHARS) {
  const picked = [];
  let used = 0;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== 'user' && message.role !== 'assistant') continue;
    const content = message.role === 'assistant' ? stripProgressFence(message.content) : String(message.content ?? '');
    if (!content.trim()) continue;
    if (used + content.length > budget && picked.length) break;
    picked.push({ role: message.role, content });
    used += content.length;
  }
  return picked.reverse();
}
