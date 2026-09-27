# Survey: Hermes memory providers — hosting & model requirements

Local survey of the bundled memory-provider plugins in `~/.hermes/hermes-agent/plugins/memory/`
(Hermes install inspected 2026-09-27). Every claim cites the plugin file it came from.
Purpose: ground the comparison table in the root README of `hermes-dakera`.

## Built-in baseline (no plugin)

- Built-in memory = `MEMORY.md` + `USER.md` files, no server.
  [source: website/docs/developer-guide/memory-provider-plugin.md:9]

## Bundled providers

| Provider | Hosting | External models needed? | Evidence |
|---|---|---|---|
| **holographic** | fully local (SQLite) | no (no LLM/embedder anywhere in plugin) | `plugins/memory/holographic/plugin.yaml` ("local SQLite fact store with FTS5"); `__init__.py` "SQLite is always available, numpy is optional" |
| **hindsight** | 3 modes: `cloud` (default, api.hindsight.vectorize.io) / `local_embedded` / `local_external` | local modes need an LLM provider for extraction — choices openai/anthropic/gemini/groq/openrouter/minimax/ollama/lmstudio/openai_compatible, default `openai` gpt-4o-mini; fully-local only if user picks ollama/lmstudio | `hindsight/__init__.py:2` ("cloud (API key), local_external, or local_embedded"), `:425-428` (mode + llm_provider schema); `settings.py:_DEFAULT_API_URL/_PROVIDER_DEFAULT_MODELS` |
| **mem0** | platform (cloud, default) / self-hosted server / OSS in-process | platform: cloud API. OSS mode needs LLM + embedder providers: openai (needs OPENAI_API_KEY) or ollama (local, needs Ollama running) | `mem0/_backend.py:20` ("Platform (MemoryClient), self-hosted (HTTP) and OSS (Memory)"); `_oss_providers.py` LLM_PROVIDERS/EMBEDDER_PROVIDERS ("openai" needs_key, "ollama" local) |
| **supermemory** | cloud default (api.supermemory.ai), base_url override for self-host | cloud default = external SaaS | `supermemory/__init__.py:23` `_DEFAULT_BASE_URL = "https://api.supermemory.ai"`, `:62` "config > SUPERMEMORY_BASE_URL > default (self-hosted support)" |
| **retaindb** | cloud API only | yes (RetainDB key) | `retaindb/plugin.yaml` `requires_env: [RETAINDB_API_KEY]`; description "cloud memory API" |
| **honcho** | cloud default (app.honcho.dev), baseUrl override for self-host | cloud default = external SaaS | `honcho/__init__.py` config schema: api_key url https://app.honcho.dev, baseUrl "for self-hosted" |
| **byterover** | local-first + optional cloud sync, needs external `brv` CLI (curl-pipe install) | LLM-driven search tier implies model access via brv | `byterover/__init__.py` ("local-first with optional cloud sync (BRV_API_KEY). Requires the brv CLI"); `plugin.yaml` external_dependencies curl-pipe install |
| **openviking** | managed VolcEngine cloud (default) or custom self-host server | managed endpoint = external SaaS | `openviking/__init__.py` `_OPENVIKING_SERVICE_ENDPOINT = "https://api.vikingdb.cn-beijing.volces.com/openviking"`; `_setup.py` "OpenViking Service (VolcEngine Cloud)" vs "local, VPS, or self-hosted" |

## Where dakera fits

`plugin/dakera/__init__.py` (this repo): talks to a **self-hosted Dakera server** the user runs;
recall/search are pure server queries (`dakera` SDK, no LLM calls from the plugin), retain stores
verbatim turn pairs — no fact-extraction LLM, no external embedding API from the client side.
Whether the server itself needs an external model is answered by the Dakera GitHub survey
(`research/dakera-github-overview.md`).
