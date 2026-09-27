# Исследование: плагин памяти для Hermes на основе Dakera

Заметки исследования по плагину для Hermes, использующему Dakera (https://github.com/Dakera-AI/dakera-py) в качестве бэкенда памяти. В этом рабочем пространстве нет конвенции для заметок (репозиторий содержит только `.omp/`), поэтому файл лежит в `research/` как новая конвенция.

Просмотренные источники: свежий shallow-клон `Dakera-AI/dakera-py` в `/tmp/dakera-py` (HEAD на момент исследования, версия SDK 0.12.13), локальная установка Hermes в `~/.hermes` (включая входящее в комплект дерево исходников `hermes-agent` в `~/.hermes/hermes-agent`) и форк omp в `<local omp fork>`, в котором уже есть работающая интеграция с Dakera. Каждое утверждение ссылается на файл, которому оно принадлежит; непроверяемые утверждения помечены [INFERENCE].

---

## TL;DR

- Dakera-py — это тонкий типизированный HTTP SDK (`dakera` на PyPI, 0.12.13, чистый `requests`) поверх self-hosted (на своём сервере) Dakera-сервера; вся поверхность памяти — это 5 REST-вызовов: `store_memory`, `recall`, `search_memories`, `update_memory`, `forget` [source: /tmp/dakera-py/src/dakera/client.py].
- В Hermes уже есть первоклассный, документированный вид плагинов ровно для этого: **memory provider plugin**, реализующий ABC `MemoryProvider` плюс точку входа `register(ctx)`, активируемый значением `memory.provider: <name>` в `config.yaml` и управляемый командой `hermes memory setup` [source: ~/.hermes/hermes-agent/website/docs/developer-guide/memory-provider-plugin.md].
- Существует работающий прецедент: интеграция `memory.backend: dakera` в форке omp (`<local omp fork>/packages/coding-agent/src/dakera/`) обращается к тому же self-hosted-серверу (живому по адресу `<private LAN URL>`, сервер v0.11.108, работоспособность проверена) через обычные `POST /v1/memory/{store,recall}` + bearer-аутентификацию и изоляцию по `agent_id` [source: <local omp fork>/packages/coding-agent/src/dakera/client.ts].
- В локальной установке Hermes сегодня **нет** никакой интеграции с Dakera (grep по `~/.hermes` не нашёл ни одного упоминания `dakera` в файлах .py/.yaml/.json); новый пользовательский плагин в `~/.hermes/plugins/dakera/` (или pip entry point) должен реализовать весь провайдер целиком: prefetch для recall, retain после каждого хода, схемы инструментов, схему конфигурации, flush по завершении сессии.
- Коллизию имён, которой нужно избежать: `.omp/config.yml` этого рабочего пространства использует `dakera:` как ключ настроек omp; в Hermes контракт, видимый пользователю, — это имя провайдера (`dakera`), значение `memory.provider` и имена инструментов (`dakera_*`) [source: ~/.hermes/hermes-agent/website/docs/developer-guide/memory-provider-plugin.md, "Setup UX" section].

---

## 1. Поверхность API Dakera-py

### 1.1 Пакет, установка, транспорт

- Пакет `dakera`, версия **0.12.13**, MIT, `requires-python >= 3.10`, рантайм-зависимости — только `requests>=2.33.0` и `urllib3>=2.6.3` [source: /tmp/dakera-py/pyproject.toml:5-33].
- Установка: `pip install dakera`; async-extra: `pip install dakera[async]` [source: /tmp/dakera-py/README.md, "Install" section].
- Публичные экспорты: `DakeraClient`, `AsyncDakeraClient`, `ChatMemorySession`, `AsyncChatMemorySession`, типы памяти (`StoreMemoryRequest`, `Memory`, `RecalledMemory`, `RecallResponse`, batch-типы), типы vector/namespace, `RetryConfig`, перечисления согласованности [source: /tmp/dakera-py/src/dakera/__init__.py:160-230].
- Транспорт — обычный HTTP/JSON (не MCP): все методы проходят через `_request()` → `requests.Session` с `Content-Type: application/json` и User-Agent `dakera-py/<version>` [source: /tmp/dakera-py/src/dakera/client.py:26-30, 311+]. MCP — **отдельный** продукт, `dakera-mcp` ("MCP server for Claude/Cursor"), указанный в таблице "Other SDKs" [source: /tmp/dakera-py/README.md, "Other SDKs" table].
- Сервер: одиночный бинарник, docker-команда одной строкой на порту 3000 с `DAKERA_ROOT_API_KEY`; `GET /health` возвращает `{"status":"ok"}`; постоянные развёртывания — через docker-compose-репозиторий `dakera-deploy` [source: /tmp/dakera-py/README.md, "Run Dakera" section]. Режимы поиска: vector (ANN/HNSW), полнотекстовый BM25, hybrid (RRF fusion), knowledge graph; embedding-и строятся на стороне сервера [source: /tmp/dakera-py/README.md, "Why Dakera?" and "Features" sections].

### 1.2 Конструирование клиента и аутентификация

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

- Аутентификация: если задан `api_key`, каждый запрос несёт `Authorization: Bearer <api_key>` [source: /tmp/dakera-py/src/dakera/client.py:202-203]. Именно этой bearer-схеме соответствует живой self-hosted-сервер (проверено: `curl -X POST $URL/v1/memory/recall -H "Authorization: Bearer $TOKEN"` вернул воспоминания с живого инстанса) [source: live probe against <private LAN URL>, 2026-09-27].
- Ошибки — типизированные исключения: `ValidationError` (400), `AuthenticationError` (401), `AuthorizationError`, `NotFoundError`, `RateLimitError`, `ServerError`, `TimeoutError`, `ConnectionError`, с `ErrorCode`, разбираемым из поля `code` тела ответа [source: /tmp/dakera-py/src/dakera/client.py:223-266; /tmp/dakera-py/src/dakera/exceptions.py]. Заголовки rate-limit фиксируются по каждому ответу в `client.last_rate_limit_headers` [source: /tmp/dakera-py/src/dakera/client.py:212-216, 225].
- Асинхронный близнец `AsyncDakeraClient` (httpx, все методы — корутины, async context manager) имеет идентичную сигнатуру конструктора [source: /tmp/dakera-py/src/dakera/async_client.py:164-230].

### 1.3 Операции с памятью (поверхность, нужная плагину)

Все вызовы памяти в области агента принимают `agent_id: str` — это единица изоляции в Dakera (никакого "банка" нет; документация omp подтверждает: "Isolation is the `agent_id`") [source: <local omp fork>/packages/coding-agent/src/dakera/backend.ts:1-6].

- **Store** — `store_memory(agent_id, content, memory_type="episodic", importance=None, metadata=None, session_id=None, tags=None, ttl_seconds=None, expires_at=None, valid_from=None) -> dict` → `POST /v1/memory/store`. `memory_type` — одно из `"episodic" | "semantic" | "procedural" | "working"`; `importance` — 0.0–1.0; `valid_from` (bi-temporal) требует сервер v0.11.98+ (DAK-7424) [source: /tmp/dakera-py/src/dakera/client.py:1061-1114].
- **Recall** — `recall(agent_id, query, top_k=5, memory_type=None, min_importance=None, include_associated=False, associated_memories_cap/depth/min_weight=None, since=None, until=None, routing=None, rerank=None, fusion=None, vector_weight=None, iterations=None, neighborhood=None) -> RecallResponse` → `POST /v1/memory/recall` [source: /tmp/dakera-py/src/dakera/client.py:1116-1216]. `routing` — это `RoutingMode`: `auto` (по умолчанию) / `vector` / `bm25` / `hybrid`; `fusion` — это `FusionStrategy`: `rrf` (по умолчанию) / `minmax` [source: /tmp/dakera-py/src/dakera/models.py:37-72].
- **Search (лексический/listing-вариант)** — `search_memories(agent_id, query, top_k=10, memory_type=None, min_importance=None, routing=None, rerank=None) -> list[dict]` → `POST /v1/memory/search` [source: /tmp/dakera-py/src/dakera/client.py:1313-1343].
- **Чтение/обновление/удаление** — `get_memory(agent_id, memory_id)` → `GET /v1/memory/get/{memory_id}`; `update_memory(agent_id, memory_id, content=None, metadata=None, memory_type=None)` → `PUT /v1/memory/update/{memory_id}`; `forget(agent_id, memory_id)` → `POST /v1/memory/forget` с `{"agent_id", "memory_ids": [id]}` [source: /tmp/dakera-py/src/dakera/client.py:1219-1244].
- **Batch** — `batch_recall(BatchRecallRequest)` (предикаты фильтрации, `POST /v1/memories/recall/batch`, embedding не нужен), `batch_forget(BatchForgetRequest)`, `store_memories_batch(BatchStoreMemoryRequest)` [source: /tmp/dakera-py/src/dakera/client.py:1246-1311].
- **Lifecycle/quality-дополнения** — `compress_agent(agent_id)`, `update_importance(...)`, `consolidate(...)`/`consolidate_agent(agent_id)`, эндпоинты обратной связи (`POST /v1/agents/{agent_id}/memories/feedback`, `POST /v1/memories/{memory_id}/feedback`, …), `wake_up(agent_id, top_n=20, min_importance=0.0)` → `GET /v1/agents/{agent_id}/wake-up` (importance×exp decay, отдаётся через metadata-index, "no embedding inference") [source: /tmp/dakera-py/src/dakera/client.py:1345-1420, 1466-1567, 1839-1857].
- **Сессии** — `start_session(agent_id, metadata=None)` → `POST /v1/sessions/start` (возвращает `result["session"]` с `id`); `end_session(session_id, summary=None)` → `POST /v1/sessions/{id}/end`; `get_session`, список сессий, `get_session_memories` [source: /tmp/dakera-py/src/dakera/client.py:1750-1800].
- **Vector/namespace API** (отдельно от памяти агента; плагину, скорее всего, не нужна) — `upsert`, `query`, `delete`, `fetch`, `batch_query`, `upsert_text`, `query_text`, `index_documents`, `fulltext_search`, `hybrid_search`, CRUD namespace-ов, `health`/`health_ready`/`health_live`, административные операции [source: /tmp/dakera-py/src/dakera/client.py:364-1060, 1003-1018, 2411-2490].

### 1.4 Модель данных

- `Memory`: `id, content, memory_type, importance (default 0.5), metadata, created_at, updated_at, access_count` [source: /tmp/dakera-py/src/dakera/models.py:666-691].
- `RecalledMemory`: `id, content, memory_type, importance, score, smart_score, weighted_score, metadata, created_at, depth, vector_score, text_score`. Ранжирование: `.score` вычисляется как `smart_score` → `weighted_score` → сырой `score` (изменено в 0.12.10, чтобы `.score` совпадал с порядком ранжирования сервера); `vector_score`/`text_score` — под-оценки hybrid из сервера v0.11.98+ [source: /tmp/dakera-py/src/dakera/models.py:693-740; /tmp/dakera-py/CHANGELOG.md, 0.12.10 and 0.12.11 entries].
- `RecallResponse`: `memories: list[RecalledMemory]`, необязательные `associated_memories` (обход KG, каждая с `depth`) [source: /tmp/dakera-py/src/dakera/models.py:742-780].
- `StoreMemoryRequest` повторяет аргументы `store_memory` плюс обходной путь с заранее вычисленным `embedding` [source: /tmp/dakera-py/src/dakera/models.py:636-664].
- Предупреждение о временных метках из самописного клиента omp: сервер возвращает **числа** Unix-секунд для `created_at`/`last_accessed_at` на каждом проверенном эндпоинте, "а не ISO-строки, которые подразумевают модели Python SDK" — SDK типизирует их как `str | None` [source: <local omp fork>/packages/coding-agent/src/dakera/client.ts:36-41 vs /tmp/dakera-py/src/dakera/models.py:713-714].

### 1.5 Высокоуровневый помощник сессии

`ChatMemorySession` оборачивает сырой API в чат-паттерн, который воспроизводит плагин памяти:

- `ChatMemorySession.create(client, agent_id, metadata=None)` → `start_session` + привязка [source: /tmp/dakera-py/src/dakera/session.py:66-91].
- `session.store(role, content, importance=0.6, tags=None)` — сохраняет ход, помечая каждое воспоминание тегом с его ролью; importance по умолчанию 0.6 [source: /tmp/dakera-py/src/dakera/session.py:97-133].
- `session.recall(query, top_k=5) -> list[RecalledMemory]` — ищет по **всей** памяти агента, а не только по текущей сессии [source: /tmp/dakera-py/src/dakera/session.py:135-156].
- `session.close(summary=None)` → `end_session`; можно использовать как context manager [source: /tmp/dakera-py/src/dakera/session.py:158-196].
- Референсное использование в `examples/ollama_memory_chat.py`: recall перед каждым вызовом LLM, инъекция как system message, затем `session.store("user", ...)` / `session.store("assistant", ...)`, посев долговременных предпочтений с importance 0.95, чтобы они ранжировались выше «болтовни», `close(summary=...)` на выходе [source: /tmp/dakera-py/examples/ollama_memory_chat.py:41-110].

### 1.6 Версионирование и эксплуатационные особенности

- Маркеры совместимости с сервером в SDK: `valid_from` и под-оценки hybrid требуют сервер v0.11.98+; живой self-hosted-инстанс работает на **v0.11.108** (`{"service":"dakera","status":"healthy","version":"0.11.108",...}`) [source: /tmp/dakera-py/CHANGELOG.md 0.12.11 entry; live `GET /health` probe, 2026-09-27].
- Клиент omp документирует добытые непросто эксплуатационные факты об этом же развёртывании: `consolidate` — это "простая конкатенация, а не синтез", и его `dry_run` игнорируется (никогда не представлять его как read-only); оценки лексического эндпоинта `search` не нормализованы (наблюдалось 1.42), поэтому их нельзя ранжировать рядом с оценками recall; `rerank: true` на этом 4-ядерном хосте стоит ~2 с на кандидата (10–22 с на запрос), поэтому omp отключает его (`recallRerank: false` в `~/.omp/agent/dakera.yml`) [source: <local omp fork>/packages/coding-agent/src/dakera/client.ts:5-19; ~/.omp/agent/dakera.yml:14-19; <local omp fork>/.dakera-migration/issue-dakera-rerank.md].
- omp, кроме того, никогда не отправляет `valid_from`, потому что "старые серверы его отвергают", и использует `POST /v1/memory/forget` с `memory_ids` вместо batch-forget-конверта с фильтрами, который ему не удалось удовлетворить [source: <local omp fork>/packages/coding-agent/src/dakera/client.ts:7-19, 291-309].

---

## 2. Система плагинов Hermes

Hermes установлен локально: бинарник `~/.local/bin/hermes`, рантайм-состояние в `~/.hermes/` и **полное дерево исходников агента** в `~/.hermes/hermes-agent` (Python; `hermes plugins list` и `hermes memory status` выполняются успешно) [source: command outputs, 2026-09-27].

### 2.1 Два слоя плагинов

1. **Обычные плагины** — обнаруживаются из комплектных `plugins/`, пользовательских `~/.hermes/plugins/<name>/`, проектных `./.hermes/plugins/` (опционально через `HERMES_ENABLE_PROJECT_PLUGINS=1`) и группы pip entry-point `hermes_agent.plugins`. Директория-плагин требует `plugin.yaml` [source: ~/.hermes/hermes-agent/hermes_cli/plugins.py:1-6, 68; hermes_cli/plugins_discovery.py:27, 141-173].
2. **Memory provider плагины** — отдельный вид с однозначным выбором, активируемый `memory.provider` в `config.yaml`, обнаруживаемый в **обратном** порядке приоритета (комплектный выигрывает): комплектный `plugins/memory/<name>/` → пользовательский `$HERMES_HOME/plugins/<name>/` → проектный `./.hermes/plugins/<name>/` → entry points `hermes_agent.memory_providers`. Обнаружение только перечисляет; ничего не импортируется, пока `memory.provider` не назовёт провайдера [source: ~/.hermes/hermes-agent/plugins/memory/__init__.py:1-30; hermes_cli/plugins_discovery.py:90-100].

Комплектные memory provider-ы на диске: `byterover`, `hindsight`, `holographic`, `honcho`, `mem0`, `openviking`, `retaindb`, `supermemory` (плюс помощники `config_schema.py`, `query_rewrite.py`) [source: ls ~/.hermes/hermes-agent/plugins/memory/]. Дерево **закрыто для новых встроенных провайдеров** — новые бэкенды поставляются отдельными репозиториями, реализующими тот же ABC [source: ~/.hermes/hermes-agent/plugins/AGENTS.md, "No new in-tree memory providers (May 2026)"].

### 2.2 Схема манифеста (`plugin.yaml`)

Разбирается `plugins_manifest.py`; виды: `standalone` (по умолчанию, опционально через `plugins.enabled`), `backend`, `exclusive`, `platform`, `model-provider` [source: ~/.hermes/hermes-agent/hermes_cli/plugins_manifest.py:28].

Поля `PluginManifest`: `name, version, description, author, requires_env, provides_tools, provides_hooks, source (bundled|user|project|entrypoint), path, kind, key, requires_hermes, portable, skill_namespace, capabilities, manifest_version (1|2), api_version`, рекомендательные `dependencies` [source: ~/.hermes/hermes-agent/hermes_cli/plugins_manifest.py:322-377].

Манифесты memory provider-ов дополнительно объявляют **`pip_dependencies`**, которые `hermes memory setup` устанавливает и повторно применяет при `hermes update` [source: ~/.hermes/hermes-agent/plugins/memory/mem0/plugin.yaml (`pip_dependencies: [mem0ai>=2.0.10,<3]`); ~/.hermes/hermes-agent/plugins/memory/supermemory/plugin.yaml (`pip_dependencies: [supermemory]`); guide "Setup UX" table].

Реальные манифесты для справки:
- `~/.hermes/hermes-agent/plugins/memory/supermemory/plugin.yaml`: name/version/description + `pip_dependencies: [supermemory]`.
- `~/.hermes/hermes-agent/plugins/memory/mem0/plugin.yaml`: та же форма, пин `mem0ai>=2.0.10,<3`.
- `~/.hermes/hermes-agent/plugins/memory/hindsight/plugin.yaml`: добавляет `requires_env: []` и верхнеуровневый `hooks: [on_session_end]`.
- `~/.hermes/plugins/orca-status/plugin.yaml` (единственный установленный здесь пользовательский плагин, `kind: standalone`, объявляет `provides_hooks:` для 10 событий жизненного цикла) [source: file contents].

### 2.3 Контракт точки входа

Каждый плагин экспонирует `register(ctx)`; загрузчик принимает экземпляр, подкласс, модуль с `register(ctx)`, фабрику или namespace с подклассом (entry points), а `register(ctx)` первым / подкласс верхнего уровня (директории) [source: ~/.hermes/hermes-agent/plugins/memory/__init__.py:263-320, 333-363].

`ctx` (a `PluginContext`) предлагает: `register_tool`, `register_hook`, `register_middleware`, `register_cli_command`/`register_command`, `register_skill`, `register_context_engine`, `register_context_reference`, `register_memory_provider`, `register_platform*`, `register_system_prompt_section`, `register_approval_transport`, `get_config`/`set_config`, `emit`/`subscribe`, `call_mcp`, `inject_message`, `spawn_task`, … [source: ~/.hermes/hermes-agent/hermes_cli/plugins.py:225-1027 — notably `register_memory_provider` at 739-750 and `register_hook` at 916].

Для memory provider-ов единственный обязательный вызов — `ctx.register_memory_provider(instance)`; коллектор перехватывает его и делегирует все остальные `register_*` настоящему `PluginContext`, так что провайдер может также регистрировать хуки/навыки [source: ~/.hermes/hermes-agent/plugins/memory/__init__.py:333-363].

Реальные примеры `register()` на диске:
- supermemory: `def register(ctx): ctx.register_memory_provider(SupermemoryMemoryProvider())` [source: ~/.hermes/hermes-agent/plugins/memory/supermemory/__init__.py:578-579]
- mem0: идентичный паттерн с `Mem0MemoryProvider()` [source: ~/.hermes/hermes-agent/plugins/memory/mem0/__init__.py:391-393]
- hindsight: идентичный с `HindsightMemoryProvider()` [source: ~/.hermes/hermes-agent/plugins/memory/hindsight/__init__.py:1252-1254]
- orca-status (только хуки, не память): `register(ctx)` в цикле вызывает `ctx.register_hook(event_name, _make_hook(event_name))` по своему списку EVENTS [source: ~/.hermes/plugins/orca-status/__init__.py:149-151]
- security-guidance: регистрирует хуки `pre_tool_call` + `transform_tool_result` [source: ~/.hermes/hermes-agent/plugins/security-guidance/__init__.py:131-133]; disk-cleanup регистрирует `post_tool_call`, `on_session_end` и slash-команду через `ctx.register_command` [source: ~/.hermes/hermes-agent/plugins/disk-cleanup/__init__.py:182-186]

### 2.4 ABC `MemoryProvider` — что реализует плагин памяти

Базовый класс `agent/memory_provider.py::MemoryProvider` [source: ~/.hermes/hermes-agent/agent/memory_provider.py:75-189]:

**Основной жизненный цикл (обязательно к реализации)**
- `name` (property) — короткий id, становится значением `memory.provider`.
- `is_available()` — только проверка конфигурации/креденшелов, **без сетевых вызовов**; гейтит активацию.
- `initialize(session_id, **kwargs)` — один раз при старте агента. kwargs всегда включают `hermes_home`, `platform`; могут включать `agent_context` (`"primary"|"subagent"|"cron"|"flush"` — пропускать записи для не-primary), `agent_identity`, `agent_workspace`, `parent_session_id`, `user_id`, `user_id_alt`, `cwd`, `gateway_session_key` [source: agent/memory_provider.py:94-102; full kwargs table in the guide, "Initialization context"].

**Хуки recall/retain (цикл памяти)**
- `prefetch(query, *, session_id="") -> str` — отформатированный recall-контекст для предстоящего хода; должен быть быстрым (recall в фоне, возврат кэша) [source: agent/memory_provider.py:111-115]. Вызывается `MemoryManager` в [source: agent/memory_manager.py:408, 414]; `queue_prefetch` (прогрев после хода) в [source: memory_manager.py:463-470].
- `sync_turn(user_content, assistant_content, *, session_id="", messages=None)` — после каждого завершённого хода; **не должен блокировать** цикл хода, MemoryManager запускает его вне потока [source: agent/memory_provider.py:124-132; memory_manager.py:485-503].
- `on_session_end(messages)` — финальная экстракция/flush [source: memory_provider.py:156-158].
- `on_pre_compress(messages) -> str` — экстракция инсайтов перед сжатием контекста; опционально fail-closed checkpoint API v2 через `pre_compress_checkpoint_api_version` [source: memory_provider.py:74-76, 167-170; guide "Pre-Compress Checkpoints" section].
- `on_turn_start`, `on_session_switch(new_session_id, ...)`, `on_delegation`, `identity_signature()`, `on_memory_write(action, target, content)` (зеркалит встроенные записи MEMORY.md), `shutdown()` [source: memory_provider.py:140-190].

**Инструменты, доступные агенту**
- `get_tool_schemas() -> list[dict]` — JSON-схемы, инъектируемые в поверхность инструментов агента (гейтится toolset-ом `memory`); MemoryManager собирает их в [source: memory_manager.py:341, 570-581], а инъекция происходит в `inject_memory_provider_tools` [source: memory_manager.py:115-160].
- `handle_tool_call(tool_name, args, **kwargs) -> str` — диспетчер; возвращает строку `tool_error(...)` при плохих аргументах или JSON-сериализованный словарь [source: memory_provider.py:136-139; memory_manager.py:590-598; supermemory handler table at supermemory/__init__.py:573-576].

**UX конфигурации**
- `get_config_schema()` — дескрипторы полей для `hermes memory setup`: `{"key", "description", "secret": bool (→ .env), "required", "env_var", "url", "default", "choices"}` [source: supermemory/__init__.py:342-345; guide "Config Schema" section with full example].
- `save_config(values, hermes_home)`, `post_setup(hermes_home, config)` (интерактивные последующие шаги; supermemory использует это, чтобы запросить ключ, записать `.env` и установить `memory.provider` в config.yaml), `get_status_config(provider_config)` (питает `hermes memory status`) [source: supermemory/__init__.py:346-378; guide "Setup UX" table].

### 2.5 Конфигурация и активация

- Ключ активации: `memory.provider` в `config.yaml` — "External memory provider plugin (empty = built-in only); only ONE at a time" [source: ~/.hermes/hermes-agent/hermes_cli/config_defaults.py:1284-1286; read by `_get_active_memory_provider()` at plugins/memory/__init__.py:414-421]. Гейтинг встроенной памяти лежит в соседней секции `memory:` (`memory_enabled`, `user_profile_enabled`) [source: ~/.hermes/config.yaml:45-47].
- Настройки конкретного провайдера **не** идут в `config.yaml`; каждый провайдер владеет JSON-файлом под `$HERMES_HOME` плюс секретами `.env`, читаемыми через `get_secret` (в области профиля): mem0 → `$HERMES_HOME/mem0.json` + `MEM0_API_KEY`/`MEM0_HOST`; supermemory → `supermemory.json` + `SUPERMEMORY_API_KEY`; hindsight → `$HERMES_HOME/hindsight/config.json` → `~/.hindsight/config.json` → env `HINDSIGHT_*` [source: mem0/__init__.py:1-8 docstring; supermemory/__init__.py:106-114; hindsight/__init__.py:258-282].
- CLI: `hermes memory setup|status|off|reset` ("Only one external provider can be active at a time"); `hermes plugins install|enable|disable|list|doctor|…` [source: `hermes memory --help`, `hermes plugins --help` outputs, 2026-09-27].
- Текущее локальное состояние: `memory.memory_enabled: false`, провайдер не задан, `plugins.enabled: [orca-status]` [source: ~/.hermes/config.yaml:45-54; `hermes memory status` output]. Список установленных провайдеров приходит из `discover_memory_providers()` (скан директорий + entry points) [source: plugins/memory/__init__.py:174-190; hermes_cli/memory_setup.py:168-190].
- Сводка рантайм-обвязки: `MemoryManager` раздаёт `initialize_all` → провайдерам, ставит prefetch в очередь после каждого хода, запускает `sync_turn` вне потока, инъектирует схемы инструментов, маршрутизирует `handle_tool_call`; путь шлюза перепроверяет `identity_signature()` на каждое входящее сообщение [source: agent/memory_manager.py:408-503, 341-347, 570-598; memory_provider.py:150-155].

### 2.6 События хуков, доступные обычным плагинам

Наблюдаемые имена хуков: `on_session_start`, `pre_llm_call`, `post_llm_call`, `pre_tool_call`, `post_tool_call`, `transform_tool_result`, `pre_approval_request`, `post_approval_response`, `on_session_end`, `on_session_finalize`, `on_session_reset` [source: ~/.hermes/plugins/orca-status/plugin.yaml provides_hooks + ~/.hermes/plugins/orca-status/__init__.py:8-18 SELECTED_KEYS; security-guidance `transform_tool_result` at security-guidance/__init__.py:132]. `config.yaml` также поддерживает хуки на основе команд (`hooks.pre_tool_call: [- command: …]`) [source: ~/.hermes/config.yaml:55-60]. Это альтернативная поверхность интеграции, если бы плагину памяти понадобилось наблюдать/сохранять диалог без ABC MemoryProvider — но ABC создан именно для этого и бесплатно даёт инъекцию инструментов + prompt prefetch.

### 2.7 Поддержка Python

Плагины — обычные Python-модули, импортируемые загрузчиком (`load_plugin_module` с синтетическими родительскими пакетами для пользовательских установок, `parents=("plugins", "plugins.memory")`) [source: plugins/memory/__init__.py:296-302]. Комплектные провайдеры — чистый Python против SDK, который они объявляют в `pip_dependencies` [source: mem0/supermemory plugin.yaml]. Гайд: "Ваш плагин реализует абстрактный базовый класс MemoryProvider из `agent/memory_provider.py`" [source: guide, "The MemoryProvider ABC"].

---

## 3. Сверка: omp ↔ Dakera (работающий прецедент)

- Конфигурация рабочего пространства `.omp/config.yml` (только локально, в gitignore) задаёт `memory.backend: dakera` + `dakera.agentId: hermes-dakera`, `retainMode: last-turn`, `retainEveryNTurns: 1` [source: file]. Живой оверлей `~/.omp/agent/dakera.yml` добавляет URL сервера (частный LAN-хост, отредактирован), скоупед API-токен (отредактирован) и `recallRerank: false` с обоснованием стоимости rerank; те же ключи есть в базовой конфигурации агента omp [source: local config files].
- Реализация (форк omp, TypeScript): `packages/coding-agent/src/dakera/` — `client.ts` представляет собой самописный fetch-клиент ("мы зависим только от тех эндпоинтов, которые реально используем, и ни от чего больше, поэтому SDK не попадает в lockfile"), бьющий в `POST /v1/memory/store` и `POST /v1/memory/recall` с `Authorization: Bearer`, таймаутами на вызов (30 с recall / 60 с retain) и retry с поддержкой `Retry-After` [source: client.ts:1-19, 139-165, 166-177, 238-266]; `bank.ts` выводит `agent_id` (`global` / `per-project` / `per-project-tagged` с тегами `project:`/`global:shared`; запись против незнакомого agent_id создаёт его автоматически) [source: bank.ts:1-58]; `state.ts` реализует auto-recall на первом ходе + auto-retain каждые N ходов, режим `last-turn` сохраняет одно воспоминание на окно пользовательского хода [source: state.ts:105-118, 341-362]; `config.ts` документирует приоритет настроек/env и принимает как `DAKERA_API_TOKEN`, так и `DAKERA_API_KEY` для bearer-токена [source: config.ts:1-13].
- **Маршрут MCP отсутствует**: `~/.omp/agent/mcp.json` не содержит сервера dakera; инструменты памяти сессии — это бэкенд памяти omp, смонтированный как устройства `xd://` (`retain`, `recall`, `reflect`, `memory_edit`, `learn` — встроенные имена, а не `dakera_*`) [source: ~/.omp/agent/mcp.json server list; <local omp fork>/packages/coding-agent/src/memory-backend/tool-names.ts:3]. Поэтому формулировка промпта "dakera_recall/dakera_store MCP tools" приблизительна: omp экспонирует Dakera через свою абстракцию бэкенда, а не MCP-сервер с именем Dakera. Паттерн, который переносится на Hermes, — **прямой HTTP-клиент + изоляция по agent_id**, что ровно и даёт связка Hermes `MemoryProvider` + `DakeraClient`.
- Проверка живых эндпоинтов: `GET /health` → здоровый v0.11.108; `POST /v1/memory/recall` с bearer-токеном omp вернул ранжированные воспоминания (конверт `smart_score`/`weighted_score`, вложенный объект `memory`) для `agent_id: hermes-dakera` [source: curl probes, 2026-09-27].

---

## 4. Анализ пробелов: что должен реализовать плагин памяти `dakera` для Hermes

Ничего специфичного для Dakera в Hermes сегодня нет (grep по `~/.hermes` на `dakera` в .py/.yaml/.json: ноль совпадений) [source: grep, 2026-09-27]. Пробел — это один новый плагин, реализующий стандартный контракт:

1. **Пакет и раскладка** — пользовательская установка в `~/.hermes/plugins/dakera/` с `__init__.py` (провайдер + `register(ctx)`), `plugin.yaml` (`name: dakera`, description, `pip_dependencies: [dakera]`), опциональный `README.md` [source: guide "Directory Provider" layout; §2.2 above]. Альтернатива — pip-пакет с `[project.entry-points."hermes_agent.memory_providers"] dakera = "…:register"` [source: guide "Packaged Provider"]. Проектный `./.hermes/plugins/` потребовал бы `HERMES_ENABLE_PROJECT_PLUGINS=1` [source: plugins_discovery.py:162-167]. Заметьте, что при коллизии имён комплектный провайдер выигрывает у пользовательского — здесь это неважно, поскольку комплектного `dakera` не существует.
2. **`DakeraMemoryProvider(MemoryProvider)`** должен реализовать:
   - `name → "dakera"`; `is_available()` → настроены URL+ключ, без сети [source: ABC requirements].
   - `initialize(session_id, **kwargs)` → загрузить конфигурацию (`$HERMES_HOME/dakera/config.json` и/или env `DAKERA_API_URL`/`DAKERA_API_KEY`/`DAKERA_AGENT_ID`, следуя конвенции omp), построить `DakeraClient`, разрешить `agent_id` (фиксированный в духе `hermes-dakera` или на профиль через `kwargs["agent_identity"]`), учесть `agent_context` (пропускать записи для subagent/cron/flush) [source: §2.4 kwargs; omp config.ts env names].
   - `prefetch()`/`queue_prefetch()` → `client.recall(agent_id, query, top_k)`, отформатированный в блок контекста; должен быть неблокирующим/кэшированным [source: §2.4; supermemory thread pattern at supermemory/__init__.py:408-440].
   - `sync_turn()` → retain после каждого хода; режим `last-turn` omp (одно episodic-воспоминание на окно хода, importance по умолчанию ~0.5–0.6, теги ролей) — проверенная форма [source: omp state.ts:341-362; dakera session.py default importance 0.6].
   - `on_session_end(messages)` → flush отложенных записей + `end_session(summary)`, если используются сессии; `shutdown()` → закрыть requests-сессию.
   - `get_tool_schemas()` + `handle_tool_call()` → например `dakera_store`, `dakera_search`, `dakera_forget` (+ snake/kebab-алиасы, как у supermemory) поверх `store_memory`/`search_memories`/`forget` [source: supermemory schema/alias pattern at supermemory/__init__.py:291-301, 506-512].
   - `get_config_schema()`/`save_config()`/`post_setup()` → `api_url`, `api_key` (secret → env `DAKERA_API_KEY`), `agent_id`; `post_setup` пишет `memory.provider: dakera` [source: supermemory post_setup at supermemory/__init__.py:357-378].
3. **Активация** — `hermes memory setup` (или вручную `memory.provider: dakera` в `~/.hermes/config.yaml`); текущая установка имеет память отключённой, так что активация также означает включение toolset-а памяти, чтобы инструменты появились [source: config.yaml:45-47; memory_manager.py:122-137 gating].
4. **Операционные знания, унаследованные от опыта omp**: пин против сервера v0.11.108 (≥0.11.98 для под-оценок/`valid_from`); не считать `consolidate` read-only; рассмотреть дефолт `recallRerank: false` с учётом измеренной стоимости CPU на этом хосте; ожидать временных меток в Unix-секундах; `RecalledMemory.created_at` в SDK типизирован как `str | None`, но сервер шлёт числа — нормализовать в одном месте [source: §1.6 cites].
5. **Замечание о sync-vs-async**: `DakeraClient` — блокирующий `requests`; `prefetch()` не должен блокировать цикл хода, поэтому либо выполнять recall в фоновом потоке (паттерн supermemory/mem0), либо использовать `AsyncDakeraClient` (httpx) с циклом, как это делает hindsight [source: async_client.py:164; hindsight module-global event loop comment at hindsight/__init__.py:1240-1251].

---

## 5. Открытые вопросы

1. **Политика agent_id**: один общий `hermes-dakera` (omp-агент этого рабочего пространства), на каждый Hermes-профиль (`agent_identity`) или скоупинг в стиле omp `global/per-project/per-project-tagged` с тегами `project:`? `bank.ts` omp — референсная реализация, но в ABC Hermes нет прямого эквивалента "фильтров recall по тегам проекта" — теги пришлось бы проводить через `store_memory(tags=…)` и фильтры тегов `recall` через сырые параметры [source: bank.ts:20-43; client.py:1116+].
2. **Дом конфигурации**: следовать конвенции провайдеров Hermes (`$HERMES_HOME/dakera/config.json` + секрет в `.env`) или env-first-подходу omp `DAKERA_API_URL`/`DAKERA_API_TOKEN`/`DAKERA_API_KEY`? Оба имени токена env принимает omp; сама экосистема сервера/SDK Dakera использует `DAKERA_API_KEY` [source: config.ts:1-13; README env usage `DAKERA_API_URL`/`DAKERA_API_KEY` in examples/memory.py:16-19].
3. **Непроверенные эндпоинты на живом сервере**: `/v1/memory/search`, batch-эндпоинты, `wake_up`, API сессий документированы SDK, но не проверялись вживую (упражнялись только `/health` и `/v1/memory/recall`); `dakera-py` 0.12.13 нацелен на сервер 0.11.98+, а инстанс — 0.11.108 — должно быть совместимо, но стоит прогнать smoke-скрипт до заморозки дизайна.
4. **Переносит ли venv hermes `pip_dependencies: [dakera]`?** `dakera` требует `requests>=2.33.0` + `urllib3>=2.6.3`; Hermes устанавливает зависимости провайдеров "under Hermes' own pins" [source: guide Setup UX table] — совместимость не проверена, пока попытка установки не предпринята.
5. **Дефолт rerank**: omp отключил rerank из-за измеренных запросов в 10–22 с на этом хосте; должен ли плагин Hermes по умолчанию использовать `rerank=false` с тумблером в конфигурации, или полагаться на серверные улучшения? [source: ~/.omp/agent/dakera.yml:14-19; issue-dakera-rerank.md]
6. **Альтернатива MCP**: `dakera-mcp` (MCP server for Claude/Cursor) существует как отдельный репозиторий и в принципе мог бы быть подключён через MCP-конфигурацию Hermes вместо нативного плагина — здесь не исследовано; нативный маршрут MemoryProvider даёт жизненный цикл prefetch/retain и инъекцию инструментов, которых MCP-сервер не обеспечил бы [source: README "Other SDKs" table; §2.4].
7. **Взаимодействие со встроенной памятью**: встроенная память Hermes MEMORY.md/USER.md сейчас отключена; гайд говорит, что встроенная "always active", тогда как `hermes memory status` показывает её отключённой локально — перед проектированием зеркалирования `on_memory_write` выяснить, что из этого авторитетно для этой установки [source: `hermes memory --help` text vs `hermes memory status` output].
