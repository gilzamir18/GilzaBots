import test from 'node:test';
import assert from 'node:assert/strict';

import {
  chatModelChoice,
  chatsUsingModel,
  globalDefaultModelId,
  resolveChatModels,
  withChatModel,
} from '../chatModels.js';

const MODELS = [
  { id: 'openrouter/openrouter/free', name: 'openrouter/free', is_default: true },
  { id: 'ollama/project', name: 'project' },
  { id: 'ollama/chat', name: 'chat' },
];

const CHATS = [
  { id: 'c1', name: 'Follows defaults', model: '', worker_model: '' },
  { id: 'c2', name: 'Own model', model: 'ollama/chat', worker_model: 'ollama/chat' },
];

test('the global default is the entry the catalog marks', () => {
  assert.equal(globalDefaultModelId(MODELS), 'openrouter/openrouter/free');
  assert.equal(globalDefaultModelId([{ id: 'a' }]), '');
  assert.equal(globalDefaultModelId(undefined), '');
});

test('a chat in a project without a model runs on the global default', () => {
  const r = resolveChatModels({ chats: CHATS, chatId: 'c1', project: { model: '' }, globalModels: MODELS });
  assert.equal(r.model, 'openrouter/openrouter/free');
  assert.equal(r.modelSource, 'global');
  assert.equal(r.defaultSource, 'global');
  assert.equal(r.workerModel, '');
  assert.equal(r.workerSource, 'orchestrator');
});

test('the project default wins over the global default', () => {
  const r = resolveChatModels({ chats: CHATS, chatId: 'c1', project: { model: 'ollama/project' }, globalModels: MODELS });
  assert.equal(r.model, 'ollama/project');
  assert.equal(r.modelSource, 'project');
  assert.equal(r.defaultModel, 'ollama/project');
});

test("a chat's own model wins, and the default it overrides stays visible", () => {
  const r = resolveChatModels({
    chats: CHATS, chatId: 'c2', project: { model: 'ollama/project', worker_model: 'ollama/project' }, globalModels: MODELS,
  });
  assert.equal(r.model, 'ollama/chat');
  assert.equal(r.modelSource, 'chat');
  assert.equal(r.defaultModel, 'ollama/project');
  assert.equal(r.defaultSource, 'project');
  assert.equal(r.workerModel, 'ollama/chat');
  assert.equal(r.workerSource, 'chat');
  assert.equal(r.workerDefaultSource, 'project');
});

test('nothing resolves when no default exists anywhere', () => {
  const r = resolveChatModels({ chats: [], chatId: 'missing', project: null, globalModels: [] });
  assert.equal(r.model, '');
  assert.equal(r.modelSource, '');
});

test('withChatModel updates one chat and one role', () => {
  const next = withChatModel(CHATS, 'c1', 'model', ' ollama/chat ');
  assert.equal(chatModelChoice(next, 'c1', 'model'), 'ollama/chat');
  assert.equal(chatModelChoice(next, 'c1', 'worker_model'), '');
  assert.equal(chatModelChoice(next, 'c2', 'model'), 'ollama/chat');
  assert.equal(chatModelChoice(CHATS, 'c1', 'model'), '', 'the input list is not mutated');
});

test('chatsUsingModel lists each chat and role that names a model', () => {
  assert.deepEqual(chatsUsingModel(CHATS, 'ollama/chat'), [
    { chatId: 'c2', role: 'model' },
    { chatId: 'c2', role: 'worker_model' },
  ]);
  assert.deepEqual(chatsUsingModel(CHATS, ''), []);
});

test('with no chat open, the pending choice stands in for the chat models', () => {
  const r = resolveChatModels({
    chats: CHATS,
    chatId: '',
    project: { model: '' },
    globalModels: [{ id: 'ollama/chat', name: 'chat' }],
    chatChoice: { model: 'ollama/chat', worker_model: '' },
  });
  assert.equal(r.model, 'ollama/chat');
  assert.equal(r.modelSource, 'chat');
  assert.equal(r.defaultSource, '');
  assert.equal(r.workerModel, '');
  assert.equal(r.workerSource, 'orchestrator');
});

test('an empty pending choice follows the defaults like a new chat would', () => {
  const r = resolveChatModels({
    chats: CHATS,
    chatId: 'c2',
    project: { model: 'ollama/project' },
    globalModels: MODELS,
    chatChoice: {},
  });
  // The choice replaces the lookup: chat c2's own models are not consulted.
  assert.equal(r.model, 'ollama/project');
  assert.equal(r.modelSource, 'project');
});
