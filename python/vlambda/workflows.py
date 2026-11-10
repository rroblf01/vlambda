"""Persistent workflows of lambdas.

A workflow is a validated directed graph of existing lambdas.  It does not
contain code: every node invokes a lambda that already exists and is owned by
the same user.  This keeps the feature an orchestration layer rather than a
new execution surface.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from vlambda.dbpool import get as db_get

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MODES = {"trigger", "join", "first", "any"}
_MAX_WORKFLOWS = int(os.environ.get("VLAMBDA_MAX_WORKFLOWS", "20"))
_MAX_NODES = int(os.environ.get("VLAMBDA_MAX_WORKFLOW_NODES", "50"))
_MAX_EDGES = int(os.environ.get("VLAMBDA_MAX_WORKFLOW_EDGES", "100"))
_MAX_FANOUT = int(os.environ.get("VLAMBDA_MAX_WORKFLOW_FANOUT", "10"))
_MAX_DEPTH = int(os.environ.get("VLAMBDA_MAX_WORKFLOW_DEPTH", "20"))
_MAX_PARALLEL = int(os.environ.get("VLAMBDA_MAX_WORKFLOW_PARALLEL", "4"))
_MAX_RUNS = int(os.environ.get("VLAMBDA_MAX_WORKFLOW_RUNS", "4"))
_MAX_INPUT_BYTES = 256 * 1024
_MAX_OUTPUT_BYTES = 1 * 1024 * 1024
_MAX_CONTEXT_BYTES = 8 * 1024 * 1024

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS workflows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        definition TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(owner_id, name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_workflows_owner ON workflows(owner_id, name)",
    """CREATE TABLE IF NOT EXISTS workflow_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        trigger_type TEXT NOT NULL DEFAULT 'manual',
        input_json TEXT NOT NULL DEFAULT '{}',
        context_json TEXT NOT NULL DEFAULT '{}',
        started_at REAL NOT NULL,
        finished_at REAL,
        error TEXT,
        attempt INTEGER NOT NULL DEFAULT 1
    )""",
    "CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow ON workflow_runs(workflow_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_runs_owner ON workflow_runs(owner_id, id)",
    """CREATE TABLE IF NOT EXISTS workflow_node_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        node_id TEXT NOT NULL,
        invocation_id TEXT NOT NULL,
        status TEXT NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 1,
        input_json TEXT,
        output_json TEXT,
        started_at REAL,
        finished_at REAL,
        error TEXT,
        lambda_slug TEXT NOT NULL DEFAULT '',
        request_id TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_workflow_node_runs_run ON workflow_node_runs(run_id, id)",
)

_pool = None
_args = None
_runs: dict[int, "_Run"] = {}
_runs_lock = threading.Lock()
_run_semaphore = threading.BoundedSemaphore(_MAX_RUNS)


def _db_path() -> str:
    return os.environ.get("VLAMBDA_DB", "./vlambda.db")


def configure(pool, args) -> None:
    global _pool, _args
    _pool = pool
    _args = args


def ensure_schema() -> None:
    with db_get(_db_path()) as conn:
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.commit()


def _owned_slug(conn, slug: str, owner_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM lambdas WHERE slug = ? AND owner_id = ?", (slug, int(owner_id))
    ).fetchone()
    return row is not None


def _validate_value(value, path: str) -> str | None:
    if value is None or isinstance(value, (bool, int, float, str)):
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            problem = _validate_value(item, f"{path}[{index}]")
            if problem:
                return problem
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path} keys must be strings"
            problem = _validate_value(item, f"{path}.{key}")
            if problem:
                return problem
        return None
    return f"{path} must be JSON-compatible"


def _validate_mapping(mapping, path: str) -> str | None:
    if mapping is None:
        return None
    if not isinstance(mapping, dict):
        return f"{path} must be an object"
    for key, value in mapping.items():
        if not isinstance(key, str):
            return f"{path} keys must be strings"
        problem = _validate_value(value, f"{path}.{key}")
        if problem:
            return problem
    return None


def _resolve_path(context: dict, expression: str):
    if not isinstance(expression, str) or not expression.startswith("$."):
        return expression
    current = context
    for part in expression[2:].split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if 0 <= index < len(current):
                current = current[index]
            else:
                return None
        else:
            return None
    return current


def _resolve_refs(value, context: dict):
    if isinstance(value, str) and value.startswith("$."):
        return _resolve_path(context, value)
    if isinstance(value, dict):
        return {key: _resolve_refs(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(item, context) for item in value]
    return value


def _build_body(value) -> bytes:
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _build_query(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return urlencode(value, doseq=True)
    return str(value)


def _validate_condition(condition) -> str | None:
    if condition is None:
        return None
    if not isinstance(condition, dict):
        return "condition must be an object"
    allowed = {"path", "equals", "not_equals", "exists", "truthy", "greater_than", "less_than", "in"}
    if set(condition) - allowed:
        return f"unknown condition keys: {', '.join(sorted(set(condition) - allowed))}"
    if "path" not in condition:
        return "condition requires 'path'"
    if not isinstance(condition["path"], str) or not condition["path"].startswith("$."):
        return "condition 'path' must be a JSON reference"
    return None


def _validate_definition(owner_id: int, raw: dict) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, "body must be a JSON object"

    name = str(raw.get("name") or "").strip()
    if not _NAME_RE.match(name):
        return None, "invalid workflow name"

    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        return None, "nodes must be a non-empty array"
    if len(nodes_raw) > _MAX_NODES:
        return None, f"too many nodes (max {_MAX_NODES})"

    nodes: list[dict] = []
    ids: set[str] = set()
    for index, item in enumerate(nodes_raw):
        if not isinstance(item, dict):
            return None, f"node {index} must be an object"
        node_id = str(item.get("id") or "").strip()
        if not node_id or node_id in ids:
            return None, f"duplicate or missing node id: {node_id or '<empty>'}"
        ids.add(node_id)

        node_type = str(item.get("type") or "lambda").strip().lower()
        if node_type != "lambda":
            return None, f"node {node_id}: unsupported type {node_type!r}"

        slug = str(item.get("slug") or "").strip()
        if not slug:
            return None, f"node {node_id}: missing slug"
        with db_get(_db_path()) as conn:
            if not _owned_slug(conn, slug, owner_id):
                return None, f"node {node_id}: lambda not found: {slug}"

        mode = str(item.get("mode") or "trigger").strip().lower()
        if mode not in _MODES:
            return None, f"node {node_id}: invalid mode {mode!r}"

        timeout = int(item.get("timeout", 0) or 0)
        if timeout < 0 or timeout > 3600:
            return None, f"node {node_id}: timeout must be between 0 and 3600"
        retry = int(item.get("retry", 0) or 0)
        if retry < 0 or retry > 5:
            return None, f"node {node_id}: retry must be between 0 and 5"
        backoff = int(item.get("backoff", 2) or 2)
        if backoff < 0 or backoff > 300:
            return None, f"node {node_id}: backoff must be between 0 and 300"
        success_statuses = item.get("success_statuses", [200, 201, 202, 204])
        if not isinstance(success_statuses, list) or not all(isinstance(x, int) for x in success_statuses):
            return None, f"node {node_id}: success_statuses must be integers"
        if item.get("input") is not None:
            problem = _validate_mapping(item.get("input"), f"node {node_id}.input")
            if problem:
                return None, problem

        max_parallel = int(item.get("max_parallel", 1) or 1)
        if max_parallel < 1 or max_parallel > _MAX_FANOUT:
            return None, f"node {node_id}: max_parallel must be between 1 and {_MAX_FANOUT}"

        next_nodes = item.get("next", [])
        if not isinstance(next_nodes, list) or not all(isinstance(x, str) for x in next_nodes):
            return None, f"node {node_id}: next must be an array of strings"
        next_nodes = list(dict.fromkeys(x.strip() for x in next_nodes if x.strip()))
        if len(next_nodes) > _MAX_FANOUT:
            return None, f"node {node_id}: too many successors (max {_MAX_FANOUT})"

        waits_for = item.get("waits_for", [])
        if not isinstance(waits_for, list) or not all(isinstance(x, str) for x in waits_for):
            return None, f"node {node_id}: waits_for must be an array of strings"
        waits_for = list(dict.fromkeys(x.strip() for x in waits_for if x.strip()))
        if waits_for and mode != "join":
            return None, f"node {node_id}: waits_for requires mode 'join'"

        next_edges = []
        for target in next_nodes:
            if target == node_id:
                return None, f"node {node_id}: cannot point to itself"
            # The target's mode decides whether this is a trigger or a join edge.
            # A node declared as "join" waits for its predecessors instead of
            # running once per incoming activation.
            target_mode = str(next((n.get("mode", "trigger") for n in nodes_raw if str(n.get("id") or "").strip() == target), "trigger")).strip().lower()
            edge_kind = "dependency" if target_mode == "join" else "trigger"
            edge = {"from": node_id, "to": target, "kind": edge_kind}
            if item.get("next_when") is not None:
                problem = _validate_condition(item.get("next_when"))
                if problem:
                    return None, f"node {node_id}: {problem}"
                edge["when"] = item.get("next_when")
            next_edges.append(edge)

        dep_edges = []
        for source in waits_for:
            if source == node_id:
                return None, f"node {node_id}: cannot wait for itself"
            edge = {"from": source, "to": node_id, "kind": "dependency"}
            dep_edges.append(edge)

        nodes.append({
            "id": node_id,
            "type": "lambda",
            "slug": slug,
            "mode": mode,
            "method": str(item.get("method") or "").strip().upper() or None,
            "path": str(item.get("path") or "").strip() or None,
            "timeout": timeout,
            "retry": retry,
            "backoff": backoff,
            "success_statuses": success_statuses,
            "max_parallel": max_parallel,
            "input": item.get("input"),
            "next": next_nodes,
            "waits_for": waits_for,
        })

    edges = []
    for item in (raw.get("edges") or []):
        if not isinstance(item, dict):
            return None, "edges must be objects"
        source = str(item.get("from") or "").strip()
        target = str(item.get("to") or "").strip()
        if source not in ids or target not in ids:
            return None, f"edge {source!r} -> {target!r}: unknown node"
        if source == target:
            return None, "an edge cannot point to itself"
        kind = str(item.get("kind") or "trigger").strip().lower()
        if kind not in ("trigger", "dependency"):
            return None, f"edge {source} -> {target}: invalid kind {kind!r}"
        problem = _validate_condition(item.get("when"))
        if problem:
            return None, f"edge {source} -> {target}: {problem}"
        problem = _validate_mapping(item.get("input"), f"edge {source} -> {target}.input")
        if problem:
            return None, problem
        edges.append({
            "from": source,
            "to": target,
            "kind": kind,
            "when": item.get("when"),
            "input": item.get("input"),
        })

    # Merge node-level shortcuts into edges.
    node_modes = {node["id"]: node.get("mode", "trigger") for node in nodes}
    for node in nodes:
        for target in node["next"]:
            edge_kind = "dependency" if node_modes.get(target) == "join" else "trigger"
            edges.append({"from": node["id"], "to": target, "kind": edge_kind, "when": None, "input": None})
        for source in node["waits_for"]:
            edges.append({"from": source, "to": node["id"], "kind": "dependency", "when": None, "input": None})

    # Deduplicate identical edges.
    unique_edges = []
    seen_edges: set[tuple] = set()
    for edge in edges:
        key = (edge["from"], edge["to"], edge["kind"])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        unique_edges.append(edge)
    edges = unique_edges
    if len(edges) > _MAX_EDGES:
        return None, f"too many edges (max {_MAX_EDGES})"

    # A node cannot receive both trigger and dependency edges.
    incoming_kinds: dict[str, set[str]] = {node["id"]: set() for node in nodes}
    for edge in edges:
        incoming_kinds[edge["to"]].add(edge["kind"])
    for node in nodes:
        if len(incoming_kinds[node["id"]]) > 1:
            return None, f"node {node['id']}: cannot mix trigger and dependency edges"

    # Fan-out limit.
    outgoing: dict[str, int] = {node["id"]: 0 for node in nodes}
    for edge in edges:
        outgoing[edge["from"]] += 1
    for node_id, count in outgoing.items():
        if count > _MAX_FANOUT:
            return None, f"node {node_id}: too many successors (max {_MAX_FANOUT})"

    # Cycle detection for triggers and dependencies independently.
    def adjacency(kind: str) -> dict[str, list[str]]:
        result = {node["id"]: [] for node in nodes}
        for edge in edges:
            if edge["kind"] == kind:
                result[edge["from"]].append(edge["to"])
        return result

    def has_cycle(graph: dict[str, list[str]]) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> bool:
            if node_id in visited:
                return False
            if node_id in visiting:
                return True
            visiting.add(node_id)
            for target in graph.get(node_id, []):
                if visit(target):
                    return True
            visiting.discard(node_id)
            visited.add(node_id)
            return False

        return any(visit(node_id) for node_id in graph)

    if has_cycle(adjacency("trigger")):
        return None, "trigger graph contains a cycle"
    if has_cycle(adjacency("dependency")):
        return None, "dependency graph contains a cycle"

    # Depth limit.
    def longest_path(graph: dict[str, list[str]]) -> int:
        memo: dict[str, int] = {}

        def depth(node_id: str) -> int:
            if node_id in memo:
                return memo[node_id]
            result = 1 + max((depth(target) for target in graph.get(node_id, [])), default=0)
            memo[node_id] = result
            return result

        return max((depth(node_id) for node_id in graph), default=0)

    if longest_path(adjacency("trigger")) > _MAX_DEPTH:
        return None, f"trigger graph is too deep (max {_MAX_DEPTH})"
    if longest_path(adjacency("dependency")) > _MAX_DEPTH:
        return None, f"dependency graph is too deep (max {_MAX_DEPTH})"

    # Clean node-level shortcuts from the stored definition; edges are canonical.
    for node in nodes:
        node.pop("next", None)
        node.pop("waits_for", None)

    definition = {
        "name": name,
        "nodes": nodes,
        "edges": edges,
        "limits": {
            "max_parallel": int(raw.get("max_parallel", _MAX_PARALLEL) or _MAX_PARALLEL),
        },
    }
    try:
        if json.dumps(definition).encode("utf-8").__len__() > _MAX_INPUT_BYTES * 4:
            return None, "workflow definition is too large"
    except Exception:
        return None, "workflow definition is too large"
    return definition, None


class _Run:
    def __init__(self, run_id: int, workflow: dict, owner_id: int, input_obj: dict, pool, args):
        self.run_id = run_id
        self.workflow = workflow
        self.owner_id = owner_id
        self.input = input_obj
        self.pool = pool
        self.args = args
        self.lock = threading.Lock()
        self.cancelled = False
        self.nodes = {node["id"]: node for node in workflow["nodes"]}
        self.edges = workflow["edges"]
        self.triggers: dict[str, list[dict]] = {node_id: [] for node_id in self.nodes}
        self.dependencies: dict[str, list[dict]] = {node_id: [] for node_id in self.nodes}
        for edge in self.edges:
            if edge["kind"] == "trigger":
                self.triggers[edge["from"]].append(edge)
            else:
                self.dependencies[edge["to"]].append(edge)
        self.incoming_triggers: dict[str, list[dict]] = {node_id: [] for node_id in self.nodes}
        self.incoming_dependencies: dict[str, list[dict]] = {node_id: [] for node_id in self.nodes}
        self.outgoing_dependencies: dict[str, list[dict]] = {node_id: [] for node_id in self.nodes}
        for edge in self.edges:
            if edge["kind"] == "trigger":
                self.incoming_triggers[edge["to"]].append(edge)
            else:
                self.incoming_dependencies[edge["to"]].append(edge)
                self.outgoing_dependencies[edge["from"]].append(edge)

        self.node_status: dict[str, str] = {node_id: "pending" for node_id in self.nodes}
        self.node_invocations: dict[str, list[dict]] = {node_id: [] for node_id in self.nodes}
        self.completed_triggers: dict[str, set[str]] = {node_id: set() for node_id in self.nodes}
        self.join_scheduled: set[str] = set()
        self.context: dict = {"input": self.input, "nodes": {}}
        self.node_inputs: dict[str, object] = {}
        self.executor = ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL, int(workflow.get("limits", {}).get("max_parallel", _MAX_PARALLEL) or _MAX_PARALLEL)))
        self.futures: dict[str, tuple[str, object]] = {}
        self.started_at = time.time()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"workflow-{self.run_id}", daemon=True)
        self.thread.start()

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True

    def retry_node(self, node_id: str) -> dict:
        if node_id not in self.nodes:
            return {"error": "unknown node"}
        with self.lock:
            if any(status == "running" for status in self.node_status.values()):
                return {"error": "run is still running"}
            if self.node_status.get(node_id) not in {"failed", "cancelled", "timed_out"}:
                return {"error": "node is not retryable"}
            if self.incoming_dependencies.get(node_id):
                predecessors = [edge["from"] for edge in self.incoming_dependencies[node_id]]
                if not all(self.node_status.get(pred) == "succeeded" for pred in predecessors):
                    return {"error": "dependencies are not satisfied"}
            input_obj = self.node_inputs.get(node_id)
            if input_obj is None:
                input_obj = {"input": self.input}
            invocation_id = uuid.uuid4().hex
            attempt = len([item for item in self.node_invocations[node_id]]) + 1
            self.node_status[node_id] = "running"
            self.node_invocations[node_id].append({"id": invocation_id, "status": "running", "output": None, "error": None})
        self._create_node_run(node_id, invocation_id, input_obj if isinstance(input_obj, dict) else {"value": input_obj}, attempt=attempt)
        try:
            future = self.executor.submit(self._execute_and_finish, node_id, invocation_id, input_obj if isinstance(input_obj, dict) else {"value": input_obj})
        except RuntimeError:
            self.executor = ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL, int(self.workflow.get("limits", {}).get("max_parallel", _MAX_PARALLEL) or _MAX_PARALLEL)))
            future = self.executor.submit(self._execute_and_finish, node_id, invocation_id, input_obj if isinstance(input_obj, dict) else {"value": input_obj})
        with self.lock:
            self.futures[invocation_id] = (node_id, future)
        self._drain()
        with self.lock:
            return {
                "retried": True,
                "node_id": node_id,
                "status": self.node_status[node_id],
                "run_status": getattr(self, "status", None),
            }

    def _drain(self) -> None:
        timeout = int(self.workflow.get("timeout", 0) or 0)
        if timeout <= 0:
            timeout = 3600
        start = time.time()
        while True:
            with self.lock:
                if self.cancelled:
                    break
                if time.time() - start > timeout:
                    self.cancelled = True
                    break
                running = [future for (_, future) in self.futures.values() if not future.done()]
                node_running = any(status == "running" for status in self.node_status.values())
            if not running and not node_running:
                break
            try:
                concurrent.futures.wait([f for (_, f) in self.futures.values()], timeout=0.5, return_when=concurrent.futures.FIRST_COMPLETED)
            except Exception:
                time.sleep(0.1)
            completed = []
            with self.lock:
                for invocation_id, (node_id, future) in list(self.futures.items()):
                    if future.done():
                        completed.append(invocation_id)
            for invocation_id in completed:
                with self.lock:
                    _, future = self.futures.pop(invocation_id)
                try:
                    future.result()
                except Exception:
                    pass
        with self.lock:
            if self.cancelled:
                self._set_run_status_unlocked("cancelled")
            elif any(status in {"failed", "timed_out"} for status in self.node_status.values()):
                self._set_run_status_unlocked("failed")
            else:
                self._set_run_status_unlocked("succeeded")

    def _set_run_status(self, status: str, error: str | None = None) -> None:
        ensure_schema()
        with db_get(_db_path()) as conn:
            conn.execute(
                "UPDATE workflow_runs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                (status, time.time(), error, self.run_id),
            )
            conn.commit()
        with self.lock:
            self.status = status

    def _set_node_status(self, node_id: str, status: str) -> None:
        with self.lock:
            self.node_status[node_id] = status
        with db_get(_db_path()) as conn:
            conn.execute(
                "UPDATE workflow_node_runs SET status = ? WHERE run_id = ? AND node_id = ? AND status IN ('pending', 'running')",
                (status, self.run_id, node_id),
            )
            conn.commit()

    def _create_node_run(self, node_id: str, invocation_id: str, input_obj: dict, attempt: int = 1) -> None:
        ensure_schema()
        self.node_inputs[node_id] = input_obj
        with db_get(_db_path()) as conn:
            conn.execute(
                "INSERT INTO workflow_node_runs (run_id, node_id, invocation_id, status, attempt, input_json, started_at, lambda_slug, request_id) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)",
                (self.run_id, node_id, invocation_id, int(attempt), json.dumps(input_obj, ensure_ascii=False), time.time(), self.nodes[node_id]["slug"], f"wf-{self.run_id}-{node_id}-{invocation_id}"),
            )
            conn.commit()

    def _finish_node_run(self, node_id: str, invocation_id: str, status: str, output=None, error: str | None = None) -> None:
        ensure_schema()
        output_json = None
        if output is not None:
            try:
                encoded = json.dumps(output, ensure_ascii=False)
                if len(encoded.encode("utf-8")) > _MAX_OUTPUT_BYTES:
                    status = "failed"
                    error = "node output exceeds the size limit"
                    output_json = None
                else:
                    output_json = encoded
            except Exception:
                status = "failed"
                error = "node output is not JSON serializable"
                output_json = None
        with db_get(_db_path()) as conn:
            conn.execute(
                "UPDATE workflow_node_runs SET status = ?, output_json = ?, finished_at = ?, error = ? WHERE run_id = ? AND node_id = ? AND invocation_id = ?",
                (status, output_json, time.time(), error, self.run_id, node_id, invocation_id),
            )
            conn.commit()

    def _resolve_input(self, node: dict, context: dict, default_input):
        mapping = node.get("input")
        if mapping is None:
            return default_input
        return _resolve_refs(mapping, context)

    def _build_request(self, node: dict, input_obj):
        method = node.get("method") or "GET"
        path = node.get("path") or "/"
        body_value = input_obj.get("body") if isinstance(input_obj, dict) and "body" in input_obj else input_obj
        query_value = input_obj.get("query") if isinstance(input_obj, dict) and "query" in input_obj else None
        body = _build_body(body_value)
        query = _build_query(query_value) if query_value is not None else (input_obj.get("query_string", "") if isinstance(input_obj, dict) else "")
        timeout = int(node.get("timeout") or 0)
        if timeout <= 0:
            timeout = int(self.args.invoke_timeout) if self.args is not None else 30
        memory_mb = int(node.get("memory_mb") or 0)
        return method, path, query, body, timeout, memory_mb

    def _execute_node(self, node: dict, invocation_id: str, input_obj: dict) -> dict:
        slug = node["slug"]
        method, path, query, body, timeout, memory_mb = self._build_request(node, input_obj)
        request_id = f"wf-{self.run_id}-{node['id']}-{invocation_id}"
        try:
            import vlambda_native
            code_info = vlambda_native.db_get_lambda_code(slug)
            project_info = vlambda_native.db_get_project(slug)
        except Exception:
            code_info = ""
            project_info = ""

        meta = json.dumps({"headers": {}, "remote_ip": "", "host": "", "scheme": "http", "env": {}})
        try:
            from vlambda.env import get_env

            meta_obj = json.loads(meta)
            meta_obj["env"] = get_env(slug) if slug else {}
            meta = json.dumps(meta_obj)
        except Exception:
            pass

        broker_token = None
        try:
            from vlambda import broker

            meta, broker_token = broker.attach(meta, slug, request_id)
            if project_info:
                parts = project_info.split("|")
                project_dir = parts[0] if parts else ""
                entrypoint = parts[1] if len(parts) > 1 else "main.py"
                project_method = parts[2] if len(parts) > 2 and parts[2] else method
                result = self.pool.execute_project(
                    project_dir, entrypoint, project_method, path, query, body,
                    timeout=timeout, request_id=request_id, slug=slug, request_meta=meta,
                )
            else:
                code = code_info.split("|")[0] if code_info else ""
                if not code:
                    raise RuntimeError("lambda not found")
                parts = code_info.split("|")
                node_method = method or (parts[1] if len(parts) > 1 and parts[1] else "GET")
                node_timeout = timeout or (int(parts[2]) if len(parts) > 2 and parts[2] else (self.args.invoke_timeout if self.args else 30))
                node_memory = memory_mb or (int(parts[3]) if len(parts) > 3 and parts[3] else 0)
                vendor_dir = ""
                try:
                    from vlambda.packages import lambda_target
                    import os as _os

                    candidate = lambda_target(slug)
                    if _os.path.isdir(candidate):
                        vendor_dir = candidate
                except Exception:
                    pass
                result = self.pool.execute(
                    code, node_method, path, query, body, node_timeout, node_memory,
                    vendor_dir, request_id, slug, meta, f"{slug}:wf",
                )
        finally:
            try:
                from vlambda import broker

                broker.release(broker_token)
            except Exception:
                pass

        if not isinstance(result, dict):
            result = {"status": 500, "body": str(result), "headers": [], "body_b64": ""}
        status = int(result.get("status", 500) or 500)
        raw_body = result.get("body", "")
        if result.get("body_b64"):
            import base64

            try:
                raw_body = base64.b64decode(result["body_b64"]).decode("utf-8", "replace")
            except Exception:
                raw_body = result.get("body", "")
        parsed_body = raw_body
        try:
            parsed_body = json.loads(raw_body) if raw_body else None
        except Exception:
            parsed_body = raw_body
        output = {
            "status": status,
            "headers": result.get("headers", []),
            "body": parsed_body,
            "raw_body": raw_body,
        }
        if status >= 400:
            raise RuntimeError(f"HTTP {status}: {raw_body[:500]}")
        return output

    def _schedule_trigger(self, target_id: str, source_id: str, edge: dict, source_output: dict) -> None:
        if self.cancelled:
            return
        if target_id not in self.nodes:
            return
        if self.node_status.get(target_id) in {"cancelled", "skipped", "failed"}:
            return
        condition = edge.get("when")
        if condition is not None:
            try:
                if not self._condition_true(condition, {"nodes": self.context["nodes"], "input": self.input, "source": source_output}):
                    return
            except Exception:
                return
        mode = self.nodes[target_id].get("mode", "trigger")
        if mode == "join":
            # A trigger edge to a join node is not allowed by validation, but
            # be defensive: treat it as a dependency completion.
            self._join_completed(target_id, source_id)
            return
        if mode not in ("trigger", "first", "any"):
            return
        if mode == "first" and self.node_status.get(target_id) in {"running", "succeeded"}:
            return
        if mode == "any" and self.node_status.get(target_id) in {"running", "succeeded"}:
            return

        input_obj = edge.get("input")
        if input_obj is None:
            input_obj = {"result": source_output}
        resolved = _resolve_refs(input_obj, {"nodes": self.context["nodes"], "input": self.input, "source": source_output})
        invocation_id = uuid.uuid4().hex
        try:
            self._create_node_run(target_id, invocation_id, resolved if isinstance(resolved, dict) else {"value": resolved})
        except Exception:
            return
        try:
            future = self.executor.submit(self._execute_and_finish, target_id, invocation_id, resolved if isinstance(resolved, dict) else {"value": resolved})
        except Exception:
            return
        self.node_status[target_id] = "running"
        self.node_invocations[target_id].append({"id": invocation_id, "status": "running", "output": None, "error": None})
        self.futures[invocation_id] = (target_id, future)

    def _join_completed(self, target_id: str, source_id: str) -> None:
        if self.cancelled or target_id not in self.nodes:
            return
        self.completed_triggers[target_id].add(source_id)
        required = [edge["from"] for edge in self.incoming_dependencies[target_id]]
        active_required = []
        for edge in self.incoming_dependencies[target_id]:
            # An inactive edge means its condition was false; it is not required.
            if edge.get("_active", True):
                active_required.append(edge["from"])
        if not active_required:
            self.node_status[target_id] = "skipped"
            self._finish_node_run(target_id, None, "skipped", None, "no active dependencies")
            return
        completed = self.completed_triggers[target_id]
        failed = [pred for pred in active_required if self.node_status.get(pred) in {"failed", "timed_out", "cancelled"}]
        if failed:
            self.node_status[target_id] = "failed"
            self._finish_node_run(target_id, None, "failed", None, f"dependency failed: {', '.join(failed)}")
            return
        if all(pred in completed for pred in active_required):
            if target_id in self.join_scheduled:
                return
            self.join_scheduled.add(target_id)
            input_obj = {"predecessors": {pred: self.context["nodes"].get(pred, {}).get("output") for pred in active_required}}
            # Keep a convenient "result" alias for single-dependency joins.
            if len(active_required) == 1:
                input_obj["result"] = input_obj["predecessors"][active_required[0]]
            invocation_id = uuid.uuid4().hex
            self.node_status[target_id] = "running"
            self.node_invocations[target_id].append({"id": invocation_id, "status": "running", "output": None, "error": None})
            self._create_node_run(target_id, invocation_id, input_obj)
            future = self.executor.submit(self._execute_and_finish, target_id, invocation_id, input_obj)
            self.futures[invocation_id] = (target_id, future)

    def _execute_and_finish(self, node_id: str, invocation_id: str, input_obj: dict) -> None:
        node = self.nodes[node_id]
        output = None
        error = None
        status = "failed"
        try:
            output = self._execute_node(node, invocation_id, input_obj)
            status = "succeeded"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            status = "failed"
        self._finish_node_run(node_id, invocation_id, status, output, error)

        with self.lock:
            invocation = next((item for item in self.node_invocations[node_id] if item["id"] == invocation_id), None)
            if invocation is not None:
                invocation["status"] = status
                invocation["output"] = output
                invocation["error"] = error
            # Aggregate node status.
            statuses = [item["status"] for item in self.node_invocations[node_id]]
            if any(s == "running" for s in statuses):
                self.node_status[node_id] = "running"
            elif statuses:
                latest = statuses[-1]
                if latest == "succeeded":
                    self.node_status[node_id] = "succeeded"
                elif latest in {"failed", "timed_out"}:
                    self.node_status[node_id] = "failed"
                elif latest in {"skipped", "cancelled"}:
                    self.node_status[node_id] = latest
                else:
                    self.node_status[node_id] = latest
            else:
                self.node_status[node_id] = status
            self.context["nodes"][node_id] = {
                "status": self.node_status[node_id],
                "output": output if output is not None else self.context["nodes"].get(node_id, {}).get("output"),
                "invocations": self.node_invocations[node_id],
            }
            self._persist_context()
            if status == "succeeded" and output is not None and len(json.dumps(output).encode("utf-8")) > _MAX_CONTEXT_BYTES:
                self.cancelled = True
                self._set_run_status("failed", "workflow context exceeds the size limit")
                return
            if self.cancelled:
                return
            if status == "succeeded":
                # Dependency edges notify join nodes that a predecessor completed.
                for edge in self.outgoing_dependencies.get(node_id, []):
                    condition = edge.get("when")
                    active = True
                    if condition is not None:
                        try:
                            active = self._condition_true(condition, {"nodes": self.context["nodes"], "input": self.input, "source": output})
                        except Exception:
                            active = False
                    edge["_active"] = active
                    if active:
                        self._join_completed(edge["to"], node_id)

                # Trigger outgoing edges.
                try:
                    for edge in self.triggers.get(node_id, []):
                        condition = edge.get("when")
                        active = True
                        if condition is not None:
                            try:
                                active = self._condition_true(condition, {"nodes": self.context["nodes"], "input": self.input, "source": output})
                            except Exception:
                                active = False
                        edge["_active"] = active
                        if active:
                            target = edge["to"]
                            target_mode = self.nodes[target].get("mode", "trigger")
                            if target_mode == "trigger":
                                edge_input = edge.get("input")
                                if edge_input is None:
                                    edge_input = {"result": output}
                                resolved = _resolve_refs(edge_input, {"nodes": self.context["nodes"], "input": self.input, "source": output})
                                self._schedule_trigger(target, node_id, edge, resolved if isinstance(resolved, dict) else {"value": resolved})
                            elif target_mode in ("join", "first", "any"):
                                # A trigger to a join node is treated as a dependency completion.
                                self._join_completed(target, node_id)
                    # Outgoing dependency edges are handled by join completion.
                    # If this node is a join, its triggers are also fired by the block above.
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    status = "failed"
            else:
                # Failure hooks.
                for target in self.nodes[node_id].get("on_failure", []):
                    if target in self.nodes:
                        self._schedule_trigger(target, node_id, {"from": node_id, "to": target, "kind": "trigger", "when": None, "input": None}, {"error": error})

        # Evaluate run completion.
        self._evaluate_run()

    def _condition_true(self, condition: dict, context: dict) -> bool:
        path = condition["path"]
        value = _resolve_path(context, path[2:])
        if "equals" in condition:
            return value == condition["equals"]
        if "not_equals" in condition:
            return value != condition["not_equals"]
        if "exists" in condition:
            return value is not None
        if "truthy" in condition:
            return bool(value) == bool(condition["truthy"])
        if "greater_than" in condition:
            try:
                return float(value) > float(condition["greater_than"])
            except (TypeError, ValueError):
                return False
        if "less_than" in condition:
            try:
                return float(value) < float(condition["less_than"])
            except (TypeError, ValueError):
                return False
        if "in" in condition:
            candidate = condition["in"]
            if isinstance(candidate, list):
                return value in candidate
            return False
        return False

    def _persist_context(self) -> None:
        ensure_schema()
        try:
            with db_get(_db_path()) as conn:
                conn.execute(
                    "UPDATE workflow_runs SET context_json = ? WHERE id = ?",
                    (json.dumps(self.context, ensure_ascii=False), self.run_id),
                )
                conn.commit()
        except Exception:
            pass

    def _evaluate_run(self) -> None:
        with self.lock:
            if self.cancelled:
                self._set_run_status_unlocked("cancelled")
                return
            statuses = list(self.node_status.values())
            if any(status == "running" for status in statuses):
                return
            if any(status in {"failed", "timed_out"} for status in statuses):
                self._set_run_status_unlocked("failed")
                return
            if all(status in {"succeeded", "skipped", "cancelled"} for status in statuses):
                self._set_run_status_unlocked("succeeded")

    def _set_run_status_unlocked(self, status: str, error: str | None = None) -> None:
        # Caller must hold self.lock.
        ensure_schema()
        with db_get(_db_path()) as conn:
            conn.execute(
                "UPDATE workflow_runs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                (status, time.time(), error, self.run_id),
            )
            conn.commit()
        self.status = status

    def _run(self) -> None:
        self._set_run_status("running")
        # Roots.
        roots = [node_id for node_id in self.nodes if not self.incoming_triggers[node_id] and not self.incoming_dependencies[node_id]]
        for root in roots:
            invocation_id = uuid.uuid4().hex
            with self.lock:
                self.node_status[root] = "running"
                self.node_invocations[root].append({"id": invocation_id, "status": "running", "output": None, "error": None})
            self._create_node_run(root, invocation_id, {"input": self.input})
            future = self.executor.submit(self._execute_and_finish, root, invocation_id, {"input": self.input})
            with self.lock:
                self.futures[invocation_id] = (root, future)

        # Main loop.
        start = time.time()
        timeout = int(self.workflow.get("timeout", 0) or 0)
        if timeout <= 0:
            timeout = 3600
        while True:
            with self.lock:
                if self.cancelled:
                    break
                if time.time() - start > timeout:
                    self.cancelled = True
                    break
                running = [future for (_, future) in self.futures.values() if not future.done()]
            if not running and not any(status == "running" for status in self.node_status.values()):
                break
            # Wait for at least one future to complete.
            try:
                concurrent.futures.wait(list(self.futures.values() and [f for (_, f) in self.futures.values()] or []), timeout=0.5, return_when=concurrent.futures.FIRST_COMPLETED)
            except Exception:
                time.sleep(0.1)
            # Process completed futures.
            completed_invocation_ids = []
            with self.lock:
                for invocation_id, (node_id, future) in list(self.futures.items()):
                    if future.done():
                        completed_invocation_ids.append(invocation_id)
            for invocation_id in completed_invocation_ids:
                with self.lock:
                    node_id, future = self.futures.pop(invocation_id)
                try:
                    future.result()
                except Exception:
                    pass
            if all(status in {"succeeded", "failed", "skipped", "cancelled", "timed_out"} for status in self.node_status.values()) and not self.futures:
                break
        # Shutdown executor.
        try:
            self.executor.shutdown(wait=True, cancel_futures=False)
        except Exception:
            pass
        # Final status.
        with self.lock:
            if self.cancelled:
                self._set_run_status_unlocked("cancelled")
            elif any(status in {"failed", "timed_out"} for status in self.node_status.values()):
                self._set_run_status_unlocked("failed")
            else:
                self._set_run_status_unlocked("succeeded")


def _validate_workflow(owner_id: int, body_json: str) -> tuple[dict | None, str | None]:
    try:
        raw = json.loads(body_json or "{}")
    except Exception as exc:
        return None, f"invalid json: {exc}"
    return _validate_definition(owner_id, raw)


def create_workflow(owner_id: int, body_json: str) -> str:
    ensure_schema()
    try:
        raw = json.loads(body_json or "{}")
    except Exception as exc:
        return json.dumps({"error": f"invalid json: {exc}"})
    definition, error = _validate_definition(owner_id, raw)
    if error:
        return json.dumps({"error": error})
    try:
        with db_get(_db_path()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM workflows WHERE owner_id = ?", (owner_id,)).fetchone()[0]
            if count >= _MAX_WORKFLOWS:
                return json.dumps({"error": f"workflow quota exceeded (max {_MAX_WORKFLOWS})"})
            now = time.time()
            cur = conn.execute(
                "INSERT INTO workflows (owner_id, name, version, definition, enabled, created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?)",
                (owner_id, definition["name"], json.dumps(definition), 1, now, now),
            )
            conn.commit()
            workflow_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return json.dumps({"error": "a workflow with that name already exists"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps({"id": workflow_id, "name": definition["name"], "version": 1, "definition": definition})


def get_workflow(workflow_id: int, owner_id: int) -> str:
    ensure_schema()
    try:
        with db_get(_db_path()) as conn:
            row = conn.execute(
                "SELECT id, owner_id, name, version, definition, enabled, created_at, updated_at FROM workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    if row is None:
        return json.dumps({"error": "workflow not found"})
    if int(row[1]) != int(owner_id):
        return json.dumps({"error": "forbidden"})
    try:
        definition = json.loads(row[4])
    except Exception:
        definition = {}
    return json.dumps({
        "id": row[0],
        "owner_id": row[1],
        "name": row[2],
        "version": row[3],
        "definition": definition,
        "enabled": bool(row[5]),
        "created_at": row[6],
        "updated_at": row[7],
    })


def list_workflows(owner_id: int) -> str:
    ensure_schema()
    try:
        with db_get(_db_path()) as conn:
            rows = conn.execute(
                "SELECT id, name, version, enabled, created_at, updated_at FROM workflows WHERE owner_id = ? ORDER BY name",
                (owner_id,),
            ).fetchall()
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps([
        {"id": row[0], "name": row[1], "version": row[2], "enabled": bool(row[3]), "created_at": row[4], "updated_at": row[5]}
        for row in rows
    ])


def update_workflow(workflow_id: int, owner_id: int, body_json: str) -> str:
    ensure_schema()
    try:
        raw = json.loads(body_json or "{}")
    except Exception as exc:
        return json.dumps({"error": f"invalid json: {exc}"})
    definition, error = _validate_definition(owner_id, raw)
    if error:
        return json.dumps({"error": error})
    try:
        with db_get(_db_path()) as conn:
            row = conn.execute("SELECT owner_id, version FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                return json.dumps({"error": "workflow not found"})
            if int(row[0]) != int(owner_id):
                return json.dumps({"error": "forbidden"})
            new_version = int(row[1] or 1) + 1
            now = time.time()
            conn.execute(
                "UPDATE workflows SET name = ?, version = ?, definition = ?, updated_at = ? WHERE id = ?",
                (definition["name"], new_version, json.dumps(definition), now, workflow_id),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return json.dumps({"error": "a workflow with that name already exists"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps({"id": workflow_id, "name": definition["name"], "version": new_version, "definition": definition})


def delete_workflow(workflow_id: int, owner_id: int) -> str:
    ensure_schema()
    try:
        with db_get(_db_path()) as conn:
            row = conn.execute("SELECT owner_id FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                return json.dumps({"error": "workflow not found"})
            if int(row[0]) != int(owner_id):
                return json.dumps({"error": "forbidden"})
            conn.execute("DELETE FROM workflow_node_runs WHERE run_id IN (SELECT id FROM workflow_runs WHERE workflow_id = ?)", (workflow_id,))
            conn.execute("DELETE FROM workflow_runs WHERE workflow_id = ?", (workflow_id,))
            conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
            conn.commit()
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps({"deleted": True})


def get_workflow_graph(workflow_id: int, owner_id: int, run_id: int | None = None) -> str:
    ensure_schema()
    workflow_json = get_workflow(int(workflow_id), int(owner_id))
    try:
        workflow = json.loads(workflow_json)
    except Exception as exc:
        return json.dumps({"error": f"invalid workflow: {exc}"})
    if isinstance(workflow, dict) and workflow.get("error"):
        return workflow_json

    definition = workflow.get("definition") or {}
    nodes = []
    for node in definition.get("nodes", []):
        nodes.append({
            "id": node.get("id"),
            "slug": node.get("slug"),
            "mode": node.get("mode", "trigger"),
            "timeout": node.get("timeout", 0),
            "status": None,
        })
    edges = []
    for edge in definition.get("edges", []):
        edges.append({
            "from": edge.get("from"),
            "to": edge.get("to"),
            "kind": edge.get("kind", "trigger"),
            "when": edge.get("when"),
            "active": edge.get("_active", None),
        })

    if run_id is not None:
        run_json = get_workflow_run(int(run_id), int(owner_id))
        try:
            run = json.loads(run_json)
        except Exception:
            run = {}
        if not (isinstance(run, dict) and run.get("error")):
            statuses = {}
            for node_run in run.get("nodes", []):
                statuses[node_run.get("node_id")] = node_run.get("status")
            for node in nodes:
                node["status"] = statuses.get(node["id"])

    return json.dumps({
        "workflow_id": workflow.get("id"),
        "run_id": run_id,
        "nodes": nodes,
        "edges": edges,
    })


def validate_workflow(owner_id: int, body_json: str) -> str:
    try:
        raw = json.loads(body_json or "{}")
    except Exception as exc:
        return json.dumps({"valid": False, "error": f"invalid json: {exc}"})
    definition, error = _validate_definition(owner_id, raw)
    if error:
        return json.dumps({"valid": False, "error": error})
    return json.dumps({"valid": True, "definition": definition})


def trigger_workflow_for_slug(slug: str, workflow_id, input_json: str = "{}") -> str:
    ensure_schema()
    try:
        wid = int(workflow_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "invalid workflow id"})
    try:
        input_obj = json.loads(input_json or "{}")
    except Exception as exc:
        return json.dumps({"error": f"invalid json: {exc}"})
    if not isinstance(input_obj, dict):
        return json.dumps({"error": "input must be a JSON object"})
    if len(json.dumps(input_obj).encode("utf-8")) > _MAX_INPUT_BYTES:
        return json.dumps({"error": "workflow input is too large"})
    try:
        with db_get(_db_path()) as conn:
            row = conn.execute("SELECT owner_id FROM lambdas WHERE slug = ?", (str(slug),)).fetchone()
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    if row is None:
        return json.dumps({"error": "lambda not found"})
    return run_workflow(wid, int(row[0]), json.dumps(input_obj))


def run_workflow(workflow_id: int, owner_id: int, input_json: str) -> str:
    ensure_schema()
    try:
        input_obj = json.loads(input_json or "{}")
    except Exception as exc:
        return json.dumps({"error": f"invalid json: {exc}"})
    if not isinstance(input_obj, dict):
        return json.dumps({"error": "input must be a JSON object"})
    if len(json.dumps(input_obj).encode("utf-8")) > _MAX_INPUT_BYTES:
        return json.dumps({"error": "workflow input is too large"})

    try:
        with db_get(_db_path()) as conn:
            row = conn.execute("SELECT owner_id, definition, enabled FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    if row is None:
        return json.dumps({"error": "workflow not found"})
    if int(row[0]) != int(owner_id):
        return json.dumps({"error": "forbidden"})
    if not bool(row[1] if False else row[2]):
        return json.dumps({"error": "workflow is disabled"})
    try:
        workflow = json.loads(row[1] if False else row[1])
    except Exception as exc:
        return json.dumps({"error": f"invalid workflow definition: {exc}"})
    # row layout: owner_id, definition, enabled
    workflow = json.loads(row[1])
    if not _run_semaphore.acquire(blocking=False):
        return json.dumps({"error": "workflow queue is full"})
    try:
        with db_get(_db_path()) as conn:
            now = time.time()
            cur = conn.execute(
                "INSERT INTO workflow_runs (workflow_id, owner_id, status, trigger_type, input_json, context_json, started_at) VALUES (?, ?, 'pending', 'manual', ?, '{}', ?)",
                (workflow_id, owner_id, json.dumps(input_obj), now),
            )
            conn.commit()
            run_id = cur.lastrowid
    except Exception as exc:
        _run_semaphore.release()
        return json.dumps({"error": str(exc)})
    runner = _Run(run_id, workflow, owner_id, input_obj, _pool, _args)
    with _runs_lock:
        _runs[run_id] = runner
    runner.start()
    return json.dumps({"run_id": run_id, "status": "pending"})

    # The above row layout uses: owner_id, definition, enabled.
    _unused = None


def get_workflow_run(run_id: int, owner_id: int) -> str:
    ensure_schema()
    try:
        with db_get(_db_path()) as conn:
            row = conn.execute(
                "SELECT id, workflow_id, owner_id, status, trigger_type, input_json, context_json, started_at, finished_at, error FROM workflow_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            node_rows = conn.execute(
                "SELECT id, node_id, invocation_id, status, attempt, input_json, output_json, started_at, finished_at, error, lambda_slug, request_id FROM workflow_node_runs WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    if row is None:
        return json.dumps({"error": "workflow run not found"})
    if int(row[2]) != int(owner_id):
        return json.dumps({"error": "forbidden"})
    try:
        context = json.loads(row[6] or "{}")
    except Exception:
        context = {}
    node_runs = []
    for item in node_rows:
        try:
            output = json.loads(item[6]) if item[6] else None
        except Exception:
            output = item[6]
        try:
            node_input = json.loads(item[5]) if item[5] else None
        except Exception:
            node_input = item[5]
        node_runs.append({
            "id": item[0],
            "node_id": item[1],
            "invocation_id": item[2],
            "status": item[3],
            "attempt": item[4],
            "input": node_input,
            "output": output,
            "started_at": item[7],
            "finished_at": item[8],
            "error": item[9],
            "lambda_slug": item[10],
            "request_id": item[11],
        })
    return json.dumps({
        "run_id": row[0],
        "workflow_id": row[1],
        "owner_id": row[2],
        "status": row[3],
        "trigger_type": row[4],
        "input": json.loads(row[5] or "{}"),
        "context": context,
        "started_at": row[7],
        "finished_at": row[8],
        "error": row[9],
        "nodes": node_runs,
    })


def list_workflow_runs(workflow_id: int, owner_id: int) -> str:
    ensure_schema()
    try:
        with db_get(_db_path()) as conn:
            owner = conn.execute("SELECT owner_id FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
            if owner is None:
                return json.dumps({"error": "workflow not found"})
            if int(owner[0]) != int(owner_id):
                return json.dumps({"error": "forbidden"})
            rows = conn.execute(
                "SELECT id, status, started_at, finished_at, error FROM workflow_runs WHERE workflow_id = ? ORDER BY id DESC LIMIT 100",
                (workflow_id,),
            ).fetchall()
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps([
        {"run_id": row[0], "status": row[1], "started_at": row[2], "finished_at": row[3], "error": row[4]}
        for row in rows
    ])


def cancel_workflow_run(run_id: int, owner_id: int) -> str:
    with _runs_lock:
        runner = _runs.get(run_id)
    if runner is None:
        # Still mark the database row cancelled if it is incomplete.
        try:
            with db_get(_db_path()) as conn:
                row = conn.execute("SELECT owner_id, status FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
                if row is None:
                    return json.dumps({"error": "workflow run not found"})
                if int(row[0]) != int(owner_id):
                    return json.dumps({"error": "forbidden"})
                if row[1] in {"succeeded", "failed", "cancelled", "timed_out"}:
                    return json.dumps({"error": f"run is already {row[1]}"})
                conn.execute("UPDATE workflow_runs SET status = 'cancelled', finished_at = ? WHERE id = ?", (time.time(), run_id))
                conn.commit()
        except Exception as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"cancelled": True})
    if runner.owner_id != owner_id:
        return json.dumps({"error": "forbidden"})
    runner.cancel()
    return json.dumps({"cancelled": True})


def retry_workflow_node(run_id: int, node_id: str, owner_id: int) -> str:
    ensure_schema()
    with _runs_lock:
        runner = _runs.get(int(run_id))
    if runner is None:
        return json.dumps({"error": "workflow run is not active"})
    if runner.owner_id != int(owner_id):
        return json.dumps({"error": "forbidden"})
    result = runner.retry_node(str(node_id))
    if result.get("error"):
        return json.dumps(result)
    # Persist final context if the runner has one.
    try:
        runner._persist_context()
    except Exception:
        pass
    return json.dumps(result)


def recover() -> None:
    ensure_schema()
    try:
        with db_get(_db_path()) as conn:
            rows = conn.execute("SELECT id, owner_id FROM workflow_runs WHERE status IN ('pending', 'running')").fetchall()
            for run_id, owner_id in rows:
                conn.execute("UPDATE workflow_node_runs SET status = 'pending' WHERE run_id = ? AND status = 'running'", (run_id,))
            conn.commit()
            for run_id, owner_id in rows:
                row = conn.execute("SELECT workflow_id, input_json FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
                if row is None:
                    continue
                workflow_row = conn.execute("SELECT definition FROM workflows WHERE id = ?", (row[0],)).fetchone()
                if workflow_row is None:
                    conn.execute("UPDATE workflow_runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?", (time.time(), "workflow not found", run_id))
                    conn.commit()
                    continue
                try:
                    workflow = json.loads(workflow_row[0])
                    input_obj = json.loads(row[1] or "{}")
                except Exception:
                    conn.execute("UPDATE workflow_runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?", (time.time(), "invalid workflow or input", run_id))
                    conn.commit()
                    continue
                runner = _Run(run_id, workflow, int(owner_id), input_obj, _pool, _args)
                with _runs_lock:
                    _runs[run_id] = runner
                runner.start()
    except Exception:
        pass


def stop() -> None:
    with _runs_lock:
        runners = list(_runs.values())
    for runner in runners:
        runner.cancel()
    # Give running nodes a short time to observe cancellation.
    time.sleep(0.2)
    with _runs_lock:
        _runs.clear()
    try:
        _run_semaphore.release()
    except Exception:
        pass
