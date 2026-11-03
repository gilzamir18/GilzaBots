---
name: command-line
description: Executes command-line operations to read, create, insert text, remove files and directories securely inside the project workspace.
---

# Command Line Skill

This skill provides the sub-agent with tools to manipulate files and directories securely, restricted to the project directory.

**FINDING FILES AND TEXT (CRITICAL):** Do not guess file locations or line numbers. If the file path is unknown, use `get_project_overview`. If you need to find a section, label, function, class, or marker inside project files, use `search_code`; it is Python-based and works across operating systems. If the orchestrator context already gives the exact file path, line range, or command to run, execute that directly and do not call `get_project_overview` first. As a last resort, if you cannot find the file, stop and ask the user for its location in normal text.

## AVAILABLE TOOLS

        get_project_overview,
        search_code,
        find_symbol,
        code_outline,
        read_file,
        read_content_pos,
        write_file,
        edit_file,
        write_content_pos,
        replace_content_range,
        compile_latex,
        run_command,
        run_background_command,
        run_interactive_command,
        search_conversation_history,
        ask_question



1. write_file: **Use `write_file` directly to create or overwrite any file.** Do NOT use `command_executor.py` for writing file content — shell quoting breaks with multi-line, HTML, CSS, or JavaScript content.

**CRITICAL: LARGE FILES & TRUNCATION PREVENTION**
If a file is large (more than ~100-200 lines, e.g., large `.tex` files or large code files), do NOT attempt to use `write_file` to write the entire file content, as the LLM output limits will cause the JSON tool call to be cut off mid-response (`[TRUNCATED RESPONSE]`).
Instead:
- Use `edit_file` to replace an exact passage, found by its text — preferred, since an earlier edit cannot shift it.
- Use `replace_content_range` to surgically replace only the specific line ranges that need changes.
- Use `write_content_pos` only when inserting new content before a specific line.
- Or, write a small Python helper script that reads the file, performs the string replacements/modifications programmatically (e.g. read, replace, write), and writes it back, then run it using `run_command`.

```
write_file("<relative_or_absolute_path>", "<full file content>")
```
Examples:
```
write_file("tictactoe.html", "<!DOCTYPE html>...")
write_file("src/utils.js", "function foo() {...}")
```


2. read_file: use read_file for directly access files without shell. For example:
```
read_file("<relative_or_absolute_path>")
```
Examples:
```
read_file("tictactoe.html")
read_file("src/utils.js")
```
Do not use `read_file` on large `.tex`, `.log`, or source files just to locate a section, label, or line range. Use `search_code` to locate the marker, then use `read_content_pos` once the line range is known.

3. read_content_pos: use read_content_pos for directly access file content at a specific line range without shell. For example:
```
read_content_pos("<relative_or_absolute_path>", "<start_line>", "<end_line>")
```
Examples:
```
read_content_pos("tictactoe.html", "1", "10")
read_content_pos("src/utils.js", "10", "20")
```

4. write_content_pos: use write_content_pos to insert content before a specific line number without shell. For example:
```
write_content_pos("<relative_or_absolute_path>", "<content>", <line_number>)
```
Examples:
```
write_content_pos("tictactoe.html", "<content>", 1)
write_content_pos("src/utils.js", "<content>", 10)
```

5. replace_content_range: use replace_content_range to replace or delete an inclusive line range without rewriting the whole file. Pass an empty content string to delete the selected lines. For example:
```
replace_content_range("<relative_or_absolute_path>", <start_line>, <end_line>, "<content>")
```
Examples:
```
replace_content_range("tictactoe.html", 1, 10, "<content>")
replace_content_range("src/utils.js", 10, 20, "<content>")
```

6. edit_file: use edit_file to replace an exact passage of an existing file. old_string must match the file's text exactly, indentation included, and occur exactly once — add surrounding lines until it is unique, or pass replace_all=True to change every occurrence. An empty new_string deletes the passage. The result shows the edited region with line numbers. For example:
```
edit_file("<relative_or_absolute_path>", "<exact current text>", "<replacement text>")
```
Examples:
```
edit_file("main.tex", "\\section{Introducao}", "\\section{Introdução}")
edit_file("src/utils.js", "const limit = 10;", "const limit = 20;")
```

7. search_code: use search_code to find text or regex matches across project files without shell commands. It returns relative paths and line numbers. For example:
```
search_code("Metodologia", "projeto_pesquisa.tex")
search_code("\\\\section\\{Metodologia\\}", ".", true)
```

8. run_command: use run_command to execute non-interactive shell commands (e.g. build, compile, list, python/pip commands). For example:
```
run_command("<command>")
```
Examples (NON-INTERACTIVE commands only):
```
run_command("python -m pytest")
run_command("uv pip install django")
```
WARNING: Do NOT use `run_command` for commands that require user input (like `npm create`, `npm init`, etc). For those, you MUST use `run_interactive_command`. Do NOT run servers or infinite processes with this tool.

**Compiling LaTeX:** use `compile_latex`, never `run_command` with `pdflatex`, `xelatex` or `latexmk`. The IDE compiles with Tectonic, and `compile_latex` runs that same compile on the same main file, answering `SUCCESS` or `FAILED` with `file:line` errors and warnings. Pass the `.tex` file you changed (a chapter compiles through the document that includes it), or no path for the project's main file:
```
compile_latex("main.tex")
compile_latex("chapters/intro.tex", draft=True)
```
`draft=True` is one fast pass for a syntax check; undefined references are expected after it. Do a full compile before reporting that the document builds. Report that you could not compile only if `compile_latex` itself says Tectonic is missing.

9. run_interactive_command: use this specifically for commands that require human interaction, choices, or input. It will open a popup terminal for the user.
```
run_interactive_command("npm create vite@latest app -- --template react")
run_interactive_command("npm init")
```

10. run_background_command: use this to start long-running servers or background processes (e.g., `npm run dev`) directly in the user's main IDE terminal. It returns immediately and does not block.
```
run_background_command("npm run dev")
run_background_command("python manage.py runserver")
```

11. get_project_overview: use get_project_overview for directly access project tree of files. Try with a minimum depth of 5.
Example:
```
get_project_overview(5)
```
Use this only when the project structure is unknown. Skip it when the task already identifies the target file or provides an explicit command.

12. search_conversation_history: use search_conversation_history for directly search conversation history without shell. For example:
```
search_conversation_history("<keyword>")
```
Examples:
```
search_conversation_history("tictactoe")
search_conversation_history("src")
```

13. ask_question: use ask_question to ask the user clarifying questions, prompt for choices, or gather missing parameters during execution without aborting the task. For example:
```
ask_question("Found multiple configuration files: [config.json, config.yaml]. Which one should I update?")
```


## Available Commands via command_executor.py

Consider using `run_python_script` to call `scripts/command_executor.py`  (insert text at a line, remove files/dirs, create empty directories). The syntax uses subcommands:

```
run_python_script("<command_executor.py_path>", "--project-path <project_path> <subcommand> <args>")
```

**Argument Explanation:**
- `<command_executor.py_path>`: The absolute path to the `command_executor.py` script.
- `<project_path>`: The path to the project root (e.g., `.`).
- `<subcommand> <args>`: The specific action you want to take (e.g., `remove-file`, `create-dir`) followed by its arguments.

**Concrete Examples:**

1. Creating a new directory:
`run_python_script("/home/user/skills/command-line/scripts/command_executor.py", "--project-path . create-dir src/components")`

2. Removing a file:
`run_python_script("/home/user/skills/command-line/scripts/command_executor.py", "--project-path . remove-file temp_debug.log")`

3. Renaming a directory:
`run_python_script("/home/user/skills/command-line/scripts/command_executor.py", "--project-path . rename old_folder new_folder")`

### 1. Insert Text

```
run_python_script("<command_executor.py_path>", "--project-path <project_path> insert-text <relative_file_path> --content-file /tmp/_gilzabots_content.txt [--line <line_number>]")
```

For multi-line content, write it first with `write_file` to a temp path, then use `--content-file`.

### 2. Remove File
```
run_python_script("<command_executor.py_path>", "--project-path <project_path> remove-file <relative_file_path>")
```

### 3. Create Directory
```
run_python_script("<command_executor.py_path>", "--project-path <project_path> create-dir <relative_directory_path>")
```

### 4. Remove Directory
```
run_python_script("<command_executor.py_path>", "--project-path <project_path> remove-dir <relative_directory_path>")
```

### 5. Rename / Move File or Directory
```
run_python_script("<command_executor.py_path>", "--project-path <project_path> rename <relative_origin_path> <relative_dest_path>")
```

### 6. Copy File or Directory
```
run_python_script("<command_executor.py_path>", "--project-path <project_path> cp <relative_origin_path> <relative_dest_path>")
```
