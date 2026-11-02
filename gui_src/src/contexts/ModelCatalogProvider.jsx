import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

/**
 * Single source of truth for the global model catalog (`global_models`).
 *
 * Every surface that lists, registers, edits, or deletes models — the chat
 * toolbar, the project dialogs, Edit Models, and onboarding — reads and writes
 * through this provider. Components must not keep private copies of
 * `/api/settings/models`: a local snapshot goes stale as soon as another screen
 * registers a model, which is how a freshly onboarded model could exist in the
 * store and in the project while the chat model pickers stayed empty.
 */
const ModelCatalogContext = createContext(null);

const MODELS_ENDPOINT = '/api/settings/models';
const PROVIDERS_ENDPOINT = '/api/settings/providers';

export function ModelCatalogProvider({ children }) {
  const [models, setModels] = useState([]);
  const [connections, setConnections] = useState([]);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(MODELS_ENDPOINT);
      const data = await res.json();
      const catalog = Array.isArray(data.models) ? data.models : [];
      setModels(catalog);
      return catalog;
    } catch (e) {
      console.error(e);
      return null;
    }
  }, []);

  const refreshConnections = useCallback(async () => {
    try {
      const res = await fetch(PROVIDERS_ENDPOINT);
      const data = await res.json();
      const catalog = Array.isArray(data.connections) ? data.connections : [];
      setConnections(catalog);
      return catalog;
    } catch (e) {
      console.error(e);
      return null;
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => { refreshConnections(); }, [refreshConnections]);

  // Registers or updates a catalog entry. Returns the backend outcome so callers
  // can surface a failure instead of silently pretending the model was saved.
  const saveModel = useCallback(async (modelData) => {
    try {
      const res = await fetch(MODELS_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(modelData)
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, error: data.error || 'model_save_failed' };
      await refresh();
      return { ok: true };
    } catch (e) {
      console.error(e);
      return { ok: false, error: 'model_save_failed' };
    }
  }, [refresh]);

  const deleteModel = useCallback(async (modelId) => {
    try {
      const res = await fetch(MODELS_ENDPOINT, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: modelId })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, error: data.error || 'model_delete_failed' };
      await refresh();
      return { ok: true };
    } catch (e) {
      console.error(e);
      return { ok: false, error: 'model_delete_failed' };
    }
  }, [refresh]);

  const loadLocalOllamaModels = useCallback(async () => {
    try {
      const res = await fetch(`${MODELS_ENDPOINT}/load-local-ollama`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { status: data.error || 'local_ollama_load_failed' };
      await Promise.all([refresh(), refreshConnections()]);
      return { status: 'loaded', count: data.added_count || 0 };
    } catch {
      return { status: 'local_ollama_load_failed' };
    }
  }, [refresh, refreshConnections]);

  // Registers or updates a provider connection (label/API key/base URL).
  // Returns the backend outcome so callers can surface a failure instead of
  // silently pretending the connection was saved.
  const saveConnection = useCallback(async (connectionData) => {
    try {
      const res = await fetch(PROVIDERS_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(connectionData)
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, error: data.error || 'connection_save_failed' };
      await Promise.all([refresh(), refreshConnections()]);
      return { ok: true, added_model: data.added_model || null };
    } catch (e) {
      console.error(e);
      return { ok: false, error: 'connection_save_failed' };
    }
  }, [refresh, refreshConnections]);

  const startOpenRouterOAuth = useCallback(async ({ label = '', connectionId = '' } = {}) => {
    try {
      const res = await fetch(`${PROVIDERS_ENDPOINT}/openrouter/oauth`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label, connection_id: connectionId })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) {
        return { ok: false, error: data.error || 'openrouter_oauth_failed' };
      }
      return {
        ok: true,
        authorization_url: data.authorization_url || '',
        session: data.session || null,
        instructions: data.instructions || '',
      };
    } catch (e) {
      console.error(e);
      return { ok: false, error: 'openrouter_oauth_failed' };
    }
  }, []);

  const completeOpenRouterOAuth = useCallback(async ({ session, label = '', connectionId = '' } = {}) => {
    try {
      const res = await fetch(`${PROVIDERS_ENDPOINT}/openrouter/oauth/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session, label, connection_id: connectionId })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) {
        return { ok: false, error: data.error || 'openrouter_oauth_failed' };
      }
      await Promise.all([refresh(), refreshConnections()]);
      return {
        ok: true,
        connection: data.connection || null,
        default_model: data.default_model || null,
        added_default_model: Boolean(data.added_default_model),
      };
    } catch (e) {
      console.error(e);
      return { ok: false, error: 'openrouter_oauth_failed' };
    }
  }, [refresh, refreshConnections]);

  const cancelOpenRouterOAuth = useCallback(async (session) => {
    if (!session) return { ok: true };
    try {
      await fetch(`${PROVIDERS_ENDPOINT}/openrouter/oauth/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session })
      });
      return { ok: true };
    } catch (e) {
      console.error(e);
      return { ok: false };
    }
  }, []);

  const deleteConnection = useCallback(async (connectionId) => {
    try {
      const res = await fetch(PROVIDERS_ENDPOINT, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: connectionId })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, error: data.error || 'connection_delete_failed' };
      await refreshConnections();
      return { ok: true };
    } catch (e) {
      console.error(e);
      return { ok: false, error: 'connection_delete_failed' };
    }
  }, [refreshConnections]);

  const value = useMemo(
    () => ({
      models, refresh, saveModel, deleteModel, loadLocalOllamaModels,
      connections, refreshConnections, saveConnection, deleteConnection,
      startOpenRouterOAuth, completeOpenRouterOAuth, cancelOpenRouterOAuth,
    }),
    [models, refresh, saveModel, deleteModel, loadLocalOllamaModels,
      connections, refreshConnections, saveConnection, deleteConnection,
      startOpenRouterOAuth, completeOpenRouterOAuth, cancelOpenRouterOAuth]
  );

  return (
    <ModelCatalogContext.Provider value={value}>
      {children}
    </ModelCatalogContext.Provider>
  );
}

export function useModelCatalog() {
  const context = useContext(ModelCatalogContext);
  if (!context) {
    throw new Error('useModelCatalog must be used inside a ModelCatalogProvider');
  }
  return context;
}
