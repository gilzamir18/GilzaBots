"""Global configuration defaults for GilzaBots."""

import os
import sys
import tempfile
import yaml
import pathlib
from dotenv import load_dotenv

# Load local .env
load_dotenv()

# Suppress the non-fatal LiteLLM logging worker warning (coroutine never awaited)
# which frequently occurs in asynchronous contexts on newer Python versions.
import warnings
warnings.filterwarnings(
    "ignore",
    category=RuntimeWarning,
    message=".*coroutine 'Logging.async_success_handler' was never awaited.*"
)

# A directory that `os.path.isdir` accepts can still be unusable (read-only
# mounts, permissions or ACLs that deny creating files, a directory owned by
# another user), and `os.access` does not catch every such case. Accepting such
# a path used to surface much later as
# `sqlite3.OperationalError: unable to open database file`, so the only usable
# probe is to actually create a file in the directory. Successful probes are
# cached because get_gilzabots_home() is called from hot paths.
_verified_data_dirs: set[str] = set()
_reported_data_dir_problems: set[str] = set()


def check_data_dir(path: str) -> str:
    """Return why `path` cannot hold GilzaBots's global data, or "" if it can."""
    if not path:
        return "the path is empty"
    if path in _verified_data_dirs:
        return ""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return f"the directory could not be created ({exc.strerror or exc})"
    if not os.path.isdir(path):
        return "the path is not a directory"
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".gilzabots-probe-"):
            pass
    except OSError as exc:
        return f"the directory is not writable ({exc.strerror or exc})"
    _verified_data_dirs.add(path)
    return ""


def project_dir_error(path: str) -> str:
    """A full, user-facing explanation of why `path` cannot be a project folder, or "".

    A project keeps its state in ``<path>/.gilzabots``, so that directory is
    probed as well: a folder can accept files while its ``.gilzabots``
    subdirectory cannot be created or written (an existing one owned by another
    user, for example).
    """
    for candidate in (path, os.path.join(path, ".gilzabots")):
        problem = check_data_dir(candidate)
        if problem:
            return f"Cannot use '{path}' as a project folder: {problem} ('{candidate}')."
    return ""


def data_dir_error(path: str) -> str:
    """A full, user-facing explanation of why `path` is unusable, or ""."""
    problem = check_data_dir(path)
    if not problem:
        return ""
    return f"'{path}' cannot be used as the GilzaBots data directory: {problem}."


def configured_gilzabots_home() -> str:
    """The data directory the user asked for, whether or not it is usable."""
    if os.environ.get("GILZABOTS_HOME"):
        return os.environ["GILZABOTS_HOME"]
    pointer_file = pathlib.Path.home() / ".gilzabotshome"
    try:
        if pointer_file.exists():
            return pointer_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


# Load global .env
def get_gilzabots_home() -> str:
    default_home = str(pathlib.Path.home() / ".gilzabots")
    if os.environ.get("GILZABOTS_HOME"):
        return os.environ["GILZABOTS_HOME"]
    custom_path = configured_gilzabots_home()
    if custom_path:
        error = data_dir_error(custom_path)
        if not error:
            return custom_path
        if custom_path not in _reported_data_dir_problems:
            _reported_data_dir_problems.add(custom_path)
            print(
                f"Warning: {error} Falling back to '{default_home}'.",
                file=sys.stderr,
            )
    return default_home

global_env = pathlib.Path(get_gilzabots_home()) / ".env"
if global_env.exists():
    load_dotenv(dotenv_path=global_env)

def _load_yaml(filename: str) -> dict:
    candidates = [
        pathlib.Path(__file__).parent / filename,
        pathlib.Path(__file__).parent.parent / filename,
        pathlib.Path(os.getcwd()) / filename,
    ]
    for config_path in candidates:
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                print(f"Warning: Failed to parse {config_path}: {e}")
    return {}

# Fallback for old imports that might expect a static dict, though they shouldn't.
_AGENTS_CONFIG = {} 
_APP_CONFIG = _load_yaml("config.yaml")

# Hardcoded defaults for internal GilzaBots skills so they work without agents.yaml
# `think` is deliberately absent from every role below: it is resolved from the
# selected model's catalog capability alone (see resolve_think_request), so a
# role-level default here would be dead configuration that reads as a second
# source of truth.
_CORE_AGENT_DEFAULTS = {
    "memgpt": {
        "num_ctx": 16384,
        # A runaway guardrail: the turn runs for as long as tool calls are
        # pending, and finishes cleanly in normal text before the limit.
        "max_heartbeats": 50,
        "debug": False,
    },
    "landscape_planner": {
        "num_ctx": 8192,
    },
    "refinement_agent": {
        "num_ctx": 8192,
    },
    "orchestrator": {
        "num_ctx": 16384,
        "max_heartbeats": 20,
        "debug": False,
        "strategy": "workflow",
    },
    "worker": {
        "num_ctx": 16384,
        "debug": False,
    }
}

def _deep_merge(target: dict, source: dict) -> dict:
    """Recursively merge source dictionary into target."""
    result = dict(target)
    for k, v in source.items():
        if isinstance(v, dict) and k in result and isinstance(result[k], dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result

def _get_agents_config() -> dict:
    """Dynamically load and merge agents.yaml from User Home and Project Path."""
    # Start with core defaults
    cfg = {"agents": _deep_merge({}, _CORE_AGENT_DEFAULTS)}
    
    # User global
    user_yaml = pathlib.Path(get_gilzabots_home()) / "agents.yaml"
    if user_yaml.exists():
        try:
            with open(user_yaml, "r", encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
                cfg = _deep_merge(cfg, user_cfg)
        except Exception as e:
            print(f"Warning: Failed to parse {user_yaml}: {e}")
            
    # Project local. Read through sys.modules rather than imported: this runs at
    # module load, and importing `tools` drags in agenticblocks and litellm
    # (~4 s), which delayed everything that only needs configuration — the
    # desktop window's splash screen included. Until `tools` has been imported
    # no project can have been set, and its get_project_path() would return
    # the working directory, which is what is used then.
    try:
        tools = sys.modules.get(f"{__package__}.tools")
        proj_path = tools.get_project_path() if tools is not None else os.getcwd()
        proj_yaml = pathlib.Path(proj_path) / ".gilzabots" / "agents.yaml"
        if not proj_yaml.exists():
            proj_yaml = pathlib.Path(proj_path) / "agents.yaml"
            
        if proj_yaml.exists():
            with open(proj_yaml, "r", encoding="utf-8") as f:
                proj_cfg = yaml.safe_load(f) or {}
                cfg = _deep_merge(cfg, proj_cfg)
    except Exception as e:
        pass
        
    return cfg

# Model used for all agents (can be overridden via CLI --model)
# Evaluated at module load to serve as CLI defaults (will read ~/.gilzabots/agents.yaml if present)
_initial_cfg = _get_agents_config()
DEFAULT_MODEL = _initial_cfg.get("default", os.getenv("GILZABOTS_MODEL", "ollama/gemma4:12b"))
WORKER_MODEL = _initial_cfg.get("worker", _initial_cfg.get("alternative", "gemini/gemini-3.5-flash-lite"))
DEFAULT_LITELLM_TIMEOUT_SECONDS = 600.0


def _get_llm_defaults():
    cfg = _get_agents_config()
    return {
        "temperature": 0.7,
        "num_ctx": 8192,
        "timeout": DEFAULT_LITELLM_TIMEOUT_SECONDS,
        "request_timeout": DEFAULT_LITELLM_TIMEOUT_SECONDS,
        **cfg.get("llm_defaults", {}),
    }

def _get_agent_overrides():
    return _get_agents_config().get("agents", {})


# Fields that are agent constructor params, not LiteLLM kwargs — strip before passing to model_kwargs.
_NON_LITELLM_FIELDS = {
    "model", "strategy",
    # MemGPTAgentBlock params
    "max_heartbeats", "max_context_tokens", "eviction_threshold",
    "auto_compact_threshold",
    "memory_pressure_threshold",
    "heartbeat_pressure_threshold",
    "empty_response_reasoning_fallback",
    "max_idle_heartbeats",
    # LLMAgentBlock params
    "max_iterations", "max_tool_calls", "on_max_iterations",
    # Shared
    "debug", "use_shared_router", "loop_detection", "loop_detection_limit",
}

# Internal GilzaBots feature flags stored with model_params, but never accepted by LiteLLM.
_INTERNAL_MODEL_PARAM_FIELDS = {
    "force_vision",
    "pdf_truncate",
    "pdf_truncate_pct",
    "supports_thinking",
}

# LiteLLM passes these fields through to providers that do not understand them.
# They are useful for local Ollama models, but OpenAI-compatible endpoints reject
# them as unknown request parameters.
_LOCAL_ONLY_LITELLM_FIELDS = {
    "num_ctx",
    "top_k",
    "min_p",
    "repetition_penalty",
    # Ollama's own name for the repetition penalty, which is what the wire
    # actually accepts. `sanitize_litellm_kwargs_for_model` renames the catalog
    # field onto it for the Ollama route; it must never reach an OpenAI-style
    # endpoint, which is exactly what listing it here guarantees.
    "repeat_penalty",
    "think",
}

_OLLAMA_COMPAT_LITELLM_FIELDS = _LOCAL_ONLY_LITELLM_FIELDS | {
    "presence_penalty",
}

_COMMON_SAMPLING_LITELLM_FIELDS = {
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
}

_LITELLM_TRANSPORT_FIELDS = {
    "api_key",
    "api_base",
    "api_version",
    "base_url",
    "custom_llm_provider",
    "organization",
    "default_headers",
    "extra_headers",
    "timeout",
    "request_timeout",
    "force_timeout",
    "stream_timeout",
    "max_retries",
    "drop_params",
    "allowed_openai_params",
    "additional_drop_params",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
}

# The allow-list for values a project may store in model_params/worker_model_params.
# Model inference parameters (temperature, max_tokens, seed, top_p, top_k, min_p,
# penalties, reasoning_effort) belong to the global model catalog entry. Only
# agent execution controls, transport (stream), the `num_ctx` budgeting override,
# and vision/attachment flags remain in project configuration.
#
# `num_ctx` is deliberately NOT an inference parameter: it is never transmitted to
# an `openai/` provider, it only drives GilzaBots-side prompt budgeting (see
# `resolve_effective_num_ctx`), and a project working against a host with a
# smaller runtime KV-cache budget than the model's catalog entry advertises needs
# to lower it for that project alone.
_MODEL_PARAMS_SCHEMA = {
    "stream": {"type": bool},
    "num_ctx": {"type": int, "min": 1},
    # Vision / attachment settings
    "force_vision": {"type": bool},
    "pdf_truncate": {"type": bool},
    "pdf_truncate_pct": {"type": int, "min": 1, "max": 100},
    # Agent / MemGPT settings
    "max_heartbeats": {"type": int, "min": 1},
    "max_context_tokens": {"type": int, "min": 1},
    "eviction_threshold": {"type": float, "min": 0.0, "max": 1.0},
    # Fraction of the window past which a turn compacts the conversation into a
    # structured summary (gilzabots/compaction.py); 1.0 disables it.
    "auto_compact_threshold": {"type": float, "min": 0.0, "max": 1.0},
    "memory_pressure_threshold": {"type": float, "min": 0.0, "max": 1.0},
    # Fraction of max_heartbeats spent past which the agent is warned to be brief.
    "heartbeat_pressure_threshold": {"type": float, "min": 0.0, "max": 1.0},
    "max_iterations": {"type": int, "min": 1},
    "max_tool_calls": {"type": int, "min": 1},
    "loop_detection": {"type": bool},
    "loop_detection_limit": {"type": int, "min": 1},
    "debug": {"type": bool},
    "empty_response_reasoning_fallback": {"type": bool},
    # Consecutive new_heartbeat requests granted with no action in between.
    "max_idle_heartbeats": {"type": int, "min": 1, "max": 10},
}

def sanitize_model_params(params: dict) -> dict:
    """Sanitize and validate model_params to prevent invalid inputs breaking Ollama/LiteLLM."""
    if not isinstance(params, dict):
        return {}
    
    sanitized = {}
    for k, v in params.items():
        if k not in _MODEL_PARAMS_SCHEMA:
            continue
            
        if v is None or v == "":
            continue
            
        spec = _MODEL_PARAMS_SCHEMA[k]
        t = spec["type"]
        
        # Parse string representation
        if isinstance(v, str):
            v_str = v.strip()
            if t is bool:
                v = v_str.lower() in ("true", "1", "yes", "on", "checked")
            elif t is float:
                v_str = v_str.replace(",", ".")
                try:
                    v = float(v_str)
                except ValueError:
                    continue
            elif t is int:
                try:
                    v = int(v_str)
                except ValueError:
                    continue
            elif t is str:
                v = v_str
        
        # Coerce types
        try:
            if t is float:
                v = float(v)
            elif t is int:
                v = int(v)
            elif t is bool:
                v = bool(v)
            elif t is str:
                v = str(v)
                if "choices" in spec and v not in spec["choices"]:
                    continue
        except (ValueError, TypeError):
            continue
            
        # Clamp bounds
        if t in (float, int):
            if "min" in spec and v < spec["min"]:
                v = spec["min"]
            if "max" in spec and v > spec["max"]:
                v = spec["max"]
                
        sanitized[k] = v
        
    return sanitized


# Agent constructor params that can be overridden per-project via model_params.
_AGENT_PARAM_KEYS = _NON_LITELLM_FIELDS - {"model", "strategy"}


from typing import Union

# "Auto" windows for self-hosted Ollama only. There `num_ctx` is sent to the
# server and sizes the KV cache it allocates, so asking for the model's maximum
# could exhaust the host's memory; a parsimonious default is the point. Every
# other provider reports its window instead (gilzabots/context_discovery.py).
LOCAL_MODEL_CONTEXT_TOKENS = 8192
SELF_HOSTED_OLLAMA_CONTEXT_TOKENS = 65536
OLLAMA_CLOUD_API_BASE = "https://ollama.com"


def is_ollama_cloud_model(model: str | None) -> bool:
    """Return True for Ollama model ids that are meant to run on Ollama Cloud."""
    model_id = str(model or "").strip().lower()
    if "/" not in model_id:
        return False
    provider, model_name = model_id.split("/", 1)
    if provider not in {"ollama", "ollama_chat"}:
        return False
    return model_name.endswith(":cloud") or model_name.endswith("-cloud")


def is_local_model(model: str | None, api_base: str | None = "") -> bool:
    """Return True when a model should use local-model context defaults."""
    model_id = str(model or "")
    api_base_value = str(api_base or "").strip().lower()

    provider = model_id.split("/", 1)[0].lower() if "/" in model_id else ""
    if provider not in {"ollama", "ollama_chat"}:
        return False

    if is_ollama_cloud_model(model_id):
        return False

    if not api_base_value:
        return True
    if "localhost" in api_base_value or "127.0.0.1" in api_base_value or "[::1]" in api_base_value:
        return True
    if api_base_value.startswith("http://0.0.0.0") or api_base_value.startswith("http://::1"):
        return True
    return False


def normalize_ollama_api_base_for_litellm(model: str | None, api_base: str | None) -> str:
    """Return the Ollama root URL expected by LiteLLM native Ollama providers."""
    value = str(api_base or "").strip()
    model_id = str(model or "").lower()
    if not value or not (
        model_id.startswith("ollama/") or model_id.startswith("ollama_chat/")
    ):
        return value
    if value.endswith("/v1"):
        return value[:-3]
    if value.endswith("/v1/"):
        return value[:-4]
    return value


def is_self_hosted_ollama_model(model: str | None, api_base: str | None = "") -> bool:
    """True for an Ollama model served by the user's own host, local or remote."""
    model_id = str(model or "")
    provider = model_id.split("/", 1)[0].lower() if "/" in model_id else ""
    if provider not in {"ollama", "ollama_chat"} or is_ollama_cloud_model(model_id):
        return False
    return OLLAMA_CLOUD_API_BASE not in str(api_base or "").strip().lower()


def default_num_ctx_for_model(model: str | None, api_base: str | None = "") -> int | None:
    """Return the fixed "Auto" window for a self-hosted Ollama model, else None.

    None means the window is not GilzaBots's to choose: it comes from the
    provider (`auto_num_ctx`), or stays unknown.
    """
    if is_local_model(model, api_base):
        return LOCAL_MODEL_CONTEXT_TOKENS
    if is_self_hosted_ollama_model(model, api_base):
        return SELF_HOSTED_OLLAMA_CONTEXT_TOKENS
    return None


def get_git_strategy() -> str:
    """Return the git strategy from config.yaml (falls back to agents.yaml for back-compat)."""
    return _APP_CONFIG.get("git_strategy", _get_agents_config().get("git_strategy", "hybrid"))


def get_vector_config() -> dict:
    """Return vector index configuration from config.yaml with defaults."""
    cfg = _APP_CONFIG.get("vector_index", {})
    return {
        "embedding_model": cfg.get("embedding_model", "ollama/nomic-embed-text"),
        "embedding_fallback": cfg.get("embedding_fallback", "sentence-transformers/all-MiniLM-L6-v2"),
        "chunk_size": int(cfg.get("chunk_size", 500)),
        "chunk_overlap": int(cfg.get("chunk_overlap", 50)),
        "top_k": int(cfg.get("top_k", 10)),
    }

def get_project_overview_max_depth() -> int:
    """Return max depth for project overview directory tree from config.yaml with default 3."""
    cfg = _APP_CONFIG.get("project_overview", {})
    return int(cfg.get("max_depth", 3))


DEFAULT_WORKSPACE_HIDDEN_FILE_EXTENSIONS = [
    ".aux",
    ".log",
    ".out",
    ".toc",
    ".lof",
    ".lot",
    ".fls",
    ".fdb_latexmk",
    ".synctex",
    ".synctex.gz",
    ".bbl",
    ".blg",
    ".bcf",
    ".run.xml",
    ".nav",
    ".snm",
    ".vrb",
    ".xdv",
    ".dvi",
    ".idx",
    ".ilg",
    ".ind",
    ".acn",
    ".acr",
    ".alg",
    ".glg",
    ".glo",
    ".gls",
    ".ist",
    ".lol",
    ".loa",
    ".maf",
    ".mtc",
    ".mtc0",
]


def get_workspace_hidden_file_extensions() -> list[str]:
    """Return file extensions hidden from the workspace tree by default."""
    cfg = _APP_CONFIG.get("workspace_files", {})
    raw_extensions = cfg.get("hidden_file_extensions", DEFAULT_WORKSPACE_HIDDEN_FILE_EXTENSIONS)
    if not isinstance(raw_extensions, list):
        return list(DEFAULT_WORKSPACE_HIDDEN_FILE_EXTENSIONS)

    normalized: list[str] = []
    seen = set()
    for value in raw_extensions:
        ext = str(value or "").strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        if ext not in seen:
            normalized.append(ext)
            seen.add(ext)
    return normalized

def get_agent_max_heartbeats(agent_name: str, default: int) -> Union[int, str]:
    """Return max_heartbeats configured for *agent_name* in agents.yaml (can be 'auto'), or *default*."""
    val = _get_agent_overrides().get(agent_name, {}).get("max_heartbeats", default)
    if val == "auto":
        return "auto"
    return int(val)


def get_agent_heartbeats_scale_factor(agent_name: str, default: float = 2.0) -> float:
    """Return heartbeats_scale_factor: per-agent override > config.yaml global > default."""
    global_scale = float(_APP_CONFIG.get("heartbeats_scale_factor", _get_agents_config().get("heartbeats_scale_factor", default)))
    return float(_get_agent_overrides().get(agent_name, {}).get("heartbeats_scale_factor", global_scale))


def get_agent_debug(agent_name: str, default: bool = False) -> bool:
    """Return debug flag configured for *agent_name* in agents.yaml, or *default*."""
    return bool(_get_agent_overrides().get(agent_name, {}).get("debug", default))


def get_agent_model(agent_name: str, default: str | None = None) -> str:
    """Return the model configured for *agent_name* in agents.yaml, or *default*."""
    override = _get_agent_overrides().get(agent_name, {}).get("model")
    if override:
        return override

    cfg = _get_agents_config()
    dyn_default = cfg.get("default", DEFAULT_MODEL)
    model = default if default is not None else dyn_default
    return model


def session_main_model(session) -> str:
    """The orchestrator model a project session runs on.

    For a ``ProjectData`` that is its chat's resolution (chat, then the
    project's default, then the catalog's global default; see
    ``project.resolve_chat_models``), never the bare ``projects.model`` column.
    """
    value = getattr(session, "active_model", None)
    if value is None:
        value = getattr(session, "model", None)
    return str(value or "")


def session_worker_model(session) -> str:
    """The worker model a project session names ("" follows the orchestrator)."""
    value = getattr(session, "active_worker_model", None)
    if value is None:
        value = getattr(session, "worker_model", None)
    return str(value or "")


def resolve_agent_model(agent_name: str) -> str:
    """Return the model *agent_name* actually runs on.

    Session first (a project's `worker_model` for the worker role, otherwise its
    main model), then the agents.yaml override/default. This is the resolution
    `get_agent_llm_kwargs` performs internally to pick provider credentials, and
    every ephemeral agent needs the same answer for the model itself.

    Callers must reach for this instead of `get_agent_model(name, "")`: an empty
    string is not None, so it beats the whole default chain in `get_agent_model`
    and resolves to a model of `""` — which reaches litellm as
    "LLM Provider NOT provided", a long way from the actual mistake.
    """
    session_model = None
    try:
        from .tools import _PROJECT_SESSION
        if _PROJECT_SESSION:
            if agent_name == "worker" and session_worker_model(_PROJECT_SESSION):
                session_model = session_worker_model(_PROJECT_SESSION)
            elif session_main_model(_PROJECT_SESSION):
                session_model = session_main_model(_PROJECT_SESSION)
    except Exception:
        pass
    return get_agent_model(agent_name, default=session_model)


def get_agent_llm_kwargs(
    agent_name: str,
    model_override: str | None = None,
    reasoning_effort_override: str | None = None,
) -> dict:
    """Return merged litellm kwargs for *agent_name*.

    Priority (highest first):
      1. Project-specific model_params (or worker_model_params if worker)
      2. User-defined parameters on the selected model's catalog entry
      3. Per-agent override in agents.yaml ``agents.<name>``
      4. Global ``llm_defaults`` in agents.yaml
      5. Hard-coded defaults above

    Non-litellm fields (model, max_heartbeats) are excluded. When
    ``model_override`` is provided, its catalog entry supplies the provider
    credentials and capabilities instead of resolving a model for the agent
    role. This is used by UI features that must follow the model currently
    selected for the chat.

    ``reasoning_effort_override`` replaces the effort from every source above,
    for callers whose task needs a different depth than the model's chat
    setting (a one-shot rewrite does not need the orchestrator's full effort).
    It applies only to a model the catalog marks ``supports_thinking``: a model
    that does not reason has no effort to lower, and forwarding the parameter
    to it would be an unknown argument. It goes through the same
    ``resolve_think_request``/sanitizer path as the catalog value, so on Ollama
    it travels as ``think`` and ``"none"`` keeps its documented meaning there.
    """
    merged = dict(_get_llm_defaults())
    merged.update(_get_agent_overrides().get(agent_name, {}))
    project_llm_params = {}

    try:
        from .tools import _PROJECT_SESSION
        if _PROJECT_SESSION:
            if agent_name == "worker" and hasattr(_PROJECT_SESSION, "worker_model_params") and _PROJECT_SESSION.worker_model_params:
                project_llm_params = {
                    k: v for k, v in _PROJECT_SESSION.worker_model_params.items()
                    if v is not None
                }
            elif hasattr(_PROJECT_SESSION, "model_params") and _PROJECT_SESSION.model_params:
                project_llm_params = {
                    k: v for k, v in _PROJECT_SESSION.model_params.items()
                    if v is not None
                }
            
    except Exception:
        pass

    # `num_ctx` is absent on purpose: it stays a project-level override (see
    # `_MODEL_PARAMS_SCHEMA` and `resolve_effective_num_ctx`).
    LEGACY_INFERENCE_KEYS = {
        "temperature", "max_tokens", "seed", "top_p", "top_k",
        "min_p", "frequency_penalty", "presence_penalty", "repetition_penalty",
        "reasoning_effort", "think",
    }
    project_llm_params = {
        k: v for k, v in project_llm_params.items()
        if k not in LEGACY_INFERENCE_KEYS
    }

    explicit_project_worker = False
    try:
        from .tools import _PROJECT_SESSION
        if (
            _PROJECT_SESSION
            and agent_name == "worker"
            and session_worker_model(_PROJECT_SESSION)
        ):
            explicit_project_worker = True
    except Exception:
        pass

    # One resolution, shared with every ephemeral agent (see resolve_agent_model):
    # credentials picked here must belong to the model that will actually run.
    resolved_model = (
        str(model_override).strip()
        if model_override and str(model_override).strip()
        else resolve_agent_model(agent_name)
    )
    from gilzabots.models_store import resolve_runtime_model_id

    runtime_model = resolve_runtime_model_id(resolved_model)

    store_api_base = None
    store_api_key = None
    store_supports_thinking = False
    store_extra_model_params = {}
    store_inference_params = {}
    session_api_base = None
    session_api_key = None
    try:
        from gilzabots.models_store import get_model, get_model_by_runtime_id
        store_model = get_model(resolved_model) or get_model_by_runtime_id(runtime_model)
        if store_model:
            store_api_base = store_model.get("api_base")
            store_api_key = store_model.get("api_key")
            store_supports_thinking = bool(store_model.get("supports_thinking", False))
            store_extra_model_params = dict(store_model.get("extra_model_params") or {})
            for p in (
                "temperature", "max_tokens", "seed", "top_p", "top_k", "min_p",
                "frequency_penalty", "presence_penalty", "repetition_penalty",
                "reasoning_effort",
            ):
                if store_model.get(p) is not None:
                    store_inference_params[p] = store_model[p]
    except Exception:
        pass

    # Catalog parameters describe the model regardless of which role selected
    # it. Project-level configuration only provides agent execution controls
    # and transport options (e.g. stream).
    merged.update(store_inference_params)
    merged.update(store_extra_model_params)
    merged.update(project_llm_params)
    if reasoning_effort_override is not None and store_supports_thinking:
        merged["reasoning_effort"] = reasoning_effort_override

    try:
        from .tools import _PROJECT_SESSION
        if _PROJECT_SESSION:
            if agent_name == "worker":
                session_api_base = getattr(_PROJECT_SESSION, "worker_api_base", None)
                session_api_key = getattr(_PROJECT_SESSION, "worker_api_key", None)
                if not explicit_project_worker:
                    session_api_base = session_api_base or getattr(_PROJECT_SESSION, "api_base", None)
                    session_api_key = session_api_key or getattr(_PROJECT_SESSION, "api_key", None)
            else:
                session_api_base = getattr(_PROJECT_SESSION, "api_base", None)
                session_api_key = getattr(_PROJECT_SESSION, "api_key", None)
    except Exception:
        pass
    if store_api_base:
        merged["api_base"] = normalize_ollama_api_base_for_litellm(
            runtime_model,
            store_api_base,
        )
    if store_api_key:
        merged["api_key"] = store_api_key
    if session_api_base:
        merged["api_base"] = normalize_ollama_api_base_for_litellm(
            runtime_model,
            session_api_base,
        )
    if session_api_key:
        merged["api_key"] = session_api_key

    if is_ollama_cloud_model(runtime_model):
        merged.setdefault("api_base", OLLAMA_CLOUD_API_BASE)
        merged.setdefault("api_key", os.getenv("OLLAMA_API_KEY", ""))
        merged.setdefault("timeout", DEFAULT_LITELLM_TIMEOUT_SECONDS)
        merged.setdefault("request_timeout", DEFAULT_LITELLM_TIMEOUT_SECONDS)

    for field in _NON_LITELLM_FIELDS | _INTERNAL_MODEL_PARAM_FIELDS:
        merged.pop(field, None)
    # `think` is never read from the merged sources above -- not from
    # agents.yaml, not from the project's model_params (which can no longer
    # store it). The model catalog's `supports_thinking` is the only input.
    think = resolve_think_request(
        store_supports_thinking,
        merged.get("reasoning_effort"),
    )
    merged.pop("think", None)
    if think is not None:
        merged["think"] = think

    # num_ctx resolution: an explicit project override always wins; otherwise
    # fall back to the model's catalog entry, then "Auto" (auto_num_ctx).
    # Centralized in resolve_effective_num_ctx so tool-budget and attachment
    # code (gilzabots/tools.py, gilzabots/agent_stdin.py) agree with this value.
    # Uses resolved_model (the catalog id, which may carry a `#<connection_id>`
    # disambiguation suffix) rather than runtime_model, since the catalog is
    # keyed by that id — "Auto" resolves the entry's own provider/name, so the
    # suffix is harmless there.
    # An unknown window is sent as no num_ctx at all, never as a guessed one.
    effective_num_ctx = resolve_effective_num_ctx(agent_name, resolved_model, merged.get("api_base"))
    if effective_num_ctx:
        merged["num_ctx"] = effective_num_ctx
    else:
        merged.pop("num_ctx", None)

    # Only the user's own `extra_model_params` names bypass the provider
    # compatibility filter -- that is what the feature is for: naming a
    # provider-specific parameter LiteLLM does not model. The catalog's typed
    # inference fields are ordinary LiteLLM params and must keep going through
    # `get_supported_openai_params`, or `top_k`/`min_p`/`repetition_penalty`
    # reach OpenAI-compatible providers as unknown arguments and every turn
    # fails with a 400 instead of the parameter simply not applying.
    return sanitize_litellm_kwargs_for_model(
        runtime_model,
        merged,
        additional_allowed_params=set(store_extra_model_params),
    )


# Effort levels a provider accepts as the `think` value itself. Probed against
# `ollama_chat/glm-5.3:cloud`, which answers `HTTP 400 invalid think value` for
# anything outside `high`/`medium`/`low`/`max`/`true`/`false` -- so the catalog's
# `none` and `xhigh` are deliberately absent and resolve to a boolean instead.
# `max` is omitted because no catalog field offers it.
_PROVIDER_THINK_LEVELS = frozenset({"low", "medium", "high"})


def resolve_think_request(
    supports_thinking: bool,
    reasoning_effort: str | None = None,
) -> bool | str | None:
    """Return the `think` value to send to the provider, or None to drop the param.

    **A reasoning effort chosen in the catalog travels as `think`, not as
    `reasoning_effort`.** Ollama expresses the effort through the same `think`
    field that switches the reasoning channel on: `think: "low"` both isolates
    the reasoning and asks for less of it. LiteLLM does map `reasoning_effort`
    onto that field, but it collapses the level to a boolean for every model
    except `gpt-oss` (`llms/ollama/chat/transformation.py`: `value in {"low",
    "medium", "high"}`), so a catalog set to "low" reached the provider as
    `think: true` — the full-effort request. Measured against
    `ollama_chat/glm-5.3:cloud`, two runs each of one arithmetic prompt:
    `think: true` reasoned for 2 004 and 3 365 characters (931/1 445 eval
    tokens), `think: "low"` for 197 and 250 (169/160) — roughly a tenth of the
    thinking, which is exactly what the setting promises and what the user was
    not getting. An explicit `think` overrides whatever `reasoning_effort` maps
    to, so the level is passed here and `reasoning_effort` is dropped for the
    Ollama route in `sanitize_litellm_kwargs_for_model`: one knob, one wire
    field, no dependence on which of the two LiteLLM happens to write last.

    **The model catalog is the single source of truth for thinking.** A model's
    `supports_thinking` capability (registered once in the Edit Models UI) decides
    everything: whether the provider is asked to isolate the reasoning channel,
    and — downstream, in `agent_stdin`/`memgpt_runtime` — whether that reasoning is
    published to the Thinking panel. There is no per-project thinking setting to
    consult, and there never needs to be: a project that selects a thinking-capable
    model has asked for thinking by selecting it.

    **`think` is a parsing switch at the provider, not a display switch.** Ollama's
    `think: false` does not stop a reasoning model from reasoning — it stops Ollama
    from separating the reasoning into its own `thinking` field. Verified against
    `ollama_chat/glm-5.3:cloud`: with `think: false` the model answered
    `"17*23 = 17*20 + 17*3 = 340 + 51 = 391" + "391"` in `content` with
    `reasoning_content` empty; with `think: true`, `content` was `"391"` and the
    working went to `reasoning_content`. The reasoning is emitted either way, and
    in the off case it arrives **undelimited** — no `<think>` tags, nothing a text
    splitter can key on — so it was published as the assistant's answer. That is
    why a capable model is always asked to separate the two channels, and why a
    user preference must never be allowed to turn the isolation off.

    **Only the levels the provider actually accepts are forwarded.** Probed
    against `ollama_chat/glm-5.3:cloud`, `think` takes `"high"`, `"medium"`,
    `"low"`, `"max"`, `true` or `false` and answers anything else with
    `HTTP 400 invalid think value`. Two catalog values are therefore not level
    strings on this route. `"none"` must never become `think: false` — that is
    the undelimited-reasoning leak described above, and it was reachable:
    `sanitize_litellm_kwargs_for_model` drops a `"none"` effort only for
    non-Ollama providers, so LiteLLM's boolean mapping turned it into
    `think: false` for exactly the models that can leak. `"xhigh"` is a real
    level for other providers but not for Ollama, and it already resolved to
    plain `true` before this change; inventing a level the provider never
    published (mapping it onto `"max"`, say) would be a substitution the user
    did not ask for, so it keeps resolving to `true` and full effort.
    """
    if not supports_thinking:
        return None
    effort = str(reasoning_effort or "").strip().lower()
    if effort in _PROVIDER_THINK_LEVELS:
        return effort
    return True


def model_supports_thinking(model: str | None) -> bool:
    """Return True only when the model store explicitly enables thinking."""
    try:
        from gilzabots.models_store import get_model_by_runtime_id
        store_model = get_model_by_runtime_id(str(model or ""))
        return bool(store_model and store_model.get("supports_thinking", False))
    except Exception:
        return False


def model_requires_single_system_message(model: str | None) -> bool:
    """Return True only when the model store explicitly flags this requirement.

    Some provider chat templates (observed with an Ollama-served qwen3.8)
    reject a request outright unless exactly one ``system``-role message is
    present as the very first message. This is a per-model catalog capability,
    not a provider-wide rule, since most models tolerate multiple/positioned
    system messages fine.
    """
    try:
        from gilzabots.models_store import get_model_by_runtime_id
        store_model = get_model_by_runtime_id(str(model or ""))
        return bool(store_model and store_model.get("requires_single_system_message", False))
    except Exception:
        return False


def model_num_ctx(model: str | None) -> int | None:
    """Return the context window configured on *model*'s catalog entry, or None.

    Same shape as `model_supports_thinking`/`model_requires_single_system_message`:
    a per-model catalog capability (`gilzabots/models_store.py`), not a project
    setting. Unset (None) means the entry is on "Auto" (see `auto_num_ctx`).

    A failure to read the catalog propagates. It used to be swallowed here and
    read as "unset", which silently replaced the configured window with a guess.
    """
    from gilzabots.models_store import get_model_by_runtime_id
    store_model = get_model_by_runtime_id(str(model or ""))
    value = store_model.get("num_ctx") if store_model else None
    return int(value) if value not in (None, "") else None


def auto_num_ctx(
    model: str | None,
    api_base: str | None = None,
    *,
    allow_network: bool = True,
) -> tuple[int | None, str]:
    """Resolve the "Auto" window of *model*: ``(window, source)``.

    ``source`` is ``"local_default"`` for a self-hosted Ollama model (a fixed,
    parsimonious value, since there `num_ctx` sizes the server's allocation),
    ``"provider"`` when the provider reported the window, and ``"unknown"`` with
    a None window otherwise. An unknown window is not replaced by a guess:
    callers then impose no GilzaBots-side limit and the provider alone decides
    whether a request fits.
    """
    from gilzabots.models_store import get_model_by_runtime_id

    entry = get_model_by_runtime_id(str(model or "")) if model else None
    if entry:
        provider = str(entry.get("provider") or "")
        name = str(entry.get("name") or "")
        runtime = f"{provider}/{name}" if provider and name else str(model or "")
        base = entry.get("api_base") or api_base
    else:
        runtime, base = str(model or ""), api_base
    fixed = default_num_ctx_for_model(runtime, base)
    if fixed:
        return fixed, "local_default"
    from gilzabots.context_discovery import provider_context_window

    window = provider_context_window(entry, allow_network=allow_network)
    if window:
        return window, "provider"
    return None, "unknown"


def resolve_effective_num_ctx(
    agent_name: str = "memgpt",
    model: str | None = None,
    api_base: str | None = None,
) -> int | None:
    """Resolve the context window that should be requested for *agent_name*.

    Priority (highest first):
      1. An explicit ``num_ctx`` in the project's ``model_params`` (or
         ``worker_model_params`` for the ``worker`` role) — set via the
         project's Advanced override, so it must always win.
      2. The ``num_ctx`` configured on the model's global catalog entry.
      3. A per-agent ``num_ctx`` override in ``agents.yaml`` (``agents.<name>``,
         e.g. the orchestrator/memgpt role default of 16384 in
         `_CORE_AGENT_DEFAULTS``) — but *only* when there is no live project
         session to resolve a model/catalog entry from at all (bare CLI/agent
         construction without a project). `_CORE_AGENT_DEFAULTS` sets a
         `num_ctx` for every built-in role, so if this ranked above the
         catalog it would permanently shadow every model's own catalog entry
         for real projects — exactly the outcome this migration exists to
         avoid. It exists as a floor for the no-project bootstrap case only.
      4. "Auto" (`auto_num_ctx`): a fixed parsimonious window for self-hosted
         Ollama, otherwise the window the provider reports for the model.

    None means the window is unknown -- the provider did not report one. It is
    not replaced by a guess: every consumer then imposes no limit of its own.

    This is the single place that implements that precedence so tool-budget
    code (`gilzabots/tools.py`), attachment truncation (`gilzabots/agent_stdin.py`)
    and the LiteLLM request kwargs (`get_agent_llm_kwargs`) cannot drift apart.
    """
    project_num_ctx = None
    resolved_model = model
    resolved_api_base = api_base
    has_session = False
    try:
        from .tools import _PROJECT_SESSION
        if _PROJECT_SESSION:
            has_session = True
            is_worker = agent_name == "worker"
            worker_params = getattr(_PROJECT_SESSION, "worker_model_params", None)
            params = worker_params if (is_worker and worker_params) else getattr(_PROJECT_SESSION, "model_params", None)
            if params:
                project_num_ctx = params.get("num_ctx")
            if resolved_model is None:
                resolved_model = (
                    session_worker_model(_PROJECT_SESSION) if is_worker else None
                ) or session_main_model(_PROJECT_SESSION) or None
            if resolved_api_base is None:
                resolved_api_base = (
                    getattr(_PROJECT_SESSION, "worker_api_base", None) if is_worker else None
                ) or getattr(_PROJECT_SESSION, "api_base", None)
    except Exception:
        pass

    if project_num_ctx not in (None, ""):
        try:
            return int(project_num_ctx)
        except (TypeError, ValueError):
            pass

    catalog_num_ctx = model_num_ctx(resolved_model) if resolved_model else None
    if catalog_num_ctx:
        return catalog_num_ctx

    if not has_session:
        agent_override_num_ctx = _get_agent_overrides().get(agent_name, {}).get("num_ctx")
        if agent_override_num_ctx not in (None, ""):
            try:
                return int(agent_override_num_ctx)
            except (TypeError, ValueError):
                pass

    return auto_num_ctx(resolved_model, resolved_api_base)[0]


def resolve_display_num_ctx(
    model: str | None,
    model_params: dict | None,
    api_base: str | None = None,
) -> int | None:
    """Resolve num_ctx for UI display given a project's own stored fields.

    Unlike `resolve_effective_num_ctx`, this does not depend on the global
    `_PROJECT_SESSION` (so it works for any project row, not just the one
    currently loaded — e.g. `ProjectStore.list_projects`). Precedence mirrors
    the "has a session" branch of `resolve_effective_num_ctx` (explicit
    override > catalog > heuristic): a listed project always has a real
    session once it is actually opened and run, so the no-project per-agent
    floor does not apply here either.
    """
    explicit = (model_params or {}).get("num_ctx")
    if explicit not in (None, ""):
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass

    catalog_num_ctx = model_num_ctx(model) if model else None
    if catalog_num_ctx:
        return catalog_num_ctx

    # Listings never wait on a provider: only a window already reported counts.
    return auto_num_ctx(model, api_base, allow_network=False)[0]


def model_prompt_profile(model: str | None) -> str:
    """Return the prompt profile ("full" or "light") configured for *model*.

    Same shape as `model_supports_thinking`/`model_requires_single_system_message`:
    a per-model catalog capability (`gilzabots/models_store.py`), not a hardcoded
    model/provider rule. "full" is the default and matches the prompt behavior
    this project has always shipped; "light" is opt-in per model, see
    `gilzabots/prompt_profiles.py`.
    """
    try:
        from gilzabots.models_store import get_model_by_runtime_id
        store_model = get_model_by_runtime_id(str(model or ""))
        profile = str(store_model.get("prompt_profile", "") if store_model else "").strip().lower()
        return profile if profile in ("full", "light") else "full"
    except Exception:
        return "full"


def model_orchestrator_policy(model: str | None) -> str:
    """Return the main agent's delegation policy ("direct" or "delegate") for *model*.

    Both policies give the main agent the same tools: it is the agent that does
    the task, so it always holds the workspace action tools (PROJECT_DESIGN
    2.23). The policy decides only how much *auxiliary* work it hands to helper
    agents. "direct" reads and searches itself and calls a helper for jobs too
    big for its own context; "delegate" sends that work -- reading long
    documents, extracting, condensing, researching the web -- to helpers by
    default and keeps its own context for the task, which saves its tokens when
    the helpers run on a smaller model.

    Deliberately a separate field from `model_prompt_profile`, not a third value
    of it: the profile controls prompt verbosity, the policy controls where the
    auxiliary work runs, and "light prompt, delegate" must stay expressible.

    Only meaningful for the main agent: a helper has no `run_skill` tool and
    nothing to delegate to. "direct" is the default.
    """
    try:
        from gilzabots.models_store import get_model_by_runtime_id
        store_model = get_model_by_runtime_id(str(model or ""))
        policy = str(store_model.get("orchestrator_policy", "") if store_model else "").strip().lower()
        return policy if policy in ("direct", "delegate") else "direct"
    except Exception:
        return "direct"


def model_supports_vision(model: str | None, params: dict | None = None) -> bool:
    """True when *model* accepts images, as far as GilzaBots can know.

    LiteLLM's registry answers for the models it lists; a local vision model
    (llava, moondream, a Qwen-VL served by Ollama) is not listed, so the
    `force_vision` model parameter lets the user declare it. *params* are the
    model parameters the caller will actually run the model with -- the
    project's `model_params` for the main agent, its `worker_model_params` for a
    helper -- so the answer and the request agree.
    """
    if params and bool(params.get("force_vision", False)):
        return True
    name = str(model or "").strip()
    if not name:
        return False
    try:
        import litellm as _litellm
        return bool(_litellm.supports_vision(name))
    except Exception:
        return False


def sanitize_litellm_kwargs_for_model(
    model: str,
    kwargs: dict,
    *,
    additional_allowed_params: set[str] | None = None,
) -> dict:
    """Remove provider-incompatible kwargs before passing them to LiteLLM."""
    cleaned = dict(kwargs or {})
    for field in _NON_LITELLM_FIELDS | _INTERNAL_MODEL_PARAM_FIELDS:
        cleaned.pop(field, None)

    provider = ""
    if model and "/" in model:
        provider = model.split("/", 1)[0].lower()

    custom_provider = cleaned.get("custom_llm_provider")
    catalog_extra_params = set(additional_allowed_params or ())
    try:
        # Sanitization happens at more than one boundary (agent construction,
        # compatibility wrapper, and a few feature-specific callers). Resolve
        # the catalog's user-defined parameter names every time so a later pass
        # cannot silently discard a provider-specific parameter the user typed.
        #
        # Only `extra_model_params` names are admitted here. The catalog's typed
        # inference fields (temperature, top_k, min_p, ...) are ordinary LiteLLM
        # params and stay subject to the provider filter below: admitting them
        # would send `top_k`/`min_p`/`repetition_penalty` to OpenAI-compatible
        # providers as unknown request arguments and fail every turn with a 400.
        from gilzabots.models_store import get_model_by_runtime_id
        catalog_model = get_model_by_runtime_id(model)
        if catalog_model:
            catalog_extra_params.update(
                (catalog_model.get("extra_model_params") or {}).keys()
            )
    except Exception:
        pass
    supported_params = None
    try:
        import litellm
        supported_params = litellm.get_supported_openai_params(
            model=model,
            custom_llm_provider=custom_provider,
        )
    except Exception:
        supported_params = None

    if supported_params:
        allowed = (
            set(supported_params)
            | _LITELLM_TRANSPORT_FIELDS
            | catalog_extra_params
        )
        if provider in {"ollama", "ollama_chat"}:
            allowed |= _OLLAMA_COMPAT_LITELLM_FIELDS
        if provider == "gemini":
            allowed |= _COMMON_SAMPLING_LITELLM_FIELDS
        cleaned = {key: value for key, value in cleaned.items() if key in allowed}
    elif provider not in {"ollama", "ollama_chat"}:
        for field in _LOCAL_ONLY_LITELLM_FIELDS:
            cleaned.pop(field, None)

    if provider in {"ollama", "ollama_chat"}:
        # Ollama has one wire field for both halves of this setting, `think`,
        # and `resolve_think_request` already put the level there. Leaving
        # `reasoning_effort` in as well would hand LiteLLM a second writer for
        # that same key, which maps it to a *boolean* for every model but
        # gpt-oss: `low` would arrive as full effort and `none` as
        # `think: false` -- the undelimited-reasoning leak. One knob, one field.
        cleaned.pop("reasoning_effort", None)
        _resolve_ollama_repeat_penalty(cleaned, model)
    elif cleaned.get("reasoning_effort") in {"", "none", None}:
        cleaned.pop("reasoning_effort", None)

    # LiteLLM bridges OpenAI GPT-5.4+ chat-completion requests to the Responses
    # API when tools and reasoning_effort are both present. AgenticBlocks expects
    # Chat Completions tool-call adjacency, so that bridge can leave Responses
    # waiting for function_call_output items and produce "No tool output found".
    # Users can still opt into Responses explicitly with openai/responses/<model>.
    if _is_openai_gpt5_chat_model(model, provider, custom_provider):
        cleaned.pop("reasoning_effort", None)
        cleaned.pop("reasoning_summary", None)
        cleaned.pop("reasoningSummary", None)
        _strip_openai_gpt5_default_only_sampling_params(cleaned)

    # Gemini 3+ deprecates request-level sampling params (temperature, top_p,
    # top_k) — Google recommends moving sampling guidance into the system
    # instructions. Only strip when using the gemini/vertex_ai provider
    # directly (NOT when routed through an OpenAI-compatible proxy, which
    # already discards sampling params via custom_llm_provider=openai).
    if _is_gemini3_direct_model(model, provider, custom_provider):
        _strip_gemini3_sampling_params(cleaned)

    # Ask LiteLLM to drop provider-unsupported optional params instead of
    # surfacing them as BadRequest errors on OpenAI-compatible endpoints.
    cleaned.setdefault("drop_params", True)

    return cleaned


_reported_penalty_conflicts: set[str] = set()


def _resolve_ollama_repeat_penalty(cleaned: dict, model: str) -> None:
    """Give Ollama's `repeat_penalty` a single, declared owner.

    Ollama's repetition control is called `repeat_penalty`, and nothing else on
    the wire means the same thing. Two catalog fields were competing for it.
    `repetition_penalty` -- the field that *is* this parameter under another
    name -- was forwarded verbatim, so it reached `options` as a key Ollama does
    not define and was ignored: the setting did nothing at all. That is
    measured, not assumed: against a local Ollama 0.34.1, a bad value for
    `repeat_penalty` is refused (`option "repeat_penalty" must be of type
    float32`) exactly as `temperature` is, while the same bad value under
    `repetition_penalty` is accepted in silence exactly as an invented option
    name is -- and in generation, `repeat_penalty` 2.0/5.0 move the output while
    the neutral 1.0 and `repetition_penalty` 5.0 both reproduce the baseline.
    Meanwhile
    LiteLLM maps `frequency_penalty` onto `repeat_penalty`
    (`llms/ollama/chat/transformation.py`), so the value that did take effect
    came from a different, additive OpenAI-style parameter the user may have set
    for unrelated reasons. Measured in the request body, a catalog carrying
    `repetition_penalty: 1.2` and `frequency_penalty: 0.5` sent
    `repeat_penalty: 0.5` plus an inert `repetition_penalty: 1.2`.

    `repetition_penalty` therefore owns the key. When both are set the loser is
    named on stderr rather than dropped in silence -- the whole class of bug
    being fixed here is a setting that quietly does nothing -- and it is
    reported once per model instead of once per request.
    """
    if cleaned.get("repetition_penalty") in (None, ""):
        # Nothing claims the key, so LiteLLM's own frequency_penalty mapping is
        # left exactly as it was: this resolves a conflict, it does not remove a
        # control that was working.
        return

    cleaned["repeat_penalty"] = cleaned.pop("repetition_penalty")
    if cleaned.get("frequency_penalty") in (None, ""):
        return

    losing_value = cleaned.pop("frequency_penalty")
    if model not in _reported_penalty_conflicts:
        _reported_penalty_conflicts.add(model)
        print(
            f"[GilzaBots] {model}: repetition_penalty="
            f"{cleaned['repeat_penalty']} and frequency_penalty={losing_value} "
            "both map to Ollama's repeat_penalty. Sending repetition_penalty; "
            "frequency_penalty is not applied for this model.",
            file=sys.stderr,
            flush=True,
        )


def _is_openai_gpt5_chat_model(model: str, provider: str, custom_provider: str | None) -> bool:
    """Return True for OpenAI GPT-5 chat models that must avoid Responses bridge."""
    if provider != "openai" and custom_provider != "openai":
        return False
    model_name = model.split("/", 1)[1] if "/" in model else model
    if model_name.startswith("responses/"):
        return False
    if not model_name.startswith("gpt-5."):
        return False
    version = model_name[len("gpt-5."):]
    prefix = version.split("-", 1)[0]
    try:
        return float(prefix) >= 4.0
    except ValueError:
        return False


def _strip_openai_gpt5_default_only_sampling_params(cleaned: dict) -> None:
    """Remove sampling knobs that GPT-5 chat models only accept at defaults."""
    for field in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        cleaned.pop(field, None)


def _is_gemini3_direct_model(model: str, provider: str, custom_provider: str | None) -> bool:
    """Return True for Gemini 3+ models reached via the gemini/vertex_ai provider.

    Requests routed through an OpenAI-compatible proxy (custom_llm_provider=openai)
    are excluded here — such proxies already discard sampling params, and the
    DeprecationWarning is only emitted by LiteLLM's vertex_and_google_ai_studio_gemini
    module.
    """
    if custom_provider == "openai":
        return False
    if provider not in {"gemini", "vertex_ai", "vertex"}:
        return False
    model_name = model.split("/", 1)[1] if "/" in model else model
    # Match gemini-3, gemini-3.1, gemini-3-pro-preview, etc. (not gemini-2.5).
    return model_name.lower().startswith("gemini-3")


def _strip_gemini3_sampling_params(cleaned: dict) -> None:
    """Remove sampling params deprecated for Gemini 3+.

    Google's guidance is to move sampling into the system instructions instead
    of sending temperature/top_p/top_k as request fields. We simply drop them
    (Gemini 3 defaults are reasonable); the system_prompt is left untouched.
    """
    for field in ("temperature", "top_p", "top_k"):
        cleaned.pop(field, None)


def resolve_model_route(model: str, llm_kwargs: dict) -> str:
    """Resolve the runtime model id and route Ollama models to the native endpoint.

    Two independent jobs, and keeping them independent is the point:

    - **Thinking capability**: `think`/`reasoning_effort` are dropped when the
      model's catalog entry does not declare thinking support, so a model never
      receives a parameter it cannot handle.
    - **Transport**: `ollama/` is remapped to `ollama_chat/` *unconditionally*.
      `ollama_chat/` is Ollama's native `/api/chat` endpoint: it returns
      reasoning as a dedicated `thinking` field per streaming chunk (LiteLLM maps
      it to `delta.reasoning_content`, which is what drives per-chunk
      `on_thinking`), and it carries tool calls in the native `tool_calls` field.
      `ollama/` is the OpenAI-compatible endpoint, where reasoning only appears
      as `<think>` tags inside `delta.content` and tool calling is markedly less
      reliable.

    This routing used to be conditional on thinking actually being requested,
    which quietly coupled a *reasoning preference* to a *transport choice*:
    unchecking "thinking" for a project dropped its orchestrator onto the weaker
    endpoint, where a perfectly capable model (observed with
    `ollama/glm-5.2:cloud`) would print `{"name": …, "arguments": …}` as text
    instead of issuing a tool call, so nothing executed. Turning reasoning off
    must never cost the agent its ability to call tools.
    """
    selected_model = model
    from gilzabots.models_store import resolve_runtime_model_id

    model = resolve_runtime_model_id(model)
    if not model_supports_thinking(selected_model):
        llm_kwargs.pop("think", None)
        llm_kwargs.pop("reasoning_effort", None)
    if model.startswith("ollama/"):
        return "ollama_chat/" + model[len("ollama/"):]
    return model


def get_project_agent_params(agent_name: str = "memgpt") -> dict:
    """Return agent constructor overrides stored in the current project's model_params.

    Only keys that belong to _AGENT_PARAM_KEYS are returned; LiteLLM kwargs are excluded.
    None values are filtered out so they don't override defaults with None.
    """
    try:
        from .tools import _PROJECT_SESSION
        if _PROJECT_SESSION:
            if agent_name == "worker" and hasattr(_PROJECT_SESSION, "worker_model_params") and _PROJECT_SESSION.worker_model_params:
                return {k: v for k, v in _PROJECT_SESSION.worker_model_params.items() if k in _AGENT_PARAM_KEYS and v is not None}
            elif hasattr(_PROJECT_SESSION, "model_params") and _PROJECT_SESSION.model_params:
                return {k: v for k, v in _PROJECT_SESSION.model_params.items() if k in _AGENT_PARAM_KEYS and v is not None}
    except Exception:
        pass
    return {}


# Maximum retry attempts for a failing subplan step
DEFAULT_MAX_RETRIES = 3

# MemGPT heartbeat guardrail per project turn
DEFAULT_MAX_HEARTBEATS = 50

# SQLite database file for session persistence
DEFAULT_DB_PATH = os.path.join(
    get_gilzabots_home(), "sessions.db"
)

# Execution mode: "auto" | "plan" | "edit"
DEFAULT_MODE = "plan"

def _get_system_lang() -> str:
    env_lang = os.getenv("GILZABOTS_LANG")
    if env_lang in ("en", "pt"):
        return env_lang
        
    try:
        for var in ("LC_ALL", "LC_CTYPE", "LANG"):
            val = os.getenv(var)
            if val and len(val) >= 2:
                lang_code = val[:2].lower()
                if lang_code in ("en", "pt"):
                    return lang_code
                    
        import locale
        loc, _ = locale.getdefaultlocale()
        if loc and len(loc) >= 2:
            lang_code = loc[:2].lower()
            if lang_code in ("en", "pt"):
                return lang_code
    except Exception:
        pass
        
    return "en"

# Default Language
DEFAULT_LANG = _get_system_lang()

# Default litellm kwargs applied to all local model calls (kept for back-compat)
LITELLM_DEFAULTS: dict = {"num_ctx": _get_llm_defaults()["num_ctx"]}

# Sensitive operations that require user approval in "edit" mode
SENSITIVE_OPS = {
    "write_file", "delete_file", "run_shell",
    "send_network_request", "create_user", "delete_user",
}

# ─── Debug Logging ────────────────────────────────────────────────────────────

# Shared run logger — set by setup_debug_logging(), used by terminal.py debug_* funcs.
_RUN_LOGGER: "logging.Logger | None" = None


def get_run_logger():
    return _RUN_LOGGER


def setup_debug_logging():
    """Enable full debug logging for a run.

    - Creates a timestamped log file: ~/.gilzabots/logs/run_<timestamp>.log
    - Logs every litellm call (full prompt + response) via success/failure callbacks.
    - Enables workflow step logs (oracle outputs, worker starts/results) to the same file.
    - Sets GILZABOTS_WORKFLOW_DEBUG=1 so terminal.py debug_* functions also fire.
    - Prints the log file path so the user knows where to look.
    """
    import litellm
    import logging
    from datetime import datetime

    global _RUN_LOGGER

    log_dir = os.path.join(get_gilzabots_home(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"run_{timestamp}.log")

    logger = logging.getLogger(f"gilzabots.run.{timestamp}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(fh)

    _RUN_LOGGER = logger

    # Enable workflow debug output in terminal.py (oracle + worker intermediate steps)
    os.environ["GILZABOTS_WORKFLOW_DEBUG"] = "1"

    # litellm verbose → logs raw HTTP-level info to stderr; we capture structured data
    # ourselves via callbacks so we leave litellm's own verbose off to reduce noise.
    litellm.set_verbose = False

    SEP = "=" * 80

    def _llm_callback(kwargs, completion_response, start_time, end_time):
        messages = kwargs.get("messages", [])
        model = kwargs.get("model", "unknown")
        elapsed = (end_time - start_time).total_seconds() if start_time and end_time else 0

        lines = [
            f"\n{SEP}",
            f"[LLM CALL] model={model}  elapsed={elapsed:.2f}s",
            f"{SEP}",
            "--- MESSAGES (input) ---",
        ]
        for msg in messages:
            role = msg.get("role", "?").upper()
            content = msg.get("content") or ""
            # tool_calls in assistant messages
            tcs = msg.get("tool_calls")
            if tcs:
                content = f"<tool_calls> {tcs}"
            lines.append(f"\n[{role}]\n{content}")

        lines.append("\n--- RESPONSE (output) ---")
        if completion_response:
            try:
                choice = completion_response.choices[0]
                msg_out = choice.message
                if msg_out.content:
                    lines.append(msg_out.content)
                if getattr(msg_out, "tool_calls", None):
                    lines.append(f"<tool_calls> {msg_out.tool_calls}")
            except Exception:
                lines.append(str(completion_response))
        else:
            lines.append("(no response)")

        lines.append(SEP)
        logger.debug("\n".join(lines))
        for handler in logger.handlers:
            handler.flush()

    litellm.success_callback = [_llm_callback]
    litellm.failure_callback = [_llm_callback]

    return log_file
