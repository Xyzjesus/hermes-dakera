# dakera — Hermes memory plugin (self-hosted Dakera)

> 🧠 Add a **Dakera long-term memory server** to any Hermes agent install: fully
> local-first durable memory across sessions, with hybrid recall, per-turn
> retain, and `dakera_recall` / `dakera_store` / `dakera_search` /
> `dakera_forget` tools that the agent can call directly.

---

## ✨ Features

### 1. Durable long-term memory across sessions
- Every conversation turn gets retained as an `[user]/[assistant]` pair in a
  self-hosted Dakera server.
- At session end the last 6 messages are summarized and stored as
  `[session summary]` (importance 0.7) — future sessions recall them.
- Isolation unit is `agent_id`: one server, many agents, zero cross-talk.
  Change `agent_id` in config → separate memory universe.

### 2. Hybrid recall (vector + BM25)
- `recall_routing` modes: `auto` | `vector` | `bm25` | `hybrid` (default
  `auto` — server picks the best strategy per query).
- Vector search finds meaning ("починить шину" recalls "replace wheel"),
  BM25 catches exact identifiers (`err_code_XYZ`, function names).
- Top-K configurable (`top_k`, default 5).
- Recall block is injected into context before each turn wrapped in
  `<dakera-memory>` tags with relevance scores, e.g.:
  `<dakera-memory>` `1. Fact (score 0.92)` `</dakera-memory>`

### 3. Four agent-facing memory tools
The agent doesn't just passively remember — it actively manages its memory:

| Tool | What it does |
|---|---|
| `dakera_recall` | semantic/hybrid search over durable memory, returns `{id, content, score}` |
| `dakera_store` | explicit save: content + `importance` (0.0–1.0) + `memory_type` (`episodic`/`semantic`/`procedural`/`working`), tagged `hermes`+`explicit` |
| `dakera_search` | lexical BM25 browse by keyword — when recall misses or you need inventory |
| `dakera_forget` | GDPR-style delete by exact `memory_ids` from recall/search results |

### 4. Per-turn retain (pair mode)
- `sync_turn` stores `[user] …` + `[assistant] …` pair per turn, tagged
  `user`/`assistant`, with `hermes_session` metadata for traceability.
- Noise gate: turns with <16 chars of combined content are skipped — no junk
  like "ok", "yes" polluting memory.

### 5. Non-blocking prefetch with one-slot cache
- Recall runs on a background thread (`queue_prefetch`); the next turn picks
  the result from a one-slot cache — exact-query matched, falls back to sync
  recall if the query changed.
- Result: memory injection adds ~0 ms to the hot path.

### 6. Fail-open everywhere
- Server down / timeout / SDK error → warning in log, agent keeps working
  with empty recall. **Memory must never kill the agent.**
- Store failures also just warn — conversation continues.

### 7. Write safety by agent context
- Writes (auto-retain + tools) are **disabled** for `cron`, `flush`,
  `subagent` contexts — only the `primary` agent writes to long-term memory.
- No memory spam from background jobs.

### 8. Cost-conscious by design
- `rerank` is never sent: server-side reranking costs ~2 s per candidate on
  small hosts. Hybrid routing gives quality without the latency.
- `consolidate` is never called — server-side compaction stays manual.
- Recall client timeout 10 s (fast-path budget); store timeout 30 s.

### 9. Secrets handled properly
- API key read from secret scope (`DAKERA_API_KEY`) → env →
  `$HERMES_HOME/dakera/config.json`; config file written with `0600`.
- `is_available()` never touches the network — pure config/credential check.

---

## 🚀 Installation (LLM-friendly)

**For agents:** follow every step top-to-bottom; do not skip. All commands are
safe to run non-interactively. Verify with the checks at the end.

### Step 0 — Preflight
```bash
hermes --version                     # Hermes must be installed
python3 --version                    # >= 3.10
```
Ask the user for `DAKERA_API_URL` (e.g. `http://your-dakera-host:3003`) and
`DAKERA_API_KEY` if not provided yet. **Do not guess the domain or key.**

### Step 1 — Install the plugin
Preferred (Git URL or `owner/repo` shorthand — Hermes copies it into
`$HERMES_HOME/plugins/` and installs `pip_dependencies` from the manifest):
```bash
hermes plugins install Xyzjesus/hermes-dakera   # or the full Git URL
hermes plugins list                           # dakera must appear, enabled
```
Manual alternative (drop-in):
```bash
mkdir -p ~/.hermes/plugins
cp -R /path/to/hermes-dakera/plugin/dakera ~/.hermes/plugins/
# or fetch the two files directly:
curl -fsSLo ~/.hermes/plugins/dakera/__init__.py https://raw.githubusercontent.com/Xyzjesus/hermes-dakera/main/plugin/dakera/__init__.py
curl -fsSLo ~/.hermes/plugins/dakera/plugin.yaml https://raw.githubusercontent.com/Xyzjesus/hermes-dakera/main/plugin/dakera/plugin.yaml
hermes plugins doctor   # validates layout + manifest
```
The `dakera` SDK (`dakera>=0.12.11,<0.13`) is installed automatically via the
manifest's `pip_dependencies` — no manual pip.

### Step 2 — Configure credentials
```bash
mkdir -p ~/.hermes/dakera
umask 077
cat > ~/.hermes/dakera/config.json <<'EOF'
{"api_url": "http://YOUR-DAKERA-HOST:3000", "api_key": "YOUR-TOKEN", "agent_id": "hermes", "top_k": 5, "recall_routing": "auto"}
EOF
chmod 600 ~/.hermes/dakera/config.json
# then edit the two placeholders: api_url and api_key
```
Or set them from the environment instead of hand-editing:
```bash
python3 - <<'PY'
import json, pathlib, os
p = pathlib.Path.home() / ".hermes/dakera/config.json"
cfg = json.loads(p.read_text())
cfg["api_url"] = os.environ["DAKERA_API_URL"]
cfg["api_key"] = os.environ["DAKERA_API_KEY"]
p.chmod(0o600)
p.write_text(json.dumps(cfg))
PY
```
Prefer `hermes memory setup` for interactive use — it writes the same file
via the plugin's `get_config_schema`/`save_config`.

### Step 3 — Activate
Create or edit `~/.hermes/config.yaml` so it contains:
```yaml
memory:
  provider: dakera
```
This is a two-line YAML edit the installing agent should make with its file
editing tools (do not `echo >>` into the config): add the `memory:` block if
missing, otherwise change the existing `provider:` line.

### Step 4 — Verify
```bash
hermes memory status        # provider: dakera, active: true
hermes -z "call the dakera_store tool with content 'install test' and then dakera_recall 'install test'"
```
Expected: tool round-trip returns the stored memory with a score. Check logs
for `dakera provider init failed` — its absence means the SDK connected.

Optional live server check (no Hermes involved) — recall must return 200:
```bash
curl -fsS -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer $DAKERA_API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"ping","top_k":1,"agent_id":"hermes"}' \
  "$DAKERA_API_URL/v1/memory/recall"
```

### Uninstall
```bash
hermes plugins remove dakera || rm -rf ~/.hermes/plugins/dakera
# and remove `memory.provider: dakera` from ~/.hermes/config.yaml
```
Memories stay on the Dakera server (scoped by `agent_id`) — deleting the
plugin does not delete data.

---

## ⚙️ Configuration reference

| Key | Env | Default | Description |
|---|---|---|---|
| `api_url` | `DAKERA_API_URL` | `http://localhost:3000` | Dakera server URL |
| `api_key` | `DAKERA_API_KEY` | — | root or scoped key (secret scope first) |
| `agent_id` | — | `hermes` | isolation unit |
| `top_k` | — | `5` | recall result count |
| `recall_routing` | — | `auto` | `auto`/`vector`/`bm25`/`hybrid` |

Resolution order for api_url/api_key: **env → `$HERMES_HOME/dakera/config.json` → default**;
api_key additionally: **secret scope first**, then env, then file.

---

## 🧪 Development

```bash
pytest tests/ -q
```

Layout: `plugin/dakera/__init__.py` (provider + tools),
`plugin/dakera/plugin.yaml` (manifest), `tests/test_dakera_provider.py`,
`research/en/dakera-memory-plugin.md` (design spec with citations).
