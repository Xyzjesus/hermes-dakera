# hermes-dakera

<p align="center">
  <a href="#-возможности-features">Возможности</a> ·
  <a href="#-установка-installation">Установка</a> ·
  <a href="#-сравнение-comparison">Сравнение</a> ·
  <a href="#-конфигурация-configuration">Конфигурация</a> ·
  <a href="#-разработка-development">Разработка</a>
</p>

🧠 **Провайдер памяти Dakera для [Hermes Agent](https://github.com/NousResearch/hermes-agent)** —
долговременная память для агента на базе [Dakera](https://github.com/Dakera-AI),
**полностью self-hosted сервера памяти**. Никакого облачного SaaS, никаких внешних
API-ключей эмбеддингов, никакого `OPENAI_API_KEY` для извлечения памяти. Ваши диалоги
не покидают ваше железо.

[English](README.md) | **Русский**

```
┌──────────────┐   recall/store    ┌─────────────────────┐
│    Hermes    │ ────────────────► │  Dakera server      │
│  (any host)  │ ◄──────────────── │  (self-hosted,      │
└──────────────┘  <dakera-memory>  │   Rust core)        │
   агент + инструменты             └─────────────────────┘
   dakera_recall / dakera_store        ваше железо, ваши данные
   dakera_search / dakera_forget       vector + BM25 гибрид
```

## Почему Dakera

- **100% self-hosted.** Сервер работает на вашей машине/VPS — один контейнер, REST + gRPC.
  В отличие от supermemory/retaindb/honcho (cloud-first) или mem0 в platform-режиме,
  ничего не отправляется в сторонние API. См. [Сравнение](#-сравнение-comparison).
- **Никаких внешних моделей.** Эмбеддинги, реранк и NER считаются **на сервере
  встроенными ONNX-моделями** (по умолчанию bge-large; кросс-энкодер
  `bge-reranker-v2-m3`). В документированном конфиге **ноль переменных для
  LLM/embedding API-ключей**, и **air-gapped работа явно поддерживается**. (LLM — сам
  Hermes; память — чистый retrieval.)
  Источники: [`research/ru/dakera-github-overview.md`](research/ru/dakera-github-overview.md).
- **Ядро на Rust.** Сервер — **один Rust-бинарник** (Cargo workspace в Dockerfile
  `dakera-deploy`; «Built with Rust. Single binary.»). Официальный CLI `dk` и MCP-сервер —
  Rust-крейты с прекомпилированными бинарниками. Никакого Python/Node-рантайма.
- **Память, управляемая агентом.** Агент получает четыре полноценных инструмента —
  recall, store, search, forget — плюс автоматический per-turn retain и
  саммари в конце сессии.
- **Заточено под команды агентов.** Нэймспейсы `agent_id`, явный CRUD `/v1/namespaces`
  с namespace-scoped API-ключами, разделяемая cross-agent память когда нужна, KG-связи
  между воспоминаниями — один сервер, много агентов.

## ✨ Возможности (Features)

| | |
|---|---|
| 🔄 **Авто-retain** | каждый ход сохраняется парой `[user]/[assistant]` (с шумовым фильтром), саммари последних 6 сообщений в конце сессии |
| 🔎 **Гибридный recall** | vector + BM25, роутинг `auto/vector/bm25/hybrid`, настраиваемый top-K; инъекция в контекст как `<dakera-memory>` со скорами |
| 🛠 **4 инструмента агента** | `dakera_recall` (семантика+score), `dakera_store` (importance 0–1, episodic/semantic/procedural/working), `dakera_search` (BM25-просмотр), `dakera_forget` (удаление по id) |
| 🧱 **Изоляция** | нэймспейсы `agent_id`: один сервер, много агентов/профилей, ноль протечек |
| 👥 **Готовность к командам** | несколько агентов Hermes на одном сервере Dakera; разделение по `agent_id` — или общий `agent_id` как общий пул памяти команды |
| 🛟 **Fail-open** | сервер лежит → warning, агент работает дальше. Запись отключена для контекстов `cron`/`flush`/`subagent` |
| ⚡ **Неблокируемость** | recall префетчится в фоновом потоке с кэшем на один слот; без вызовов `rerank`/`consolidate` (безопасные для латентности дефолты) |
| 🔐 **Секреты** | secret scope → env → `$HERMES_HOME/dakera/config.json` (mode 0600) |

Что даёт сам сервер (из
[обзора организации Dakera](research/ru/dakera-github-overview.md)):

- **Гибридный retrieval**: HNSW-вектор + BM25 (стемминг Snowball), RRF-фьюжн,
  классификатор запросов, PRF; опциональный кросс-энкодер-реранк; `smart_score` =
  релевантность + importance + свежесть + частота.
- **Модель памяти**: episodic/semantic/procedural/working, importance 0–1,
  би-темпоральный `valid_from`, TTL/`expires_at`, decay, батч-store (1–1000 за один
  проход эмбеддинга), session-эндпоинты, консолидация (дедуп/саммари).
- **Граф знаний**: ассоциации (`related_to`/`shares_entity`/`precedes`/`linked_by`),
  recall с `include_associated` для контекста окрестности.
- **Ops**: один контейнер, HA-режим (3 ноды, gossip + Traefik), Prometheus/Grafana,
  ярусное хранилище L1/L2/L3, MCP-сервер, официальные SDK (py/ts/go/rs) + CLI `dk`.
- **LoCoMo**: Dakera сама сообщает **88.2% Recall@20** на
  [LoCoMo](https://arxiv.org/abs/2402.17753) (v0.11.107, 1 536 вопросов, adversarial
  исключён; LLM-оцениваемый retrieval recall, не QA accuracy — вендорские цифры, не
  независимо проверенные; детали и разбивка по категориям в обзоре).

Полный разбор возможностей: [`plugin/dakera/README.ru.md`](plugin/dakera/README.ru.md#-возможности).

## 🚀 Установка (Installation)

**Агент-friendly:** отдайте установочный раздел
[`plugin/dakera/README.ru.md`](plugin/dakera/README.ru.md#-установка-llm-friendly)
своему агенту — там preflight, `hermes plugins install Xyzjesus/hermes-dakera`
(или ручной drop-in в `~/.hermes/plugins/`), настройка креденшелов, активация
(`memory.provider: dakera` в `~/.hermes/config.yaml`) и проверка.

Коротко:

```bash
# 1. Запустить сервер (Docker; HA/k8s/helm — см. dakera-deploy):
docker run -d -p 3000:3000 -e DAKERA_ROOT_API_KEY=YOUR-TOKEN ghcr.io/dakera-ai/dakera:latest
# 2. Установить плагин:
hermes plugins install Xyzjesus/hermes-dakera      # ставит pip_dependencies (SDK dakera)
mkdir -p ~/.hermes/dakera
printf '{"api_url":"http://YOUR-DAKERA-HOST:3000","api_key":"YOUR-TOKEN","agent_id":"hermes","top_k":5,"recall_routing":"auto"}\n' \
  > ~/.hermes/dakera/config.json && chmod 600 ~/.hermes/dakera/config.json
# 3. Активировать и проверить:
#    в ~/.hermes/config.yaml:  memory: { provider: dakera }
hermes memory status                              # provider: dakera, active
```

## 📊 Сравнение (Comparison)

Hermes поставляется с 8+ провайдерами памяти. Чем отличается dakera — хостинг и
требования к моделям (факты из
[`research/ru/hermes-memory-plugins-survey.md`](research/ru/hermes-memory-plugins-survey.md),
исходники локальных плагинов; колонка Dakera — из `research/ru/dakera-github-overview.md`):

| Провайдер | Хостинг | Памяти нужна внешняя модель/API? | Примечания |
|---|---|---|---|
| built-in (MEMORY.md/USER.md) | локальные файлы | нет | простые markdown-файлы, без семантического поиска, без инструментов |
| **dakera** ⭐ | **self-hosted сервер (Rust, один контейнер; есть HA-режим)** | **нет — server-side ONNX эмбеддинги/реранк встроены, air-gapped ок** | гибрид HNSW+BM25, KG-ассоциации, нэймспейсы + скоуп-ключи, MCP-сервер, инструменты агента, per-turn retain |
| holographic | локальный SQLite | нет | ключев поиск FTS5, без vector/hybrid |
| hindsight | облако (дефолт) или локально | локальным режимам нужен LLM для извлечения (дефолт gpt-4o-mini; для полностью локального — ollama) | KG + разрешение сущностей |
| mem0 | platform-облако (дефолт) / self-host / OSS | platform = облако; OSS нужен LLM+эмбеддер (ключ OpenAI или Ollama) | извлечение фактов через LLM |
| supermemory | облако (дефолт), self-host через base_url | облако = внешний SaaS | containers/profile recall |
| honcho | облако (дефолт), self-host через baseUrl | облако = внешний SaaS | диалектическое моделирование пользователя |
| retaindb | только облако | ключ RetainDB API | 7 типов памяти |
| byterover | local-first + опциональное облако | внешний CLI `brv`; LLM-уровень поиска | дерево знаний |
| openviking | managed-облако VolcEngine (дефолт) / self-host | managed = внешний SaaS | база контекста |

В одну строку: **только dakera и holographic работают полностью self-hosted без
ключей моделей из коробки — dakera добавляет семантический гибридный recall,
инструменты памяти и сервер для мультиагентных/командных сценариев; holographic —
однофайловый SQLite-стор.**

Примечание о бенчмарках (честная подача цифр): Dakera сама сообщает 88.2% LoCoMo
Recall@20 — метрика **LLM-оцениваемого retrieval recall**, которая, по словам самой
Dakera, *напрямую несравнима* с QA-accuracy цифрами Zep (94.7%) / Mem0 (92.5%) /
Letta (74.0%) — метрики разные (и Zep/Mem0 спорят друг с другом). Атрибуция и
методология: [`research/ru/dakera-github-overview.md`](research/ru/dakera-github-overview.md),
источник [dakera.ai/benchmark](https://dakera.ai/benchmark).

## ⚙️ Конфигурация (Configuration)

| Ключ | Env | По умолчанию | Значение |
|---|---|---|---|
| `api_url` | `DAKERA_API_URL` | `http://localhost:3000` | URL сервера Dakera |
| `api_key` | `DAKERA_API_KEY` | — | root- или скоуп-ключ |
| `agent_id` | — | `hermes` | нэймспейс памяти / единица изоляции |
| `top_k` | — | `5` | число результатов recall |
| `recall_routing` | — | `auto` | `auto`/`vector`/`bm25`/`hybrid` |

## 🧪 Разработка (Development)

```bash
pytest tests/ -q        # 28 тестов
hermes plugins validate plugin/dakera   # каталоговый гейт (обязан проходить)
```

Раскладка: `plugin/dakera/` — провайдер (манифест, провайдер + 4 инструмента);
`tests/` — тесты провайдера; `research/ru/` — дизайнерские/бенчмарк-заметки с источниками:
`dakera-memory-plugin.md` (дизайн-спека), `dakera-github-overview.md` (обзор Dakera org),
`hermes-memory-plugins-survey.md` (источники сравнения).

## Лицензия

TBD.
