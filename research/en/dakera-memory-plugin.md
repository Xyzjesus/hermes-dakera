# Research: Dakera-backed memory plugin for Hermes

Research notes for a Hermes plugin that uses Dakera (https://github.com/Dakera-AI/dakera-py) as its memory backend. This workspace has no notes convention (repo contains only `.omp/`), so this file lives under `research/` as a new convention.

Sources inspected: a fresh shallow clone of `Dakera-AI/dakera-py` at `/tmp/dakera-py` (HEAD at research time, SDK version 0.12.13), the local Hermes install at `~/.hermes` (including the bundled `hermes-agent` source tree at `~/.hermes/hermes-agent`), and the omp fork at `<local omp fork>` which already has a working Dakera integration. Every claim cites the file that owns it; untraceable claims are marked [INFERENCE].

---

## TL;DR

- Dakera-py is a thin, typed HTTP SDK (`dakera` on PyPI, 0.12.13, pure `requests`) in front of a self-hosted Dakera server; the whole memory surface is 5 REST calls: `store_memory`, `recall`, `search_memories`, `update_memory`, `forget` [source: /tmp/dakera-py/src/dakera/client.py].
- Hermes already has a first-class, documented plugin kind for exactly this: a **memory provider plugin** implementing the `MemoryProvider` ABC plus a `register(ctx)` entry point, activated by `memory.provider: <name>` in `config.yaml` and managed by `hermes memory setup` [source: ~/.hermes/hermes-agent/website/docs/developer-guide/memory-provider-plugin.md].
- A working precedent exists: the omp fork's `memory.backend: dakera` integration (`<local omp fork>/packages/coding-agent/src/dakera/`) talks to the same self-hosted server (live at `<private LAN URL>`, server v0.11.108, verified healthy) with plain `POST /v1/memory/{store,recall}` + bearer auth and per-`agent_id` isolation [source: <local omp fork>/packages/coding-agent/src/dakera/client.ts].
- The local Hermes install has **zero** Dakera wiring today (grep across `~/.hermes` found no `dakera` reference in any .py/.yaml/.json); a new user plugin at `~/.hermes/plugins/dakera/` (or a pip entry point) must implement the full provider: recall prefetch, per-turn retain, tool schemas, config schema, session-end flush.
- The name collision to avoid: this workspace's `.omp/config.yml` uses `dakera:` as the omp settings key; in Hermes the provider name (`dakera`), the `memory.provider` value, and the tool names (`dakera_*`) are the user-facing contract [source: ~/.hermes/hermes-agent/website/docs/developer-guide/memory-provider-plugin.md, "Setup UX" section].

---

## 1. Dakera-py API surface

### 1.1 Package, install, transport

- Package `dakera`, version **0.12.13**, MIT, `requires-python >= 3.10`, runtime deps only `requests>=2.33.0` and `urllib3>=2.6.3` [source: /tmp/dakera-py/pyproject.toml:5-33].
- Install: `pip install dakera`; async extra: `pip install dakera[async]` [source: /tmp/dakera-py/README.md, "Install" section].
- Public exports: `DakeraClient`, `AsyncDakeraClient`, `ChatMemorySession`, `AsyncChatMemorySession`, memory types (`StoreMemoryRequest`, `Memory`, `RecalledMemory`, `RecallResponse`, batch types), vector/namespace types, `RetryConfig`, consistency enums [source: /tmp/dakera-py/src/dakera/__init__.py:160-230].
- Transport is plain HTTP/JSON (not MCP): all methods funnel through `_request()` → `requests.Session` with `Content-Type: application/json` and User-Agent `dakera-py/<version>` [source: /tmp/dakera-py/src/dakera/client.py:26-30, 311+]. MCP is a **separate** product, `dakera-mcp` ("MCP server for Claude/Cursor") listed under "Other SDKs" [source: /tmp/dakera-py/README.md, "Other SDKs" table].
- Server: single-binary, Docker one-liner on port 3000 with `DAKERA_ROOT_API_KEY`; `GET /health` returns `{"status":"ok"}`; persistent deployments via the `dakera-deploy` docker-compose repo [source: /tmp/dakera-py/README.md, "Run Dakera" section]. Search modes: vector (ANN/HNSW), BM25 full-text, hybrid (RRF fusion), knowledge graph; embeddings are built in server-side [source: /tmp/dakera-py/README.md, "Why Dakera?" and "Features" sections].

### 1.2 Client construction & auth

```python
from dakera import DakeraClient, RetryConfig
client = DakeraClient(
    base_url="http://localhost:3000",   # positional; .rstrip("/") applied
    api_key="dk-mykey",                  # optional
    timeout=30.0,                        # per-request, seconds
    connect_timeout=None,                # defaults to timeout
    max_retries=3,                       # ignored when retry_config given
    retry_config=None,                   # RetryConfig(max_retries, base_delay=0.1, max_delay=60.0, jitter=True)
    headers=None,                        # extra headers
    ode_url=None,                        # dakera-ode sidecar, only for extract_entities
)
```
[source: /tmp/dakera-py/src/dakera/client.py:155-201; RetryConfig at /tmp/dakera-py/src/dakera/models.py:74-88]

- Auth: when `api_key` is set, every request carries `Authorization: Bearer <api_key>` [source: /tmp/dakera-py/src/dakera/client.py:202-203]. The same bearer scheme is what the live self-hosted server expects (verified: `curl -X POST $URL/v1/memory/recall -H "Authorization: Bearer $TOKEN"` returned memories from the live instance) [source: live probe against <private LAN URL>, 2026-09-27].
- Errors are typed exceptions: `ValidationError` (400), `AuthenticationError` (401), `AuthorizationError`, `NotFoundError`, `RateLimitError`, `ServerError`, `TimeoutError`, `ConnectionError`, with `ErrorCode` parsed from the body's `code` field [source: /tmp/dakera-py/src/dakera/client.py:223-266; /tmp/dakera-py/src/dakera/exceptions.py]. Rate-limit headers are captured per response via `client.last_rate_limit_headers` [source: /tmp/dakera-py/src/dakera/client.py:212-216, 225].
- Async twin `AsyncDakeraClient` (httpx, all methods coroutines, async context manager) has identical constructor signature [source: /tmp/dakera-py/src/dakera/async_client.py:164-230].

### 1.3 Memory operations (the surface a plugin needs)

All agent-scoped memory calls take an `agent_id: str` — this is Dakera's isolation unit (there is no "bank"; omp's docs confirm: "Isolation is the `agent_id`") [source: <local omp fork>/packages/coding-agent/src/dakera/backend.ts:1-6].

- **Store** — `store_memory(agent_id, content, memory_type="episodic", importance=None, metadata=None, session_id=None, tags=None, ttl_seconds=None, expires_at=None, valid_from=None) -> dict` → `POST /v1/memory/store`. `memory_type` is one of `"episodic" | "semantic" | "procedural" | "working"`; `importance` is 0.0–1.0; `valid_from` (bi-temporal) requires server v0.11.98+ (DAK-7424) [source: /tmp/dakera-py/src/dakera/client.py:1061-1114].
- **Recall** — `recall(agent_id, query, top_k=5, memory_type=None, min_importance=None, include_associated=False, associated_memories_cap/depth/min_weight=None, since=None, until=None, routing=None, rerank=None, fusion=None, vector_weight=None, iterations=None, neighborhood=None) -> RecallResponse` → `POST /v1/memory/recall` [source: /tmp/dakera-py/src/dakera/client.py:1116-1216]. `routing` is `RoutingMode`: `auto` (default) / `vector` / `bm25` / `hybrid`; `fusion` is `FusionStrategy`: `rrf` (default) / `minmax` [source: /tmp/dakera-py/src/dakera/models.py:37-72].
- **Search (lexical/listing flavor)** — `search_memories(agent_id, query, top_k=10, memory_type=None, min_importance=None, routing=None, rerank=None) -> list[dict]` → `POST /v1/memory/search` [source: /tmp/dakera-py/src/dakera/client.py:1313-1343].
- **Read/update/delete** — `get_memory(agent_id, memory_id)` → `GET /v1/memory/get/{memory_id}`; `update_memory(agent_id, memory_id, content=None, metadata=None, memory_type=None)` → `PUT /v1/memory/update/{memory_id}`; `forget(agent_id, memory_id)` → `POST /v1/memory/forget` with `{"agent_id", "memory_ids": [id]}` [source: /tmp/dakera-py/src/dakera/client.py:1219-1244].
- **Batch** — `batch_recall(BatchRecallRequest)` (filter predicates, `POST /v1/memories/recall/batch`, no embedding needed), `batch_forget(BatchForgetRequest)`, `store_memories_batch(BatchStoreMemoryRequest)` [source: /tmp/dakera-py/src/dakera/client.py:1246-1311].
- **Lifecycle/quality extras** — `compress_agent(agent_id)`, `update_importance(...)`, `consolidate(...)`/`consolidate_agent(agent_id)`, feedback endpoints (`POST /v1/agents/{agent_id}/memories/feedback`, `POST /v1/memories/{memory_id}/feedback`, …), `wake_up(agent_id, top_n=20, min_importance=0.0)` → `GET /v1/agents/{agent_id}/wake-up` (importance×exp decay, metadata-index served, "no embedding inference") [source: /tmp/dakera-py/src/dakera/client.py:1345-1420, 1466-1567, 1839-1857].
- **Sessions** — `start_session(agent_id, metadata=None)` → `POST /v1/sessions/start` (returns `result["session"]` with `id`); `end_session(session_id, summary=None)` → `POST /v1/sessions/{id}/end`; `get_session`, session listing, `get_session_memories` [source: /tmp/dakera-py/src/dakera/client.py:1750-1800].
- **Vector/namespace API** (separate from agent memory; a plugin likely doesn't need it) — `upsert`, `query`, `delete`, `fetch`, `batch_query`, `upsert_text`, `query_text`, `index_documents`, `fulltext_search`, `hybrid_search`, namespace CRUD, `health`/`health_ready`/`health_live`, admin ops [source: /tmp/dakera-py/src/dakera/client.py:364-1060, 1003-1018, 2411-2490].

### 1.4 Data model

- `Memory`: `id, content, memory_type, importance (default 0.5), metadata, created_at, updated_at, access_count` [source: /tmp/dakera-py/src/dakera/models.py:666-691].
- `RecalledMemory`: `id, content, memory_type, importance, score, smart_score, weighted_score, metadata, created_at, depth, vector_score, text_score`. Ranking: `.score` resolves `smart_score` → `weighted_score` → raw `score` (changed in 0.12.10 so `.score` matches server rank order); `vector_score`/`text_score` are hybrid sub-scores from server v0.11.98+ [source: /tmp/dakera-py/src/dakera/models.py:693-740; /tmp/dakera-py/CHANGELOG.md, 0.12.10 and 0.12.11 entries].
- `RecallResponse`: `memories: list[RecalledMemory]`, optional `associated_memories` (KG traversal, each with `depth`) [source: /tmp/dakera-py/src/dakera/models.py:742-780].
- `StoreMemoryRequest` mirrors `store_memory` args plus a precomputed `embedding` bypass [source: /tmp/dakera-py/src/dakera/models.py:636-664].
- Timestamp caveat from omp's hand-rolled client: the server returns Unix-seconds **numbers** for `created_at`/`last_accessed_at` on every probed endpoint, "not the ISO strings the Python SDK's models imply" — SDK types them `str | None` [source: <local omp fork>/packages/coding-agent/src/dakera/client.ts:36-41 vs /tmp/dakera-py/src/dakera/models.py:713-714].

### 1.5 High-level session helper

`ChatMemorySession` wraps the raw API in the chat pattern a memory plugin replicates:

- `ChatMemorySession.create(client, agent_id, metadata=None)` → `start_session` + bind [source: /tmp/dakera-py/src/dakera/session.py:66-91].
- `session.store(role, content, importance=0.6, tags=None)` — persists a turn, tags each memory with its role, default importance 0.6 [source: /tmp/dakera-py/src/dakera/session.py:97-133].
- `session.recall(query, top_k=5) -> list[RecalledMemory]` — searches the agent's **full** memory, not just the current session [source: /tmp/dakera-py/src/dakera/session.py:135-156].
- `session.close(summary=None)` → `end_session`; usable as a context manager [source: /tmp/dakera-py/src/dakera/session.py:158-196].
- Reference usage in `examples/ollama_memory_chat.py`: recall before each LLM call, inject as system message, `session.store("user", ...)` / `session.store("assistant", ...)` after, seed durable preferences at importance 0.95 so they outrank chatter, `close(summary=...)` at exit [source: /tmp/dakera-py/examples/ollama_memory_chat.py:41-110].

### 1.6 Versioning & operational caveats

- Server compat markers in the SDK: `valid_from` and hybrid sub-scores need server v0.11.98+; the live self-hosted instance runs **v0.11.108** (`{"service":"dakera","status":"healthy","version":"0.11.108",...}`) [source: /tmp/dakera-py/CHANGELOG.md 0.12.11 entry; live `GET /health` probe, 2026-09-27].
- omp's client documents hard-won operational facts about this same deployment: `consolidate` is "a plain concatenation, not a synthesis" and its `dry_run` is ignored (never present it as read-only); the lexical `search` endpoint's scores are unnormalized (observed 1.42) so they can't be ranked beside recall's; `rerank: true` on this 4-core host costs ~2s per candidate (10–22s per query), so omp disables it (`recallRerank: false` in `~/.omp/agent/dakera.yml`) [source: <local omp fork>/packages/coding-agent/src/dakera/client.ts:5-19; ~/.omp/agent/dakera.yml:14-19; <local omp fork>/.dakera-migration/issue-dakera-rerank.md].
- omp additionally never sends `valid_from` because "older servers reject it", and uses `POST /v1/memory/forget` with `memory_ids` instead of the batch-forget filter envelope it couldn't satisfy [source: <local omp fork>/packages/coding-agent/src/dakera/client.ts:7-19, 291-309].

---

## 2. Hermes plugin system

Hermes is installed locally: binary `~/.local/bin/hermes`, runtime state in `~/.hermes/`, and the **full agent source tree** at `~/.hermes/hermes-agent` (Python; `hermes plugins list` and `hermes memory status` both execute successfully) [source: command outputs, 2026-09-27].

### 2.1 Two plugin layers

1. **General plugins** — discovered from bundled `plugins/`, user `~/.hermes/plugins/<name>/`, project `./.hermes/plugins/` (opt-in via `HERMES_ENABLE_PROJECT_PLUGINS=1`), and the `hermes_agent.plugins` pip entry-point group. A directory plugin needs `plugin.yaml` [source: ~/.hermes/hermes-agent/hermes_cli/plugins.py:1-6, 68; hermes_cli/plugins_discovery.py:27, 141-173].
2. **Memory provider plugins** — a dedicated single-select kind activated by `memory.provider` in `config.yaml`, discovered in **reverse** precedence (bundled wins): bundled `plugins/memory/<name>/` → user `$HERMES_HOME/plugins/<name>/` → project `./.hermes/plugins/<name>/` → `hermes_agent.memory_providers` entry points. Discovery only enumerates; nothing imports until `memory.provider` names the provider [source: ~/.hermes/hermes-agent/plugins/memory/__init__.py:1-30; hermes_cli/plugins_discovery.py:90-100].

Bundled memory providers on disk: `byterover`, `hindsight`, `holographic`, `honcho`, `mem0`, `openviking`, `retaindb`, `supermemory` (plus `config_schema.py`, `query_rewrite.py` helpers) [source: ls ~/.hermes/hermes-agent/plugins/memory/]. The tree is **closed to new in-tree providers** — new backends ship as standalone repos implementing the same ABC [source: ~/.hermes/hermes-agent/plugins/AGENTS.md, "No new in-tree memory providers (May 2026)"].

### 2.2 Manifest (`plugin.yaml`) schema

Parsed by `plugins_manifest.py`; kinds: `standalone` (default, opt-in via `plugins.enabled`), `backend`, `exclusive`, `platform`, `model-provider` [source: ~/.hermes/hermes-agent/hermes_cli/plugins_manifest.py:28].

`PluginManifest` fields: `name, version, description, author, requires_env, provides_tools, provides_hooks, source (bundled|user|project|entrypoint), path, kind, key, requires_hermes, portable, skill_namespace, capabilities, manifest_version (1|2), api_version`, advisory `dependencies` [source: ~/.hermes/hermes-agent/hermes_cli/plugins_manifest.py:322-377].

Memory-provider manifests additionally declare **`pip_dependencies`**, which `hermes memory setup` installs and re-applies across `hermes update` [source: ~/.hermes/hermes-agent/plugins/memory/mem0/plugin.yaml (`pip_dependencies: [mem0ai>=2.0.10,<3]`); ~/.hermes/hermes-agent/plugins/memory/supermemory/plugin.yaml (`pip_dependencies: [supermemory]`); guide "Setup UX" table].

Real manifests for reference:
- `~/.hermes/hermes-agent/plugins/memory/supermemory/plugin.yaml`: name/version/description + `pip_dependencies: [supermemory]`.
- `~/.hermes/hermes-agent/plugins/memory/mem0/plugin.yaml`: same shape, pins `mem0ai>=2.0.10,<3`.
- `~/.hermes/hermes-agent/plugins/memory/hindsight/plugin.yaml`: adds `requires_env: []` and top-level `hooks: [on_session_end]`.
- `~/.hermes/plugins/orca-status/plugin.yaml` (the only user plugin installed here, `kind: standalone`, declares `provides_hooks:` for 10 lifecycle events) [source: file contents].

### 2.3 Entrypoint contract

Every plugin exposes `register(ctx)`; the loader accepts an instance, a subclass, a module with `register(ctx)`, a factory, or a namespace holding a subclass (entry points), and `register(ctx)` first / top-level subclass (directories) [source: ~/.hermes/hermes-agent/plugins/memory/__init__.py:263-320, 333-363].

`ctx` (a `PluginContext`) offers: `register_tool`, `register_hook`, `register_middleware`, `register_cli_command`/`register_command`, `register_skill`, `register_context_engine`, `register_context_reference`, `register_memory_provider`, `register_platform*`, `register_system_prompt_section`, `register_approval_transport`, `get_config`/`set_config`, `emit`/`subscribe`, `call_mcp`, `inject_message`, `spawn_task`, … [source: ~/.hermes/hermes-agent/hermes_cli/plugins.py:225-1027 — notably `register_memory_provider` at 739-750 and `register_hook` at 916].

For memory providers the only required call is `ctx.register_memory_provider(instance)`; the collector captures it and delegates every other `register_*` to a real `PluginContext`, so a provider can also register hooks/skills [source: ~/.hermes/hermes-agent/plugins/memory/__init__.py:333-363].

Real `register()` examples on disk:
- supermemory: `def register(ctx): ctx.register_memory_provider(SupermemoryMemoryProvider())` [source: ~/.hermes/hermes-agent/plugins/memory/supermemory/__init__.py:578-579]
- mem0: identical pattern with `Mem0MemoryProvider()` [source: ~/.hermes/hermes-agent/plugins/memory/mem0/__init__.py:391-393]
- hindsight: identical with `HindsightMemoryProvider()` [source: ~/.hermes/hermes-agent/plugins/memory/hindsight/__init__.py:1252-1254]
- orca-status (hook-only, non-memory): `register(ctx)` loops `ctx.register_hook(event_name, _make_hook(event_name))` over its EVENTS list [source: ~/.hermes/plugins/orca-status/__init__.py:149-151]
- security-guidance: registers `pre_tool_call` + `transform_tool_result` hooks [source: ~/.hermes/hermes-agent/plugins/security-guidance/__init__.py:131-133]; disk-cleanup registers `post_tool_call`, `on_session_end`, and a slash command via `ctx.register_command` [source: ~/.hermes/hermes-agent/plugins/disk-cleanup/__init__.py:182-186]

### 2.4 The `MemoryProvider` ABC — what a memory plugin hooks

Base class `agent/memory_provider.py::MemoryProvider` [source: ~/.hermes/hermes-agent/agent/memory_provider.py:75-189]:

**Core lifecycle (must implement)**
- `name` (property) — short id, becomes the `memory.provider` value.
- `is_available()` — config/creds check only, **no network calls**; gates activation.
- `initialize(session_id, **kwargs)` — once at agent startup. kwargs always include `hermes_home`, `platform`; may include `agent_context` (`"primary"|"subagent"|"cron"|"flush"` — skip writes for non-primary), `agent_identity`, `agent_workspace`, `parent_session_id`, `user_id`, `user_id_alt`, `cwd`, `gateway_session_key` [source: agent/memory_provider.py:94-102; full kwargs table in the guide, "Initialization context"].

**Recall/retain hooks (the memory loop)**
- `prefetch(query, *, session_id="") -> str` — formatted recall context for the upcoming turn; must be fast (recall in background, return cached) [source: agent/memory_provider.py:111-115]. Called by `MemoryManager` at [source: agent/memory_manager.py:408, 414]; `queue_prefetch` (post-turn pre-warm) at [source: memory_manager.py:463-470].
- `sync_turn(user_content, assistant_content, *, session_id="", messages=None)` — after each completed turn; **must not block** the turn loop, MemoryManager runs it off-thread [source: agent/memory_provider.py:124-132; memory_manager.py:485-503].
- `on_session_end(messages)` — final extraction/flush [source: memory_provider.py:156-158].
- `on_pre_compress(messages) -> str` — insight extraction before context compression; optionally fail-closed checkpoint API v2 via `pre_compress_checkpoint_api_version` [source: memory_provider.py:74-76, 167-170; guide "Pre-Compress Checkpoints" section].
- `on_turn_start`, `on_session_switch(new_session_id, ...)`, `on_delegation`, `identity_signature()`, `on_memory_write(action, target, content)` (mirror built-in MEMORY.md writes), `shutdown()` [source: memory_provider.py:140-190].

**Agent-facing tools**
- `get_tool_schemas() -> list[dict]` — JSON schemas injected into the agent tool surface (gated by the `memory` toolset); MemoryManager collects them at [source: memory_manager.py:341, 570-581] and injection happens in `inject_memory_provider_tools` [source: memory_manager.py:115-160].
- `handle_tool_call(tool_name, args, **kwargs) -> str` — dispatcher; return `tool_error(...)` string for bad args or a JSON-encoded dict [source: memory_provider.py:136-139; memory_manager.py:590-598; supermemory handler table at supermemory/__init__.py:573-576].

**Config UX**
- `get_config_schema()` — field descriptors for `hermes memory setup`: `{"key", "description", "secret": bool (→ .env), "required", "env_var", "url", "default", "choices"}` [source: supermemory/__init__.py:342-345; guide "Config Schema" section with full example].
- `save_config(values, hermes_home)`, `post_setup(hermes_home, config)` (interactive follow-ups; supermemory uses it to prompt for the key, write `.env`, and set `memory.provider` in config.yaml), `get_status_config(provider_config)` (feeds `hermes memory status`) [source: supermemory/__init__.py:346-378; guide "Setup UX" table].

### 2.5 Config flow & activation

- Activation key: `memory.provider` in `config.yaml` — "External memory provider plugin (empty = built-in only); only ONE at a time" [source: ~/.hermes/hermes-agent/hermes_cli/config_defaults.py:1284-1286; read by `_get_active_memory_provider()` at plugins/memory/__init__.py:414-421]. Built-in memory gating lives in the adjacent `memory:` section (`memory_enabled`, `user_profile_enabled`) [source: ~/.hermes/config.yaml:45-47].
- Provider-specific settings do **not** go into `config.yaml`; each provider owns a JSON file under `$HERMES_HOME` plus `.env` secrets read via `get_secret` (profile-scoped): mem0 → `$HERMES_HOME/mem0.json` + `MEM0_API_KEY`/`MEM0_HOST`; supermemory → `supermemory.json` + `SUPERMEMORY_API_KEY`; hindsight → `$HERMES_HOME/hindsight/config.json` → `~/.hindsight/config.json` → `HINDSIGHT_*` env [source: mem0/__init__.py:1-8 docstring; supermemory/__init__.py:106-114; hindsight/__init__.py:258-282].
- CLI: `hermes memory setup|status|off|reset` ("Only one external provider can be active at a time"); `hermes plugins install|enable|disable|list|doctor|…` [source: `hermes memory --help`, `hermes plugins --help` outputs, 2026-09-27].
- Current local state: `memory.memory_enabled: false`, provider none, `plugins.enabled: [orca-status]` [source: ~/.hermes/config.yaml:45-54; `hermes memory status` output]. Installed-provider list comes from `discover_memory_providers()` (directory scan + entry points) [source: plugins/memory/__init__.py:174-190; hermes_cli/memory_setup.py:168-190].
- Runtime wiring summary: `MemoryManager` fans `initialize_all` → providers, queues prefetch after each turn, runs `sync_turn` off-thread, injects tool schemas, routes `handle_tool_call`; the gateway path re-checks `identity_signature()` per inbound message [source: agent/memory_manager.py:408-503, 341-347, 570-598; memory_provider.py:150-155].

### 2.6 Hook events available to general plugins

Observed hook names: `on_session_start`, `pre_llm_call`, `post_llm_call`, `pre_tool_call`, `post_tool_call`, `transform_tool_result`, `pre_approval_request`, `post_approval_response`, `on_session_end`, `on_session_finalize`, `on_session_reset` [source: ~/.hermes/plugins/orca-status/plugin.yaml provides_hooks + ~/.hermes/plugins/orca-status/__init__.py:8-18 SELECTED_KEYS; security-guidance `transform_tool_result` at security-guidance/__init__.py:132]. `config.yaml` also supports command-based hooks (`hooks.pre_tool_call: [- command: …]`) [source: ~/.hermes/config.yaml:55-60]. These are the alternative integration surface if a memory plugin wanted to observe/store conversation without the MemoryProvider ABC — but the ABC is purpose-built and gives tool injection + prompt prefetch for free.

### 2.7 Python support

Plugins are plain Python modules imported by the loader (`load_plugin_module` with synthetic parent packages for user installs, `parents=("plugins", "plugins.memory")`) [source: plugins/memory/__init__.py:296-302]. Bundled providers are pure Python against the SDK they declare in `pip_dependencies` [source: mem0/supermemory plugin.yaml]. Guide: "Your plugin implements the MemoryProvider abstract base class from `agent/memory_provider.py`" [source: guide, "The MemoryProvider ABC"].

---

## 3. Cross-check: omp ↔ Dakera (the working precedent)

- Workspace config `.omp/config.yml` (local-only, gitignored) sets `memory.backend: dakera` + `dakera.agentId: hermes-dakera`, `retainMode: last-turn`, `retainEveryNTurns: 1` [source: file]. The live overlay `~/.omp/agent/dakera.yml` adds the server URL (private LAN host, redacted), a scoped API token (redacted) and `recallRerank: false` with the rerank-cost rationale; the same keys exist in the base omp agent config [source: local config files].
- Implementation (omp fork, TypeScript): `packages/coding-agent/src/dakera/` — `client.ts` is a hand-rolled fetch client ("we depend on the endpoints we actually use and nothing else, so no SDK goes into the lockfile") hitting `POST /v1/memory/store` and `POST /v1/memory/recall` with `Authorization: Bearer`, per-call timeouts (30s recall / 60s retain) and retry with `Retry-After` support [source: client.ts:1-19, 139-165, 166-177, 238-266]; `bank.ts` derives `agent_id` (`global` / `per-project` / `per-project-tagged` with `project:`/`global:shared` tags; storing against an unseen agent_id auto-creates it) [source: bank.ts:1-58]; `state.ts` implements auto-recall on first turn + auto-retain every N turns, `last-turn` mode storing one memory per user-turn window [source: state.ts:105-118, 341-362]; `config.ts` documents settings/env precedence and accepts both `DAKERA_API_TOKEN` and `DAKERA_API_KEY` for the bearer token [source: config.ts:1-13].
- **No MCP route**: `~/.omp/agent/mcp.json` contains no dakera server; the session's memory tools are the omp memory backend mounted as `xd://` devices (`retain`, `recall`, `reflect`, `memory_edit`, `learn` — built-in names, not `dakera_*`) [source: ~/.omp/agent/mcp.json server list; <local omp fork>/packages/coding-agent/src/memory-backend/tool-names.ts:3]. The prompt's "dakera_recall/dakera_store MCP tools" is therefore approximate: omp exposes Dakera through its backend abstraction, not a Dakera-named MCP server. The pattern that maps to Hermes is **direct HTTP client + agent_id isolation**, which is exactly what a Hermes `MemoryProvider` + `DakeraClient` gives.
- Live-endpoint verification: `GET /health` → healthy v0.11.108; `POST /v1/memory/recall` with the omp bearer token returned ranked memories (`smart_score`/`weighted_score` envelope, nested `memory` object) for `agent_id: hermes-dakera` [source: curl probes, 2026-09-27].

---

## 4. Gap analysis: what a `dakera` Hermes memory plugin must implement

Nothing Dakera-specific exists in Hermes today (grep across `~/.hermes` for `dakera` in .py/.yaml/.json: zero hits) [source: grep, 2026-09-27]. The gap is one new plugin implementing the standard contract:

1. **Package & layout** — user install at `~/.hermes/plugins/dakera/` with `__init__.py` (provider + `register(ctx)`), `plugin.yaml` (`name: dakera`, description, `pip_dependencies: [dakera]`), optional `README.md` [source: guide "Directory Provider" layout; §2.2 above]. Alternatively a pip package with `[project.entry-points."hermes_agent.memory_providers"] dakera = "…:register"` [source: guide "Packaged Provider"]. Project-local `./.hermes/plugins/` would need `HERMES_ENABLE_PROJECT_PLUGINS=1` [source: plugins_discovery.py:162-167]. Note bundled wins over user on name collision — irrelevant here since no bundled `dakera` exists.
2. **`DakeraMemoryProvider(MemoryProvider)`** must implement:
   - `name → "dakera"`; `is_available()` → URL+key configured, no network [source: ABC requirements].
   - `initialize(session_id, **kwargs)` → load config (`$HERMES_HOME/dakera/config.json` and/or `DAKERA_API_URL`/`DAKERA_API_KEY`/`DAKERA_AGENT_ID` env, matching omp's convention), build `DakeraClient`, resolve `agent_id` (fixed `hermes-dakera`-style, or per-profile via `kwargs["agent_identity"]`), honor `agent_context` (skip writes for subagent/cron/flush) [source: §2.4 kwargs; omp config.ts env names].
   - `prefetch()`/`queue_prefetch()` → `client.recall(agent_id, query, top_k)` formatted into a context block; must be non-blocking/cached [source: §2.4; supermemory thread pattern at supermemory/__init__.py:408-440].
   - `sync_turn()` → per-turn retain; omp's `last-turn` mode (one episodic memory per turn window, importance default ~0.5–0.6, role tags) is the proven shape [source: omp state.ts:341-362; dakera session.py default importance 0.6].
   - `on_session_end(messages)` → flush pending writes + `end_session(summary)` if sessions are used; `shutdown()` → close the requests session.
   - `get_tool_schemas()` + `handle_tool_call()` → e.g. `dakera_store`, `dakera_search`, `dakera_forget` (+ snake/kebab aliases like supermemory) backed by `store_memory`/`search_memories`/`forget` [source: supermemory schema/alias pattern at supermemory/__init__.py:291-301, 506-512].
   - `get_config_schema()`/`save_config()`/`post_setup()` → `api_url`, `api_key` (secret → `DAKERA_API_KEY` env), `agent_id`; `post_setup` writes `memory.provider: dakera` [source: supermemory post_setup at supermemory/__init__.py:357-378].
3. **Activation** — `hermes memory setup` (or manual `memory.provider: dakera` in `~/.hermes/config.yaml`); current install has memory disabled, so activation also means enabling the memory toolset for tools to surface [source: config.yaml:45-47; memory_manager.py:122-137 gating].
4. **Operational inherited from omp's experience**: pin against server v0.11.108 (≥0.11.98 for sub-scores/`valid_from`); avoid `consolidate` as read-only; consider `recallRerank: false` default given the measured CPU cost on this host; expect Unix-second timestamps; SDK `RecalledMemory.created_at` is typed `str | None` but the server sends numbers — normalize in one place [source: §1.6 cites].
5. **Sync-vs-async note**: `DakeraClient` is blocking `requests`; `prefetch()` must not block the turn loop, so either run recall on a background thread (supermemory/mem0 pattern) or use `AsyncDakeraClient` (httpx) with a loop like hindsight does [source: async_client.py:164; hindsight module-global event loop comment at hindsight/__init__.py:1240-1251].

---

## 5. Open questions

1. **agent_id policy**: single shared `hermes-dakera` (this workspace's omp agent), per-Hermes-profile (`agent_identity`), or omp-style `global/per-project/per-project-tagged` scoping with `project:` tags? omp's `bank.ts` is the reference implementation but Hermes has no direct equivalent of "project tag recall filters" in its ABC — tags would ride in `store_memory(tags=…)` and `recall` tag filters via raw params [source: bank.ts:20-43; client.py:1116+].
2. **Config home**: follow Hermes provider convention (`$HERMES_HOME/dakera/config.json` + `.env` secret) or omp's env-first `DAKERA_API_URL`/`DAKERA_API_TOKEN`/`DAKERA_API_KEY`? Both token env names are accepted by omp; the Dakera server/SDK ecosystem itself uses `DAKERA_API_KEY` [source: config.ts:1-13; README env usage `DAKERA_API_URL`/`DAKERA_API_KEY` in examples/memory.py:16-19].
3. **Unverified endpoints on the live server**: `/v1/memory/search`, batch endpoints, `wake_up`, sessions API are SDK-documented but were not probed live (only `/health` and `/v1/memory/recall` were exercised); `dakera-py` 0.12.13 targets server 0.11.98+ while the instance is 0.11.108 — should be fine, but worth a smoke script before design freeze.
4. **Does the hermes venv tolerate `pip_dependencies: [dakera]`?** `dakera` needs `requests>=2.33.0` + `urllib3>=2.6.3`; Hermes installs provider deps "under Hermes' own pins" [source: guide Setup UX table] — compatibility unverified until an install is attempted.
5. **Rerank default**: omp disabled rerank due to measured 10–22s queries on this host; should the Hermes plugin default `rerank=false` with a config toggle, or rely on server-side improvements? [source: ~/.omp/agent/dakera.yml:14-19; issue-dakera-rerank.md]
6. **MCP alternative**: `dakera-mcp` (MCP server for Claude/Cursor) exists as a separate repo and could in principle be wired via Hermes MCP config instead of a native plugin — unexplored here; the native MemoryProvider route gives prefetch/retain lifecycle and tool injection that an MCP server would lack [source: README "Other SDKs" table; §2.4].
7. **Built-in memory interplay**: Hermes built-in MEMORY.md/USER.md memory is currently disabled; the guide says built-in is "always active" while `hermes memory status` shows it disabled locally — confirm which is authoritative for this install before designing `on_memory_write` mirroring [source: `hermes memory --help` text vs `hermes memory status` output].
