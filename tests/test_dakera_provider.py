import json
from pathlib import Path
from dakera_provider import DakeraMemoryProvider

def _write_config(home: Path, **over):
    d = home / "dakera"; d.mkdir(parents=True, exist_ok=True)
    cfg = {"api_url": "http://test.local:3000", "api_key": "k", "agent_id": "hermes-test",
           "top_k": 5, "recall_routing": "auto"}
    cfg.update(over)
    (d / "config.json").write_text(json.dumps(cfg))

def test_name_is_dakera():
    assert DakeraMemoryProvider(client=object()).name == "dakera"

def test_is_available_config_only_no_network(tmp_path, monkeypatch):
    monkeypatch.delenv("DAKERA_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert not DakeraMemoryProvider(client=object()).is_available()
    _write_config(tmp_path)
    assert DakeraMemoryProvider(client=object()).is_available()

def test_initialize_builds_client_and_scopes_agent(tmp_path):
    _write_config(tmp_path, agent_id="hermes-test")
    calls = []
    class FakeClient:
        def __init__(self, **kw): calls.append(kw)
    p = DakeraMemoryProvider(client=None)
    p.initialize("s1", hermes_home=str(tmp_path), platform="cli", client_factory=FakeClient)
    assert calls[0]["base_url"] == "http://test.local:3000"
    assert p._agent_id == "hermes-test"

def test_initialize_disables_writes_for_non_primary(tmp_path):
    _write_config(tmp_path)
    p = DakeraMemoryProvider(client=object())
    p.initialize("s1", hermes_home=str(tmp_path), platform="cli", agent_context="subagent")
    assert p._can_write() is False

def test_config_from_env_overrides_file(tmp_path, monkeypatch):
    _write_config(tmp_path, api_key="file-key")
    monkeypatch.setenv("DAKERA_API_KEY", "env-key")
    from dakera_provider import _load_config
    cfg = _load_config(str(tmp_path))
    assert cfg["api_key"] == "env-key"

def test_malformed_top_k_falls_back_and_availability_ok(tmp_path, monkeypatch):
    _write_config(tmp_path, top_k="many")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from dakera_provider import _load_config
    cfg = _load_config(str(tmp_path))
    assert cfg["top_k"] == 5
    _write_config(tmp_path, top_k=["a"])
    assert _load_config(str(tmp_path))["top_k"] == 5
    assert DakeraMemoryProvider(client=object()).is_available() is True

# -- recall path (Task 2) ------------------------------------------------
import threading
import time

class FakeRecalled:
    def __init__(self, content, score=0.9): self.content, self.score = content, score

class FakeRecallResponse:
    def __init__(self, memories): self.memories = memories

class FakeRecallClient:
    def __init__(self, memories, delay=0.0):
        self.memories, self.delay, self.calls = memories, delay, []
    def recall(self, agent_id, query, **kw):
        self.calls.append((agent_id, query, kw))
        if self.delay: threading.Event().wait(self.delay)
        return FakeRecallResponse([FakeRecalled(m) for m in self.memories])

def _ready(p, tmp_path, client):
    _write_config(tmp_path)
    p.initialize("s1", hermes_home=str(tmp_path), platform="cli")
    p._client = client
    p._active = True
    return p

def test_prefetch_formats_memories(tmp_path):
    c = FakeRecallClient(["likes rust", "uses bun"])
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    out = p.prefetch("favorite language?", session_id="s1")
    assert "likes rust" in out and "uses bun" in out
    assert c.calls[0][0] == "hermes-test"
    assert c.calls[0][2]["top_k"] == 5
    st = p.recall_status()
    assert st.count == 2 and st.provider_label == "Dakera"

def test_prefetch_empty_returns_no_indicator(tmp_path):
    # Contract (agent/memory_provider.py): recall_status() is None => "no indicator";
    # host describe_recall renders an indicator for ANY non-None status, so a
    # RecallStatus with count 0 would falsely claim "recalled relevant memory".
    p = _ready(DakeraMemoryProvider(), tmp_path, FakeRecallClient([]))
    assert p.prefetch("anything", session_id="s1") == ""
    assert p.recall_status() is None

def test_prefetch_failure_is_swallowed(tmp_path):
    class Boom:
        def recall(self, *a, **k): raise RuntimeError("down")
    p = _ready(DakeraMemoryProvider(), tmp_path, Boom())
    assert p.prefetch("q", session_id="s1") == ""
    assert p.recall_status() is None

def test_system_prompt_block_active(tmp_path):
    p = _ready(DakeraMemoryProvider(), tmp_path, FakeRecallClient([]))
    block = p.system_prompt_block()
    assert "dakera_recall" in block and "dakera_store" in block

def test_prefetch_blank_query_keeps_previous_status(tmp_path):
    c = FakeRecallClient(["kept"])
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    assert p.prefetch("real query", session_id="s1")
    first = p.recall_status()
    assert first.count == 1
    assert p.prefetch("   ", session_id="s1") == ""
    assert c.calls  # blank query never reached the client
    assert len(c.calls) == 1
    st = p.recall_status()
    assert st.count == 1 and st.provider_label == first.provider_label

def test_queue_prefetch_runs_in_background(tmp_path):
    # Regression: spawn_context_thread returns an UNSTARTED thread; queue_prefetch
    # must .start() it or the queued recall never executes.
    c = FakeRecallClient(["bg memory"])
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    p.queue_prefetch("bg query", session_id="s1")
    deadline = time.monotonic() + 2.0
    while len(c.calls) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(c.calls) == 1
    assert c.calls[0][1] == "bg query"
    assert p.prefetch("bg query", session_id="s1")  # consumed cache; count comes from cache
    assert p.recall_status().count == 1

def test_queue_prefetch_cache_serves_next_prefetch_without_second_recall(tmp_path):
    # Finding 2: queue_prefetch runs a recall whose block must be cached and
    # consumed by the next prefetch with the SAME query; no second recall call.
    c = FakeRecallClient(["cached memory"])
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    p.queue_prefetch("the query", session_id="s1")
    deadline = time.monotonic() + 2.0
    while len(c.calls) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(c.calls) == 1  # background recall ran
    block = p.prefetch("the query", session_id="s1")
    assert "cached memory" in block
    assert len(c.calls) == 1  # EXACTLY ONE recall: cache hit, no sync re-recall
    assert p.recall_status().count == 1  # status updated from the cached block

def test_queue_prefetch_cache_miss_falls_back_to_sync_recall(tmp_path):
    # Guard: cache serves only an exact query match; a different query must
    # trigger a fresh synchronous recall.
    c = FakeRecallClient(["bg memory"])
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    p.queue_prefetch("query a", session_id="s1")
    deadline = time.monotonic() + 2.0
    while len(c.calls) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    block = p.prefetch("query b", session_id="s1")
    assert "bg memory" in block
    assert len(c.calls) == 2  # queued recall + sync recall for the new query

# -- retain path (Task 3) ------------------------------------------------

class FakeStoreClient(FakeRecallClient):
    def __init__(self):
        super().__init__([])
        self.stores = []
    def store_memory(self, agent_id, content, **kw):
        self.stores.append((agent_id, content, kw))
        return {"id": "m1"}

def test_sync_turn_stores_one_episodic_memory(tmp_path):
    c = FakeStoreClient()
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    p.sync_turn("what is bun?", "bun is a JS runtime", session_id="s1")
    assert len(c.stores) == 1
    aid, content, kw = c.stores[0]
    assert aid == "hermes-test" and "what is bun?" in content and "bun is a JS runtime" in content
    assert kw["memory_type"] == "episodic" and kw["importance"] == 0.6
    assert "user" in kw["tags"]

def test_sync_turn_skipped_when_cannot_write(tmp_path):
    c = FakeStoreClient()
    p = DakeraMemoryProvider()
    p.initialize("s1", hermes_home=str(tmp_path), platform="cli", agent_context="cron")
    p._client, p._active = c, True
    p.sync_turn("u", "a", session_id="s1")
    assert c.stores == []

def test_sync_turn_skips_blank_pair(tmp_path):
    # Regression: gate must run on stripped CONTENT lengths BEFORE formatting;
    # formatted labels alone are 19 chars, so a content-level gate is the only
    # one that can skip empty/trivial turns.
    c = FakeStoreClient()
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    p.sync_turn("   ", "", session_id="s1")
    assert c.stores == []

def test_sync_turn_skips_short_content_pair(tmp_path):
    # "hi" + "ok" = 4 chars of content: below the 16-char retain floor even
    # though the formatted text (23 chars) would pass a post-format gate.
    c = FakeStoreClient()
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    p.sync_turn("hi", "ok", session_id="s1")
    assert c.stores == []

def test_on_session_end_stores_summary(tmp_path):
    # Regression: spawn_context_thread returns an UNSTARTED thread; on_session_end
    # must .start() it, so poll like the queue_prefetch regression test.
    c = FakeStoreClient()
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    p.on_session_end([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    deadline = time.monotonic() + 2.0
    while len(c.stores) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(c.stores) == 1
    assert c.stores[0][2]["memory_type"] == "episodic"
    assert c.stores[0][2]["importance"] == 0.7
    assert "session-end" in c.stores[0][2]["tags"]

def test_on_session_end_flush_thread_is_non_daemon(tmp_path):
    # Finding 4: the summary store must survive process exit right after session
    # end, so the flush thread must be non-daemon (spawn_context_thread daemon=False).
    c = FakeStoreClient()
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    p.on_session_end([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    t = p._last_flush_thread
    assert t is not None
    assert t.daemon is False
    t.join(timeout=2.0)  # deterministic: wait for the store to land
    assert len(c.stores) == 1

# -- agent tools (Task 4) --------------------------------------------------

def _schemas(p):
    return {s["name"]: s for s in p.get_tool_schemas()}

def test_tool_schemas_present(tmp_path):
    p = _ready(DakeraMemoryProvider(), tmp_path, FakeRecallClient([]))
    s = _schemas(p)
    assert {"dakera_recall", "dakera_store", "dakera_search", "dakera_forget"} <= set(s)
    assert s["dakera_recall"]["parameters"]["properties"]["query"]["type"] == "string"

def test_tool_schemas_mutation_isolated(tmp_path):
    # Regression: consumers mutating a returned schema must not corrupt the
    # provider's class-level _SCHEMAS (deep copy, not shared nested dicts).
    p = _ready(DakeraMemoryProvider(), tmp_path, FakeRecallClient([]))
    s1 = p.get_tool_schemas()
    s1[0]["name"] = "mutated"
    s1[0]["parameters"]["properties"]["x"] = {"type": "string"}
    s2 = p.get_tool_schemas()
    assert s2[0]["name"] == "dakera_recall"
    assert "x" not in s2[0]["parameters"]["properties"]

def test_handle_recall(tmp_path):
    c = FakeRecallClient(["fact one"])
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    out = json.loads(p.handle_tool_call("dakera_recall", {"query": "q"}))
    assert "fact one" in json.dumps(out)
    assert c.calls[0][0] == "hermes-test" and c.calls[0][1] == "q"

def test_handle_store_and_forget(tmp_path):
    c = FakeStoreClient()
    c.forgets = []
    def _forget(agent_id, memory_id):  # SDK forget is single-id: one call per memory
        c.forgets.append(memory_id)
        return {"ok": True}
    c.forget = _forget
    p = _ready(DakeraMemoryProvider(), tmp_path, c)
    out = json.loads(p.handle_tool_call("dakera_store", {"content": "user prefers zsh", "importance": 0.9}))
    assert out.get("ok") and c.stores[0][1] == "user prefers zsh"
    assert c.stores[0][2]["importance"] == 0.9 and c.stores[0][2]["memory_type"] == "semantic"
    out = json.loads(p.handle_tool_call("dakera_forget", {"memory_ids": ["a", "b"]}))
    assert out["ok"] and out["forgotten"] == ["a", "b"]
    assert c.forgets == ["a", "b"]  # one SDK forget call per id, both reached

def test_handle_error_path(tmp_path):
    class Boom(FakeRecallClient):
        def recall(self, *a, **k): raise RuntimeError("503")
    p = _ready(DakeraMemoryProvider(), tmp_path, Boom([]))
    out = json.loads(p.handle_tool_call("dakera_recall", {"query": "q"}))
    assert "error" in out

def test_handle_unknown_tool(tmp_path):
    p = _ready(DakeraMemoryProvider(), tmp_path, FakeRecallClient([]))
    assert "error" in p.handle_tool_call("dakera_nope", {})

# -- setup UX (Task 5) ------------------------------------------------
import os
def test_config_schema_shape():
    schema = DakeraMemoryProvider(client=object()).get_config_schema()
    assert isinstance(schema, list) and schema
    assert all(isinstance(e, dict) and "key" in e and "description" in e for e in schema)
    by_key = {e["key"]: e for e in schema}
    assert by_key["api_key"]["secret"] is True
    assert by_key["api_key"]["env_var"] == "DAKERA_API_KEY"
    assert {"auto", "hybrid"} <= set(by_key["recall_routing"]["choices"])

def test_save_config_merges_and_restricts(tmp_path):
    _write_config(tmp_path, api_url="http://seed:1")
    p = DakeraMemoryProvider(client=object())
    p.save_config({"top_k": 7}, str(tmp_path))
    path = tmp_path / "dakera" / "config.json"
    saved = json.loads(path.read_text())
    assert saved["api_url"] == "http://seed:1" and saved["top_k"] == 7
    assert os.stat(path).st_mode & 0o777 == 0o600
