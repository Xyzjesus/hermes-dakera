# Обзор: memory-провайдеры Hermes — требования к хостингу и моделям

Локальный обзор плагинов memory-провайдеров, входящих в поставку Hermes, в
`~/.hermes/hermes-agent/plugins/memory/`
(установка Hermes проверена 2026-09-27). Каждое утверждение ссылается на файл плагина, из которого оно взято.
Цель — обосновать сравнительную таблицу в корневом README репозитория `hermes-dakera`.

## Встроенная база (без плагина)

- Встроенная memory — это файлы `MEMORY.md` + `USER.md`, без сервера.
  [source: website/docs/developer-guide/memory-provider-plugin.md:9]

## Провайдеры в комплекте поставки

| Провайдер | Хостинг | Нужны ли внешние модели? | Обоснование |
|---|---|---|---|
| **holographic** | полностью локальный (SQLite) | нет (в плагине нет ни LLM, ни embedder) | `plugins/memory/holographic/plugin.yaml` ("local SQLite fact store with FTS5"); `__init__.py` "SQLite is always available, numpy is optional" |
| **hindsight** | 3 режима: `cloud` (по умолчанию, api.hindsight.vectorize.io) / `local_embedded` / `local_external` | локальным режимам для извлечения нужен LLM-провайдер — варианты openai/anthropic/gemini/groq/openrouter/minimax/ollama/lmstudio/openai_compatible, по умолчанию `openai` gpt-4o-mini; полностью локальная работа — только если выбраны ollama/lmstudio | `hindsight/__init__.py:2` ("cloud (API key), local_external, or local_embedded"), `:425-428` (схема mode + llm_provider); `settings.py:_DEFAULT_API_URL/_PROVIDER_DEFAULT_MODELS` |
| **mem0** | platform (облако, по умолчанию) / self-hosted (на своём сервере) / OSS внутри процесса | platform: облачный API. Для режима OSS нужны LLM- и embedder-провайдеры: openai (нужен OPENAI_API_KEY) или ollama (локально, нужен запущенный Ollama) | `mem0/_backend.py:20` ("Platform (MemoryClient), self-hosted (HTTP) and OSS (Memory)"); `_oss_providers.py` LLM_PROVIDERS/EMBEDDER_PROVIDERS ("openai" needs_key, "ollama" local) |
| **supermemory** | облако по умолчанию (api.supermemory.ai), переопределение base_url для self-host | облако по умолчанию = внешний SaaS | `supermemory/__init__.py:23` `_DEFAULT_BASE_URL = "https://api.supermemory.ai"`, `:62` "config > SUPERMEMORY_BASE_URL > default (self-hosted support)" |
| **retaindb** | только облачный API | да (ключ RetainDB) | `retaindb/plugin.yaml` `requires_env: [RETAINDB_API_KEY]`; описание "cloud memory API" |
| **honcho** | облако по умолчанию (app.honcho.dev), переопределение baseUrl для self-host | облако по умолчанию = внешний SaaS | `honcho/__init__.py` схема конфигурации: api_key url https://app.honcho.dev, baseUrl "for self-hosted" |
| **byterover** | local-first + опциональная облачная синхронизация, требуется внешний `brv` CLI (установка через curl-pipe) | поисковый уровень на базе LLM подразумевает доступ к модели через brv | `byterover/__init__.py` ("local-first with optional cloud sync (BRV_API_KEY). Requires the brv CLI"); `plugin.yaml` external_dependencies curl-pipe install |
| **openviking** | управляемое облако VolcEngine (по умолчанию) или собственный self-host-сервер | управляемый endpoint = внешний SaaS | `openviking/__init__.py` `_OPENVIKING_SERVICE_ENDPOINT = "https://api.vikingdb.cn-beijing.volces.com/openviking"`; `_setup.py` "OpenViking Service (VolcEngine Cloud)" vs "local, VPS, or self-hosted" |

## Место dakera в этой схеме

`plugin/dakera/__init__.py` (этот репозиторий): обращается к **self-hosted Dakera серверу**,
который разворачивает пользователь; recall/search — чистые запросы к серверу (SDK `dakera`,
плагин не делает вызовов LLM), retain сохраняет пары turn'ов дословно (verbatim) — без LLM
для извлечения фактов и без внешнего embedding API на стороне клиента.
Вопрос, нужна ли внешняя модель самому серверу, рассматривается в обзоре Dakera на GitHub
(`research/dakera-github-overview.md`).
