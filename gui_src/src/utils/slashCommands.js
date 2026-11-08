// The chat composer's `/` completion: which commands match what the user typed,
// and what accepting one does to the input.
//
// A slash command is an operation on the project or the chat, not a message:
// its output belongs in a window of its own (SlashCommandOutputModal), never in
// the conversation. Kept free of React so the matching rules are unit-tested.

/**
 * The command token being typed, or null when the menu should be closed.
 *
 * The menu is open only while the input is a single `/word` with nothing after
 * it: once a space follows, the user is typing arguments, and a message that
 * merely contains a slash ("and/or") is never a command.
 */
export function slashQuery(input) {
  const text = String(input ?? '');
  const match = /^\/(\S*)$/.exec(text.trimStart());
  return match ? match[1].toLowerCase() : null;
}

/** True when the composer holds a command rather than a message. */
export function isSlashCommandText(input) {
  return String(input ?? '').trim().startsWith('/');
}

const bare = (name) => String(name || '').replace(/^\//, '').toLowerCase();

/**
 * Commands matching `query`, best first: a name or alias that starts with the
 * query, then one that contains it, then a description that does. Registry
 * order is kept inside each tier, so an empty query lists `/help`'s order.
 */
export function filterSlashCommands(commands, query) {
  const list = Array.isArray(commands) ? commands : [];
  const q = String(query ?? '').toLowerCase();
  if (!q) return list.slice();
  const tiers = [[], [], []];
  for (const command of list) {
    const names = [command.name, ...(command.aliases || [])].map(bare);
    if (names.some((name) => name.startsWith(q))) tiers[0].push(command);
    else if (names.some((name) => name.includes(q))) tiers[1].push(command);
    else if (String(command.description || '').toLowerCase().includes(q)) tiers[2].push(command);
  }
  return [...tiers[0], ...tiers[1], ...tiers[2]];
}

/** True when the command declares no arguments at all. */
export function takesNoArguments(command) {
  return !String(command?.usage || '').trim();
}

/**
 * True when the command cannot run bare: its usage names a `<required>`
 * argument. Optional ones (`[n]`, `[auto | plan | edit]`) have a bare form —
 * `/help`, `/models` — so choosing the command with Enter runs it.
 */
export function requiresArguments(command) {
  return /</.test(String(command?.usage || ''));
}

/**
 * What the input becomes when `command` is accepted with Tab: its name, with a
 * trailing space when it takes arguments so the user can type them next.
 */
export function completionText(command) {
  const name = String(command?.name || '');
  return takesNoArguments(command) ? name : `${name} `;
}

/** The highlighted row after moving by `delta`, wrapping at both ends. */
export function moveSelection(index, delta, count) {
  if (!count) return 0;
  return ((index + delta) % count + count) % count;
}
