// Which layouts offer the compact chat, and whether the user wants it there.
//
// The compact chat keeps only the conversation and the prompt on screen: the
// chat selector, the model selectors and the web-search bar move into an
// overlay opened from the header, and the effort selector moves into the input
// row. It exists for layouts that give the chat a short, wide cell — the
// studio's bottom row — where those toolbars would otherwise take most of the
// panel's height. A tall sidebar has no such problem, so the IDE layout is not
// in the list and keeps the full panel.
//
// The preference is stored per layout so that turning it off in one layout
// never changes another. Compact is the default: the layouts listed here are
// the ones where the full panel leaves almost no room for the conversation.

import { preferenceStorage } from './preferences.js';

export const COMPACT_CHAT_LAYOUTS = ['studio'];

const COMPACT_CHAT_LAYOUT_SET = new Set(COMPACT_CHAT_LAYOUTS);

/** The layout key to pass to the chat panel for `mode`, or null when `mode` has no compact chat. */
export const compactChatLayoutFor = (mode) => (COMPACT_CHAT_LAYOUT_SET.has(mode) ? mode : null);

export const chatCompactStorageKey = (layout) => `chatCompact.${layout}`;

// The backend-owned preference store (see utils/preferences.js).
const defaultStorage = () => preferenceStorage;

/** Whether the chat should be compact in `layout`. Always false for a layout without a compact chat. */
export const readChatCompact = (layout, storage = defaultStorage()) => {
  if (!COMPACT_CHAT_LAYOUT_SET.has(layout)) return false;
  let stored = null;
  try {
    stored = storage?.getItem(chatCompactStorageKey(layout)) ?? null;
  } catch (_) {
    stored = null;
  }
  return stored === null ? true : stored === 'true';
};

export const writeChatCompact = (layout, value, storage = defaultStorage()) => {
  if (!COMPACT_CHAT_LAYOUT_SET.has(layout)) return;
  try {
    storage?.setItem(chatCompactStorageKey(layout), String(Boolean(value)));
  } catch (_) {
    // Storage can be unavailable (private window, blocked site data); the
    // preference then lasts for the session only.
  }
};
