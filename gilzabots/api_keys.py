import os
import pathlib
from rich.prompt import Prompt, Confirm
from . import terminal as T
from .i18n import _

PROVIDER_KEYS = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "together_ai": "TOGETHERAI_API_KEY",
}

def get_env_var_for_model(model: str) -> str:
    if "/" in model:
        provider = model.split("/")[0].lower()
        return PROVIDER_KEYS.get(provider, f"{provider.upper()}_API_KEY")
    return ""

def ensure_api_key(model: str) -> bool:
    """
    Checks if the model requires an API key and if it's present.
    If not, prompts the user to enter it and offers to save it globally.
    Returns True if the key is available, False if the user skipped.
    """
    env_var = get_env_var_for_model(model)
    # If no specific env var is determined, assume it doesn't need one (like local ollama models)
    if not env_var or "ollama" in model.lower() or "local" in model.lower():
        return True

    if os.getenv(env_var):
        return True

    T.warning(f"The model '{model}' requires the environment variable {env_var}, which was not found.")
    
    key = Prompt.ask(f"[bold cyan]Please enter your {env_var} (leave blank to skip)[/bold cyan]", password=True)
    
    if not key.strip():
        T.info("No key provided. The system will fallback to the default model.")
        return False

    key = key.strip()
    # Set in current process
    os.environ[env_var] = key

    save = Confirm.ask("Do you want to save this key to ~/.gilzabots/.env for future sessions?", default=True)
    if save:
        env_path = pathlib.Path.home() / ".gilzabots" / ".env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        
        lines = []
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        
        updated = False
        for i, line in enumerate(lines):
            if line.startswith(f"{env_var}="):
                lines[i] = f"{env_var}={key}\n"
                updated = True
                break
                
        if not updated:
            if lines and not lines[-1].endswith("\n"):
                lines.append("\n")
            lines.append(f"{env_var}={key}\n")
            
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
            
        T.success(f"Key successfully saved to {env_path}")

    return True
