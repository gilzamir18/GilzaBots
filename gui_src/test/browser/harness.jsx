// ─────────────────────────────────────────────────────────────────────────────
// harness.jsx
//
// The browser suite's landing page: mounts the terminal on its own page so the
// suite can drive it. Deliberately NOT part of the app bundle: `vite build`
// only takes the root index.html as an entry, so nothing here reaches a release.
//
// The harness reproduces the one thing about the real IDE that the terminal's
// selection defects depended on: `.vscode-app`, which carries the accessibility
// scale as a CSS `zoom`. The suite runs at several scales, so the harness sets
// `--ui-scale` on the root element and lets the class do the rest, as App.jsx
// does it.
// ─────────────────────────────────────────────────────────────────────────────

import React from 'react';
import { createRoot } from 'react-dom/client';

import '../../src/i18n/index.js';
import '../../src/index.css';
import TerminalInstance from '../../src/components/TerminalInstance.jsx';

// The terminal fixture exercises only the browser-side xterm integration. Its
// backend transport is inert so the harness stays deterministic and offline.
window.EventSource = class HarnessEventSource {
  close() {}
};

(function installTerminalApi() {
  const real = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    if (String(input).includes('/api/terminal/')) {
      return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
    }
    return real(input, init);
  };
}());

const params = new URLSearchParams(location.search);
const uiScale = Number(params.get('uiScale') || '1');
document.documentElement.style.setProperty('--ui-scale', String(uiScale));

function Harness() {
  return (
    <div className="vscode-app">
      <div
        id="terminal-fixture"
        style={{
          display: 'none', position: 'fixed', left: '48px', top: '48px',
          width: '640px', height: '320px', zIndex: 10000,
          background: 'var(--vscode-terminal-bg)',
        }}
      >
        <TerminalInstance
          termId="browser-check"
          activeProject={{ project_path: '/tmp' }}
          activeBottomTab="terminal"
          bottomPanelHeight={320}
          isTerminalCollapsed={false}
          theme="dark"
          isActive
          fontSize={13}
          uiScale={uiScale}
          onMount={(term) => { window.__terminal = term; }}
        />
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<Harness />);
window.__ready = true;
