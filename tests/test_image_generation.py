"""Tests for image generation: the AgenticBlocks block and the OpalaTex tool.

Nothing here reaches a provider. The framework tests register a fake adapter and
fake LiteLLM entry points; the OpalaTex tests replace the block with a stub. What
is being pinned down is the contract every provider must be normalised into --
one file on disk, a project-relative path in the result, and a classified error
when generation cannot happen.
"""

import asyncio
import base64
import io
import json
from types import SimpleNamespace

import pytest

from agenticblocks.blocks.image import (
    ImageArtifact,
    ImageGenerationBlock,
    ImageGenerationError,
    ImageGenerationInput,
    extension_for_mime,
    register_image_adapter,
    resolve_image_bytes,
    sniff_image_mime,
)
from agenticblocks.blocks.image import adapters as image_adapters


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _png_b64() -> str:
    return base64.b64encode(_png_bytes()).decode("ascii")


# ─── Framework: block dispatch and validation ────────────────────────────────

def test_block_dispatches_to_registered_adapter():
    seen = {}

    async def fake_adapter(model, request, model_kwargs):
        seen["model"] = model
        seen["prompt"] = request.prompt
        seen["kwargs"] = model_kwargs
        return [ImageArtifact(data_b64=_png_b64(), mime="image/png")]

    register_image_adapter("fake_route", fake_adapter)
    block = ImageGenerationBlock(
        name="t", model="provider/model", route="fake_route", model_kwargs={"api_key": "k"}
    )

    out = asyncio.run(block.run(ImageGenerationInput(prompt="a red cube")))

    assert seen["model"] == "provider/model"
    assert seen["prompt"] == "a red cube"
    assert seen["kwargs"] == {"api_key": "k"}
    assert out.route == "fake_route"
    assert out.images[0].has_bytes()


def test_block_rejects_unknown_route():
    block = ImageGenerationBlock(name="t", model="m", route="nope")
    with pytest.raises(ImageGenerationError) as exc:
        asyncio.run(block.run(ImageGenerationInput(prompt="x")))
    assert exc.value.kind == "unknown_route"


def test_block_rejects_missing_model_and_empty_prompt():
    with pytest.raises(ImageGenerationError) as exc:
        asyncio.run(ImageGenerationBlock(name="t", model="").run(ImageGenerationInput(prompt="x")))
    assert exc.value.kind == "not_configured"

    with pytest.raises(ImageGenerationError) as exc:
        asyncio.run(ImageGenerationBlock(name="t", model="m").run(ImageGenerationInput(prompt="  ")))
    assert exc.value.kind == "bad_request"


def test_block_fails_loudly_when_provider_returns_nothing():
    async def empty_adapter(model, request, model_kwargs):
        return []

    register_image_adapter("empty_route", empty_adapter)
    block = ImageGenerationBlock(name="t", model="m", route="empty_route")

    with pytest.raises(ImageGenerationError) as exc:
        asyncio.run(block.run(ImageGenerationInput(prompt="x")))
    assert exc.value.kind == "empty"


# ─── Framework: bytes, mime and URL resolution ───────────────────────────────

def test_sniff_image_mime_and_extension():
    assert sniff_image_mime(_png_bytes()) == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_image_mime(b"not an image") == ""
    assert extension_for_mime("image/jpeg") == ".jpg"
    assert extension_for_mime("") == ".png"


def test_resolve_image_bytes_prefers_real_bytes_over_declared_mime():
    artifact = ImageArtifact(data_b64=_png_b64(), mime="image/jpeg")
    data, mime = asyncio.run(resolve_image_bytes(artifact))
    assert data.startswith(b"\x89PNG")
    assert mime == "image/png"


def test_resolve_image_bytes_refuses_non_http_url():
    with pytest.raises(ImageGenerationError) as exc:
        asyncio.run(resolve_image_bytes(ImageArtifact(url="file:///etc/passwd")))
    assert exc.value.kind == "bad_request"


def test_url_only_artifact_does_not_decode_to_empty_bytes():
    with pytest.raises(ImageGenerationError):
        ImageArtifact(url="https://example.com/a.png").to_bytes()


# ─── Framework: the images_api adapter ───────────────────────────────────────

def test_images_api_adapter_normalises_response_and_filters_chat_kwargs(monkeypatch):
    captured = {}

    async def fake_aimage_generation(model, prompt, **kwargs):
        captured["model"] = model
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            data=[{"b64_json": _png_b64(), "revised_prompt": "a red cube, studio light"}]
        )

    import litellm

    monkeypatch.setattr(litellm, "aimage_generation", fake_aimage_generation)

    request = ImageGenerationInput(prompt="a red cube", size="512x512", negative_prompt="blurry", seed=7)
    artifacts = asyncio.run(
        image_adapters.images_api_adapter(
            "openai/stable-diffusion",
            request,
            # A chat kwargs bag: only the transport fields may survive.
            {"api_key": "k", "api_base": "http://localhost:8080/v1", "temperature": 0.7, "num_ctx": 8192, "think": True},
        )
    )

    assert captured["kwargs"]["api_base"] == "http://localhost:8080/v1"
    assert captured["kwargs"]["size"] == "512x512"
    assert captured["kwargs"]["negative_prompt"] == "blurry"
    assert captured["kwargs"]["seed"] == 7
    assert "temperature" not in captured["kwargs"]
    assert "num_ctx" not in captured["kwargs"]
    assert "think" not in captured["kwargs"]
    assert artifacts[0].revised_prompt == "a red cube, studio light"


def test_images_api_adapter_keeps_remote_url_and_unpacks_data_uri(monkeypatch):
    async def fake_aimage_generation(model, prompt, **kwargs):
        return SimpleNamespace(
            data=[
                {"url": "https://cdn.example.com/a.png"},
                {"url": f"data:image/png;base64,{_png_b64()}"},
            ]
        )

    import litellm

    monkeypatch.setattr(litellm, "aimage_generation", fake_aimage_generation)

    artifacts = asyncio.run(
        image_adapters.images_api_adapter("openai/dall-e-3", ImageGenerationInput(prompt="x"), {})
    )

    assert artifacts[0].url == "https://cdn.example.com/a.png" and not artifacts[0].has_bytes()
    assert artifacts[1].has_bytes() and artifacts[1].url == ""


def test_images_api_adapter_classifies_missing_route_as_unsupported(monkeypatch):
    import litellm

    async def fake_aimage_generation(model, prompt, **kwargs):
        raise litellm.exceptions.NotFoundError(
            message="404 page not found", model=model, llm_provider="openai"
        )

    monkeypatch.setattr(litellm, "aimage_generation", fake_aimage_generation)

    with pytest.raises(ImageGenerationError) as exc:
        asyncio.run(
            image_adapters.images_api_adapter("openai/x", ImageGenerationInput(prompt="x"), {})
        )
    assert exc.value.kind == "unsupported"


def test_images_api_adapter_classifies_auth_and_connection_failures(monkeypatch):
    import litellm

    for exception, expected in (
        (litellm.exceptions.AuthenticationError(message="bad key", model="m", llm_provider="openai"), "auth"),
        (litellm.exceptions.APIConnectionError(message="refused", model="m", llm_provider="openai"), "connection"),
    ):
        async def fake(model, prompt, _exc=exception, **kwargs):
            raise _exc

        monkeypatch.setattr(litellm, "aimage_generation", fake)
        with pytest.raises(ImageGenerationError) as exc:
            asyncio.run(image_adapters.images_api_adapter("m", ImageGenerationInput(prompt="x"), {}))
        assert exc.value.kind == expected


# ─── Framework: the chat_multimodal adapter ──────────────────────────────────

def test_chat_multimodal_adapter_reads_images_from_the_message(monkeypatch):
    captured = {}

    async def fake_acompletion(model, messages, **kwargs):
        captured["modalities"] = kwargs.get("modalities")
        captured["messages"] = messages
        message = SimpleNamespace(
            images=[{"image_url": {"url": f"data:image/png;base64,{_png_b64()}"}}],
            content="here it is",
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    artifacts = asyncio.run(
        image_adapters.chat_multimodal_adapter(
            "gemini/gemini-2.5-flash-image",
            ImageGenerationInput(prompt="a red cube", negative_prompt="blurry"),
            {"api_key": "k"},
        )
    )

    assert captured["modalities"] == ["text", "image"]
    assert "Avoid: blurry" in captured["messages"][0]["content"]
    assert artifacts[0].has_bytes()
    assert artifacts[0].mime == "image/png"


# ─── OpalaTex: configuration ─────────────────────────────────────────────────

def test_image_config_round_trip_and_output_dir_containment(tmp_path, monkeypatch):
    from opalatex import image_gen_config

    monkeypatch.setattr(image_gen_config, "_CONFIG_PATH", tmp_path / "image_gen.json")

    image_gen_config.save_config(
        {"enabled": True, "model": "openai/sd", "size": "512x512", "output_dir": "../../etc", "route": ""}
    )
    cfg = image_gen_config.load_config()

    assert cfg["model"] == "openai/sd"
    assert cfg["size"] == "512x512"
    # A path that escapes the project is normalised, never persisted as-is.
    assert cfg["output_dir"] == "etc"

    image_gen_config.save_config({**cfg, "output_dir": "/abs/figures"})
    assert image_gen_config.load_config()["output_dir"] == "abs/figures"


def test_configuration_problem_reports_disabled_and_unset_model(tmp_path, monkeypatch):
    from opalatex import image_gen_config

    cfg_path = tmp_path / "image_gen.json"
    monkeypatch.setattr(image_gen_config, "_CONFIG_PATH", cfg_path)

    cfg_path.write_text(json.dumps({"enabled": False, "model": "openai/sd"}), encoding="utf-8")
    assert "disabled" in image_gen_config.configuration_problem()

    cfg_path.write_text(json.dumps({"enabled": True, "model": ""}), encoding="utf-8")
    assert "No image generation model is configured" in image_gen_config.configuration_problem()

    cfg_path.write_text(json.dumps({"enabled": True, "model": "openai/sd"}), encoding="utf-8")
    assert image_gen_config.configuration_problem() == ""


def test_transport_kwargs_drops_chat_parameters(monkeypatch):
    from opalatex import image_gen_config

    monkeypatch.setattr(
        "opalatex.config.get_agent_llm_kwargs",
        lambda name, model_override=None: {
            "api_key": "k",
            "api_base": "http://localhost:12434/engines/diffusers/v1",
            "temperature": 0.7,
            "num_ctx": 16384,
            "stream": True,
        },
    )

    assert image_gen_config.transport_kwargs("openai/sd") == {
        "api_key": "k",
        "api_base": "http://localhost:12434/engines/diffusers/v1",
    }


def test_route_resolution_prefers_override_then_catalog(monkeypatch, tmp_path):
    from opalatex import image_gen_config

    cfg_path = tmp_path / "image_gen.json"
    monkeypatch.setattr(image_gen_config, "_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(
        "opalatex.models_store.get_model",
        lambda model_id: {"id": model_id, "image_route": "chat_multimodal"},
    )

    cfg_path.write_text(json.dumps({"route": ""}), encoding="utf-8")
    assert image_gen_config.resolve_route("gemini/flash-image") == "chat_multimodal"

    cfg_path.write_text(json.dumps({"route": "images_api"}), encoding="utf-8")
    assert image_gen_config.resolve_route("gemini/flash-image") == "images_api"


def test_models_store_normalises_image_capability():
    from opalatex.models_store import normalize_model_entry

    entry = normalize_model_entry(
        {"id": "openai/sd", "provider": "openai", "name": "sd", "supports_image_generation": True, "image_route": "IMAGES_API"}
    )
    assert entry["supports_image_generation"] is True
    assert entry["image_route"] == "images_api"

    assert normalize_model_entry({"id": "a"})["supports_image_generation"] is False
    assert normalize_model_entry({"id": "a", "image_route": "bogus"})["image_route"] == ""


# ─── OpalaTex: the generate_image tool ───────────────────────────────────────

def _stub_block(monkeypatch, artifact=None, error=None):
    from agenticblocks.blocks.image import ImageGenerationOutput

    class _Block:
        model = "openai/stable-diffusion"

        async def run(self, request):
            if error is not None:
                raise error
            return ImageGenerationOutput(
                images=[artifact or ImageArtifact(data_b64=_png_b64(), mime="image/png")],
                model=self.model,
                route="images_api",
            )

    monkeypatch.setattr("opalatex.image_gen_config.build_block", lambda *a, **k: _Block())


def _configure(tmp_path, monkeypatch, **overrides):
    from opalatex import image_gen_config
    from opalatex.tools import set_project_context

    cfg_path = tmp_path / "image_gen.json"
    cfg_path.write_text(
        json.dumps({"enabled": True, "model": "openai/sd", "size": "512x512", "output_dir": "figures", **overrides}),
        encoding="utf-8",
    )
    monkeypatch.setattr(image_gen_config, "_CONFIG_PATH", cfg_path)
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))


def _run_tool(**kwargs):
    from opalatex.tools import generate_image

    raw = getattr(generate_image, "_func", None) or generate_image
    return asyncio.run(raw(**kwargs))


def test_generate_image_saves_file_and_returns_relative_path(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    _stub_block(monkeypatch)

    result = _run_tool(prompt="a red cube on a table", filename="cube")

    saved = tmp_path / "figures" / "cube.png"
    assert saved.exists() and saved.read_bytes().startswith(b"\x89PNG")
    assert "figures/cube.png" in result
    assert "\\includegraphics" in result and "figures/cube}" in result
    # The bytes stay on disk: a base64 payload in the result would blow the window.
    assert _png_b64() not in result
    assert "4x4" in result
    # A small file must not be reported as "0 KB".
    assert "0 KB" not in result and "bytes)" in result


def test_generate_image_derives_filename_and_never_overwrites(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    _stub_block(monkeypatch)

    first = _run_tool(prompt="A Red Cube!")
    second = _run_tool(prompt="A Red Cube!")

    assert "figures/a-red-cube.png" in first
    assert "figures/a-red-cube-2.png" in second
    assert (tmp_path / "figures" / "a-red-cube.png").exists()
    assert (tmp_path / "figures" / "a-red-cube-2.png").exists()


def test_generate_image_uses_configured_output_dir_and_size(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, output_dir="assets/img")

    captured = {}
    from agenticblocks.blocks.image import ImageGenerationOutput

    class _Block:
        model = "gemini/imagen"

        async def run(self, request):
            captured["size"] = request.size
            captured["seed"] = request.seed
            return ImageGenerationOutput(
                images=[ImageArtifact(data_b64=_png_b64(), mime="image/png")], model=self.model
            )

    monkeypatch.setattr("opalatex.image_gen_config.build_block", lambda *a, **k: _Block())

    result = _run_tool(prompt="x", filename="fig", seed=42)

    assert captured["size"] == "512x512"
    assert captured["seed"] == 42
    assert (tmp_path / "assets" / "img" / "fig.png").exists()
    assert "assets/img/fig.png" in result


def test_generate_image_reports_configuration_problem_without_calling_provider(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, model="")

    def _explode(*a, **k):
        raise AssertionError("the provider must not be called when unconfigured")

    monkeypatch.setattr("opalatex.image_gen_config.build_block", _explode)

    result = _run_tool(prompt="x")
    assert result.startswith("CRITICAL ERROR")
    assert "Settings > General > Image Generation" in result


def test_generate_image_turns_a_missing_route_into_actionable_guidance(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    _stub_block(monkeypatch, error=ImageGenerationError("no route here", kind="unsupported"))

    result = _run_tool(prompt="x")

    assert result.startswith("CRITICAL ERROR")
    assert "LocalAI" in result and "Docker Model Runner" in result


def test_generate_image_rejects_empty_prompt(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    assert _run_tool(prompt="   ").startswith("CRITICAL ERROR")


# ─── OpalaTex: registration ──────────────────────────────────────────────────

def test_generate_image_is_registered_for_the_main_agent_not_helpers(monkeypatch):
    from opalatex.agent_stdin import ALL_TOOLS_MAP
    from opalatex.tools import get_helper_tools, get_workspace_action_tools

    assert "generate_image" in ALL_TOOLS_MAP
    assert any(getattr(t, "name", "") == "generate_image" for t in get_workspace_action_tools())
    # A helper returns data; an image is a deliverable, made by the main agent.
    assert not any(getattr(t, "name", "") == "generate_image" for t in get_helper_tools())


def test_generate_image_is_not_safe_in_plan_mode(monkeypatch):
    import opalatex.tools as tools

    monkeypatch.setattr(tools, "_PROJECT_SESSION", SimpleNamespace(mode="plan", results={}))
    raw = getattr(tools.generate_image, "_func", None) or tools.generate_image
    result = asyncio.run(raw(prompt="x"))
    assert "Execution blocked" in result
