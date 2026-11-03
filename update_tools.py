import os

path = 'gilzabots/tools.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Imports
content = content.replace(
    'from agenticblocks.core.function_block import as_tool',
    'import functools\nimport asyncio\nimport concurrent.futures\nfrom agenticblocks.core.function_block import as_tool'
)

# 2. Decorator code
dec_code = '''
SAFE_SHELL_COMMANDS = {
    "ls", "pwd", "cat", "grep", "find", "whoami", "head", "tail", "less", "more", "tree", "cd", "echo",
    "dir", "type", "findstr", "Get-ChildItem", "Get-Location", "Get-Content", "Select-String", "tasklist"
}

def gilzabots_tool(name: str, description: str, is_safe: bool = False):
    """
    Decorator that wraps the agenticblocks tool.
    Enforces 'plan', 'edit', and 'auto' mode safety rules.
    """
    def decorator(func):
        is_async = asyncio.iscoroutinefunction(func)

        def _check_permission(*args, **kwargs):
            mode = getattr(_PROJECT_SESSION, "mode", "auto") if _PROJECT_SESSION else "auto"
            
            # Auto mode or safe tool -> always execute
            if mode == "auto" or is_safe:
                return True
                
            # Now we are in 'edit' or 'plan' mode, and the tool is NOT safe.
            is_run_command_safe = False
            question = f"O agente quer usar a ferramenta '{name}'. Permitir?"
            
            if name == "run_command":
                cmd = kwargs.get("command") or (args[0] if args else "")
                base_cmd = cmd.strip().split()[0] if cmd else ""
                is_writing = ">" in cmd or "|" in cmd
                if base_cmd in SAFE_SHELL_COMMANDS and not is_writing:
                    is_run_command_safe = True
                else:
                    question = f"O agente quer executar o comando: '{cmd}'. Permitir?"
            
            if is_run_command_safe:
                return True
                
            if mode == "plan":
                return "Execution blocked: In 'plan' mode, you are not allowed to execute operations that modify the system."
                
            if mode == "edit":
                return question
                
            return True

        if is_async:
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                res = _check_permission(*args, **kwargs)
                if isinstance(res, str):
                    if res.startswith("Execution blocked"):
                        raise ValueError(res)
                    # It's a question for edit mode
                    approved = await T.aask(res)
                    if not approved or approved.lower() in ("cancel", "no", "false", "n", "não"):
                        raise ValueError("Operação cancelada pelo usuário (Modo Edit).")
                return await func(*args, **kwargs)
            return as_tool(name=name, description=description)(async_wrapper)
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                res = _check_permission(*args, **kwargs)
                if isinstance(res, str):
                    if res.startswith("Execution blocked"):
                        raise ValueError(res)
                    # Use ThreadPoolExecutor to call async T.aask safely
                    try:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            future = pool.submit(asyncio.run, T.aask(res))
                            approved = future.result(timeout=120)
                    except Exception:
                        approved = False
                    if not approved or approved.lower() in ("cancel", "no", "false", "n", "não"):
                        raise ValueError("Operação cancelada pelo usuário (Modo Edit).")
                return func(*args, **kwargs)
            return as_tool(name=name, description=description)(sync_wrapper)
            
    return decorator

# ─── Tools ───────────────────────────────────────────────────────────────────
'''
content = content.replace('# ─── Tools ───────────────────────────────────────────────────────────────────', dec_code)

# 3. Replacements for single line decorators
replacements = {
    '@as_tool(name="read_file",': '@gilzabots_tool(name="read_file", is_safe=True,',
    '@as_tool(name="write_file",': '@gilzabots_tool(name="write_file", is_safe=False,',
    '@as_tool(name="run_command",': '@gilzabots_tool(name="run_command", is_safe=False,',
    '@as_tool(name="run_background_command",': '@gilzabots_tool(name="run_background_command", is_safe=False,',
    '@as_tool(name="run_python_script",': '@gilzabots_tool(name="run_python_script", is_safe=False,',
    '@as_tool(name="run_interactive_command",': '@gilzabots_tool(name="run_interactive_command", is_safe=False,',
    '@as_tool(name="search_code",': '@gilzabots_tool(name="search_code", is_safe=True,',
    '@as_tool(name="read_core_memory",': '@gilzabots_tool(name="read_core_memory", is_safe=True,',
    '@as_tool(name="append_core_memory",': '@gilzabots_tool(name="append_core_memory", is_safe=False,',
    '@as_tool(name="search_conversation_history",': '@gilzabots_tool(name="search_conversation_history", is_safe=True,'
}
for old, new in replacements.items():
    content = content.replace(old, new)

# 4. Replacements for multiline decorators
multiline_replacements = {
    '@as_tool(\\n    name="ask_human",': '@gilzabots_tool(\\n    name="ask_human",\\n    is_safe=True,',
    '@as_tool(\\n    name="get_project_overview",': '@gilzabots_tool(\\n    name="get_project_overview",\\n    is_safe=True,',
    '@as_tool(\\n    name="write_content_pos",': '@gilzabots_tool(\\n    name="write_content_pos",\\n    is_safe=False,',
    '@as_tool(\\n    name="read_content_pos",': '@gilzabots_tool(\\n    name="read_content_pos",\\n    is_safe=True,',
    '@as_tool(\\n    name="update_achievements_memory",': '@gilzabots_tool(\\n    name="update_achievements_memory",\\n    is_safe=True,',
    '@as_tool(\\n    name="web_search",': '@gilzabots_tool(\\n    name="web_search",\\n    is_safe=True,'
}
for old, new in multiline_replacements.items():
    content = content.replace(old, new)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print('Done!')
