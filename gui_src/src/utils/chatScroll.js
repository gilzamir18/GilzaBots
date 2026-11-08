// Where the chat history should scroll as a turn unfolds.
//
// The view follows new content only while the user lets it. Three modes:
//
// - `bottom`: keep the end of the history in view (after opening a chat, or
//   when the user scrolls back down to the end).
// - `turnStart`: the user just sent a message and the reply is on its way.
//   New content is followed, but never past the start of the reply: once the
//   reply is taller than the viewport its first line stays at the top, so the
//   user reads it from the beginning instead of being dropped at its end.
// - `off`: the user scrolled away; nothing moves the view.

export const FOLLOW_BOTTOM = 'bottom';
export const FOLLOW_TURN_START = 'turnStart';
export const FOLLOW_OFF = 'off';

// Room left above an anchored reply so its header is not flush with the edge.
export const REPLY_ANCHOR_MARGIN_PX = 8;

function lastVisibleRoleIndex(messages) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const role = messages[i]?.role;
    if (role === 'user' || role === 'assistant') return i;
  }
  return -1;
}

// Decides what a change of the message list does to the view.
// Returns `{ mode, action, index }`, where `action` is one of:
// - `bottom`: scroll to the end of the history;
// - `anchor`: bring the message at `index` to the top of the viewport;
// - `none`: leave the view where it is.
export function planMessagesScroll(mode, messages) {
  const list = Array.isArray(messages) ? messages : [];
  const index = lastVisibleRoleIndex(list);
  const last = index >= 0 ? list[index] : null;
  if (last?.role === 'user') {
    // A message the user just sent always brings the view back down, and the
    // reply that follows it is read from its start.
    return { mode: FOLLOW_TURN_START, action: 'bottom', index };
  }
  if (mode === FOLLOW_TURN_START && last?.role === 'assistant') {
    return { mode: FOLLOW_TURN_START, action: 'anchor', index };
  }
  if (mode === FOLLOW_BOTTOM) {
    return { mode, action: 'bottom', index: -1 };
  }
  return { mode, action: 'none', index: -1 };
}

// The scroll position that shows content starting at `anchorTop` (in the
// scroller's content coordinates) at the top of the viewport, without going
// past the end of the history. Short content therefore ends up fully visible
// at the bottom, exactly as following the bottom would leave it.
export function anchoredScrollTop(anchorTop, maxScrollTop, margin = REPLY_ANCHOR_MARGIN_PX) {
  const max = Math.max(0, Number(maxScrollTop) || 0);
  if (!Number.isFinite(anchorTop)) return max;
  return Math.min(max, Math.max(0, anchorTop - margin));
}

export function maxScrollTopOf(scroller) {
  return Math.max(0, scroller.scrollHeight - scroller.clientHeight);
}

// Top of `element` in `scroller`'s content coordinates (the same space as
// `scrollTop`). The chat history carries a CSS `zoom`, so bounding rectangles
// (viewport pixels) are converted with the scroller's own rendered-to-layout
// ratio rather than assumed to match `scrollTop`'s units.
export function topWithinScroller(scroller, element) {
  const scrollerRect = scroller.getBoundingClientRect();
  const elementRect = element.getBoundingClientRect();
  const scale = scroller.offsetHeight > 0 && scrollerRect.height > 0
    ? scrollerRect.height / scroller.offsetHeight
    : 1;
  const offset = (elementRect.top - scrollerRect.top) / scale - (scroller.clientTop || 0);
  return offset + scroller.scrollTop;
}
