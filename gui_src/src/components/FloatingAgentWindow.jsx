import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';

import { readUiScale, viewportPxToApp } from '../utils/uiScale';
import { getPreference, setPreference } from '../utils/preferences';
import {
  clampPlanWindowRect,
  defaultPlanWindowRect,
  movePlanWindowRect,
  parsePlanWindowRect,
  resizePlanWindowRect,
} from '../utils/floatingWindow';

/** The viewport in the app's own CSS pixels — see utils/uiScale.js. */
const appViewport = () => {
  const scale = readUiScale();
  return {
    width: viewportPxToApp(window.innerWidth, scale),
    height: viewportPxToApp(window.innerHeight, scale),
  };
};

/**
 * Shared non-modal shell for agent requests that wait for a user response.
 *
 * There is deliberately no backdrop and no close button. The IDE remains
 * usable while the agent waits, but the request cannot be dismissed in a way
 * that would strand the backend future. Geometry is persisted independently
 * for each caller and clamped so the response controls stay reachable.
 */
export default function FloatingAgentWindow({
  ariaLabel,
  children,
  className = '',
  collapseLabel,
  collapsedStorageKey,
  dragHint,
  expandLabel,
  icon,
  rectStorageKey,
  renderHeaderActions,
  resizeHint,
  title,
  waitingLabel,
}) {
  const [collapsed, setCollapsed] = useState(
    () => getPreference(collapsedStorageKey, 'false') === 'true'
  );
  const [rect, setRect] = useState(() => {
    const viewport = appViewport();
    const stored = parsePlanWindowRect(getPreference(rectStorageKey, null));
    return stored ? clampPlanWindowRect(stored, viewport) : defaultPlanWindowRect(viewport);
  });
  const [gesture, setGesture] = useState(null);
  const pointerRef = useRef(null);
  const collapsedRef = useRef(collapsed);
  collapsedRef.current = collapsed;
  const rectRef = useRef(rect);
  rectRef.current = rect;

  const startGesture = (kind) => (event) => {
    if (event.button !== 0 || event.target.closest('button')) return;
    pointerRef.current = { x: event.clientX, y: event.clientY };
    setGesture(kind);
    event.preventDefault();
  };

  useEffect(() => {
    if (!gesture) return undefined;
    const scale = readUiScale();
    const handleMove = (event) => {
      const last = pointerRef.current;
      if (!last) return;
      const dx = viewportPxToApp(event.clientX - last.x, scale);
      const dy = viewportPxToApp(event.clientY - last.y, scale);
      pointerRef.current = { x: event.clientX, y: event.clientY };
      setRect((previous) => (gesture === 'move'
        ? movePlanWindowRect(previous, dx, dy, appViewport(), { collapsed: collapsedRef.current })
        : resizePlanWindowRect(previous, dx, dy, appViewport())));
    };
    const endGesture = () => {
      pointerRef.current = null;
      setGesture(null);
      setPreference(rectStorageKey, JSON.stringify(rectRef.current));
    };
    window.addEventListener('pointermove', handleMove);
    window.addEventListener('pointerup', endGesture);
    window.addEventListener('pointercancel', endGesture);
    return () => {
      window.removeEventListener('pointermove', handleMove);
      window.removeEventListener('pointerup', endGesture);
      window.removeEventListener('pointercancel', endGesture);
    };
  }, [gesture, rectStorageKey]);

  useLayoutEffect(() => {
    const reclamp = () => {
      setRect((previous) => {
        const next = clampPlanWindowRect(previous, appViewport(), { collapsed: collapsedRef.current });
        return next.x === previous.x && next.y === previous.y
          && next.width === previous.width && next.height === previous.height
          ? previous
          : next;
      });
    };
    reclamp();
    window.addEventListener('resize', reclamp);
    return () => window.removeEventListener('resize', reclamp);
  }, [collapsed]);

  const toggleCollapsed = () => {
    setCollapsed((previous) => {
      const next = !previous;
      setPreference(collapsedStorageKey, String(next));
      return next;
    });
  };

  return (
    <div
      className={`plan-window agent-request-window${className ? ` ${className}` : ''}${gesture ? ' is-gesturing' : ''}${collapsed ? ' is-collapsed' : ''}`}
      role="dialog"
      aria-modal="false"
      aria-label={ariaLabel || title}
      style={{
        left: `${rect.x}px`,
        top: `${rect.y}px`,
        width: `${rect.width}px`,
        height: collapsed ? 'auto' : `${rect.height}px`,
      }}
    >
      <div
        className="plan-window-header"
        onPointerDown={startGesture('move')}
        onDoubleClick={toggleCollapsed}
        title={dragHint}
      >
        {icon}
        <span className="plan-window-title">{title}</span>
        <span className="plan-window-waiting" aria-live="polite">
          <span className="plan-window-waiting-dot" />
          {waitingLabel}
        </span>
        <span className="plan-window-header-spacer" />
        {!collapsed && renderHeaderActions?.()}
        <button
          type="button"
          className="plan-window-header-btn plan-window-icon-btn"
          onClick={toggleCollapsed}
          title={collapsed ? expandLabel : collapseLabel}
          aria-label={collapsed ? expandLabel : collapseLabel}
        >
          {collapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      </div>

      {!collapsed && (
        <>
          {children}
          <div
            className="plan-window-resize"
            onPointerDown={startGesture('resize')}
            title={resizeHint}
          />
        </>
      )}
    </div>
  );
}
