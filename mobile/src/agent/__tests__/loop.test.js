import test from 'node:test';
import assert from 'node:assert/strict';
import { runAgentTurn, MAX_REPEATED_FAILURES } from '../loop.js';
import { createTools, normalizeProjectPath, ToolInputError } from '../tools.js';
import { parseModelId, portableParams, resolveModel } from '../providers.js';
import { AgentLimitError } from '../errors.js';
import { buildHistory, buildSystemPrompt, recursiveSummaryOf } from '../context.js';

function memoryWorkspace(files) {
  const store = new Map(Object.entries(files).map(([path, content]) => [path, { content, md5: `md5-${content.length}` }]));
  return {
    writes: [],
    async listPaths() { return [...store.keys()].filter((p) => !p.startsWith('.gilzabots/')).sort(); },
    async listTextPaths() { return this.listPaths(); },
    async readText(path) {
      const entry = store.get(path);
      if (!entry) throw new ToolInputError(`${path} does not exist`);
      return entry;
    },
    async writeText(path, content, md5) {
      this.writes.push({ path, content, md5 });
      store.set(path, { content, md5: `md5-${content.length}` });
    },
  };
}

function scripted(responses) {
  const requests = [];
  const complete = async (request) => {
    requests.push(request.messages.map((m) => ({ ...m })));
    const next = responses.shift();
    return { content: '', reasoning: '', toolCalls: [], ...next };
  };
  complete.requests = requests;
  return complete;
}

const call = (name, args, id = `${name}-1`) => ({ id, name, arguments: JSON.stringify(args) });

test('tools run and their results go back to the model', async () => {
  const workspace = memoryWorkspace({ 'main.tex': 'line one\nline two' });
  const complete = scripted([
    { toolCalls: [call('read_file', { path: 'main.tex', start_line: 2 })] },
    { content: 'It says line two.' },
  ]);
  const result = await runAgentTurn({ complete, messages: [{ role: 'user', content: 'q' }], tools: createTools(workspace), approve: async () => true });
  assert.equal(result.content, 'It says line two.');
  const toolMessage = complete.requests[1].find((m) => m.role === 'tool');
  assert.match(toolMessage.content, /2: line two/);
});

test('an edit is written only after approval, against the checksum that was read', async () => {
  const workspace = memoryWorkspace({ 'a.tex': 'Hello world' });
  const complete = scripted([
    { toolCalls: [call('edit_file', { path: 'a.tex', old_string: 'world', new_string: 'Drive' })] },
    { content: 'Done.' },
  ]);
  const seen = [];
  await runAgentTurn({
    complete, messages: [], tools: createTools(workspace),
    approve: async (change) => { seen.push(change); return true; },
  });
  assert.equal(seen[0].before, 'world');
  assert.equal(seen[0].after, 'Drive');
  assert.deepEqual(workspace.writes, [{ path: 'a.tex', content: 'Hello Drive', md5: 'md5-11' }]);
});

test('a declined edit writes nothing and tells the model', async () => {
  const workspace = memoryWorkspace({ 'a.tex': 'Hello world' });
  const complete = scripted([
    { toolCalls: [call('edit_file', { path: 'a.tex', old_string: 'world', new_string: 'x' })] },
    { content: 'OK.' },
  ]);
  await runAgentTurn({ complete, messages: [], tools: createTools(workspace), approve: async () => false });
  assert.deepEqual(workspace.writes, []);
  assert.match(complete.requests[1].at(-1).content, /declined/);
});

test('GilzaBots internals are never writable', async () => {
  const [, , , edit] = createTools(memoryWorkspace({}));
  await assert.rejects(edit.prepare({ path: '.gilzabots/session/chats.json', old_string: 'a', new_string: 'b' }), ToolInputError);
  assert.throws(() => normalizeProjectPath('../etc/passwd'), ToolInputError);
});

test('an ambiguous edit is refused with a reason', async () => {
  const [, , , edit] = createTools(memoryWorkspace({ 'a.tex': 'x x' }));
  await assert.rejects(edit.prepare({ path: 'a.tex', old_string: 'x', new_string: 'y' }), /more than once/);
});

test('an unknown tool is reported to the model, not substituted', async () => {
  const complete = scripted([{ toolCalls: [call('compile_latex', {})] }, { content: 'Cannot compile here.' }]);
  const result = await runAgentTurn({ complete, messages: [], tools: createTools(memoryWorkspace({})), approve: async () => true });
  assert.equal(result.content, 'Cannot compile here.');
  assert.match(complete.requests[1].at(-1).content, /no tool named "compile_latex"/);
});

test('the same failing call repeated stops the turn', async () => {
  const bad = call('read_file', { path: 'missing.tex' });
  const complete = scripted(Array.from({ length: MAX_REPEATED_FAILURES }, () => ({ toolCalls: [bad] })));
  await assert.rejects(
    runAgentTurn({ complete, messages: [], tools: createTools(memoryWorkspace({})), approve: async () => true }),
    AgentLimitError,
  );
});

test('a turn that never answers hits the step limit', async () => {
  let n = 0;
  const complete = async () => ({ content: '', reasoning: '', toolCalls: [call('list_files', {}, `c${n++}`)] });
  await assert.rejects(
    runAgentTurn({ complete, messages: [], tools: createTools(memoryWorkspace({ a: 'x' })), approve: async () => true, maxSteps: 3 }),
    /within 3 steps/,
  );
});

test('model ids map to configured connections, and only to them', () => {
  assert.deepEqual(parseModelId('openrouter/anthropic/claude-x'), { provider: 'openrouter', model: 'anthropic/claude-x' });
  assert.deepEqual(parseModelId('ollama_chat/qwen3:8b'), { provider: 'ollama', model: 'qwen3:8b' });
  const connections = [{ id: 'k1', provider: 'ollama', baseUrl: 'http://192.168.0.10:11434/v1' }];
  const resolved = resolveModel('ollama/qwen3:8b', connections);
  assert.equal(resolved.ok, true);
  assert.equal(resolved.baseUrl, 'http://192.168.0.10:11434/v1');
  assert.deepEqual(resolveModel('openai/gpt-4o', connections), { ok: false, reason: 'no-connection', provider: 'openai', model: 'gpt-4o' });
  assert.equal(resolveModel('gpt-4o', connections).reason, 'no-provider');
});

test('only portable generation parameters are sent', () => {
  assert.deepEqual(portableParams('{"stream": true, "force_vision": true, "temperature": 0.2}'), { temperature: 0.2 });
  assert.deepEqual(portableParams('not json'), {});
});

test('context borrows the summary and the newest turns under a budget', () => {
  const chat = { agent_state: JSON.stringify({ recursive_summary: 'Talked about minimax.' }), core_memory: 'Prefers pt-BR' };
  const prompt = buildSystemPrompt({ projectName: 'alpha', settings: { description: 'IA course' }, chat, today: '2026-10-19' });
  assert.match(prompt, /Talked about minimax/);
  assert.match(prompt, /Prefers pt-BR/);
  assert.match(prompt, /IA course/);
  assert.equal(recursiveSummaryOf({ agent_state: JSON.stringify({ recursive_summary: 'No history has been evicted yet.' }) }), '');
  const history = buildHistory([
    { role: 'user', content: 'a'.repeat(30) },
    { role: 'assistant', content: '```gilzabots-progress\nx\n```\nb' },
    { role: 'user', content: 'c' },
  ], 5);
  assert.deepEqual(history, [{ role: 'assistant', content: 'b' }, { role: 'user', content: 'c' }]);
});
