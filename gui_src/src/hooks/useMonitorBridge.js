import { useEffect, useRef, useState } from 'react';

// How often the window tells the monitor daemon it is alive and asks for work.
// The daemon treats a window that has not polled for 20 s as closed
// (opalatex/monitors/service.py GUI_ALIVE_SECONDS) and runs that project's
// wake-ups itself, so this must stay well below that.
export const MONITOR_POLL_MS = 4000;

/**
 * The window's side of the monitor daemon (PROJECT_DESIGN 2.24).
 *
 * Every few seconds it posts `/api/monitors/poll`, which
 *  - registers this window as the host of its open project, so the daemon
 *    leaves that project's wake-ups to it;
 *  - hands it a wake-up of that project when no turn is running. The wake-up
 *    runs as an ordinary turn (`runWake`), streamed in the chat it belongs to,
 *    and its outcome is reported back so a refused one returns to the queue;
 *  - delivers a notification click (`openChat`) and the questions background
 *    turns are waiting on (`inputs`).
 *
 * Callbacks are read through a ref: the poll loop is started once per project,
 * and the callbacks it calls change on every render.
 */
export default function useMonitorBridge({ activeProject, isTurnBusy, runWake, openChat, onInputs }) {
  const [inputs, setInputs] = useState([]);
  const callbacks = useRef({});
  callbacks.current = { isTurnBusy, runWake, openChat, onInputs };
  const wakeInFlight = useRef(false);
  const inputsKey = useRef('');
  const projectName = activeProject?.name || '';
  const projectPath = activeProject?.project_path || '';

  useEffect(() => {
    let stopped = false;
    let timer = null;

    const finishWake = async (id, status, error = '') => {
      try {
        await fetch('/api/monitors/wake/finish', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id, status, error }),
        });
      } catch (err) {
        console.error('Could not report the monitor wake-up outcome', err);
      }
    };

    const handleWake = async (wake) => {
      wakeInFlight.current = true;
      try {
        const outcome = await callbacks.current.runWake(wake);
        const status = outcome === 'completed' ? 'done' : outcome === 'failed' ? 'failed' : 'released';
        await finishWake(wake.id, status);
      } catch (err) {
        await finishWake(wake.id, 'failed', String(err?.message || err));
      } finally {
        wakeInFlight.current = false;
      }
    };

    const tick = async () => {
      try {
        const idle = !wakeInFlight.current && !callbacks.current.isTurnBusy();
        const res = await fetch('/api/monitors/poll', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_name: projectName, project_path: projectPath, idle }),
        });
        if (res.ok && !stopped) {
          const data = await res.json();
          const waiting = Array.isArray(data.inputs) ? data.inputs : [];
          // Only a change re-renders: `inputs` lives in App, and a fresh array
          // on every poll re-rendered the whole application every few seconds.
          const key = waiting.map(item => `${item.id}:${item.status}`).join('|');
          if (key !== inputsKey.current) {
            inputsKey.current = key;
            setInputs(waiting);
          }
          callbacks.current.onInputs?.(waiting);
          if (data.open) callbacks.current.openChat?.(data.open);
          if (data.wake) handleWake(data.wake);
        }
      } catch (err) {
        // The server restarts with the window; a missed poll is retried.
      } finally {
        if (!stopped) timer = setTimeout(tick, MONITOR_POLL_MS);
      }
    };

    tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [projectName, projectPath]);

  return { inputs };
}
