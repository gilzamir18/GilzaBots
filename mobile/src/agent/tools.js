/**
 * What the phone's agent can do with the project.
 *
 * Reading is free; anything that changes a file in Drive needs the user's
 * approval, shown with the exact change, the same contract the desktop applies
 * to its write tools. Nothing writes under `.gilzabots/`, which holds the
 * desktops' sync state and the conversation files.
 *
 * `workspace` is the project as files: see project/workspace.js.
 */

const MAX_READ_CHARS = 60000;
const MAX_LIST_ENTRIES = 500;
const MAX_MATCHES = 100;

export class ToolInputError extends Error {}

export function normalizeProjectPath(path) {
  const text = String(path ?? '').trim().replace(/\\/g, '/').replace(/^\.\/+/, '').replace(/^\/+/, '').replace(/\/+$/, '');
  if (text.split('/').some((part) => part === '..')) throw new ToolInputError('Paths must stay inside the project.');
  return text;
}

function assertWritable(path) {
  if (!path) throw new ToolInputError('A file path is required.');
  if (path === '.gilzabots' || path.startsWith('.gilzabots/')) {
    throw new ToolInputError('Files under .gilzabots/ belong to GilzaBots itself and cannot be written.');
  }
}

function requireString(args, name) {
  const value = args?.[name];
  if (typeof value !== 'string') throw new ToolInputError(`"${name}" must be a string.`);
  return value;
}

export function createTools(workspace) {
  return [
    {
      name: 'list_files',
      description: 'List the files of the project, optionally under one folder. Paths are relative to the project root.',
      parameters: {
        type: 'object',
        properties: { path: { type: 'string', description: 'Folder to list; empty for the whole project.' } },
      },
      async run(args) {
        const dir = normalizeProjectPath(args?.path || '');
        const prefix = dir ? `${dir}/` : '';
        const paths = (await workspace.listPaths()).filter((path) => !prefix || path.startsWith(prefix));
        if (!paths.length) return dir ? `No files under "${dir}".` : 'The project has no files.';
        const shown = paths.slice(0, MAX_LIST_ENTRIES);
        const more = paths.length - shown.length;
        return shown.join('\n') + (more > 0 ? `\n… and ${more} more` : '');
      },
    },
    {
      name: 'read_file',
      description: 'Read a text file of the project. Optionally a line range (1-based, inclusive).',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string' },
          start_line: { type: 'integer', minimum: 1 },
          end_line: { type: 'integer', minimum: 1 },
        },
        required: ['path'],
      },
      async run(args) {
        const path = normalizeProjectPath(requireString(args, 'path'));
        const { content } = await workspace.readText(path);
        const lines = content.split('\n');
        const start = Math.max(1, Number(args.start_line) || 1);
        const end = Math.min(lines.length, Number(args.end_line) || lines.length);
        let text = lines.slice(start - 1, end).map((line, index) => `${start + index}: ${line}`).join('\n');
        if (text.length > MAX_READ_CHARS) {
          text = `${text.slice(0, MAX_READ_CHARS)}\n… truncated; read a narrower line range.`;
        }
        return `${path} (lines ${start}-${end} of ${lines.length})\n${text}`;
      },
    },
    {
      name: 'search_files',
      description: 'Search the text files of the project for a string (case-insensitive). Returns path:line: text.',
      parameters: {
        type: 'object',
        properties: {
          query: { type: 'string' },
          path: { type: 'string', description: 'Limit the search to this folder.' },
        },
        required: ['query'],
      },
      async run(args) {
        const query = requireString(args, 'query').toLowerCase();
        if (!query) throw new ToolInputError('"query" must not be empty.');
        const dir = normalizeProjectPath(args.path || '');
        const prefix = dir ? `${dir}/` : '';
        const matches = [];
        for (const path of await workspace.listTextPaths()) {
          if (prefix && !path.startsWith(prefix)) continue;
          const { content } = await workspace.readText(path);
          const lines = content.split('\n');
          for (let index = 0; index < lines.length; index += 1) {
            if (lines[index].toLowerCase().includes(query)) {
              matches.push(`${path}:${index + 1}: ${lines[index].slice(0, 300)}`);
              if (matches.length >= MAX_MATCHES) return `${matches.join('\n')}\n… stopped at ${MAX_MATCHES} matches.`;
            }
          }
        }
        return matches.length ? matches.join('\n') : 'No matches.';
      },
    },
    {
      name: 'edit_file',
      description:
        'Replace one exact passage of a text file. old_string must occur exactly once. ' +
        'The user reviews and approves the change before it is written to Google Drive.',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string' },
          old_string: { type: 'string' },
          new_string: { type: 'string' },
        },
        required: ['path', 'old_string', 'new_string'],
      },
      requiresApproval: true,
      async prepare(args) {
        const path = normalizeProjectPath(requireString(args, 'path'));
        assertWritable(path);
        const oldString = requireString(args, 'old_string');
        const newString = requireString(args, 'new_string');
        if (!oldString) throw new ToolInputError('"old_string" must not be empty.');
        const { content, md5 } = await workspace.readText(path);
        const first = content.indexOf(oldString);
        if (first < 0) throw new ToolInputError(`old_string does not occur in ${path}.`);
        if (content.indexOf(oldString, first + 1) >= 0) {
          throw new ToolInputError(`old_string occurs more than once in ${path}; include more context.`);
        }
        const updated = content.slice(0, first) + newString + content.slice(first + oldString.length);
        return { kind: 'edit', path, before: oldString, after: newString, content: updated, md5 };
      },
      async commit(change) {
        await workspace.writeText(change.path, change.content, change.md5);
        return `Edited ${change.path}.`;
      },
    },
    {
      name: 'create_file',
      description: 'Create a new text file. Fails if it exists. The user approves it before it is written.',
      parameters: {
        type: 'object',
        properties: { path: { type: 'string' }, content: { type: 'string' } },
        required: ['path', 'content'],
      },
      requiresApproval: true,
      async prepare(args) {
        const path = normalizeProjectPath(requireString(args, 'path'));
        assertWritable(path);
        const content = requireString(args, 'content');
        if ((await workspace.listPaths()).includes(path)) {
          throw new ToolInputError(`${path} already exists; use edit_file to change it.`);
        }
        return { kind: 'create', path, before: '', after: content, content, md5: null };
      },
      async commit(change) {
        await workspace.writeText(change.path, change.content, null);
        return `Created ${change.path}.`;
      },
    },
  ];
}
