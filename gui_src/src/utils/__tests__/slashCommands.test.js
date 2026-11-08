import test from 'node:test';
import assert from 'node:assert/strict';

import {
  applySlashCompletion,
  completionText,
  filterSlashCommands,
  isBuiltinSlashCommand,
  isSlashCommandText,
  moveSelection,
  requiresArguments,
  slashTokenAt,
  takesNoArguments,
} from '../slashCommands.js';

const commands = [
  { name: '/help', aliases: [], usage: '[command]', description: 'List what is available' },
  { name: '/cost', aliases: ['/context'], usage: '', description: 'Token usage' },
  { name: '/compact', aliases: [], usage: '', description: 'Summarize the conversation' },
  { name: '/clear_chat', aliases: [], usage: '', description: 'Clear only the current chat' },
  { name: '/models', aliases: [], usage: '[list | add ...]', description: 'Model catalog' },
];
const names = (list) => list.map((command) => command.name);

const query = (text, caret) => slashTokenAt(text, caret)?.query ?? null;

test('the menu opens on a bare slash and while the command word is typed', () => {
  assert.equal(query('/'), '');
  assert.equal(query('/co'), 'co');
  assert.equal(query('  /CO'), 'co');
  assert.deepEqual(slashTokenAt('  /co'), { query: 'co', start: 2, end: 5, alone: true });
});

test('the menu closes once arguments start, and never opens for prose', () => {
  assert.equal(query('/help '), null);
  assert.equal(query('/help models'), null);
  assert.equal(query('and/or'), null);
  assert.equal(query('see src/app.js'), null);
  assert.equal(query('run /usr/bin'), null);
  assert.equal(query(''), null);
  assert.equal(query(undefined), null);
});

test('a /word in the middle or at the end of a message opens the menu', () => {
  assert.deepEqual(slashTokenAt('please run /to'), { query: 'to', start: 11, end: 14, alone: false });
  // The caret decides which word: here it sits inside "/to", before " now".
  assert.deepEqual(slashTokenAt('run /to now', 7), { query: 'to', start: 4, end: 7, alone: false });
  assert.equal(query('run /to now', 11), null);
  // A command word followed by text is no longer a bare command line.
  assert.equal(slashTokenAt('/to now', 3).alone, false);
  assert.equal(query('line one\n/sp'), 'sp');
});

test('a command is recognized by its leading slash, not by the menu state', () => {
  assert.equal(isSlashCommandText(' /help models'), true);
  assert.equal(isSlashCommandText('see /help'), false);
});

test('an empty query lists every command in registry order', () => {
  assert.deepEqual(names(filterSlashCommands(commands, '')), names(commands));
});

test('prefix matches come before substring and description matches', () => {
  assert.deepEqual(names(filterSlashCommands(commands, 'co')), ['/cost', '/compact']);
  assert.deepEqual(names(filterSlashCommands(commands, 'chat')), ['/clear_chat']);
  // "conversation" only appears in a description.
  assert.deepEqual(names(filterSlashCommands(commands, 'conversation')), ['/compact']);
});

test('an alias finds its command', () => {
  assert.deepEqual(names(filterSlashCommands(commands, 'context')), ['/cost']);
});

test('accepting a command with arguments leaves room to type them', () => {
  assert.equal(takesNoArguments(commands[0]), false);
  assert.equal(completionText(commands[0]), '/help ');
  assert.equal(takesNoArguments(commands[2]), true);
  assert.equal(completionText(commands[2]), '/compact');
});

test('accepting inside a message writes the name over the word and never runs it', () => {
  const skill = { name: '/to-spec', usage: '', kind: 'skill' };
  const atEnd = 'please run /to';
  assert.deepEqual(applySlashCompletion(atEnd, slashTokenAt(atEnd), skill),
    { text: 'please run /to-spec ', caret: 20 });
  const inside = 'run /to now';
  assert.deepEqual(applySlashCompletion(inside, slashTokenAt(inside, 7), skill),
    { text: 'run /to-spec now', caret: 13 });
  // The caret lands past the space, so the menu closes.
  assert.equal(slashTokenAt('run /to-spec now', 13), null);
});

test('accepting a bare command line keeps completionText', () => {
  assert.deepEqual(applySlashCompletion('/he', slashTokenAt('/he'), commands[0]), { text: '/help ', caret: 6 });
  assert.deepEqual(applySlashCompletion('/comp', slashTokenAt('/comp'), commands[2]), { text: '/compact', caret: 8 });
});

test('only built-in commands, aliases included, count as built-in', () => {
  const catalog = [
    ...commands.map((command) => ({ ...command, kind: 'command' })),
    { name: '/to-spec', aliases: [], usage: '', kind: 'skill' },
  ];
  assert.equal(isBuiltinSlashCommand(catalog, '/compact'), true);
  assert.equal(isBuiltinSlashCommand(catalog, '/context'), true);
  assert.equal(isBuiltinSlashCommand(catalog, '/to-spec'), false);
  assert.equal(isBuiltinSlashCommand(catalog, '/nothing'), false);
  assert.equal(isBuiltinSlashCommand([], '/compact'), false);
});

test('only a <required> argument stops Enter from running the command bare', () => {
  assert.equal(requiresArguments({ name: '/rename', usage: '<new_name>' }), true);
  assert.equal(requiresArguments(commands[0]), false);
  assert.equal(requiresArguments(commands[4]), false);
  assert.equal(requiresArguments(commands[2]), false);
});

test('the highlight wraps at both ends', () => {
  assert.equal(moveSelection(0, -1, 3), 2);
  assert.equal(moveSelection(2, 1, 3), 0);
  assert.equal(moveSelection(1, 1, 0), 0);
});
