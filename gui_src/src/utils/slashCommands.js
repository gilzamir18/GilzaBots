// The chat composer's `/` completion: which commands match what the user typed,
// and what accepting one does to the input.
//
// A slash command is an operation on the project or the chat, not a message:
// its output belongs in a window of its own (SlashCommandOutputModal), never in
// the conversation. Kept free of React so the matching rules are unit-tested.

/**
 * The `/word` under the caret, or null when the menu should be closed.
 *
 * The word may sit anywhere in the input: at its start, where the line is a
 * command, or in the middle or at the end of a message, where accepting a row
 * only writes the name into the text. A word counts when it starts with `/`
 * after the start of the input or whitespace and holds no other slash, so a
 * message that merely contains one ("and/or", "src/app.js", "/usr/bin") never
 * opens it. `caret` defaults to the end of the input.
 *
 * Returns `{ query, start, end, alone }`: the name typed so far (lowercase,
 * without the slash), the word's bounds in the input, and whether the word is
 * the whole input apart from surrounding whitespace — only then is the line a
 * bare command that Enter may run.
 */
export function slashTokenAt(input, caret) {
  const text = String(input ?? '');
  const pos = Number.isInteger(caret) ? Math.max(0, Math.min(caret, text.length)) : text.length;
  let start = pos;
  while (start > 0 && !/\s/.test(text[start - 1])) start -= 1;
  let end = pos;
  while (end < text.length && !/\s/.test(text[end])) end += 1;
  const match = /^\/([^\s/]*)$/.exec(text.slice(start, end));
  if (!match) return null;
  return {
    query: match[1].toLowerCase(),
    start,
    end,
    alone: !text.slice(0, start).trim() && !text.slice(end).trim(),
  };
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

/**
 * The input after `command` is accepted for `token` (from `slashTokenAt`), and
 * where the caret goes: the word is replaced by the command's name. A bare
 * command line follows `completionText`; inside a message the name is followed
 * by a space so the sentence can go on (the one already there, if any).
 */
export function applySlashCompletion(input, token, command) {
  const text = String(input ?? '');
  const name = String(command?.name || '');
  const after = text.slice(token.end);
  if (token.alone) {
    const insert = completionText(command);
    return { text: text.slice(0, token.start) + insert + after, caret: token.start + insert.length };
  }
  // The caret ends past the separating space either way, so the word under it
  // is no longer the command and the list closes.
  const spaced = /^\s/.test(after);
  const insert = spaced ? name : `${name} `;
  return {
    text: text.slice(0, token.start) + insert + after,
    caret: token.start + insert.length + (spaced ? 1 : 0),
  };
}

/**
 * True when `name` (the first word of a command line) is one of the built-in
 * commands in `commands` (the `list_slash_commands` catalog), alias included.
 * Anything else is a skill's `/name` or not a command at all.
 */
export function isBuiltinSlashCommand(commands, name) {
  const wanted = bare(name);
  if (!wanted) return false;
  return (Array.isArray(commands) ? commands : []).some((command) => (
    command?.kind !== 'skill'
    && [command?.name, ...(command?.aliases || [])].map(bare).includes(wanted)
  ));
}

/** The highlighted row after moving by `delta`, wrapping at both ends. */
export function moveSelection(index, delta, count) {
  if (!count) return 0;
  return ((index + delta) % count + count) % count;
}
