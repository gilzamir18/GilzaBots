// ─────────────────────────────────────────────────────────────────────────────
// editor.jsx
//
// Mounts the editor panel on its own page so the browser suite can check what
// Monaco shows when the open file changes.
//
// The host stands in for App.jsx's buffer bookkeeping and nothing else: one
// live buffer (`fileContent`) for the selected file, a cache of the others
// (`fileContents`), and the same sequence `handleFileSelect` runs to switch —
// stash the live editor text under the file being left, then show the cached
// buffer or read the file from the (stubbed) API. `window.__open` switches
// files the way an agent's `open_file` does, without a click that would take
// the keyboard focus away from Monaco. `window.__setCached` stands in for the
// agent sync updating a hidden tab's buffer after a write.
// ─────────────────────────────────────────────────────────────────────────────

import React, { useCallback, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';

import '../../src/i18n/index.js';
import '../../src/index.css';
import EditorPanel from '../../src/components/EditorPanel.jsx';
import { CustomDialogProvider } from '../../src/components/modals/CustomDialogProvider.jsx';

const FILES = {
  'src/regressao.py': 'import random\n\nXd = [[1, 60], [1, 80]]\n',
  'src/anim/gradient.html': '<!DOCTYPE html>\n<html><body>gradient</body></html>\n',
};

// File reads answer after a delay, as the server's do: the switch to an
// uncached file then lands in a later commit than the click that asked for it.
(function installApi() {
  const real = window.fetch.bind(window);
  const json = (body) => new Response(JSON.stringify(body), {
    status: 200, headers: { 'content-type': 'application/json' },
  });
  window.fetch = async (input, init) => {
    const url = String(input);
    if (url.startsWith('/api/file/read')) {
      const filePath = new URL(url, window.location.href).searchParams.get('filePath');
      await new Promise(resolve => setTimeout(resolve, 80));
      return json({ content: FILES[filePath] ?? '' });
    }
    if (url.startsWith('/api/')) return json({});
    return real(input, init);
  };
})();

function Host() {
  const [selectedFile, setSelectedFile] = useState(null);
  const [openFiles, setOpenFiles] = useState([]);
  const [fileContent, setFileContentState] = useState('');
  const [fileContents, setFileContents] = useState({});
  const fileContentRef = useRef('');
  const editorRef = useRef(null);

  const setFileContent = useCallback(value => {
    const next = typeof value === 'function' ? value(fileContentRef.current) : value;
    fileContentRef.current = next;
    setFileContentState(next);
  }, []);

  const liveText = () => editorRef.current?.getModel?.()?.getValue?.() ?? fileContentRef.current;

  const handleFileSelect = async (filePath) => {
    if (selectedFile) {
      const current = liveText();
      setFileContents(prev => ({ ...prev, [selectedFile]: current }));
      fileContentRef.current = current;
    }
    setOpenFiles(prev => (prev.includes(filePath) ? prev : [...prev, filePath]));
    if (fileContents[filePath] !== undefined) {
      setFileContent(fileContents[filePath]);
      setSelectedFile(filePath);
      return;
    }
    const res = await fetch(`/api/file/read?projectPath=/p&filePath=${encodeURIComponent(filePath)}`);
    const data = await res.json();
    setFileContent(data.content);
    setFileContents(prev => ({ ...prev, [filePath]: data.content }));
    setSelectedFile(filePath);
  };

  window.__open = handleFileSelect;
  window.__setCached = (filePath, content) => setFileContents(prev => ({ ...prev, [filePath]: content }));
  window.__editor = () => {
    const editor = editorRef.current;
    const model = editor?.getModel?.();
    return {
      selected: selectedFile,
      model: model ? model.uri.path.replace(/^\//, '') : null,
      text: model ? model.getValue() : null,
      buffer: fileContent,
      cached: fileContents,
      focused: !!editor?.hasTextFocus?.(),
    };
  };

  return (
    <div style={{ display: 'flex', height: '100vh' }}>
      <div style={{ width: 220 }}>
        {Object.keys(FILES).map(filePath => (
          <div
            key={filePath}
            className="editor-fixture-file"
            data-path={filePath}
            style={{ cursor: 'pointer', userSelect: 'none', padding: 4 }}
            onClick={() => handleFileSelect(filePath)}
          >
            {filePath}
          </div>
        ))}
      </div>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
        <EditorPanel
          selectedFile={selectedFile}
          openFiles={openFiles}
          fileContent={fileContent}
          originalFileContents={{}}
          unsavedFiles={new Set()}
          agentChangedFiles={new Set()}
          isSaving={false}
          theme="dark"
          editorFontSize={14}
          setEditorFontSize={() => {}}
          editorTabSize={4}
          editorWordWrap="off"
          handleFileSelect={handleFileSelect}
          handleCloseTab={() => {}}
          handleCloseOtherTabs={() => {}}
          handleCloseAllTabs={() => {}}
          saveFile={async () => true}
          handleEditorDidMount={(editor) => { editorRef.current = editor; window.__ready = true; }}
          setFileContent={setFileContent}
          activeProject={{ project_path: '/p' }}
          jumpToLine={null}
          setJumpToLine={() => {}}
        />
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')).render(
  <CustomDialogProvider><Host /></CustomDialogProvider>,
);
