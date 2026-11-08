// Chat compaction markers (`/compact` and automatic compaction during a turn).
//
// Compaction replaces only what the model receives; every message stays in the
// chat. The point where it happened is shown as a divider carrying the summary
// the agent continues from. The backend stores it as a `compaction` activity
// row (gilzabots/compaction.py) -- the summary as the row's content, the counts
// in its payload -- so the divider comes back after a reload, merged into the
// transcript by timestamp like the stored error bubbles.

export const COMPACTION_ACTIVITY_EVENT = 'compaction';

const asInt = (value) => {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.round(n) : 0;
};

/** Build the chat row for one completed compaction. */
export function compactionMessage(payload = {}, timestamp) {
  return {
    role: 'system',
    content: '',
    timestamp: timestamp || payload.timestamp || new Date().toISOString(),
    _compaction: {
      trigger: payload.trigger === 'auto' ? 'auto' : 'manual',
      preTokens: asInt(payload.pre_tokens),
      postTokens: asInt(payload.post_tokens),
      contextWindow: asInt(payload.context_window),
      messagesCompacted: asInt(payload.messages_compacted),
      summary: String(payload.summary || ''),
      customInstructions: String(payload.custom_instructions || ''),
      // Files re-read into the new context, and the ones that were not.
      restoredFiles: (payload.restored_files || []).map(String),
      skippedFiles: (payload.skipped_files || [])
        .filter(item => item && item.path)
        .map(item => ({ path: String(item.path), reason: String(item.reason || '') })),
      // PreCompact / PostCompact hook runs that failed; successful ones are silent.
      hookFailures: (payload.hooks || [])
        .filter(item => item && (item.outcome === 'error' || item.outcome === 'timeout'))
        .map(item => ({
          event: String(item.event || ''),
          command: String(item.command || ''),
          message: String(item.message || ''),
        })),
    },
  };
}

/** The divider rows for the `compaction` activity of a reloaded chat. */
export function compactionMessagesFromActivity(activity = []) {
  return (activity || [])
    .filter(item => item?.event === COMPACTION_ACTIVITY_EVENT)
    .map(item => ({
      ...compactionMessage(
        { ...(item.payload || {}), summary: item.content || item.payload?.summary || '' },
        item.timestamp,
      ),
      _fromActivity: true,
    }));
}

/** Short token count for the divider label: 950, 12.3k, 1.2M. */
export function formatTokenCount(value) {
  const n = asInt(value);
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, '')}k`;
  return String(n);
}
