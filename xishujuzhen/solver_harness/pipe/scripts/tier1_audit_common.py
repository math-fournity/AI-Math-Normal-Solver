#!/usr/bin/env python3
"""Shared read-only helpers for the tier=1 rerun audit scripts.

The helpers in this module deliberately do not update ArangoDB, Redis, or the
trajectory store.  They normalize the three historical retention formats used
by the project:

* ATIF ``exports/conversation.json``
* ``sessions_db/trajectory.jsonl``
* early MITM captures under ``mitm/``

The public function :func:`inspect_candidate_asset` answers the three questions
needed by the rerun audit: is reasoning retained, is a proof retained, and did
the run invoke any tools?
"""

from __future__ import annotations

import ast
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from arango import ArangoClient
import redis


EXPECTED_DB = "xishujuzhen_math_glm52"
BATCH_ID = "pipe-runner"
TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")
SOLVER_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-dir")
ORIGINAL_WORKTREE = Path("/Users/user/glm5.2-math-worktree")

INFRA_STATUSES = {
    "rate_limited",
    "dead_session",
    "crash_recovered",
    "failed_connection",
    "launch_error",
}
MODEL_STATUSES = {
    "failed_token_limit",
    "ai_gave_up",
    "failed_thinking_spin",
    "failed_tool_stall",
    "failed_no_proof",
    "failed_stall",
}
DATA_STATUSES = {"answer_leak", "answer_leak_in_input"}
NONTERMINAL_STATUSES = {"running", "queued", "stopped"}
PROOF_MARKERS = ("PROOF COMPLETE", "证明完成")


def connect_db():
    """Connect to the required ArangoDB and reject accidental DB fallback."""
    db_name = os.environ.get("ARANGO_DB", EXPECTED_DB)
    if db_name != EXPECTED_DB:
        raise RuntimeError(
            f"ARANGO_DB must be {EXPECTED_DB!r}, got {db_name!r}. "
            "Source the glm5.2-worktree .env before running the audit."
        )
    host = os.environ.get("ARANGO_HOST", "http://localhost:8529")
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "")
    client = ArangoClient(hosts=host, request_timeout=300)
    return client.db(db_name, username=user, password=password)


def connect_redis() -> redis.Redis:
    """Connect to the pipe Redis database in read-only call sites."""
    return redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)


def load_tier_problems(db, tier: int) -> dict[str, dict[str, Any]]:
    """Return tier problems keyed by ``problem_extraction_progress._key``."""
    query = """
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == @tier
  RETURN {
    key: p._key,
    problem_id: p.problem_id,
    source_dataset: p.source_dataset,
    problem_text: p.problem_text,
    extraction_status: p.extraction_status,
    external_ref: p.external_ref
  }
"""
    rows = db.aql.execute(query, bind_vars={"tier": tier}, ttl=300, batch_size=5000)
    return {row["key"]: row for row in rows}


def load_pipe_terminal_runs(db) -> list[dict[str, Any]]:
    """Load all terminal pipe runs in deterministic newest-first order."""
    query = """
FOR r IN devin_problem_runs
  FILTER r.batch_id == @batch_id
  FILTER r.status != "running"
  SORT r.problem_id, r.ended_at DESC
  RETURN {
    problem_id: r.problem_id,
    status: r.status,
    verdict: r.verdict,
    ended_at: r.ended_at,
    started_at: r.started_at,
    exp_id: r.exp_id,
    batch_id: r.batch_id,
    progress_key: r.progress_key,
    end_reason: r.end_reason
  }
"""
    return list(
        db.aql.execute(
            query,
            bind_vars={"batch_id": BATCH_ID},
            ttl=300,
            batch_size=5000,
        )
    )


def last_run_by_problem(runs: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Select the first row per problem from newest-first run rows."""
    result: dict[str, dict[str, Any]] = {}
    for run in runs:
        result.setdefault(str(run.get("problem_id")), run)
    return result


def load_nonpipe_runs_for_problems(
    db,
    problems: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Load historical non-pipe runs, accepting all three historical key forms."""
    keys = list(problems)
    text_ids = sorted(
        {
            str(problem.get("problem_id"))
            for problem in problems.values()
            if problem.get("problem_id")
        }
    )
    query = """
FOR r IN devin_problem_runs
  FILTER r.batch_id != @batch_id
  FILTER r.problem_id IN @keys
      OR r.problem_id IN @text_ids
      OR TO_STRING(r.progress_key) IN @keys
  SORT r.ended_at ASC
  RETURN {
    problem_id: r.problem_id,
    progress_key: TO_STRING(r.progress_key),
    status: r.status,
    verdict: r.verdict,
    ended_at: r.ended_at,
    started_at: r.started_at,
    exp_id: r.exp_id,
    batch_id: r.batch_id,
    end_reason: r.end_reason
  }
"""
    rows = list(
        db.aql.execute(
            query,
            bind_vars={"batch_id": BATCH_ID, "keys": keys, "text_ids": text_ids},
            ttl=300,
            batch_size=5000,
        )
    )

    alias: dict[str, str] = {key: key for key in problems}
    for key, problem in problems.items():
        text_id = problem.get("problem_id")
        if text_id:
            alias[str(text_id)] = key

    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in problems}
    for row in rows:
        key = alias.get(str(row.get("problem_id"))) or alias.get(
            str(row.get("progress_key"))
        )
        if key in grouped:
            grouped[key].append(row)
    return grouped


def file_size(path: Path) -> int:
    """Return a regular file's size, or zero when absent."""
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _read_json(path: Path) -> tuple[Any, str | None]:
    if not path.is_file():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace")), None
    except Exception as exc:  # the error text is part of the audit evidence
        return None, f"{type(exc).__name__}: {exc}"


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], []
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
            except Exception as exc:
                errors.append(f"line {line_number}: {type(exc).__name__}: {exc}")
    return rows, errors


def _contains_proof(text: str | None) -> bool:
    value = text or ""
    return any(marker in value for marker in PROOF_MARKERS)


def _tool_records(tool_calls: Any) -> list[dict[str, Any]]:
    if not tool_calls:
        return []
    if isinstance(tool_calls, dict):
        return [
            {"name": str(name), "arguments": value}
            for name, value in tool_calls.items()
        ]
    if not isinstance(tool_calls, list):
        return [{"name": "<unknown>", "arguments": None}]
    records: list[dict[str, Any]] = []
    for call in tool_calls:
        if isinstance(call, dict):
            records.append(
                {
                    "name": str(
                        call.get("function_name") or call.get("name") or "<unknown>"
                    ),
                    "arguments": call.get("arguments"),
                }
            )
        else:
            records.append({"name": "<unknown>", "arguments": None})
    return records


def _export_structure(data: Any) -> str:
    if data is None:
        return "missing"
    if not isinstance(data, dict):
        return "invalid:top_level_not_object"
    required = {"schema_version", "session_id", "agent", "steps", "final_metrics"}
    missing = sorted(required - set(data))
    if missing:
        return f"invalid:missing_keys:{','.join(missing)}"
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        return "invalid:steps_empty_or_not_list"
    if not any(isinstance(step, dict) and step.get("source") == "agent" for step in steps):
        return "invalid:no_agent_step"
    return "passed"


def inspect_candidate_asset(exp_id: str) -> dict[str, Any]:
    """Inspect retained reasoning, proof, and tools for one candidate run.

    Export data is authoritative when present.  Historical fallback formats are
    loaded only when export reasoning/proof is absent, which keeps the full
    29k-candidate audit reasonably fast while preserving old-run coverage.
    """
    trajectory_dir = TRAJECTORY_BASE / exp_id
    solver_dir = SOLVER_BASE / exp_id
    paths = {
        "session_info": trajectory_dir / "session_info.json",
        "export": trajectory_dir / "exports" / "conversation.json",
        "trajectory": trajectory_dir / "sessions_db" / "trajectory.jsonl",
        "pane": trajectory_dir / "collector" / "pane_snapshot.txt",
        "pane_clean": trajectory_dir / "collector" / "pane_snapshot_clean.txt",
        "tmux_log": trajectory_dir / "tmux" / "tmux.log",
        "tmux_pipe": trajectory_dir / "tmux" / "tmux_pipe.log",
        "proof_file": solver_dir / "proof.md",
        "mitm_readable": trajectory_dir / "mitm" / "thinking_readable.txt",
        "mitm_live_txt": trajectory_dir / "mitm" / "thinking_live.txt",
        "mitm_live_jsonl": trajectory_dir / "mitm" / "thinking_live.jsonl",
        "mitm_trajectory": trajectory_dir / "mitm" / "trajectory.jsonl",
    }
    sizes = {name: file_size(path) for name, path in paths.items()}
    exists = {name: path.is_file() for name, path in paths.items()}

    export, export_error = _read_json(paths["export"])
    export_steps = export.get("steps", []) if isinstance(export, dict) else []
    agent_steps = [
        step
        for step in export_steps
        if isinstance(step, dict) and step.get("source") == "agent"
    ]
    reasoning_sources: list[str] = []
    proof_sources: list[str] = []
    tool_records: list[dict[str, Any]] = []

    if any(bool(step.get("reasoning_content")) for step in agent_steps):
        reasoning_sources.append("export.reasoning_content")
    if any(_contains_proof(step.get("message")) for step in agent_steps):
        proof_sources.append("export.message")
    for step in agent_steps:
        tool_records.extend(_tool_records(step.get("tool_calls")))

    fallback_needed = not reasoning_sources or not proof_sources or not paths["export"].is_file()
    trajectory_rows: list[dict[str, Any]] = []
    trajectory_errors: list[str] = []
    mitm_rows: list[dict[str, Any]] = []
    mitm_errors: list[str] = []
    if fallback_needed:
        trajectory_rows, trajectory_errors = _read_jsonl(paths["trajectory"])
        if any(bool(row.get("thinking")) for row in trajectory_rows):
            reasoning_sources.append("trajectory.thinking")
        if any(_contains_proof(row.get("content")) for row in trajectory_rows):
            proof_sources.append("trajectory.content")
        for row in trajectory_rows:
            tool_records.extend(_tool_records(row.get("tool_calls")))

        mitm_rows, mitm_errors = _read_jsonl(paths["mitm_trajectory"])
        if any(bool(row.get("content_thinking")) for row in mitm_rows):
            reasoning_sources.append("mitm.trajectory.content_thinking")
        for row in mitm_rows:
            tool_records.extend(_tool_records(row.get("tool_calls")))

        if sizes["mitm_readable"] > 100:
            reasoning_sources.append("mitm.thinking_readable")
        if sizes["mitm_live_txt"] > 100:
            reasoning_sources.append("mitm.thinking_live_txt")
        if sizes["mitm_live_jsonl"] > 100:
            reasoning_sources.append("mitm.thinking_live_jsonl")

        if sizes["proof_file"] > 100:
            proof_sources.append("solver.proof_file")
        for pane_name in ("pane_clean", "pane"):
            pane_path = paths[pane_name]
            if pane_path.is_file():
                try:
                    if _contains_proof(
                        pane_path.read_text(encoding="utf-8", errors="replace")
                    ):
                        proof_sources.append(f"collector.{pane_name}")
                        break
                except OSError:
                    pass

    unique_tools = sorted({record["name"] for record in tool_records})
    valid = bool(reasoning_sources and proof_sources and not unique_tools)
    return {
        "exp_id": exp_id,
        "valid_reasoning_proof_no_tools": valid,
        "has_reasoning": bool(reasoning_sources),
        "has_proof": bool(proof_sources),
        "tool_names": unique_tools,
        "tool_calls": tool_records,
        "reasoning_sources": sorted(set(reasoning_sources)),
        "proof_sources": sorted(set(proof_sources)),
        "export_structure": _export_structure(export),
        "export_error": export_error,
        "trajectory_errors": trajectory_errors,
        "mitm_errors": mitm_errors,
        "exists": exists,
        "sizes": sizes,
    }


def extract_constant_set(path: Path, variable_name: str) -> set[str]:
    """Read a top-level literal set assignment without importing service code."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == variable_name
            for target in node.targets
        ):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, set):
            return {str(item) for item in value}
    raise ValueError(f"literal set {variable_name!r} not found in {path}")


def read_lean_problem(path: Path) -> str:
    """Extract the informal statement from a Compfiles Lean source."""
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"/-!\s*(.*?)\s*-/", text, re.DOTALL)
    if match:
        lines = [
            line.strip()
            for line in match.group(1).splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if lines:
            return "\n".join(lines)
    match = re.search(r"/-\s*(.*?)\s*-/", text, re.DOTALL)
    if match and "copyright" not in match.group(1).lower():
        return match.group(1).strip()
    match = re.search(r"problem\s+\w+\s*:\s*(.+?)\s*:=\s*by", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def json_dump(data: Any, export_path: str | None) -> None:
    """Print JSON and optionally persist the same result for independent review."""
    rendered = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if export_path:
        path = Path(export_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
        print(f"\nAudit JSON written to: {path}")
