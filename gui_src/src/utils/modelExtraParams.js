// These names belong to GilzaBots/AgenticBlocks rather than the provider's
// model request. The backend enforces the authoritative copy of this list;
// keeping it here gives immediate feedback in the model editor.
export const RESERVED_EXTRA_MODEL_PARAM_NAMES = new Set([
  'id', 'previous_id', 'provider', 'name', 'connection_id', 'connection_label',
  'api_key', 'api_base', 'api_version', 'base_url', 'custom_llm_provider',
  'organization', 'default_headers', 'extra_headers', 'extra_body',
  'timeout', 'request_timeout', 'force_timeout', 'stream_timeout', 'max_retries',
  'drop_params', 'allowed_openai_params', 'additional_drop_params',
  'model', 'messages', 'tools', 'tool_choice', 'parallel_tool_calls', 'stream',
  'stream_options', 'client', 'async_client', 'http_client', 'mock_response',
  'custom_prompt_dict', 'logger_fn', 'logging_obj', 'litellm_logging_obj',
  'dynamic_input_callbacks', 'dynamic_success_callbacks',
  'dynamic_async_success_callbacks', 'dynamic_failure_callbacks',
  'dynamic_async_failure_callbacks', 'proxy_server_request', 'secret_fields',
  'completion_call_id', 'litellm_call_id', 'function_id', 'deployment_id',
  'model_info', 'model_group', 'model_id', 'fallbacks', 'context_window_fallbacks',
  'input_cost_per_token', 'output_cost_per_token', 'input_cost_per_second',
  'output_cost_per_second',
  'supports_thinking', 'requires_single_system_message', 'prompt_profile',
  'orchestrator_policy', 'supports_image_generation', 'image_route', 'num_ctx',
  'think', 'extra_model_params',
  'temperature', 'max_tokens', 'seed', 'top_p', 'top_k', 'min_p',
  'frequency_penalty', 'presence_penalty', 'repetition_penalty', 'reasoning_effort',
  'max_heartbeats', 'max_context_tokens', 'eviction_threshold', 'auto_compact_threshold',
  'memory_pressure_threshold', 'empty_response_reasoning_fallback',
  'max_idle_heartbeats', 'max_iterations', 'max_tool_calls', 'on_max_iterations',
  'debug', 'use_shared_router', 'loop_detection', 'loop_detection_limit',
  'force_vision', 'pdf_truncate', 'pdf_truncate_pct',
]);

const PARAM_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
const MAX_EXTRA_MODEL_PARAMS = 100;

export function extraModelParamsToRows(params = {}) {
  return Object.entries(params || {}).map(([name, value]) => ({
    name,
    value: JSON.stringify(value),
  }));
}

export function parseExtraModelParamRows(rows = []) {
  if (rows.length > MAX_EXTRA_MODEL_PARAMS) {
    return { error: { code: 'tooMany', name: '' } };
  }
  const params = {};

  for (const row of rows) {
    const name = String(row?.name || '').trim();
    const rawValue = String(row?.value ?? '').trim();
    if (!name && !rawValue) continue;
    if (!name || !rawValue) return { error: { code: 'incomplete', name } };
    if (!PARAM_NAME_RE.test(name)) return { error: { code: 'invalidName', name } };
    if (RESERVED_EXTRA_MODEL_PARAM_NAMES.has(name)) {
      return { error: { code: 'reserved', name } };
    }
    if (Object.prototype.hasOwnProperty.call(params, name)) {
      return { error: { code: 'duplicate', name } };
    }
    try {
      params[name] = JSON.parse(rawValue);
    } catch {
      return { error: { code: 'invalidJson', name } };
    }
  }

  return { params };
}
