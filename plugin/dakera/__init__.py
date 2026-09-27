"""Dakera memory plugin (MemoryProvider): hybrid recall, per-turn retain, dakera_* tools."""
from __future__ import annotations
import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error
logger = logging.getLogger(__name__)
DEFAULT_API_URL = "http://localhost:3000"
DEFAULT_AGENT_ID = "hermes"
_WRITE_EXCLUDED_CONTEXTS = {"cron", "flush", "subagent"}
# No per-call recall timeout here: the dakera SDK recall() takes none, and the
# host's 8 s prefetch join bound (memory_manager.py) supersedes a per-call budget.
_STORE_TIMEOUT = 30.0

def _read_json_or_empty(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}

def _load_config(hermes_home: str) -> dict:
    """Resolution: env DAKERA_API_URL / DAKERA_API_KEY > $HERMES_HOME/dakera/config.json > defaults."""
    path = Path(hermes_home) / "dakera" / "config.json"
    raw = _read_json_or_empty(path)
    try:
        top_k = int(raw.get("top_k") or 5)
    except (ValueError, TypeError):
        top_k = 5
    return {
        "api_url": os.environ.get("DAKERA_API_URL") or raw.get("api_url") or DEFAULT_API_URL,
        "api_key": os.environ.get("DAKERA_API_KEY") or raw.get("api_key") or "",
        "agent_id": raw.get("agent_id") or DEFAULT_AGENT_ID,
        "top_k": top_k,
        "recall_routing": raw.get("recall_routing") or "auto",
    }

class DakeraMemoryProvider(MemoryProvider):
    """MemoryProvider backed by a self-hosted Dakera server via the dakera SDK."""
    def __init__(self, client: Any = None, config: Optional[dict] = None):
        self._injected_client = client
        self._injected_config = config
        self._client = None
        self._agent_id = DEFAULT_AGENT_ID
        self._api_url = DEFAULT_API_URL
        self._top_k = 5
        self._recall_routing = "auto"
        self._write_enabled = True
        self._active = False
        self._unavailable_reason = ""
        self._session_id = ""
        self._hermes_home = ""
        self._last_count = 0
        self._lock = threading.Lock()
        self._last_flush_thread: Optional[threading.Thread] = None
        # Finding 2 one-slot prefetch cache: (query, block) queued by
        # queue_prefetch's background recall, consumed by the next prefetch.
        self._pending: Optional[tuple] = None

    # -- lifecycle -------------------------------------------------------
    @property
    def name(self) -> str:
        return "dakera"

    def is_available(self) -> bool:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        cfg = self._injected_config or _load_config(home)
        return bool(cfg.get("api_key", "")) and bool(cfg.get("agent_id"))

    def initialize(self, session_id: str, **kwargs) -> None:
        from agent.secret_scope import get_secret
        self._hermes_home = kwargs.get("hermes_home") or self._hermes_home or str(
            Path.home() / ".hermes")
        self._session_id = session_id
        cfg = self._injected_config or _load_config(self._hermes_home)
        cfg["api_key"] = get_secret("DAKERA_API_KEY", "") or cfg["api_key"]
        self._api_url = cfg["api_url"]
        self._agent_id = cfg["agent_id"]
        self._top_k = cfg["top_k"]
        self._recall_routing = cfg["recall_routing"]
        self._write_enabled = kwargs.get("agent_context", "primary") not in _WRITE_EXCLUDED_CONTEXTS
        factory: Callable[..., Any] = kwargs.get("client_factory") or self._default_client_factory(cfg)
        try:
            self._client = self._injected_client or factory(
                base_url=cfg["api_url"], api_key=cfg["api_key"] or None, timeout=_STORE_TIMEOUT)
            self._active = self._client is not None
        except Exception as exc:  # fail open: memory must not kill the agent
            self._client, self._active = None, False
            self._unavailable_reason = f"client init failed: {exc}"
            logger.warning("dakera provider init failed: %s", exc)

    @staticmethod
    def _default_client_factory(cfg: dict) -> Callable[..., Any]:
        def _build(base_url: str, api_key: Optional[str], timeout: float) -> Any:
            from dakera import DakeraClient
            return DakeraClient(base_url=base_url, api_key=api_key, timeout=timeout)
        return _build

    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    # -- setup UX ---------------------------------------------------------
    def get_config_schema(self):
        return [
            {"key": "api_url", "description": "Dakera server URL", "default": DEFAULT_API_URL,
             "required": True},
            {"key": "api_key", "description": "Dakera API key (server DAKERA_ROOT_API_KEY or scoped key)",
             "secret": True, "required": True, "env_var": "DAKERA_API_KEY"},
            {"key": "agent_id", "description": "Dakera agent_id (isolation unit)",
             "default": DEFAULT_AGENT_ID},
            {"key": "top_k", "description": "Recall result count", "default": 5},
            {"key": "recall_routing", "description": "Recall routing mode",
             "default": "auto", "choices": ["auto", "vector", "bm25", "hybrid"]},
        ]

    def save_config(self, values, hermes_home):
        from utils import atomic_json_write
        path = Path(hermes_home) / "dakera" / "config.json"
        atomic_json_write(path, {**_read_json_or_empty(path), **values}, mode=0o600)

    # -- agent tools -----------------------------------------------------
    _SCHEMAS = [
        {"name": "dakera_recall",
         "description": "Search durable long-term memory for facts relevant to a query. Returns top matches with scores.",
         "parameters": {"type": "object", "properties": {
             "query": {"type": "string", "description": "What to recall."},
             "top_k": {"type": "integer", "description": "Max results (default 5)."}},
             "required": ["query"]}},
        {"name": "dakera_store",
         "description": "Persist a durable fact to long-term memory (semantic by default). Use for preferences, decisions, project facts.",
         "parameters": {"type": "object", "properties": {
             "content": {"type": "string", "description": "The fact, self-contained."},
             "importance": {"type": "number", "description": "0.0-1.0; 0.9+ for explicit 'remember this'."},
             "memory_type": {"type": "string", "enum": ["episodic", "semantic", "procedural", "working"]}},
             "required": ["content"]}},
        {"name": "dakera_search",
         "description": "Browse long-term memory lexically (BM25) by keyword; use when recall misses or for inventory.",
         "parameters": {"type": "object", "properties": {
             "query": {"type": "string"}, "limit": {"type": "integer"}},
             "required": ["query"]}},
        {"name": "dakera_forget",
         "description": "Delete memories by exact id (from dakera_recall/dakera_search results).",
         "parameters": {"type": "object", "properties": {
             "memory_ids": {"type": "array", "items": {"type": "string"}}},
             "required": ["memory_ids"]}},
    ]

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            return self._dispatch_tool(tool_name, args or {})
        except Exception as exc:
            logger.warning("dakera tool %s failed: %s", tool_name, exc)
            return tool_error(f"{tool_name} failed: {exc}")

    def _dispatch_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        if not self._active or self._client is None:
            return tool_error("dakera provider inactive")
        if tool_name == "dakera_recall":
            return self._tool_recall(args)
        if tool_name == "dakera_store":
            return self._tool_store(args)
        if tool_name == "dakera_search":
            return self._tool_search(args)
        if tool_name == "dakera_forget":
            return self._tool_forget(args)
        return tool_error(f"unknown tool: {tool_name}")

    def _tool_recall(self, args: Dict[str, Any]) -> str:
        query = str(args.get("query", ""))
        if not query.strip():
            return tool_error("dakera_recall: query is required")
        resp = self._client.recall(
            self._agent_id, query,
            top_k=int(args.get("top_k") or self._top_k),
            routing=self._recall_routing)
        mems = list(getattr(resp, "memories", None) or [])
        return json.dumps({"memories": [
            {"id": getattr(m, "id", None),
             "content": getattr(m, "content", str(m)),
             "score": getattr(m, "score", None)}
            for m in mems]})

    def _tool_store(self, args: Dict[str, Any]) -> str:
        if not self._can_write():
            return tool_error("dakera store disabled for this session")
        content = str(args.get("content", ""))
        if not content.strip():
            return tool_error("dakera_store: content is required")
        try:
            importance = float(args["importance"]) if "importance" in args else None
        except (TypeError, ValueError):
            return tool_error("dakera_store: importance must be a number 0.0-1.0")
        memory_type = str(args.get("memory_type") or "semantic")
        result = self._client.store_memory(
            self._agent_id, content, memory_type=memory_type,
            importance=importance,
            tags=["hermes", "explicit"],
            session_id=self._session_id or None,
            metadata={"hermes_session": self._session_id})
        return json.dumps({"ok": True, "result": result})

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = str(args.get("query", ""))
        if not query.strip():
            return tool_error("dakera_search: query is required")
        res = self._client.search_memories(
            self._agent_id, query,
            top_k=int(args.get("limit") or 10))
        return json.dumps({"results": res})

    def _tool_forget(self, args: Dict[str, Any]) -> str:
        ids = args.get("memory_ids") or []
        if not isinstance(ids, list) or not ids:
            return tool_error("dakera_forget: memory_ids must be a non-empty list")
        forgotten, errors = [], {}
        for mid in ids:  # SDK forget is single-id: loop per id
            try:
                self._client.forget(self._agent_id, str(mid))
                forgotten.append(str(mid))
            except Exception as exc:
                errors[str(mid)] = str(exc)
        return json.dumps({"ok": not errors, "forgotten": forgotten, "errors": errors})

    def _can_write(self) -> bool:
        return bool(self._active and self._write_enabled and self._client is not None)

    # -- recall ----------------------------------------------------------
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._active or not query.strip():
            return ""
        # One-slot cache: consume the block queued by queue_prefetch's background
        # recall, but only for an exact query match (else fall back to sync recall).
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None and pending[0] == query:
            return pending[1]
        try:
            resp = self._client.recall(
                self._agent_id, query, top_k=self._top_k,
                routing=self._recall_routing)
            mems = list(getattr(resp, "memories", None) or [])
        except Exception as exc:  # fail open: recall must not kill the agent
            logger.warning("dakera recall failed: %s", exc)
            mems = []
        with self._lock:
            self._last_count = len(mems)
        if not mems:
            return ""
        lines = ["<dakera-memory>"]
        for i, m in enumerate(mems, 1):
            score = getattr(m, "score", None)
            score_s = f" (score {score:.2f})" if isinstance(score, (int, float)) else ""
            lines.append(f"{i}. {getattr(m, 'content', str(m))}{score_s}")
        lines.append("</dakera-memory>")
        return "\n".join(lines)

    def recall_status(self):
        from agent.memory_provider import RecallStatus
        count = getattr(self, "_last_count", 0)
        # None = "no indicator" (memory_provider.py): a count-0 status would make
        # describe_recall claim "recalled relevant memory" with nothing injected.
        return RecallStatus(provider_label="Dakera", count=count) if count else None

    def system_prompt_block(self) -> str:
        if not self._active:
            return ""
        return ("# Dakera\nActive. Long-term memory server. Use dakera_recall (search past "
                "context), dakera_store (save a durable fact), dakera_search (browse), and "
                "dakera_forget (delete by id) when the user asks to remember or forget.")

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        from agent.memory_provider import spawn_context_thread

        def _prefetch_into_cache():
            block = self.prefetch(query, session_id=session_id)
            with self._lock:  # one-slot cache consumed by the next prefetch
                self._pending = (query, block)

        spawn_context_thread(_prefetch_into_cache,
                             name="dakera-queue-prefetch").start()

    def shutdown(self) -> None:
        client, self._client, self._active = self._client, None, False
        try:
            if client is not None and hasattr(client, "close"):
                client.close()
        except Exception:
            pass

    # -- retain ----------------------------------------------------------
    def _store(self, content: str, *, memory_type: str = "episodic",
               importance: float = 0.6, tags: Optional[List[str]] = None,
               session_id: str = "") -> None:
        client = self._client
        try:
            client.store_memory(
                self._agent_id, content, memory_type=memory_type,
                importance=importance,
                tags=["hermes"] + (tags or []),
                session_id=session_id or self._session_id or None,
                metadata={"hermes_session": session_id or self._session_id})
        except Exception as exc:
            logger.warning("dakera store failed: %s", exc)

    # messages/turn_author (and any future extras) are accepted-and-ignored:
    # pair-mode policy stores only the user/assistant content pair.
    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "",
                  messages: Optional[List[Dict[str, Any]]] = None,
                  turn_author: Optional[Dict[str, Any]] = None, **kwargs) -> None:
        if not self._can_write():
            return
        u = (user_content or "").strip()
        a = (assistant_content or "").strip()
        if len(u) + len(a) < 16:  # noise gate on CONTENT, before formatting
            return
        text = f"[user] {u}\n[assistant] {a}"
        self._store(text, tags=["user", "assistant"], session_id=session_id)

    def on_session_end(self, messages) -> None:
        if not self._can_write() or not messages:
            return
        tail = messages[-6:]
        summary = "\n".join(
            f"[{m.get('role', '?')}] {str(m.get('content', ''))[:400]}" for m in tail)
        from agent.memory_provider import spawn_context_thread
        # spawn_context_thread returns an UNSTARTED thread (Task 2 lesson).
        # daemon=False: the summary store must survive process exit right after
        # session end (same contract as supermemory).
        t = spawn_context_thread(
            lambda: self._store(f"[session summary]\n{summary}",
                                memory_type="episodic", importance=0.7,
                                tags=["session-end"]),
            name="dakera-session-end", daemon=False)
        self._last_flush_thread = t
        t.start()

def register(ctx):
    ctx.register_memory_provider(DakeraMemoryProvider())
