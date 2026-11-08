import test from 'node:test';
import assert from 'node:assert/strict';

import {
  completionText,
  filterSlashCommands,
  isSlashCommandText,
  moveSelection,
  requiresArguments,
  slashQuery,
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

test('the menu opens on a bare slash and while the command word is typed', () => {
  assert.equal(slashQuery('/'), '');
  assert.equal(slashQuery('/co'), 'co');
  assert.equal(slashQuery('  /CO'), 'co');
});

test('the menu closes once arguments start, and never opens for prose', () => {
  assert.equal(slashQuery('/help '), null);
  assert.equal(slashQuery('/help models'), null);
  assert.equal(slashQuery('and/or'), null);
  assert.equal(slashQuery(''), null);
  assert.equal(slashQuery(undefined), null);
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
