# Image Generation in GilzaBots

Status: **implemented**. This file replaces the earlier analysis, which predated
the feature and still referred to the discontinued Cloud proxy.

The design rationale lives in [PROJECT_DESIGN.md](../PROJECT_DESIGN.md) §2.11.
This page is the operational guide: what the pieces are, and how to point them
at a provider.

## Architecture in one line

```
gilzabots/tools.py: generate_image        tool: prompt in, project file out
        ↓
gilzabots/image_gen_config.py             which model, which route, where to save
        ↓
agenticblocks/blocks/image/              ImageGenerationBlock + adapter registry
        ├─ images_api        → litellm.aimage_generation  (/v1/images/generations)
        └─ chat_multimodal   → litellm.acompletion(modalities=["text","image"])
```

Everything is normalised to the OpenAI Images shape, so a provider is a catalog
entry — a name plus an `api_base` — and only a genuinely different protocol
needs new code (`register_image_adapter`).

## Setting it up

1. **Register the model** — top bar → *Edit Models* → new model on a provider
   connection, then tick **"Generates images"** and pick the transport.
2. **Select it** — *Settings → General → Image Generation*: enable the feature,
   choose the model, set the default size and the output folder (project-relative,
   defaults to `figures`).
3. **Use it** — ask the agent for an illustration. It calls `generate_image`,
   the file lands in the project, the chat shows it inline, and the returned
   path drops into `\includegraphics`.

## Provider matrix

| Where it runs | Connection provider / base URL | Notes |
|---|---|---|
| OpenAI | `openai`, default base URL | `gpt-image-*`, `dall-e-3`. Returns a URL; the block downloads it. |
| Google | `gemini` | Imagen ids use `:predict`, `gemini-*-flash-image` uses `:generateContent`; LiteLLM picks the endpoint. The flash-image models can also run on `chat_multimodal` for conversational editing. |
| Stability, Recraft, fal.ai, Bedrock, Vertex, OpenRouter, Black Forest Labs | their own provider slug | Covered by the same `images_api` route. |
| **LocalAI** | `openai`, `http://localhost:8080/v1` | Offline, CPU or GPU. |
| **Docker Model Runner** | `openai`, `http://localhost:12434/engines/diffusers/v1` | Offline; NVIDIA CUDA, Apple MPS or CPU fallback. |
| **vLLM-Omni** | `openai`, `http://localhost:8000/v1` | Linux + NVIDIA, compute capability ≥ 7.0. |
| **Ollama** | `openai`, `http://<host>:11434/v1` | Image generation is experimental and **macOS-only** at the time of writing; the Linux build returns a bare 404 for the route. A non-Mac client can drive a Mac over the network. |

A local server is registered as provider `openai` on purpose: what makes it work
is the OpenAI Images contract it serves, and `api_base` is what selects it.

## Failure messages

`generate_image` never falls back to another provider or to a placeholder. Each
failure is classified and tells the user what to do:

| Kind | Means | Action offered |
|---|---|---|
| `not_configured` | no model selected | configure it in Settings |
| `auth` | key missing or rejected | set it in *Edit Models* |
| `connection` | endpoint unreachable | start the local server / fix `api_base` |
| `unsupported` | the endpoint has no image route (404) | use LocalAI/DMR/vLLM-Omni, or a host that serves it |
| `bad_request` | provider rejected model or size | reported verbatim |
| `empty` | accepted, returned no image | usually a content filter |

## Tests

`tests/test_image_generation.py` covers the block, both adapters (with fake
LiteLLM entry points), the config, and the tool. The provider is never contacted.
The transport itself was additionally verified end-to-end against a local HTTP
server implementing `/v1/images/generations`, including the 404 and
connection-refused paths.
