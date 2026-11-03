def test_sanitize_tool_call_messages_strips_incomplete_tool_call():
    from gilzabots.litellm_compat import sanitize_tool_call_messages

    messages = [
        {"role": "user", "content": "Look at this image"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_missing",
                    "type": "function",
                    "function": {"name": "analyze_image", "arguments": "{}"},
                }
            ],
        },
        {"role": "user", "content": "What happened?"},
    ]

    sanitized = sanitize_tool_call_messages(messages)

    assert sanitized[1]["role"] == "assistant"
    assert "tool_calls" not in sanitized[1]
    assert "call_missing" not in sanitized[1]["content"]
    assert "analyze_image" in sanitized[1]["content"]
    assert sanitized[2]["role"] == "user"


def test_sanitize_tool_call_messages_converts_orphan_tool_message():
    from gilzabots.litellm_compat import sanitize_tool_call_messages

    sanitized = sanitize_tool_call_messages([
        {"role": "tool", "name": "read_file", "tool_call_id": "call_old", "content": "ok"}
    ])

    assert sanitized == [
        {"role": "system", "content": "[Recovered orphan tool result from 'read_file']\nok"}
    ]


def test_sanitize_tool_call_messages_preserves_complete_tool_pair():
    from gilzabots.litellm_compat import sanitize_tool_call_messages

    messages = [
        {"role": "user", "content": "read x"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_ok",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_ok", "name": "read_file", "content": "ok"},
        {"role": "user", "content": "continue"},
    ]

    assert sanitize_tool_call_messages(messages) == messages


def test_sanitize_tool_call_messages_preserves_pair_after_assistant_turn():
    """assistant -> assistant(tool_calls) is valid: only id matching is enforced."""
    from gilzabots.litellm_compat import sanitize_tool_call_messages

    messages = [
        {"role": "assistant", "content": "Previous assistant text"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_after_assistant",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_after_assistant", "name": "read_file", "content": "ok"},
    ]

    assert sanitize_tool_call_messages(messages) == messages


def test_sanitize_tool_call_messages_preserves_pair_after_system_alert():
    """A system alert before an assistant turn must not break the tool pairing.

    The assistant placeholder below is a legacy shape: the empty-response
    correction now drops the silent turn and inserts only the alert. It still has
    to survive sanitization, because a chat or agent state saved before that
    change can replay it.
    """
    from gilzabots.litellm_compat import sanitize_tool_call_messages

    messages = [
        {"role": "user", "content": "edit the file"},
        {"role": "assistant", "content": "(removed: empty response)"},
        {"role": "system", "content": "SYSTEM ALERT: You returned no text and no native tool call."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_after_alert",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_after_alert", "name": "read_file", "content": "ok"},
    ]

    assert sanitize_tool_call_messages(messages) == messages


def test_sanitize_tool_call_messages_preserves_pair_at_history_start():
    from gilzabots.litellm_compat import sanitize_tool_call_messages

    messages = [
        {"role": "system", "content": "summary"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_at_start",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_at_start", "name": "read_file", "content": "ok"},
    ]

    assert sanitize_tool_call_messages(messages) == messages


def test_sanitize_tool_call_messages_moves_interleaved_system_alert_past_tool_block():
    """A system alert must never split an assistant tool_calls message from its results."""
    from gilzabots.litellm_compat import sanitize_tool_call_messages

    alert = {"role": "system", "content": "SYSTEM ALERT: the tool call was empty."}
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ],
    }
    first_result = {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "err"}
    second_result = {"role": "tool", "tool_call_id": "c2", "name": "read_file", "content": "ok"}

    sanitized = sanitize_tool_call_messages([
        {"role": "user", "content": "hi"},
        assistant,
        first_result,
        alert,
        second_result,
    ])

    assert sanitized == [
        {"role": "user", "content": "hi"},
        assistant,
        first_result,
        second_result,
        alert,
    ]


def test_sanitize_tool_call_messages_downgrades_only_when_a_result_is_missing():
    from gilzabots.litellm_compat import sanitize_tool_call_messages

    sanitized = sanitize_tool_call_messages([
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "write_file", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "ok"},
    ])

    assert sanitized[1]["role"] == "assistant"
    assert "tool_calls" not in sanitized[1]
    assert "read_file" in sanitized[1]["content"]
    assert sanitized[2]["role"] == "system"
    assert "Recovered orphan tool result" in sanitized[2]["content"]


def test_wrap_agent_litellm_compat_adds_drop_params(monkeypatch):
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    calls = {}

    class FakeAgent:
        model = "openai/gpt-5.5"

        async def _acompletion(self, messages, **kwargs):
            calls["messages"] = messages
            calls["kwargs"] = kwargs
            return "ok"

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    result = asyncio.run(agent._acompletion([{"role": "user", "content": "hi"}]))

    assert result == "ok"
    assert calls["kwargs"]["drop_params"] is True


def test_wrap_agent_litellm_compat_sanitizes_runtime_kwargs():
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    calls = {}

    class FakeAgent:
        model = "openai/gpt-5.5"

        async def _acompletion(self, messages, **kwargs):
            calls["kwargs"] = kwargs
            return "ok"

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    result = asyncio.run(agent._acompletion(
        [{"role": "user", "content": "hi"}],
        temperature=0.7,
        top_p=0.9,
        max_tokens=64,
    ))

    assert result == "ok"
    assert "temperature" not in calls["kwargs"]
    assert "top_p" not in calls["kwargs"]
    assert calls["kwargs"]["max_tokens"] == 64
    assert calls["kwargs"]["drop_params"] is True


def test_wrap_agent_litellm_compat_normalizes_ollama_keyerror_message():
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    class FakeOllamaError(Exception):
        pass

    class FakeAgent:
        model = "ollama_chat/gemma4:12b"

        async def _acompletion(self, messages, **kwargs):
            raise FakeOllamaError(
                "Ollama_chatException - KeyError: 'message', Got unexpected "
                "response from Ollama: {'error': 'Internal Server Error "
                "(ref: abc)'}"
            )

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio
    import pytest

    with pytest.raises(FakeOllamaError) as raised:
        asyncio.run(agent._acompletion([{"role": "user", "content": "hi"}]))

    msg = str(raised.value)
    assert "Ollama returned HTTP 500" in msg
    assert "selected model supports the requested chat/tool/thinking features" in msg
    assert "Original LiteLLM error" in msg
    assert raised.value.__cause__ is not None


def test_wrap_agent_litellm_compat_repairs_internal_history_in_place():
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    class FakeAgent:
        def __init__(self):
            self.internal_history = [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_stale",
                            "type": "function",
                            "function": {"name": "analyze_image", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "user", "content": "next question"},
            ]

        async def _acompletion(self, messages, **kwargs):
            return "ok"

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    result = asyncio.run(agent._acompletion(list(agent.internal_history)))

    assert result == "ok"
    assert agent.internal_history[0]["role"] == "assistant"
    assert "tool_calls" not in agent.internal_history[0]
    assert "analyze_image" in agent.internal_history[0]["content"]
    assert agent.internal_history[1]["role"] == "user"


def test_wrap_agent_litellm_compat_preserves_native_tool_role_for_ollama():
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    calls = {}
    messages = [
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_ok",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_ok", "name": "read_file", "content": "ok"},
    ]

    class FakeAgent:
        model = "ollama/gemma4:12b"

        async def _acompletion(self, messages, **kwargs):
            calls["messages"] = messages
            return "ok"

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    result = asyncio.run(agent._acompletion(messages))

    assert result == "ok"
    assert calls["messages"][2]["role"] == "tool"
    assert calls["messages"][2]["tool_call_id"] == "call_ok"


def test_wrap_agent_litellm_compat_preserves_stream_and_tools_for_ollama():
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    calls = {}

    class FakeAgent:
        model = "ollama/gemma4:26b"
        tools = [object()]

        async def _acompletion(self, messages, **kwargs):
            calls["kwargs"] = kwargs
            return "ok"

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    result = asyncio.run(agent._acompletion(
        [{"role": "user", "content": "hi"}],
        stream=True,
        stream_options={"include_usage": True},
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    ))

    assert result == "ok"
    assert calls["kwargs"]["stream"] is True
    assert calls["kwargs"]["tools"] == [{"type": "function", "function": {"name": "read_file"}}]


def test_sanitize_litellm_kwargs_preserves_ollama_tool_schema():
    from gilzabots.config import sanitize_litellm_kwargs_for_model

    tools = [{"type": "function", "function": {"name": "read_file"}}]

    cleaned = sanitize_litellm_kwargs_for_model(
        "ollama/gemma4:26b",
        {
            "stream": True,
            "tools": tools,
            "tool_choice": "auto",
            "num_ctx": 8192,
        },
    )

    assert cleaned["tools"] == tools
    assert cleaned["tool_choice"] == "auto"
    assert cleaned["num_ctx"] == 8192


def test_wrap_agent_litellm_compat_repairs_history_before_run():
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    class FakeAgent:
        model = "openai/gpt-5.5"

        def __init__(self):
            self.internal_history = [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_stale",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "user", "content": "continue"},
            ]

        async def _acompletion(self, messages, **kwargs):
            return "ok"

        async def run(self, input):
            assert "tool_calls" not in self.internal_history[0]
            return "ok"

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    assert asyncio.run(agent.run(object())) == "ok"


def test_find_tool_for_args_returns_none_when_schema_match_is_ambiguous():
    from agenticblocks.core.function_block import as_tool
    from gilzabots.litellm_compat import find_tool_for_args

    @as_tool(name="run_command")
    def run_command(command: str) -> str:
        return command

    @as_tool(name="run_background_command")
    def run_background_command(command: str) -> str:
        return command

    assert find_tool_for_args(
        {"command": "npm run dev"},
        [run_command, run_background_command],
    ) is None


def test_find_tool_for_args_requires_complete_unique_schema_match():
    from agenticblocks.core.function_block import as_tool
    from gilzabots.litellm_compat import find_tool_for_args

    @as_tool(name="read_file")
    def read_file(path: str) -> str:
        return path

    @as_tool(name="replace_content_range")
    def replace_content_range(path: str, start_pos: int, end_pos: int, content: str) -> str:
        return path

    assert find_tool_for_args(
        {"path": "main.tex", "start_pos": 2, "end_pos": 4, "content": "x"},
        [read_file, replace_content_range],
    ) == "replace_content_range"

    assert find_tool_for_args(
        {"path": "main.tex"},
        [read_file, replace_content_range],
    ) == "read_file"


def test_repeated_tool_validation_errors_are_detected():
    from gilzabots.litellm_compat import _has_repeated_tool_validation_errors

    messages = [
        {
            "role": "tool",
            "name": "replace_content_range",
            "content": "3 validation errors for ReplaceContentRangeInput\npath\n  Field required",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [],
        },
        {
            "role": "tool",
            "name": "replace_content_range",
            "content": "3 validation errors for ReplaceContentRangeInput\nstart_pos\n  Field required",
        },
    ]

    assert _has_repeated_tool_validation_errors(messages)


def test_single_tool_validation_error_does_not_trip_loop_breaker():
    from gilzabots.litellm_compat import _has_repeated_tool_validation_errors

    messages = [
        {
            "role": "tool",
            "name": "replace_content_range",
            "content": "3 validation errors for ReplaceContentRangeInput\npath\n  Field required",
        },
    ]

    assert not _has_repeated_tool_validation_errors(messages)


def test_repeated_validation_disables_only_the_rejected_tool():
    """A bad search call must not take read_file or create_plan out with it."""
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    captured = {}

    class FakeAgent:
        model = "ollama/glm-5.3:cloud"
        name = "chat_orchestrator"
        tools = []

        async def _acompletion(self, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return "ok"

    messages = [{"role": "user", "content": "Read the source file"}]
    for index in range(2):
        call_id = f"bad-{index}"
        messages.extend([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "search_code",
                        "arguments": '{"path":"source.py"}',
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "search_code",
                "content": "1 validation error for SearchCodeInput\nquery\n Field required",
            },
        ])
    schemas = [
        {"type": "function", "function": {"name": name}}
        for name in ("search_code", "read_file", "create_plan")
    ]
    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    assert asyncio.run(agent._acompletion(
        messages, tools=schemas, tool_choice="auto"
    )) == "ok"
    assert captured["kwargs"]["tool_choice"] == "auto"
    assert [
        item["function"]["name"] for item in captured["kwargs"]["tools"]
    ] == ["read_file", "create_plan"]
    alert = captured["messages"][-1]["content"]
    assert "search_code" in alert
    assert "read_file(path)" in alert
    assert "create_plan" in alert


def test_the_breaker_alert_names_only_tools_the_request_still_declares():
    """An agent with its own tool set must not be pointed at read_file or create_plan."""
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    captured = {}

    class FakeAgent:
        model = "ollama/glm-5.3:cloud"
        name = "snippet_query"
        tools = []

        async def _acompletion(self, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return "ok"

    messages = [{"role": "user", "content": "Look this up"}]
    for index in range(2):
        call_id = f"bad-{index}"
        messages.extend([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "search_project", "arguments": '{"path":"."}'},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "search_project",
                "content": "1 validation error for SearchProjectInput\nquery\n Field required",
            },
        ])
    schemas = [
        {"type": "function", "function": {"name": name}}
        for name in ("search_project", "read_project_file")
    ]
    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    asyncio.run(agent._acompletion(messages, tools=schemas, tool_choice="auto"))
    alert = captured["messages"][-1]["content"]
    assert "search_project" in alert
    for foreign in ("read_file", "search_code", "create_plan"):
        assert foreign not in alert


def test_repeated_validation_reports_each_offending_tool():
    from gilzabots.litellm_compat import _repeated_tool_validation_error_names

    messages = []
    for name in ("search_code", "read_file"):
        messages.extend([
            {"role": "tool", "name": name, "content": "1 validation error\npath\n Field required"},
            {"role": "tool", "name": name, "content": "1 validation error\npath\n Field required"},
        ])

    assert _repeated_tool_validation_error_names(messages) == {"search_code", "read_file"}


def test_repeated_validation_is_scoped_to_the_current_user_turn():
    from gilzabots.litellm_compat import _repeated_tool_validation_error_names

    error = {
        "role": "tool",
        "name": "search_code",
        "content": "1 validation error\nquery\n Field required",
    }
    messages = [error, error, {"role": "user", "content": "Try a different task"}]

    assert _repeated_tool_validation_error_names(messages) == set()


def test_function_tool_schema_omits_adapter_implementation_docs():
    from agenticblocks.core.function_block import as_tool
    from agenticblocks.tools.a2a_bridge import block_to_tool_schema

    @as_tool(name="read_file", description="Read one file by path.")
    def read_file(path: str) -> str:
        return path

    description = block_to_tool_schema(read_file)["function"]["description"]

    assert description == "Read one file by path."
    assert "thread pool" not in description


def test_analyze_image_resanitizes_kwargs_for_final_model(monkeypatch):
    import gilzabots.tools as tools

    calls = {}

    class FakeMessage:
        content = "image description"

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeSession:
        model = "openai/gpt-5.5"
        project_path = "/fake/path"
        model_params = {
            "num_ctx": 8192,
            "top_k": 40,
            "temperature": 0.2,
        }

    def fake_completion(**kwargs):
        calls.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(tools, "_PROJECT_SESSION", FakeSession())
    monkeypatch.setattr("litellm.completion", fake_completion)
    monkeypatch.setattr("gilzabots.ui_settings.load_ui_settings", lambda: {})
    tools.set_recent_file_attachments({
        "input_file_0.jpg": {
            "type": "image",
            "data": "aGVsbG8=",
            "mime": "image/jpeg",
            "name": "input_file_0.jpg",
        }
    })

    raw = getattr(tools.analyze_image, "_func", None) or tools.analyze_image
    import asyncio

    result = asyncio.run(raw("input_file_0.jpg", "Describe"))

    assert result == "image description"
    assert calls["model"] == "openai/gpt-5.5"
    assert calls["drop_params"] is True
    assert "temperature" not in calls
    assert "num_ctx" not in calls
    assert "top_k" not in calls

def test_consolidate_leading_system_messages_merges_stray_system_messages_into_one():
    from gilzabots.litellm_compat import consolidate_leading_system_messages

    messages = [
        {"role": "system", "content": "base prompt"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "system", "content": "SYSTEM ALERT: mid-turn correction"},
        {"role": "user", "content": "continue"},
    ]

    consolidated = consolidate_leading_system_messages(messages)

    assert [m["role"] for m in consolidated] == ["system", "user", "assistant", "user"]
    assert consolidated[0]["content"] == "base prompt\n\nSYSTEM ALERT: mid-turn correction"


def test_consolidate_leading_system_messages_noop_for_single_system_message():
    from gilzabots.litellm_compat import consolidate_leading_system_messages

    messages = [
        {"role": "system", "content": "base prompt"},
        {"role": "user", "content": "hi"},
    ]

    assert consolidate_leading_system_messages(messages) is messages


def test_wrap_agent_litellm_compat_merges_mid_turn_system_alert_when_catalog_requires_it(monkeypatch):
    from gilzabots import litellm_compat
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    monkeypatch.setattr(litellm_compat, "model_requires_single_system_message", lambda model: True)

    calls = {}
    messages = [
        {"role": "system", "content": "base prompt"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "system", "content": "SYSTEM ALERT: empty response correction"},
    ]

    class FakeAgent:
        model = "ollama_chat/qwen3.8:latest"

        async def _acompletion(self, messages, **kwargs):
            calls["messages"] = messages
            return "ok"

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    result = asyncio.run(agent._acompletion(messages))

    assert result == "ok"
    assert [m["role"] for m in calls["messages"]] == ["system", "user", "assistant"]
    assert calls["messages"][0]["content"] == "base prompt\n\nSYSTEM ALERT: empty response correction"


def test_wrap_agent_litellm_compat_leaves_message_order_when_catalog_does_not_require_it(monkeypatch):
    from gilzabots import litellm_compat
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    monkeypatch.setattr(litellm_compat, "model_requires_single_system_message", lambda model: False)

    calls = {}
    messages = [
        {"role": "system", "content": "base prompt"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "system", "content": "SYSTEM ALERT: empty response correction"},
    ]

    class FakeAgent:
        model = "ollama_chat/gemma4:26b"

        async def _acompletion(self, messages, **kwargs):
            calls["messages"] = messages
            return "ok"

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    result = asyncio.run(agent._acompletion(messages))

    assert result == "ok"
    assert [m["role"] for m in calls["messages"]] == ["system", "user", "assistant", "system"]


def test_wrap_agent_litellm_compat_leaves_message_order_for_non_ollama_provider(monkeypatch):
    from gilzabots import litellm_compat
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    monkeypatch.setattr(litellm_compat, "model_requires_single_system_message", lambda model: False)

    calls = {}
    messages = [
        {"role": "system", "content": "base prompt"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "system", "content": "SYSTEM ALERT: empty response correction"},
    ]

    class FakeAgent:
        model = "openai/gpt-5"

        async def _acompletion(self, messages, **kwargs):
            calls["messages"] = messages
            return "ok"

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    result = asyncio.run(agent._acompletion(messages))

    assert result == "ok"
    assert [m["role"] for m in calls["messages"]] == ["system", "user", "assistant", "system"]


def test_wrap_agent_litellm_compat_merges_system_messages_for_openai_compatible_model(monkeypatch):
    """The capability is per model, not per provider prefix.

    The same chat templates that reject multiple system messages under Ollama
    reject them when the model is served over an OpenAI-compatible endpoint
    (vLLM/NIM), where the runtime id carries an ``openai/`` prefix.
    """
    from gilzabots import litellm_compat
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    monkeypatch.setattr(litellm_compat, "model_requires_single_system_message", lambda model: True)

    calls = {}
    messages = [
        {"role": "system", "content": "base prompt"},
        {"role": "system", "content": "recursive summary"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "system", "content": "SYSTEM ALERT: empty response correction"},
    ]

    class FakeAgent:
        model = "openai/nvidia/Qwen3.6-35B-A3B-NVFP4"

        async def _acompletion(self, messages, **kwargs):
            calls["messages"] = messages
            return "ok"

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    result = asyncio.run(agent._acompletion(messages))

    assert result == "ok"
    assert [m["role"] for m in calls["messages"]] == ["system", "user", "assistant"]
    assert calls["messages"][0]["content"] == (
        "base prompt\n\nrecursive summary\n\nSYSTEM ALERT: empty response correction"
    )


def test_consolidate_leading_system_messages_flattens_content_parts():
    from gilzabots.litellm_compat import consolidate_leading_system_messages

    messages = [
        {"role": "system", "content": [{"type": "text", "text": "base prompt"}]},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "SYSTEM ALERT: correction"},
    ]

    consolidated = consolidate_leading_system_messages(messages)

    assert consolidated[0]["content"] == "base prompt\n\nSYSTEM ALERT: correction"


def test_normalize_ollama_tool_call_parse_error_keeps_diagnostic_detail():
    from gilzabots.litellm_compat import _normalize_ollama_unexpected_response_error

    exc = Exception(
        "Ollama_chatException - KeyError: 'message', Got unexpected response "
        "from Ollama: {'error': 'error parsing tool call: invalid character "
        "comma in string escape code'}"
    )

    normalized = _normalize_ollama_unexpected_response_error(exc)

    assert "tool-call JSON was invalid" in str(normalized)
    assert "Original LiteLLM error" in str(normalized)


def test_normalize_llama_server_tool_call_rejection_as_tool_call_json_error():
    """Wording of llama-server-backed Ollama builds, recorded from a real session."""
    from gilzabots.litellm_compat import _normalize_ollama_unexpected_response_error

    normalized = _normalize_ollama_unexpected_response_error(Exception((
    "litellm.BadRequestError: Ollama_chatException - KeyError: 'message', Got unexpected "
    "response from Ollama: {'error': 'llama-server returned invalid tool call arguments "
    "for \"run_skill\": invalid character \\'\\\\n\\' in string literal'}"
)))

    assert "GILZABOTS_OLLAMA_TOOL_CALL_JSON_ESCAPE" in str(normalized)
    assert "run_skill" in str(normalized)
    assert "unexpected response instead of a chat message" not in str(normalized)


def test_wrap_agent_litellm_compat_merges_system_messages_for_thinking_remapped_model(tmp_path, monkeypatch):
    """Regression: the catalog flag must survive the ollama/ -> ollama_chat/ remap.

    An Ollama-served qwen3.8 rejects a request with more than one system
    message; the agent runs under the remapped `ollama_chat/` id while the
    catalog entry is stored as `ollama/`.
    """
    from gilzabots import models_store
    from gilzabots.config import resolve_model_route
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models.json")
    models_store.add_or_update_connection({
        "id": "ollama-conn",
        "label": "Ollama",
        "provider": "ollama",
        "api_key": "",
        "api_base": "http://localhost:11434",
    })
    models_store.save_models([
        {
            "id": "ollama/qwen3.8:latest",
            "connection_id": "ollama-conn",
            "name": "qwen3.8:latest",
            "supports_thinking": True,
            "requires_single_system_message": True,
        },
    ])

    runtime_model = resolve_model_route("ollama/qwen3.8:latest", {"think": True})
    assert runtime_model == "ollama_chat/qwen3.8:latest"

    calls = {}
    messages = [
        {"role": "system", "content": "base prompt"},
        {"role": "system", "content": "recursive summary"},
        {"role": "user", "content": "hi"},
    ]

    class FakeAgent:
        model = runtime_model

        async def _acompletion(self, messages, **kwargs):
            calls["messages"] = messages
            return "ok"

    agent = wrap_agent_litellm_compat(FakeAgent())

    import asyncio

    assert asyncio.run(agent._acompletion(messages)) == "ok"
    assert [m["role"] for m in calls["messages"]] == ["system", "user"]
    assert calls["messages"][0]["content"] == "base prompt\n\nrecursive summary"
