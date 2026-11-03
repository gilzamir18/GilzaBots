// Model-id helpers shared by the UI.
//
// isLocalModelId mirrors `is_local_model` in gilzabots/config.py: a local model
// runs on the user's own machine, where the context window is small and every
// attachment competes with the conversation for it. Cloud models — including
// Ollama's own ":cloud" ids — do not have that constraint.

const LOCAL_PROVIDERS = new Set(['ollama', 'ollama_chat']);

export function isOllamaCloudModel(model) {
  const modelId = String(model || '').trim().toLowerCase();
  if (!modelId.includes('/')) return false;
  const [provider, name] = [modelId.slice(0, modelId.indexOf('/')), modelId.slice(modelId.indexOf('/') + 1)];
  if (!LOCAL_PROVIDERS.has(provider)) return false;
  return name.endsWith(':cloud') || name.endsWith('-cloud');
}

export function isLocalModelId(model, apiBase = '') {
  const modelId = String(model || '');
  if (!modelId.includes('/')) return false;
  const provider = modelId.slice(0, modelId.indexOf('/')).toLowerCase();
  if (!LOCAL_PROVIDERS.has(provider)) return false;
  if (isOllamaCloudModel(modelId)) return false;

  const base = String(apiBase || '').trim().toLowerCase();
  if (!base) return true;
  if (base.includes('localhost') || base.includes('127.0.0.1') || base.includes('[::1]')) return true;
  return base.startsWith('http://0.0.0.0') || base.startsWith('http://::1');
}
