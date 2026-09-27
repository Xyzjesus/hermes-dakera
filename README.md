# hermes-dakera

**English** | [Русский](README.ru.md)

<p align="center">
  <a href="#features">Features</a> ·
  <a href="#installation">Installation</a> ·
  <a href="#comparison">Comparison</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#development">Development</a>
</p>

🧠 **Dakera memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent)** —
give your agent durable long-term memory backed by [Dakera](https://github.com/Dakera-AI), a
**fully self-hosted memory server**. No cloud SaaS, no external embedding API keys, no
`OPENAI_API_KEY` for memory extraction. Your conversations never leave your hardware.

```
┌──────────────┐   recall/store    ┌─────────────────────┐
│    Hermes    │ ────────────────► │  Dakera server      │
│  (any host)  │ ◄──────────────── │  (self-hosted,      │
└──────────────┘  <dakera-memory>  │   Rust core)        │
   agent + tools                   └─────────────────────┘
   dakera_recall / dakera_store        your box, your data
   dakera_search / dakera_forget       vector + BM25 hybrid
```

## Why Dakera

- **100% self-hosted.** The server runs on your machine/VPS — one container, REST + gRPC.
  Unlike supermemory/retaindb/honcho (cloud-first) or mem0 platform mode, nothing is sent
  to a third-party API. See [Comparison](#comparison).
- **No external models.** Embeddings, rerank and NER run **server-side on built-in ONNX
  models** (bge-large by default; cross-encoder `bge-reranker-v2-m3`). The documented
  config has **zero LLM/embedding API-key variables**, and **air-gapped operation is
  explicitly supported**. (Hermes itself is the LLM; memory is plain retrieval.)
  Sources: [`research/en/dakera-github-overview.md`](research/en/dakera-github-overview.md).
- **Rust core.** The server is a **single Rust binary** (Cargo workspace in
  `dakera-deploy`'s Dockerfile; "Built with Rust. Single binary."). The official `dk` CLI
  and MCP server are Rust crates with prebuilt binaries. No Python/Node runtime to babysit.
- **Agent-managed memory.** The agent gets four first-class tools — recall, store,
  search, forget — plus automatic per-turn retain and session-end summaries.
- **Built for teams of agents.** `agent_id` namespaces, explicit `/v1/namespaces` CRUD
  with namespace-scoped API keys, shared cross-agent memory when you want it, KG
  associations across memories — one server, many agents.

## Features

| | |
|---|---|
| 🔄 **Auto-retain** | every turn stored as `[user]/[assistant]` pair (noise-gated), session-end summary of last 6 messages |
| 🔎 **Hybrid recall** | vector + BM25, routing `auto/vector/bm25/hybrid`, top-K configurable; injected into context as `<dakera-memory>` with scores |
| 🛠 **4 agent tools** | `dakera_recall` (semantic+score), `dakera_store` (importance 0–1, episodic/semantic/procedural/working), `dakera_search` (BM25 browse), `dakera_forget` (delete by id) |
| 🧱 **Isolation** | `agent_id` namespaces: one server, many agents/profiles, zero cross-talk |
| 👥 **Team-ready** | multiple Hermes agents can share one Dakera server; per-`agent_id` separation, or a shared `agent_id` for a team memory pool |
| 🛟 **Fail-open** | server down → warning, agent keeps working. Writes gated off for `cron`/`flush`/`subagent` contexts |
| ⚡ **Non-blocking** | recall prefetched on a background thread with a one-slot cache; no `rerank`/`consolidate` calls (latency-safe defaults) |
| 🔐 **Secrets** | secret scope → env → `$HERMES_HOME/dakera/config.json` (mode 0600) |

What the server itself brings (from the
[Dakera org survey](research/en/dakera-github-overview.md)):

- **Hybrid retrieval**: HNSW vector + BM25 (Snowball stemming), RRF fusion, query
  classifier routing, PRF; optional cross-encoder rerank; `smart_score` =
  relevance + importance + recency + frequency.
- **Memory model**: episodic/semantic/procedural/working, importance 0–1,
  bi-temporal `valid_from`, TTL/`expires_at`, decay, batch store (1–1000 in one
  embedding pass), session endpoints, consolidation (dedupe/summarize).
- **Knowledge graph**: associations (`related_to`/`shares_entity`/`precedes`/`linked_by`),
  `include_associated` recall for neighborhood context.
- **Ops**: single container, HA mode (3-node gossip + Traefik), Prometheus/Grafana,
  tiered L1/L2/L3 storage, MCP server, official SDKs (py/ts/go/rs) + `dk` CLI.
- **LoCoMo**: Dakera self-reports **88.2% Recall@20** on
  [LoCoMo](https://arxiv.org/abs/2402.17753) (v0.11.107, 1,536 questions, adversarial
  excluded; LLM-judged retrieval recall, not QA accuracy — vendor numbers, not
  independently verified; details & per-category breakdown in the survey).

Full feature walkthrough: [`plugin/dakera/README.md`](plugin/dakera/README.md#-features).

## Installation

**Agent-friendly:** hand the installer section of
[`plugin/dakera/README.md`](plugin/dakera/README.md#-installation-llm-friendly)
to your agent — it covers preflight, `hermes plugins install <owner>/hermes-dakera`
(or manual drop-in to `~/.hermes/plugins/`), credential setup, activation
(`memory.provider: dakera` in `~/.hermes/config.yaml`), and verification.

Short version:

```bash
# 1. Run the server (Docker; see dakera-deploy for HA/k8s/helm):
docker run -d -p 3000:3000 -e DAKERA_ROOT_API_KEY=YOUR-TOKEN ghcr.io/dakera-ai/dakera:latest
# 2. Install the plugin:
hermes plugins install Xyzjesus/hermes-dakera      # installs pip_dependencies (dakera SDK)
mkdir -p ~/.hermes/dakera
printf '{"api_url":"http://YOUR-DAKERA-HOST:3000","api_key":"YOUR-TOKEN","agent_id":"hermes","top_k":5,"recall_routing":"auto"}\n' \
  > ~/.hermes/dakera/config.json && chmod 600 ~/.hermes/dakera/config.json
# 3. Activate + verify:
#    set in ~/.hermes/config.yaml:  memory: { provider: dakera }
hermes memory status                              # provider: dakera, active
```

## Comparison

Hermes ships 8+ memory providers. How dakera differs — hosting and model
requirements (facts sourced in
[`research/en/hermes-memory-plugins-survey.md`](research/en/hermes-memory-plugins-survey.md),
local plugin sources; Dakera column from `research/en/dakera-github-overview.md`):

| Provider | Hosting | Memory needs external model/API? | Notes |
|---|---|---|---|
| built-in (MEMORY.md/USER.md) | local files | no | plain markdown files, no semantic search, no tools |
| **dakera** ⭐ | **self-hosted server (Rust, one container; HA mode available)** | **no — server-side ONNX embeddings/rerank built in, air-gapped OK** | hybrid HNSW+BM25, KG associations, namespaces + scoped keys, MCP server, agent tools, per-turn retain |
| holographic | local SQLite | no | FTS5 keyword search, no vector/hybrid |
| hindsight | cloud (default) or local | local modes need an LLM for extraction (default gpt-4o-mini; ollama for fully-local) | KG + entity resolution |
| mem0 | platform cloud (default) / self-host / OSS | platform = cloud; OSS needs LLM+embedder (OpenAI key or Ollama) | fact extraction via LLM |
| supermemory | cloud (default), self-host via base_url | cloud = external SaaS | containers/profile recall |
| honcho | cloud (default), self-host via baseUrl | cloud = external SaaS | dialectic user modeling |
| retaindb | cloud only | RetainDB API key | 7 memory types |
| byterover | local-first + optional cloud | external `brv` CLI; LLM-driven search tier | knowledge tree |
| openviking | VolcEngine managed cloud (default) / self-host | managed = external SaaS | context database |

The pitch in one line: **only dakera and holographic run fully self-hosted with zero
model keys out of the box — dakera adds semantic hybrid recall, memory tools, and a
server for multi-agent/team setups; holographic is a single-file SQLite store.**

Benchmark note (fair use of numbers): Dakera self-reports 88.2% LoCoMo Recall@20 —
an **LLM-judged retrieval-recall** metric that Dakera itself says is *not directly
rankable* against the QA-accuracy numbers Zep (94.7%) / Mem0 (92.5%) / Letta (74.0%)
publish, because metrics differ (and Zep/Mem0 dispute each other). Attribution and
methodology: [`research/en/dakera-github-overview.md`](research/en/dakera-github-overview.md),
source [dakera.ai/benchmark](https://dakera.ai/benchmark).

## Configuration

| Key | Env | Default | Meaning |
|---|---|---|---|
| `api_url` | `DAKERA_API_URL` | `http://localhost:3000` | Dakera server URL |
| `api_key` | `DAKERA_API_KEY` | — | root or scoped key |
| `agent_id` | — | `hermes` | memory namespace / isolation unit |
| `top_k` | — | `5` | recall result count |
| `recall_routing` | — | `auto` | `auto`/`vector`/`bm25`/`hybrid` |

## Development

```bash
pytest tests/ -q        # 28 tests
hermes plugins validate plugin/dakera   # catalog admission gate (must pass)
```

Layout: `plugin/dakera/` — the provider (manifest, provider + 4 tools);
`tests/` — provider test suite; `research/en/` — cited design/benchmark notes:
`dakera-memory-plugin.md` (design spec), `dakera-github-overview.md` (Dakera org
survey), `hermes-memory-plugins-survey.md` (this comparison's sources).

## License

TBD.
