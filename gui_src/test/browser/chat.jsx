// ─────────────────────────────────────────────────────────────────────────────
// chat.jsx
//
// Mounts the chat panel on its own page so the browser suite can drive the
// message editor (the pencil on a user message) with real keys and clicks.
//
// The editor's contract with App.jsx is `onEditUserMessage`, which resolves to
// `{ accepted, error }`. The harness stands in for App.jsx there and nowhere
// else: `window.__editMode` decides whether the next edit is accepted or
// refused, and `window.__editCalls` records what the panel handed over. What is
// under test is what the panel does with either answer — whether a refused
// edit keeps its text and says why, and whether the keys do what they promise.
// ─────────────────────────────────────────────────────────────────────────────

import React, { useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';

import '../../src/i18n/index.js';
import '../../src/index.css';
import ChatPanel from '../../src/components/ChatPanel.jsx';
import { CustomDialogProvider } from '../../src/components/modals/CustomDialogProvider.jsx';

// The panel's own requests (settings, chat list) reach the Python server, which
// vite does not run. They are answered empty so nothing logs network noise.
(function installInertApi() {
  const real = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    if (String(input).startsWith('/api/')) {
      return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
    }
    return real(input, init);
  };
})();

const MESSAGES = [
  { id: 1, role: 'user', content: 'First question', client_message_id: 'cid-a', timestamp: '2026-11-01T10:00:00Z' },
  { id: 2, role: 'assistant', content: 'First answer', timestamp: '2026-11-01T10:00:05Z' },
  { id: 3, role: 'user', content: 'Second question', client_message_id: 'cid-c', timestamp: '2026-11-01T10:01:00Z' },
  { id: 4, role: 'assistant', content: 'Second answer', timestamp: '2026-11-01T10:01:05Z' },
];

window.__editMode = 'refuse';
window.__editCalls = [];

function Host() {
  const [chatMessages, setChatMessages] = useState(MESSAGES);
  const [chatInput, setChatInput] = useState('');
  const [pendingAttachments, setPendingAttachments] = useState([]);
  const [chats, setChats] = useState([{ id: 'chat-1', name: 'Chat' }]);
  const [activeChatId, setActiveChatId] = useState('chat-1');
  const [chatContextUsage, setChatContextUsage] = useState(null);
  const [webSearchConfig, setWebSearchConfig] = useState({});
  const [heartbeatMode, setHeartbeatMode] = useState('medium');
  const chatEndRef = useRef(null);
  window.__composerText = () => chatInput;
  window.__sentFromComposer = window.__sentFromComposer || [];

  const onEditUserMessage = async (messageIndex, message, content) => {
    window.__editCalls.push({ messageIndex, id: message.id, content });
    await new Promise(r => setTimeout(r, 50));
    if (window.__editMode === 'refuse') {
      return { accepted: false, error: 'Refused by the harness: the agent is still working.' };
    }
    return { accepted: true };
  };

  return (
    <ChatPanel
      chatMessages={chatMessages}
      setChatMessages={setChatMessages}
      chatInput={chatInput}
      setChatInput={setChatInput}
      isAgentRunning={false}
      heartbeatMode={heartbeatMode}
      setHeartbeatMode={setHeartbeatMode}
      chatContextUsage={chatContextUsage}
      setChatContextUsage={setChatContextUsage}
      activeProject={{ name: 'harness', project_path: '/tmp/harness', model: '' }}
      isChatVisible
      setIsChatVisible={() => {}}
      chatWidth={720}
      fillContainer
      handleSendMessage={() => { window.__sentFromComposer.push(chatInput); }}
      onEditUserMessage={onEditUserMessage}
      chatEndRef={chatEndRef}
      webSearchConfig={webSearchConfig}
      setWebSearchConfig={setWebSearchConfig}
      activeChatId={activeChatId}
      setActiveChatId={setActiveChatId}
      chats={chats}
      setChats={setChats}
      onSwitchChat={() => {}}
      pendingAttachments={pendingAttachments}
      setPendingAttachments={setPendingAttachments}
      isChatMode
    />
  );
}

createRoot(document.getElementById('root')).render(
  <CustomDialogProvider>
    <Host />
  </CustomDialogProvider>
);
window.__ready = true;
