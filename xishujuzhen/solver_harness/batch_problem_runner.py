#!/usr/bin/env python3
"""
Batch runner for Devin Solver experiments.

This script deliberately launches Solver instances only through
solver_harness.py. The harness owns tmux, mitmproxy, sessions.db polling,
pipe-pane capture, and export/decode; this runner owns problem selection,
batch metadata, database records, and coarse health observation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from arango import ArangoClient
except ImportError as exc:  # pragma: no cover - depends on local venv
    raise SystemExit(
        "python-arango is required. Run with the repo venv, e.g. "
        "./.venv/bin/python xishujuzhen/solver_harness/batch_problem_runner.py ..."
    ) from exc


REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "xishujuzhen" / "solver_harness" / "solver_harness.py"

EXPECTED_DB = "xishujuzhen_math_glm52"
SOLVER_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-dir")
TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")
BATCH_BASE = TRAJECTORY_BASE / "_batches"

BATCH_COLLECTION = "devin_batch_runs"
ATTEMPT_COLLECTION = "devin_problem_runs"
EVENT_COLLECTION = "devin_run_events"
COUNTER_COLLECTION = "devin_counters"

TERMINAL_STATUSES = {
    "candidate_solved",
    "failed_no_proof",
    "failed_token_limit",
    "failed_timeout",
    "failed_tool_stall",
    "rate_limited",
    "failed_connection",
    "launch_error",
    "stopped",
    "answer_leak",
    "dead_session",
}

RUNNING_STATUSES = {"launching", "running", "stalled_warning"}

RATE_LIMIT_PATTERNS = (
    "rate limit",
    "ratelimit",
    "too many requests",
    "quota exceeded",
    "api rate",
    "限流",
    "http 429",
    "status 429",
    "error 429",
)

TOKEN_LIMIT_PATTERNS = (
    "response truncated",
    "token limit",
    "maximum context",
    "context length",
    "exceeded the model",
)

CONNECTION_PATTERNS = (
    "connection failed",
    "connection reset",
    "connection error",
    "network error",
    "api error",
    "cognition.ai/errorKind",
    "unavailable",
    "retryable",
)

# 361号§5 verdict 两级判定映射。auto_status 是 runner 内部状态机值；
# runner_status 是 361号契约的外部判定词；failure_reason 是失败归因分类。
RUNNER_STATUS_MAP = {
    "candidate_solved": "candidate_solved",
    "failed_no_proof": "failed_no_proof",
    "failed_token_limit": "failed_token_limit",
    "failed_tool_stall": "failed_tool_stall",
    "rate_limited": "rate_limited",
    "failed_connection": "infra_error",
    "failed_timeout": "infra_error",
    "launch_error": "infra_error",
    "stopped": "stopped",
    "stalled_warning": "running",
    "running": "running",
    "launching": "running",
    "queued": "queued",
}

FAILURE_REASON_MAP = {
    "failed_token_limit": "token_limit",
    "rate_limited": "rate_limit",
    "failed_connection": "connection_error",
    "failed_no_proof": "no_final_write",
    "failed_tool_stall": "tool_overuse",
    "failed_timeout": "no_final_write",
    "launch_error": "connection_error",
    # candidate_solved / stopped / running / queued: 无 failure_reason
}


def make_verdict(
    auto_status: str,
    reason: str,
    *,
    confidence: str = "none",
    needs_human_math_review: bool = True,
    failure_summary: str = "",
) -> dict[str, Any]:
    """构造符合361号§5两级判定的 verdict 字典。

    runner_status: 运行层自动判定（candidate_solved/failed_*/rate_limited/infra_error/...）。
    math_review_status: 数学审查层判定，candidate_solved 时为 unreviewed，其余为 n/a。
    failure_reason: 失败归因分类（361号§10 taxonomy），仅失败状态有值。
    """
    runner_status = RUNNER_STATUS_MAP.get(auto_status, auto_status)
    failure_reason = FAILURE_REASON_MAP.get(auto_status, "")
    math_review_status = "unreviewed" if auto_status == "candidate_solved" else "n/a"
    return {
        "auto_status": auto_status,
        "runner_status": runner_status,
        "math_review_status": math_review_status,
        "confidence": confidence,
        "needs_human_math_review": needs_human_math_review,
        "failure_reason": failure_reason,
        "failure_summary": failure_summary or reason,
        "reviewer_notes_path": "",
        "reason": reason,
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def seconds_since(value: str | None) -> float | None:
    dt = parse_utc(value)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def slug(value: Any, max_len: int = 64) -> str:
    text = str(value or "unknown")
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    text = re.sub(r"-{2,}", "-", text)
    if not text:
        text = "unknown"
    return text[:max_len].strip("-._") or "unknown"


def stable_short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def connect_db():
    load_dotenv()
    db_name = os.environ.get("ARANGO_DB")
    if db_name != EXPECTED_DB:
        raise SystemExit(
            f"Refusing to run with ARANGO_DB={db_name!r}; expected {EXPECTED_DB!r}. "
            "Source .env or fix the environment before launching Solver experiments."
        )

    host = os.environ.get("ARANGO_HOST", "http://localhost:8529")
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "")
    client = ArangoClient(hosts=host)
    return client.db(db_name, username=user, password=password)


def ensure_schema(db) -> None:
    for name in (BATCH_COLLECTION, ATTEMPT_COLLECTION, EVENT_COLLECTION, COUNTER_COLLECTION):
        if not db.has_collection(name):
            db.create_collection(name)
    # 初始化run_id counter
    if not db.collection(COUNTER_COLLECTION).has("run_id"):
        db.collection(COUNTER_COLLECTION).insert({"_key": "run_id", "value": 0})


def next_run_id(db) -> int:
    """原子递增全局运行ID，确保每次attempt的exp_id唯一。
    用ArangoDB事务保证并发安全——多个monitor同时调也不会冲突。"""
    # 用ArangoDB的原子update+return
    cursor = db.aql.execute(
        'FOR c IN devin_counters FILTER c._key == "run_id" '
        'UPDATE c WITH {value: c.value + 1} IN devin_counters '
        'RETURN NEW.value'
    )
    return next(cursor)


def ensure_indexes(db) -> None:
    indexes = {
        BATCH_COLLECTION: [
            ("idx_status", ["status"], False),
            ("idx_created_at", ["created_at"], False),
        ],
        ATTEMPT_COLLECTION: [
            ("idx_batch_id", ["batch_id"], False),
            ("idx_progress_key", ["progress_key"], False),
            ("idx_problem_id", ["problem_id"], False),
            ("idx_status", ["status"], False),
            ("idx_exp_id", ["exp_id"], True),
        ],
        EVENT_COLLECTION: [
            ("idx_batch_id", ["batch_id"], False),
            ("idx_attempt_key", ["attempt_key"], False),
            ("idx_observed_at", ["observed_at"], False),
        ],
    }
    for collection_name, collection_indexes in indexes.items():
        col = db.collection(collection_name)
        for name, fields, unique in collection_indexes:
            try:
                if hasattr(col, "add_index"):
                    col.add_index({"type": "persistent", "fields": fields, "unique": unique, "name": name})
                else:
                    col.add_persistent_index(fields=fields, unique=unique, name=name)
            except Exception:
                # Index may already exist under this python-arango version.
                pass


def upsert(collection, doc: dict[str, Any]) -> None:
    existing = collection.get(doc["_key"])
    if existing:
        collection.update(doc)
    else:
        collection.insert(doc)


def insert_event(
    db,
    batch_id: str,
    event_type: str,
    detail: dict[str, Any] | None = None,
    attempt_key: str | None = None,
) -> None:
    doc = {
        "_key": f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:12]}",
        "batch_id": batch_id,
        "attempt_key": attempt_key,
        "event_type": event_type,
        "observed_at": utc_now(),
        "detail": detail or {},
    }
    db.collection(EVENT_COLLECTION).insert(doc)


def select_candidates(
    db,
    *,
    limit: int,
    tier: int | None,
    start_seq: int | None,
    end_seq: int | None,
    bare_ai_expected: list[str],
    problem_ids: list[str],
    progress_keys: list[str],
    allow_repeat: bool,
) -> list[dict[str, Any]]:
    ensure_schema(db)
    query = """
    FOR p IN problem_extraction_progress
      FILTER @tier == null OR p.difficulty_tier == @tier
      FILTER @start_seq == null OR p.global_sequence >= @start_seq
      FILTER @end_seq == null OR p.global_sequence <= @end_seq
      FILTER LENGTH(@problem_ids) == 0 OR p.problem_id IN @problem_ids
      FILTER LENGTH(@progress_keys) == 0 OR p._key IN @progress_keys
      LET profile = DOCUMENT(CONCAT("problem_profiles/", p.problem_id))
      FILTER profile != null
      FILTER HAS(profile, "problem_text") AND LENGTH(TRIM(profile.problem_text)) > 0
      FILTER LENGTH(@bare_ai_expected) == 0 OR profile.bare_ai_expected IN @bare_ai_expected
      FILTER @allow_repeat OR LENGTH(
        FOR r IN devin_problem_runs
          FILTER r.progress_key == p._key
          LIMIT 1
          RETURN 1
      ) == 0
      SORT p.global_sequence ASC, p._key ASC
      LIMIT @limit
      RETURN {progress: p, profile: profile}
    """
    cursor = db.aql.execute(
        query,
        bind_vars={
            "limit": limit,
            "tier": tier,
            "start_seq": start_seq,
            "end_seq": end_seq,
            "bare_ai_expected": bare_ai_expected,
            "problem_ids": problem_ids,
            "progress_keys": progress_keys,
            "allow_repeat": allow_repeat,
        },
    )
    return list(cursor)


def select_by_progress_for_feed(
    db: Any,
    *,
    tier: int,
    limit: int,
    batch_id: str,
) -> list[dict[str, Any]]:
    """持续喂入模式选题：从problem_extraction_progress选未跑过或需要重跑的tier题。
    排除已solved的题和当前batch中已有的题。曾经失败（dead_session/failed_connection等）的题可以重跑。"""
    query = """
    FOR p IN problem_extraction_progress
      FILTER p.difficulty_tier == @tier
      FILTER p.external_ref != null
      FILTER p.external_ref.local_path != null
      FILTER p.external_ref.local_path != ""
      // 排除已solved的题（不需要重跑）
      FILTER LENGTH(
        FOR r IN devin_problem_runs
          FILTER r.progress_key == p._key
          FILTER r.status == "candidate_solved"
          LIMIT 1
          RETURN 1
      ) == 0
      // 排除当前batch中已有的题（避免重复加入）
      FILTER LENGTH(
        FOR r IN devin_problem_runs
          FILTER r.progress_key == p._key
          FILTER r.batch_id == @batch_id
          LIMIT 1
          RETURN 1
      ) == 0
      SORT p.global_sequence ASC, p._key ASC
      LIMIT @limit
      RETURN p
    """
    cursor = db.aql.execute(query, bind_vars={"tier": tier, "limit": limit, "batch_id": batch_id})
    return list(cursor)


def load_problem_text_from_progress(progress: dict[str, Any]) -> str:
    """从progress的external_ref读源文件提取题面。复用extract_problem_text的逻辑。"""
    import pyarrow.parquet as pq
    import re as _re

    ext = progress.get("external_ref") or {}
    path = ext.get("local_path", "")
    idx = ext.get("original_index", 0)

    if not path:
        raise ValueError(f"no local_path in progress {progress.get('_key')}")

    if not os.path.isabs(path):
        repo_root = Path(__file__).resolve().parents[2]
        full_path = repo_root / path
    else:
        full_path = Path(path)

    if not full_path.exists():
        raise FileNotFoundError(f"source file not found: {full_path}")

    suffix = full_path.suffix.lower()

    if suffix == ".parquet":
        table = pq.read_table(str(full_path))
        if idx >= len(table):
            raise IndexError(f"parquet row {idx} out of range")
        row = table.slice(idx, 1).to_pydict()
        for col in ("problem_markdown", "problem", "question", "prompt"):
            if col in row and row[col]:
                val = row[col]
                if isinstance(val, list):
                    val = val[0] if val else ""
                return str(val).strip()
        raise KeyError(f"no problem column in {full_path}")

    elif suffix == ".jsonl":
        with open(full_path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i == idx:
                    d = json.loads(line)
                    for col in ("problem", "question", "prompt", "problem_text"):
                        if col in d and d[col]:
                            return str(d[col]).strip()
                    raise KeyError(f"no problem field in {full_path} row {idx}")
        raise IndexError(f"jsonl row {idx} not found in {full_path}")

    elif suffix == ".json":
        # JSON数组格式：支持FATE和MathArena两种schema
        with open(full_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            # 用original_id_in_source匹配id字段，fallback到数组索引
            src_id = ext.get("original_id_in_source")
            item = None
            if src_id is not None:
                for d in data:
                    if str(d.get("id", "")) == str(src_id):
                        item = d
                        break
            if item is None and idx < len(data):
                item = data[idx]
            if item is None:
                raise IndexError(f"json item not found (src_id={src_id}, idx={idx}, len={len(data)})")

            # FATE格式：item有informal_statement字段
            for col in ("informal_statement", "problem", "question", "prompt", "problem_text", "statement"):
                if col in item and item[col]:
                    text = str(item[col]).strip()
                    for cut_kw in ["Formalization notes", "## Formalization", "solution sketch", "The proof follows"]:
                        cut_idx = text.find(cut_kw)
                        if cut_idx > 0:
                            text = text[:cut_idx].strip()
                    return text

            # MathArena格式：item有columns字段（dict），columns里有problem字段
            if "columns" in item and isinstance(item["columns"], dict):
                cols = item["columns"]
                for col in ("problem", "problem_markdown", "question", "prompt", "problem_text"):
                    if col in cols and cols[col]:
                        return str(cols[col]).strip()

            raise KeyError(f"no problem field in {full_path}: keys={list(item.keys())}")
        raise ValueError(f"expected JSON array in {full_path}, got {type(data)}")

    elif suffix == ".lean":
        text = full_path.read_text(encoding="utf-8")
        # compfiles格式：/-! ... -/块注释（第一个块是题面）
        match = _re.search(r"/-!\s*(.*?)\s*-/", text, _re.DOTALL)
        if match:
            block = match.group(1).strip()
            lines = [l.strip() for l in block.split("\n") if l.strip() and not l.strip().startswith("#")]
            if lines:
                result = "\n".join(lines)
                # 安全截断：去掉"Formalization notes"等解答提示段
                for cut_keyword in ["Formalization notes", "## Formalization", "solution sketch", "The proof follows"]:
                    cut_idx = result.find(cut_keyword)
                    if cut_idx > 0:
                        result = result[:cut_idx].strip()
                return result
        # fallback: /- ... -/
        match = _re.search(r"/-\s*(.*?)\s*-/", text, _re.DOTALL)
        if match:
            block = match.group(1).strip()
            if "Copyright" not in block and "license" not in block.lower():
                lines = [l.strip() for l in block.split("\n") if l.strip() and not l.strip().startswith("#")]
                if lines:
                    result = "\n".join(lines)
                    for cut_keyword in ["Formalization notes", "## Formalization", "solution sketch", "The proof follows"]:
                        cut_idx = result.find(cut_keyword)
                        if cut_idx > 0:
                            result = result[:cut_idx].strip()
                    return result
        # fallback: problem声明
        match = _re.search(r"problem\s+\w+\s*:\s*(.+?)\s*:=\s*by", text, _re.DOTALL)
        if match:
            return match.group(1).strip()
        cutoff = text.find(":= by")
        if cutoff > 0:
            return text[:cutoff].strip()
        return text[:500].strip()

    else:
        raise ValueError(f"unsupported source format: {suffix}")


def make_batch_id(label: str | None) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = slug(label, 24) if label else "bare"
    return f"dpb-{stamp}-{suffix}"


def make_exp_id(batch_id: str, ordinal: int, progress_key: str, global_sequence: Any, problem_id: str, run_id: int = 0) -> str:
    pid = slug(problem_id, 54)
    # run_id是数据库自增的全局运行ID，确保exp_id唯一
    # 保留ordinal和progress_key便于人读，但run_id是唯一性保证
    if run_id:
        return f"{batch_id}-{ordinal:02d}-p{progress_key}-r{run_id:07d}-{pid}"
    # fallback：没有run_id时用旧的global_sequence格式（向后兼容）
    gseq = int(global_sequence or 0)
    return f"{batch_id}-{ordinal:02d}-p{progress_key}-g{gseq:06d}-{pid}"


def attempt_key(batch_id: str, progress_key: str, ordinal: int) -> str:
    return f"{batch_id}-{ordinal:02d}-p{progress_key}"


def build_problem_file_text(progress: dict[str, Any], profile: dict[str, Any]) -> str:
    problem_text = profile.get("problem_text") or ""
    source_dataset = progress.get("source_dataset") or profile.get("source_dataset") or "unknown"
    source_ref = progress.get("external_ref") or {}
    local_path = source_ref.get("local_path") or ""
    global_sequence = progress.get("global_sequence")
    difficulty_tier = progress.get("difficulty_tier")
    priority = progress.get("priority")
    problem_id = progress.get("problem_id") or profile.get("_key")
    progress_key = progress.get("_key")

    return "\n".join(
        [
            "# Solver Problem",
            "",
            "You are a mathematical problem solver. Solve the problem completely.",
            "Do not search for this exact problem, its official answer, or its solution.",
            "You may use computation for exploration or verification.",
            "Output your complete proof directly in your response (in this TUI).",
            "Do NOT write any files — do not use write/edit tools.",
            "End your proof with a line containing exactly: ### PROOF COMPLETE",
            "Your full reasoning and output are automatically captured by the system.",
            "",
            "## Metadata",
            "",
            f"- progress_key: {progress_key}",
            f"- global_sequence: {global_sequence}",
            f"- problem_id: {problem_id}",
            f"- source_dataset: {source_dataset}",
            f"- difficulty_tier: {difficulty_tier}",
            f"- priority: {priority}",
            f"- source_local_path: {local_path}",
            "",
            "## Problem",
            "",
            problem_text.strip(),
            "",
        ]
    )


def parse_case_spec(spec: str) -> tuple[str, Path, str | None]:
    parts = spec.split(":", 2)
    if len(parts) < 2:
        raise SystemExit(
            "Invalid --case. Use progress_key:path[:problem_id], e.g. "
            "383691:runs/vms_poc_0/vms8_problem_files/1631_bare.txt:mathnet_001631"
        )
    progress_key = parts[0].strip()
    file_path = Path(parts[1]).expanduser()
    problem_id = parts[2].strip() if len(parts) == 3 and parts[2].strip() else None
    if not progress_key:
        raise SystemExit(f"Invalid --case without progress_key: {spec}")
    if not file_path.is_absolute():
        file_path = REPO_ROOT / file_path
    if not file_path.exists():
        raise SystemExit(f"Problem file not found for --case {spec}: {file_path}")
    return progress_key, file_path, problem_id


def load_file_cases(db, case_specs: list[str]) -> list[dict[str, Any]]:
    ensure_schema(db)
    selected: list[dict[str, Any]] = []
    progress_col = db.collection("problem_extraction_progress")
    profile_col = db.collection("problem_profiles") if db.has_collection("problem_profiles") else None
    for spec in case_specs:
        progress_key, file_path, problem_id_override = parse_case_spec(spec)
        progress = progress_col.get(progress_key)
        if not progress:
            raise SystemExit(
                f"progress_key {progress_key!r} was not found in problem_extraction_progress. "
                "Use the numeric _key from the problem table so the run directory can be traced back."
            )
        problem_id = problem_id_override or progress.get("problem_id") or file_path.stem
        profile = profile_col.get(problem_id) if profile_col else None
        file_text = file_path.read_text(encoding="utf-8")
        selected.append(
            {
                "progress": progress,
                "profile": profile or {"_key": problem_id, "problem_text": file_text},
                "problem_file_text": file_text.rstrip() + "\n",
                "problem_id_override": problem_id,
                "source_mode": "manual_file",
                "case_metadata": {
                    "source_file": str(file_path),
                    "case_spec": spec,
                    "profile_found": bool(profile),
                },
            }
        )
    return selected


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """361号§6: launch_log.jsonl / status_snapshots.jsonl 追加写入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def tail_text(path: Path, max_bytes: int = 240_000) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        data = fh.read()
    return data.decode("utf-8", errors="ignore")


def contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(pattern in lower for pattern in patterns)


def tmux_running(session_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def tmux_pane_content(session_name: str) -> str:
    """捕获tmux pane当前可见内容。Devin CLI退出后pane变空白。"""
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", session_name, "-p"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
    return result.stdout.decode("utf-8", errors="ignore")


def tmux_pane_is_empty(session_name: str) -> bool:
    """判断tmux pane是否为空白（Devin CLI已退出但session还在）。

    空白判定：去掉空行和纯空白字符后内容长度<5。
    Devin CLI运行时pane总有内容（TUI界面、thinking输出等）。
    """
    content = tmux_pane_content(session_name)
    stripped = "\n".join(line.strip() for line in content.split("\n") if line.strip())
    return len(stripped) < 5


def run_harness(args: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(HARNESS), *args]
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n\n$ {' '.join(cmd)}\n")
        log.flush()
        result = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        log.write(f"\n[exit_code] {result.returncode}\n")
    return result.returncode


def solver_dir(exp_id: str) -> Path:
    return SOLVER_BASE / exp_id


def trajectory_dir(exp_id: str) -> Path:
    return TRAJECTORY_BASE / exp_id


def make_attempt_paths(exp_id: str) -> dict[str, str]:
    sdir = solver_dir(exp_id)
    tdir = trajectory_dir(exp_id)
    return {
        "solver_dir": str(sdir),
        "trajectory_dir": str(tdir),
        "proof_path": str(sdir / "proof.md"),
        "problem_path": str(sdir / "problem.txt"),
        "session_info_path": str(tdir / "session_info.json"),
        "tmux_log_path": str(tdir / "tmux" / "tmux.log"),
        "tmux_pipe_path": str(tdir / "tmux" / "tmux_pipe.log"),
        "export_path": str(tdir / "exports" / "conversation.json"),
        "db_trajectory_path": str(tdir / "sessions_db" / "trajectory.jsonl"),
        "thinking_readable_path": str(tdir / "mitm" / "thinking_readable.txt"),
        "mitm_thinking_live_jsonl": str(tdir / "mitm" / "thinking_live.jsonl"),
        "mitm_thinking_live_txt": str(tdir / "mitm" / "thinking_live.txt"),
        "mitm_trajectory_jsonl": str(tdir / "mitm" / "trajectory.jsonl"),
    }


def create_batch(
    db,
    *,
    batch_id: str,
    selected: list[dict[str, Any]],
    model: str,
    concurrency: int,
    selection: dict[str, Any],
    max_runtime_seconds: int | None,
    stall_seconds: int | None,
) -> list[dict[str, Any]]:
    batch_dir = BATCH_BASE / batch_id
    problems_dir = batch_dir / "problems"
    logs_dir = batch_dir / "logs"
    problems_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    attempts: list[dict[str, Any]] = []
    for ordinal, item in enumerate(selected, start=1):
        progress = item["progress"]
        profile = item["profile"]
        progress_key = str(progress["_key"])
        problem_id = item.get("problem_id_override") or progress.get("problem_id") or profile["_key"]
        run_id = next_run_id(db)
        exp_id = make_exp_id(batch_id, ordinal, progress_key, progress.get("global_sequence"), problem_id, run_id=run_id)
        key = attempt_key(batch_id, progress_key, ordinal)
        problem_source_path = problems_dir / f"{exp_id}.txt"
        problem_source_path.write_text(
            item.get("problem_file_text") or build_problem_file_text(progress, profile),
            encoding="utf-8",
        )
        paths = make_attempt_paths(exp_id)
        now = utc_now()
        doc = {
            "_key": key,
            "batch_id": batch_id,
            "ordinal": ordinal,
            "run_id": run_id,  # 数据库自增运行ID，确保exp_id唯一
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "model": model,
            "concurrency": concurrency,
            "progress_key": progress_key,
            "global_sequence": progress.get("global_sequence"),
            "problem_id": problem_id,
            "profile_doc_id": progress.get("profile_doc_id") or profile.get("_id"),
            "source_dataset": progress.get("source_dataset") or profile.get("source_dataset"),
            "source_mode": item.get("source_mode", "profile"),
            "case_metadata": item.get("case_metadata") or {},
            "difficulty_tier": progress.get("difficulty_tier"),
            "priority": progress.get("priority"),
            "exp_id": exp_id,
            "tmux_session": f"harness-{exp_id}",
            "problem_source_path": str(problem_source_path),
            "paths": paths,
            "observability": {
                "activity_signature": "",
                "last_observed_activity_at": None,
                "last_observed_at": None,
                "markers": {},
                "file_sizes": {},
            },
            "verdict": make_verdict("queued", "not launched", needs_human_math_review=False),
        }
        db.collection(ATTEMPT_COLLECTION).insert(doc)
        attempts.append(doc)

    batch_doc = {
        "_key": batch_id,
        "status": "created",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "model": model,
        "concurrency": concurrency,
        "selected_count": len(attempts),
        "attempt_keys": [doc["_key"] for doc in attempts],
        "selection": selection,
        "paths": {
            "batch_dir": str(batch_dir),
            "problems_dir": str(problems_dir),
            "logs_dir": str(logs_dir),
            "solver_base": str(SOLVER_BASE),
            "trajectory_base": str(TRAJECTORY_BASE),
        },
        "timeouts": {
            "max_runtime_seconds": max_runtime_seconds,
            "stall_seconds": stall_seconds,
        },
        "schema_version": "v0",
    }
    db.collection(BATCH_COLLECTION).insert(batch_doc)
    write_json(batch_dir / "batch_manifest.json", batch_doc | {"attempts": attempts})
    # 361号§6: selected_problems.jsonl 是输入冻结文件，正式启动后不允许修改。
    selected_jsonl = batch_dir / "selected_problems.jsonl"
    with selected_jsonl.open("w", encoding="utf-8") as fh:
        for item in selected:
            progress = item["progress"]
            profile = item["profile"]
            fh.write(
                json.dumps(
                    {
                        "progress_key": str(progress["_key"]),
                        "global_sequence": progress.get("global_sequence"),
                        "problem_id": item.get("problem_id_override") or progress.get("problem_id"),
                        "difficulty_tier": progress.get("difficulty_tier"),
                        "source_dataset": progress.get("source_dataset"),
                        "source_mode": item.get("source_mode", "profile"),
                        "problem_text_chars": len(profile.get("problem_text") or ""),
                        "case_metadata": item.get("case_metadata") or {},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    insert_event(db, batch_id, "batch_created", {"selected_count": len(attempts)})
    return attempts


def load_batch(db, batch_id: str) -> dict[str, Any]:
    doc = db.collection(BATCH_COLLECTION).get(batch_id)
    if not doc:
        raise SystemExit(f"Batch not found: {batch_id}")
    return doc


def load_attempts(db, batch_id: str) -> list[dict[str, Any]]:
    cursor = db.aql.execute(
        f"""
        FOR r IN {ATTEMPT_COLLECTION}
          FILTER r.batch_id == @batch_id
          SORT r.ordinal ASC
          RETURN r
        """,
        bind_vars={"batch_id": batch_id},
    )
    return list(cursor)


def update_batch_status(db, batch_id: str, status: str, extra: dict[str, Any] | None = None) -> None:
    doc = {"_key": batch_id, "status": status, "updated_at": utc_now()}
    if extra:
        doc.update(extra)
    db.collection(BATCH_COLLECTION).update(doc)


def launch_attempt(db, attempt: dict[str, Any], batch_dir: Path) -> dict[str, Any]:
    now = utc_now()
    attempt_key_value = attempt["_key"]
    launch_log = batch_dir / "logs" / f"launch-{attempt['exp_id']}.log"
    print(f"  [launch] START {attempt['problem_id']} (exp={attempt['exp_id'][:60]}...)", flush=True)
    t0 = time.time()
    db.collection(ATTEMPT_COLLECTION).update(
        {
            "_key": attempt_key_value,
            "status": "launching",
            "launch_started_at": now,
            "updated_at": now,
            "verdict": make_verdict("launching", "solver_harness launch in progress"),
        }
    )
    insert_event(
        db,
        attempt["batch_id"],
        "attempt_launching",
        {"exp_id": attempt["exp_id"], "log_path": str(launch_log)},
        attempt_key=attempt_key_value,
    )
    code = run_harness(
        [
            "launch",
            "--exp-id",
            attempt["exp_id"],
            "--problem-file",
            attempt["problem_source_path"],
            "--model",
            attempt["model"],
            "--no-mitm",
            "--interactive",
        ],
        launch_log,
    )
    t1 = time.time()
    print(f"  [launch] DONE {attempt['problem_id']} exit={code} took={t1-t0:.1f}s", flush=True)
    # 361号§6: launch_log.jsonl 记录每次 launch 事件。
    append_jsonl(
        batch_dir / "launch_log.jsonl",
        {
            "attempt_key": attempt_key_value,
            "exp_id": attempt["exp_id"],
            "progress_key": attempt["progress_key"],
            "problem_id": attempt["problem_id"],
            "launch_started_at": now,
            "log_path": str(launch_log),
        },
    )
    session_info = read_json(Path(attempt["paths"]["session_info_path"])) or {}
    update = {
        "_key": attempt_key_value,
        "launch_completed_at": utc_now(),
        "updated_at": utc_now(),
        "launch_exit_code": code,
        "devin_session_id": session_info.get("devin_session_id"),
        "session_info": session_info,
    }
    if code == 0:
        update["status"] = "running"
        update["started_at"] = session_info.get("started_at") or utc_now()
        update["observability"] = observe_attempt_files(attempt, old_observability=attempt.get("observability") or {})
        update["verdict"] = make_verdict("running", "tmux solver launched through solver_harness")
        insert_event(db, attempt["batch_id"], "attempt_running", {"exp_id": attempt["exp_id"]}, attempt_key=attempt_key_value)
    else:
        update["status"] = "launch_error"
        update["ended_at"] = utc_now()
        update["verdict"] = make_verdict(
            "launch_error",
            f"solver_harness launch exited with {code}",
            confidence="high",
            needs_human_math_review=False,
        )
        insert_event(
            db,
            attempt["batch_id"],
            "attempt_launch_error",
            {"exp_id": attempt["exp_id"], "exit_code": code},
            attempt_key=attempt_key_value,
        )
    db.collection(ATTEMPT_COLLECTION).update(update)
    refreshed = db.collection(ATTEMPT_COLLECTION).get(attempt_key_value)
    return refreshed


def observe_attempt_files(attempt: dict[str, Any], *, old_observability: dict[str, Any]) -> dict[str, Any]:
    paths = attempt["paths"]
    path_map = {name: Path(value) for name, value in paths.items() if name.endswith("_path")}
    sizes = {name: file_size(path) for name, path in path_map.items()}

    log_text = "\n".join(
        [
            tail_text(Path(paths["tmux_log_path"])),
            tail_text(Path(paths["tmux_pipe_path"])),
            tail_text(Path(paths["thinking_readable_path"])),
        ]
    )
    markers = {
        "rate_limited": contains_any(log_text, RATE_LIMIT_PATTERNS),
        "token_limited": contains_any(log_text, TOKEN_LIMIT_PATTERNS),
        "connection_error": contains_any(log_text, CONNECTION_PATTERNS),
    }
    proof_path = Path(paths["proof_path"])
    proof_exists = proof_path.exists() and proof_path.stat().st_size > 0
    # TUI模式：用tmux capture-pane直接抓pane内容检测PROOF COMPLETE
    # 比扫pipe更可靠——pane是当前显示内容，不受ANSI过滤/UTF-8截断影响
    tmux_session = attempt.get("tmux_session") or ""
    pane_text = ""
    if tmux_session:
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", tmux_session, "-p", "-S", "-500"],
                capture_output=True, text=True, timeout=10
            )
            pane_text = result.stdout
        except Exception:
            pass
    proof_in_tui = "PROOF COMPLETE" in pane_text
    markers["proof_exists"] = proof_exists or proof_in_tui
    markers["proof_in_tui"] = proof_in_tui
    # 答案泄漏自检
    answer_leak = "ANSWER LEAK DETECTED" in pane_text
    markers["answer_leak"] = answer_leak

    # activity_signature只基于有意义的文件（排除tmux_log_path——tmux自己会写它）
    # 僵尸session检测：tmux session还在但Devin CLI已退出，pane变空白
    meaningful_sizes = {
        k: v for k, v in sizes.items()
        if k not in ("tmux_log_path", "session_info_path")
    }
    activity_signature = stable_short_hash(json.dumps(meaningful_sizes, sort_keys=True))
    last_activity = old_observability.get("last_observed_activity_at")
    if activity_signature != old_observability.get("activity_signature"):
        last_activity = utc_now()

    # 僵尸session检测：tmux pane空白 = Devin CLI已退出
    pane_empty = False
    tmux_session = attempt.get("tmux_session")
    if tmux_session and tmux_running(tmux_session):
        pane_empty = tmux_pane_is_empty(tmux_session)

    return {
        "activity_signature": activity_signature,
        "last_observed_activity_at": last_activity,
        "last_observed_at": utc_now(),
        "markers": markers,
        "file_sizes": sizes,
        "pane_empty": pane_empty,
    }


def capture_thinking(attempt: dict[str, Any]) -> str:
    """Ctrl+O展开thinking后capture-pane抓取完整thinking内容。
    交互模式下thinking在TUI scrollback中，export只有最终输出，pipe只有UI行。
    必须发Ctrl+O展开后capture-pane才能抓到thinking文本。"""
    tmux_session = attempt.get("tmux_session") or ""
    if not tmux_session:
        return ""
    try:
        # 发Ctrl+O展开thinking
        subprocess.run(["tmux", "send-keys", "-t", tmux_session, "Ctrl+O"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
        time.sleep(2)
        # capture-pane抓scrollback（-S -5000抓历史5000行）
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", tmux_session, "-p", "-S", "-5000"],
            capture_output=True, text=True, timeout=15
        )
        thinking_text = result.stdout
        # 存到thinking_capture.txt
        paths = attempt.get("paths", {})
        capture_path = Path(paths.get("tmux_pipe_path", "")).parent / "thinking_capture.txt"
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_path.write_text(thinking_text, encoding="utf-8")
        return thinking_text
    except Exception:
        return ""


def stop_attempt(db, attempt: dict[str, Any], batch_dir: Path, *, decode: bool) -> int:
    # 交互模式：stop之前先抓thinking（Ctrl+O展开+capture-pane）
    # export只有最终输出，thinking在TUI scrollback中，必须主动抓
    capture_thinking(attempt)
    stop_log = batch_dir / "logs" / f"stop-{attempt['exp_id']}.log"
    args = ["stop", "--exp-id", attempt["exp_id"]]
    if not decode:
        args.append("--no-decode")
    code = run_harness(args, stop_log)
    db.collection(ATTEMPT_COLLECTION).update(
        {
            "_key": attempt["_key"],
            "harness_stop_exit_code": code,
            "harness_stopped_at": utc_now(),
            "updated_at": utc_now(),
        }
    )
    insert_event(
        db,
        attempt["batch_id"],
        "attempt_harness_stopped",
        {"exp_id": attempt["exp_id"], "exit_code": code, "decode": decode},
        attempt_key=attempt["_key"],
    )
    return code


def decode_all(db, batch_id: str, batch_dir: Path) -> int:
    decode_log = batch_dir / "logs" / "decode-all.log"
    code = run_harness(["decode-all"], decode_log)
    update_batch_status(
        db,
        batch_id,
        "decoded" if code == 0 else "decode_error",
        {"decoded_at": utc_now(), "decode_exit_code": code},
    )
    insert_event(db, batch_id, "batch_decoded", {"exit_code": code, "log_path": str(decode_log)})
    return code


def write_final_report(db, batch_id: str, batch_dir: Path) -> Path:
    """361号§12.7: 批次结束生成 final_report.md，含每题 solved/failed/verdict/failure_reason。"""
    batch = load_batch(db, batch_id)
    attempts = load_attempts(db, batch_id)
    report_path = batch_dir / "final_report.md"

    counts: dict[str, int] = {}
    for a in attempts:
        counts[a.get("status", "unknown")] = counts.get(a.get("status", "unknown"), 0) + 1

    lines = [
        f"# Final Report — batch `{batch_id}`",
        "",
        f"- model: `{batch.get('model')}`",
        f"- concurrency: {batch.get('concurrency')}",
        f"- selected_count: {batch.get('selected_count')}",
        f"- batch status: {batch.get('status')}",
        f"- created_at: {batch.get('created_at')}",
        f"- completed_at: {batch.get('completed_at') or batch.get('stopped_at') or utc_now()}",
        "",
        "## Status counts",
        "",
        "| status | count |",
        "|---|---:|",
    ]
    for status in sorted(counts):
        lines.append(f"| {status} | {counts[status]} |")

    lines += [
        "",
        "## Per-problem verdicts",
        "",
        "| # | progress_key | problem_id | status | runner_status | failure_reason | proof | math_review | exp_id |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for a in attempts:
        v = a.get("verdict") or {}
        obs = a.get("observability") or {}
        markers = obs.get("markers") or {}
        lines.append(
            f"| {a.get('ordinal')} | p{a.get('progress_key')} | {a.get('problem_id')} "
            f"| {a.get('status')} | {v.get('runner_status', '')} | {v.get('failure_reason', '')} "
            f"| {'yes' if markers.get('proof_exists') else 'no'} "
            f"| {v.get('math_review_status', '')} | `{a.get('exp_id')}` |"
        )

    lines += [
        "",
        "## Failure summaries",
        "",
    ]
    for a in attempts:
        v = a.get("verdict") or {}
        if v.get("failure_reason"):
            lines.append(
                f"- **p{a.get('progress_key')} {a.get('problem_id')}** "
                f"({v.get('runner_status')} / {v.get('failure_reason')}): "
                f"{v.get('failure_summary', v.get('reason', ''))}"
            )

    lines += [
        "",
        "## Candidate-solved (awaiting math review)",
        "",
    ]
    candidates = [a for a in attempts if a.get("status") == "candidate_solved"]
    if not candidates:
        lines.append("_None._")
    for a in candidates:
        lines.append(
            f"- p{a.get('progress_key')} {a.get('problem_id')} → `{a.get('exp_id')}` "
            f"proof: `{a['paths']['proof_path']}`"
        )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    insert_event(db, batch_id, "final_report_written", {"path": str(report_path)})
    return report_path


def classify_finished(attempt: dict[str, Any], observability: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    markers = observability.get("markers", {})
    # 答案泄漏：最高优先级——Solver自检发现题目中有解答泄漏
    if markers.get("answer_leak"):
        return (
            "answer_leak",
            make_verdict(
                "answer_leak",
                "Solver detected answer/solution leak in problem text; attempt stopped",
                confidence="high",
                needs_human_math_review=False,
            ),
        )
    if markers.get("proof_exists"):
        proof_method = "TUI ### PROOF COMPLETE marker" if markers.get("proof_in_tui") else "proof.md file"
        return (
            "candidate_solved",
            make_verdict(
                "candidate_solved",
                f"{proof_method} detected; mathematical correctness not reviewed by this runner",
                confidence="medium",
            ),
        )
    if markers.get("rate_limited"):
        return (
            "rate_limited",
            make_verdict(
                "rate_limited",
                "rate-limit marker found in tmux/thinking logs and no proof.md exists",
                confidence="high",
                needs_human_math_review=False,
            ),
        )
    if markers.get("token_limited"):
        return (
            "failed_token_limit",
            make_verdict(
                "failed_token_limit",
                "token/context-limit marker found and no proof.md exists",
                confidence="high",
                needs_human_math_review=False,
            ),
        )
    if markers.get("connection_error"):
        return (
            "failed_connection",
            make_verdict(
                "failed_connection",
                "connection/API error marker found and no proof.md exists",
                confidence="high",
                needs_human_math_review=False,
            ),
        )
    return (
        "failed_no_proof",
        make_verdict(
            "failed_no_proof",
            "tmux session ended without proof.md and without a known infrastructure marker",
            confidence="medium",
        ),
    )


def refresh_attempt(
    db,
    attempt: dict[str, Any],
    batch_dir: Path,
    *,
    max_runtime_seconds: int | None,
    stall_seconds: int | None,
    stop_on_stall: bool,
) -> dict[str, Any]:
    status = attempt.get("status")
    if status in TERMINAL_STATUSES or status == "queued":
        return attempt

    observability = observe_attempt_files(attempt, old_observability=attempt.get("observability") or {})
    is_running = tmux_running(attempt["tmux_session"])
    elapsed = seconds_since(attempt.get("started_at") or attempt.get("launch_completed_at")) or 0
    idle = seconds_since(observability.get("last_observed_activity_at")) or 0
    update: dict[str, Any] = {
        "_key": attempt["_key"],
        "observability": observability,
        "updated_at": utc_now(),
        "last_tmux_running": is_running,
        "runtime_seconds": int(elapsed),
        "idle_seconds": int(idle),
    }

    # 答案泄漏自检：Solver输出### ANSWER LEAK DETECTED时立即停止
    if is_running and observability.get("markers", {}).get("answer_leak"):
        stop_attempt(db, attempt, batch_dir, decode=False)
        final_status, verdict = classify_finished(attempt, observability)
        update.update({
            "status": final_status,
            "ended_at": utc_now(),
            "end_reason": "answer_leak_detected_by_solver",
            "verdict": verdict,
        })
        insert_event(
            db, attempt["batch_id"], "answer_leak_detected",
            {"exp_id": attempt["exp_id"], "problem_id": attempt.get("problem_id")},
            attempt_key=attempt["_key"],
        )
    # 僵尸session检测：tmux session还在但Devin CLI已退出（pane空白）
    # 这种情况下tmux_running=True但实际Solver已死，必须主动kill并判定终态
    elif is_running and observability.get("pane_empty"):
        stop_attempt(db, attempt, batch_dir, decode=False)
        # 先kill tmux session（stop_attempt可能只停harness不kill tmux）
        subprocess.run(["tmux", "kill-session", "-t", attempt["tmux_session"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        final_status, verdict = classify_finished(attempt, observability)
        # 如果classify没识别出具体错误，覆盖为dead_session
        if final_status == "failed_no_proof":
            final_status = "dead_session"
            verdict = make_verdict(
                "dead_session",
                "tmux session alive but pane empty — Devin CLI exited (likely API connection error)",
                confidence="high",
                needs_human_math_review=False,
            )
        update.update({
            "status": final_status,
            "ended_at": utc_now(),
            "end_reason": "dead_session_pane_empty",
            "verdict": verdict,
        })
        insert_event(
            db, attempt["batch_id"], "dead_session_detected",
            {"exp_id": attempt["exp_id"], "problem_id": attempt.get("problem_id"),
             "runtime_seconds": int(elapsed)},
            attempt_key=attempt["_key"],
        )
    elif is_running and max_runtime_seconds and elapsed > max_runtime_seconds:
        stop_attempt(db, attempt, batch_dir, decode=False)
        update.update(
            {
                "status": "failed_timeout",
                "ended_at": utc_now(),
                "end_reason": "max_runtime_seconds",
                "verdict": make_verdict(
                    "failed_timeout",
                    f"runtime exceeded {max_runtime_seconds} seconds",
                    confidence="high",
                    needs_human_math_review=False,
                ),
            }
        )
        insert_event(
            db,
            attempt["batch_id"],
            "attempt_timeout",
            {"exp_id": attempt["exp_id"], "runtime_seconds": int(elapsed)},
            attempt_key=attempt["_key"],
        )
    elif is_running and stall_seconds and idle > stall_seconds:
        if stop_on_stall:
            stop_attempt(db, attempt, batch_dir, decode=False)
            update.update(
                {
                    "status": "failed_tool_stall",
                    "ended_at": utc_now(),
                    "end_reason": "stall_seconds",
                    "verdict": make_verdict(
                        "failed_tool_stall",
                        f"no observed trajectory/log activity for {int(idle)} seconds",
                        confidence="medium",
                    ),
                }
            )
            insert_event(
                db,
                attempt["batch_id"],
                "attempt_stall_stopped",
                {"exp_id": attempt["exp_id"], "idle_seconds": int(idle)},
                attempt_key=attempt["_key"],
            )
        else:
            update.update(
                {
                    "status": "stalled_warning",
                    "verdict": make_verdict(
                        "stalled_warning",
                        f"no observed trajectory/log activity for {int(idle)} seconds; tmux still running",
                        confidence="medium",
                    ),
                }
            )
    elif is_running and observability.get("markers", {}).get("proof_in_tui"):
        # PROOF COMPLETE detected in pane but session still running (interactive mode)
        # 主动stop并判定candidate_solved
        # 先stop_attempt（让devin cli写export），等10秒再kill tmux（确保export落盘）
        stop_attempt(db, attempt, batch_dir, decode=False)
        time.sleep(10)  # 等devin cli写完export文件
        subprocess.run(["tmux", "kill-session", "-t", attempt["tmux_session"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        update.update({
            "status": "candidate_solved",
            "ended_at": utc_now(),
            "end_reason": "proof_in_tui_detected",
            "verdict": make_verdict(
                "candidate_solved",
                "TUI PROOF COMPLETE marker detected; mathematical correctness not reviewed",
                confidence="medium",
            ),
        })
        insert_event(
            db, attempt["batch_id"], "attempt_solved",
            {"exp_id": attempt["exp_id"], "problem_id": attempt.get("problem_id"),
             "method": "tui_pane_capture"},
            attempt_key=attempt["_key"],
        )
    elif is_running:
        update.update(
            {
                "status": "running",
                "verdict": make_verdict("running", "tmux session is still running"),
            }
        )
    else:
        if not attempt.get("harness_stopped_at"):
            stop_attempt(db, attempt, batch_dir, decode=False)
        final_status, verdict = classify_finished(attempt, observability)
        update.update(
            {
                "status": final_status,
                "ended_at": utc_now(),
                "end_reason": "tmux_session_ended",
                "verdict": verdict,
            }
        )
        insert_event(
            db,
            attempt["batch_id"],
            "attempt_terminal",
            {"exp_id": attempt["exp_id"], "status": final_status},
            attempt_key=attempt["_key"],
        )

    db.collection(ATTEMPT_COLLECTION).update(update)
    return db.collection(ATTEMPT_COLLECTION).get(attempt["_key"])


def print_status(attempts: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = {}
    for attempt in attempts:
        counts[attempt.get("status", "unknown")] = counts.get(attempt.get("status", "unknown"), 0) + 1
    print(json.dumps({"counts": counts, "total": len(attempts)}, ensure_ascii=False, sort_keys=True))
    for attempt in attempts:
        obs = attempt.get("observability") or {}
        markers = obs.get("markers") or {}
        sizes = obs.get("file_sizes") or {}
        thinking_bytes = sizes.get("thinking_readable_path", 0)
        active_markers = [k for k, v in markers.items() if v and k != "proof_exists"]
        marker_str = ",".join(active_markers) if active_markers else "-"
        last_activity = obs.get("last_observed_activity_at") or "-"
        print(
            f"{attempt['ordinal']:02d} {attempt['status']:<18} "
            f"p{attempt['progress_key']} g{attempt.get('global_sequence')} "
            f"{attempt['problem_id']} proof={'TUI' if markers.get('proof_in_tui') else ('file' if markers.get('proof_exists') else False)} "
            f"think={thinking_bytes}B idle={attempt.get('idle_seconds')}s "
            f"tmux={attempt.get('tmux_session')} "
            f"last={last_activity} markers={marker_str}"
        )


def monitor_batch(
    db,
    batch_id: str,
    *,
    poll_seconds: int,
    max_runtime_seconds: int | None,
    stall_seconds: int | None,
    stop_on_stall: bool,
    launch_queued: bool,
    decode: bool,
    once: bool,
    launch_interval_seconds: int = 8,
    rate_limit_pause: bool = True,
    continuous: bool = False,
    feed_tier: int = 1,
    feed_batch_size: int = 10,
) -> None:
    batch = load_batch(db, batch_id)
    batch_dir = Path(batch["paths"]["batch_dir"])
    update_batch_status(db, batch_id, "running")
    insert_event(db, batch_id, "monitor_started", {"poll_seconds": poll_seconds, "continuous": continuous, "feed_tier": feed_tier})
    rate_limit_warned = False
    last_concurrency = int(batch["concurrency"])

    while True:
        # 动态并发：每轮从DB读取batch.concurrency，支持运行中通过set-concurrency命令调整
        batch = load_batch(db, batch_id)
        concurrency = int(batch.get("concurrency", last_concurrency))
        if concurrency != last_concurrency:
            insert_event(db, batch_id, "concurrency_changed", {"old": last_concurrency, "new": concurrency})
            print(f"  [dynamic] concurrency {last_concurrency} → {concurrency}")
            last_concurrency = concurrency

        attempts = load_attempts(db, batch_id)
        running_count = sum(1 for item in attempts if item.get("status") in RUNNING_STATUSES)
        queued_count = sum(1 for item in attempts if item.get("status") == "queued")
        # 361号§7: 限流感知——任一运行中 attempt 出现 rate_limited marker 时暂停补新题。
        rate_limited_active = any(
            (item.get("observability") or {}).get("markers", {}).get("rate_limited")
            and item.get("status") in RUNNING_STATUSES
            for item in attempts
        )
        if rate_limit_pause and rate_limited_active:
            if not rate_limit_warned:
                update_batch_status(db, batch_id, "rate_limited_warning")
                insert_event(
                    db,
                    batch_id,
                    "rate_limited_pause",
                    {"reason": "rate-limit marker on a running attempt; pausing new launches"},
                )
                rate_limit_warned = True
        elif rate_limit_warned and not rate_limited_active:
            # 限流缓解后恢复
            update_batch_status(db, batch_id, "running")
            insert_event(db, batch_id, "rate_limited_resumed", {})
            rate_limit_warned = False

        # 持续喂入模式：running+queued < concurrency时，自动从DB选题追加
        if continuous and not (rate_limit_pause and rate_limited_active):
            slots_available = concurrency - running_count - queued_count
            if slots_available > 0:
                feed_count = min(slots_available, feed_batch_size)
                print(f"  [feed] slots_available={slots_available} running={running_count} queued={queued_count} concurrency={concurrency} → trying feed_count={feed_count}", flush=True)
                new_items = select_by_progress_for_feed(db, tier=feed_tier, limit=feed_count, batch_id=batch_id)
                print(f"  [feed] select_by_progress returned {len(new_items)} items", flush=True)
                if new_items:
                    # 提取题面
                    fed = []
                    for prog in new_items:
                        try:
                            text = load_problem_text_from_progress(prog)
                            if text and len(text.strip()) >= 10:
                                fed.append({
                                    "progress": prog,
                                    "profile": {"_key": prog.get("problem_id", f"p{prog['_key']}")},
                                    "problem_file_text": text.strip() + "\n",
                                    "problem_id_override": prog.get("problem_id"),
                                    "source_mode": "continuous_feed",
                                    "case_metadata": {"feed_tier": feed_tier},
                                })
                        except Exception as e:
                            print(f"  [feed] SKIP p{prog['_key']}: {e}")
                    if fed:
                        add_cases_to_batch(db, batch_id, fed, batch.get("model", "glm-5-2"))
                        print(f"  [feed] added {len(fed)} new problems (slots={slots_available}, tier={feed_tier})")
                        # 重新加载attempts
                        attempts = load_attempts(db, batch_id)
                        queued_count = sum(1 for item in attempts if item.get("status") == "queued")

        if launch_queued and not (rate_limit_pause and rate_limited_active):
            queued_to_launch = [a for a in attempts if a.get("status") == "queued"]
            print(f"  [launch] queued={len(queued_to_launch)} running={running_count} concurrency={concurrency}", flush=True)
            for attempt in attempts:
                if running_count >= concurrency:
                    print(f"  [launch] running_count={running_count} >= concurrency={concurrency}, stop", flush=True)
                    break
                if attempt.get("status") == "queued":
                    attempt = launch_attempt(db, attempt, batch_dir)
                    running_count += 1 if attempt.get("status") in RUNNING_STATUSES else 0
                    print(f"  [launch] after launch: running_count={running_count}, status={attempt.get('status')}", flush=True)
                    # 361号§7: 每次成功 launch 后间隔，避免瞬间打满。
                    if attempt.get("status") in RUNNING_STATUSES and launch_interval_seconds > 0:
                        time.sleep(launch_interval_seconds)

        refreshed: list[dict[str, Any]] = []
        for attempt in load_attempts(db, batch_id):
            refreshed.append(
                refresh_attempt(
                    db,
                    attempt,
                    batch_dir,
                    max_runtime_seconds=max_runtime_seconds,
                    stall_seconds=stall_seconds,
                    stop_on_stall=stop_on_stall,
                )
            )

        print_status(refreshed)
        # 361号§6: status_snapshots.jsonl 记录每轮观察快照。
        append_jsonl(
            batch_dir / "status_snapshots.jsonl",
            {
                "observed_at": utc_now(),
                "counts": {
                    status: sum(1 for item in refreshed if item.get("status") == status)
                    for status in sorted({item.get("status") for item in refreshed})
                },
                "attempts": [
                    {
                        "attempt_key": item["_key"],
                        "status": item.get("status"),
                        "runtime_seconds": item.get("runtime_seconds"),
                        "idle_seconds": item.get("idle_seconds"),
                        "proof_exists": (item.get("observability") or {}).get("markers", {}).get("proof_exists"),
                    }
                    for item in refreshed
                ],
            },
        )
        terminal_count = sum(1 for item in refreshed if item.get("status") in TERMINAL_STATUSES)
        queued_count = sum(1 for item in refreshed if item.get("status") == "queued")
        # continuous模式：所有题终结且feed源耗尽时才完成
        if continuous:
            feed_exhausted = len(select_by_progress_for_feed(db, tier=feed_tier, limit=1, batch_id=batch_id)) == 0
            if terminal_count == len(refreshed) and queued_count == 0 and feed_exhausted:
                if decode:
                    decode_all(db, batch_id, batch_dir)
                status_counts = {
                    status: sum(1 for item in refreshed if item.get("status") == status)
                    for status in sorted({item.get("status") for item in refreshed})
                }
                update_batch_status(db, batch_id, "completed", {"completed_at": utc_now(), "terminal_count": terminal_count, "status_counts": status_counts})
                write_final_report(db, batch_id, batch_dir)
                insert_event(db, batch_id, "batch_completed", {"terminal_count": terminal_count, "feed_exhausted": True})
                print(f"\n  [continuous] FEED EXHAUSTED — all tier {feed_tier} problems have been processed.")
                return
        elif terminal_count == len(refreshed) and queued_count == 0:
            if decode:
                decode_all(db, batch_id, batch_dir)
            status_counts = {
                status: sum(1 for item in refreshed if item.get("status") == status)
                for status in sorted({item.get("status") for item in refreshed})
            }
            update_batch_status(
                db,
                batch_id,
                "completed",
                {
                    "completed_at": utc_now(),
                    "terminal_count": terminal_count,
                    "status_counts": status_counts,
                },
            )
            write_final_report(db, batch_id, batch_dir)
            insert_event(db, batch_id, "batch_completed", {"terminal_count": terminal_count})
            return

        if once:
            update_batch_status(db, batch_id, "observed")
            return
        time.sleep(poll_seconds)


def cmd_select(args: argparse.Namespace) -> int:
    db = connect_db()
    selected = select_candidates(
        db,
        limit=args.limit,
        tier=args.tier,
        start_seq=args.start_seq,
        end_seq=args.end_seq,
        bare_ai_expected=args.bare_ai_expected or [],
        problem_ids=args.problem_id or [],
        progress_keys=args.progress_key or [],
        allow_repeat=args.allow_repeat,
    )
    rows = []
    for item in selected:
        progress = item["progress"]
        profile = item["profile"]
        rows.append(
            {
                "progress_key": progress["_key"],
                "global_sequence": progress.get("global_sequence"),
                "problem_id": progress.get("problem_id"),
                "difficulty_tier": progress.get("difficulty_tier"),
                "source_dataset": progress.get("source_dataset"),
                "domain": profile.get("domain"),
                "problem_text_chars": len(profile.get("problem_text") or ""),
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    db = connect_db()
    ensure_schema(db)
    selected = select_candidates(
        db,
        limit=args.limit,
        tier=args.tier,
        start_seq=args.start_seq,
        end_seq=args.end_seq,
        bare_ai_expected=args.bare_ai_expected or [],
        problem_ids=args.problem_id or [],
        progress_keys=args.progress_key or [],
        allow_repeat=args.allow_repeat,
    )
    if not selected:
        raise SystemExit("No candidate problems selected.")

    batch_id = args.batch_id or make_batch_id(args.label)
    selection = {
        "limit": args.limit,
        "tier": args.tier,
        "start_seq": args.start_seq,
        "end_seq": args.end_seq,
        "bare_ai_expected": args.bare_ai_expected or [],
        "problem_ids": args.problem_id or [],
        "progress_keys": args.progress_key or [],
        "allow_repeat": args.allow_repeat,
    }
    create_batch(
        db,
        batch_id=batch_id,
        selected=selected,
        model=args.model,
        concurrency=args.concurrency,
        selection=selection,
        max_runtime_seconds=args.max_runtime_seconds,
        stall_seconds=args.stall_seconds,
    )
    print(f"created batch: {batch_id}")
    if args.no_monitor:
        monitor_batch(
            db,
            batch_id,
            poll_seconds=args.poll_seconds,
            max_runtime_seconds=args.max_runtime_seconds,
            stall_seconds=args.stall_seconds,
            stop_on_stall=args.stop_on_stall,
            launch_queued=True,
            decode=False,
            once=True,
            launch_interval_seconds=args.launch_interval_seconds,
            rate_limit_pause=not args.no_rate_limit_pause,
        )
    else:
        monitor_batch(
            db,
            batch_id,
            poll_seconds=args.poll_seconds,
            max_runtime_seconds=args.max_runtime_seconds,
            stall_seconds=args.stall_seconds,
            stop_on_stall=args.stop_on_stall,
            launch_queued=True,
            decode=not args.no_decode,
            once=False,
            launch_interval_seconds=args.launch_interval_seconds,
            rate_limit_pause=not args.no_rate_limit_pause,
        )
    return 0


def cmd_run_continuous(args: argparse.Namespace) -> int:
    """持续喂入模式：保持N并发，自动从DB选题喂入，直到tier题全部跑完。"""
    db = connect_db()
    ensure_schema(db)

    batch_id = args.batch_id or make_batch_id(args.label or f"continuous-tier{args.feed_tier}")

    # --batch-id模式下：跳过初始feed直接进monitor循环（batch已存在，不调create_batch）
    existing_queued = []
    if args.batch_id:
        existing_attempts = load_attempts(db, batch_id)
        existing_queued = [a for a in existing_attempts if a.get("status") == "queued"]
        print(f"  [resume] batch {batch_id}: {len(existing_queued)} queued, skipping initial feed", flush=True)
        selected = []
    else:
        # 新batch：先选一批初始题目（concurrency数量）作为种子
        initial = select_by_progress_for_feed(db, tier=args.feed_tier, limit=args.concurrency, batch_id=batch_id)
        if not initial:
            print(f"No unprocessed tier {args.feed_tier} problems found.", file=sys.stderr)
            return 1

        selected = []
        for prog in initial:
            try:
                text = load_problem_text_from_progress(prog)
                if text and len(text.strip()) >= 10:
                    selected.append({
                        "progress": prog,
                        "profile": {"_key": prog.get("problem_id", f"p{prog['_key']}")},
                        "problem_file_text": text.strip() + "\n",
                        "problem_id_override": prog.get("problem_id"),
                        "source_mode": "continuous_feed",
                        "case_metadata": {"feed_tier": args.feed_tier},
                    })
            except Exception as e:
                print(f"  SKIP p{prog['_key']}: {e}", file=sys.stderr)

    if not selected and not existing_queued:
        print("Failed to extract any problem text.", file=sys.stderr)
        return 1

    selection = {"mode": "continuous_feed", "feed_tier": args.feed_tier, "initial_count": len(selected)}
    if selected:
        create_batch(
            db,
            batch_id=batch_id,
            selected=selected,
            model=args.model,
            concurrency=args.concurrency,
            selection=selection,
            max_runtime_seconds=args.max_runtime_seconds,
            stall_seconds=args.stall_seconds,
        )
        print(f"created continuous batch: {batch_id} (initial {len(selected)}, target concurrency={args.concurrency})")
    else:
        # resume模式：更新concurrency但不创建新batch
        db.collection("devin_batch_runs").update({"_key": batch_id, "concurrency": args.concurrency})
        print(f"resuming batch: {batch_id} ({len(existing_queued)} queued, target concurrency={args.concurrency})")
    monitor_batch(
        db,
        batch_id,
        poll_seconds=args.poll_seconds,
        max_runtime_seconds=args.max_runtime_seconds,
        stall_seconds=args.stall_seconds,
        stop_on_stall=args.stop_on_stall,
        launch_queued=True,
        decode=not args.no_decode,
        once=False,
        launch_interval_seconds=args.launch_interval_seconds,
        rate_limit_pause=not args.no_rate_limit_pause,
        continuous=True,
        feed_tier=args.feed_tier,
        feed_batch_size=args.feed_batch_size,
    )
    return 0


def cmd_run_files(args: argparse.Namespace) -> int:
    db = connect_db()
    ensure_schema(db)
    selected = load_file_cases(db, args.case)
    if not selected:
        raise SystemExit("No file cases supplied.")

    batch_id = args.batch_id or make_batch_id(args.label or "files")
    selection = {
        "mode": "manual_file",
        "cases": args.case,
    }
    create_batch(
        db,
        batch_id=batch_id,
        selected=selected,
        model=args.model,
        concurrency=args.concurrency,
        selection=selection,
        max_runtime_seconds=args.max_runtime_seconds,
        stall_seconds=args.stall_seconds,
    )
    print(f"created batch: {batch_id}")
    if args.no_monitor:
        monitor_batch(
            db,
            batch_id,
            poll_seconds=args.poll_seconds,
            max_runtime_seconds=args.max_runtime_seconds,
            stall_seconds=args.stall_seconds,
            stop_on_stall=args.stop_on_stall,
            launch_queued=True,
            decode=False,
            once=True,
            launch_interval_seconds=args.launch_interval_seconds,
            rate_limit_pause=not args.no_rate_limit_pause,
        )
    else:
        monitor_batch(
            db,
            batch_id,
            poll_seconds=args.poll_seconds,
            max_runtime_seconds=args.max_runtime_seconds,
            stall_seconds=args.stall_seconds,
            stop_on_stall=args.stop_on_stall,
            launch_queued=True,
            decode=not args.no_decode,
            once=False,
            launch_interval_seconds=args.launch_interval_seconds,
            rate_limit_pause=not args.no_rate_limit_pause,
        )
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    db = connect_db()
    monitor_batch(
        db,
        args.batch_id,
        poll_seconds=args.poll_seconds,
        max_runtime_seconds=args.max_runtime_seconds,
        stall_seconds=args.stall_seconds,
        stop_on_stall=args.stop_on_stall,
        launch_queued=not args.no_launch_queued,
        decode=not args.no_decode,
        once=args.once,
        launch_interval_seconds=args.launch_interval_seconds,
        rate_limit_pause=not args.no_rate_limit_pause,
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db = connect_db()
    batch = load_batch(db, args.batch_id)
    attempts = load_attempts(db, args.batch_id)
    if args.refresh:
        batch_dir = Path(batch["paths"]["batch_dir"])
        attempts = [
            refresh_attempt(
                db,
                attempt,
                batch_dir,
                max_runtime_seconds=None,
                stall_seconds=None,
                stop_on_stall=False,
            )
            for attempt in attempts
        ]
    if args.json:
        print(json.dumps({"batch": batch, "attempts": attempts}, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"batch: {args.batch_id} status={batch.get('status')} concurrency={batch.get('concurrency')}")
        print_status(attempts)
    return 0


def cmd_set_concurrency(args: argparse.Namespace) -> int:
    """动态修改运行中批次的并发量。monitor循环每轮从DB读取batch.concurrency。"""
    db = connect_db()
    batch = load_batch(db, args.batch_id)
    old = int(batch.get("concurrency", 0))
    db.collection(BATCH_COLLECTION).update(
        {"_key": args.batch_id, "concurrency": args.concurrency, "updated_at": utc_now()}
    )
    insert_event(
        db,
        args.batch_id,
        "concurrency_changed",
        {"old": old, "new": args.concurrency, "source": "set_concurrency_command"},
    )
    print(f"batch {args.batch_id}: concurrency {old} → {args.concurrency}")
    print(f"  (monitor will pick up the new value on its next poll cycle)")
    return 0


def add_cases_to_batch(
    db: Any,
    batch_id: str,
    selected: list[dict[str, Any]],
    model: str,
) -> list[dict[str, Any]]:
    """往运行中批次追加题目。新题目status=queued，monitor下一轮poll自动launch。"""
    batch = load_batch(db, batch_id)
    batch_dir = Path(batch["paths"]["batch_dir"])
    problems_dir = batch_dir / "problems"

    # 找当前最大ordinal
    existing_attempts = load_attempts(db, batch_id)
    max_ordinal = max((a.get("ordinal", 0) for a in existing_attempts), default=0)

    new_attempts: list[dict[str, Any]] = []
    for i, item in enumerate(selected, start=max_ordinal + 1):
        progress = item["progress"]
        profile = item["profile"]
        progress_key = str(progress["_key"])
        problem_id = item.get("problem_id_override") or progress.get("problem_id") or profile["_key"]
        run_id = next_run_id(db)
        exp_id = make_exp_id(batch_id, i, progress_key, progress.get("global_sequence"), problem_id, run_id=run_id)
        key = attempt_key(batch_id, progress_key, i)
        problem_source_path = problems_dir / f"{exp_id}.txt"
        problem_source_path.write_text(
            item.get("problem_file_text") or build_problem_file_text(progress, profile),
            encoding="utf-8",
        )
        paths = make_attempt_paths(exp_id)
        now = utc_now()
        doc = {
            "_key": key,
            "batch_id": batch_id,
            "ordinal": i,
            "run_id": run_id,  # 数据库自增运行ID，确保exp_id唯一
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "model": model,
            "concurrency": int(batch.get("concurrency", 10)),
            "progress_key": progress_key,
            "global_sequence": progress.get("global_sequence"),
            "problem_id": problem_id,
            "profile_doc_id": progress.get("profile_doc_id") or profile.get("_id"),
            "source_dataset": progress.get("source_dataset") or profile.get("source_dataset"),
            "source_mode": item.get("source_mode", "manual_file"),
            "case_metadata": item.get("case_metadata") or {},
            "difficulty_tier": progress.get("difficulty_tier"),
            "priority": progress.get("priority"),
            "exp_id": exp_id,
            "tmux_session": f"harness-{exp_id}",
            "problem_source_path": str(problem_source_path),
            "paths": paths,
            "observability": {
                "activity_signature": "",
                "last_observed_activity_at": None,
                "last_observed_at": None,
                "markers": {},
                "file_sizes": {},
            },
            "verdict": make_verdict("queued", "added via add-cases command", needs_human_math_review=False),
        }
        db.collection(ATTEMPT_COLLECTION).insert(doc)
        new_attempts.append(doc)

    # 更新batch的attempt_keys和selected_count
    all_keys = [a["_key"] for a in existing_attempts] + [a["_key"] for a in new_attempts]
    db.collection(BATCH_COLLECTION).update(
        {
            "_key": batch_id,
            "attempt_keys": all_keys,
            "selected_count": len(all_keys),
            "updated_at": utc_now(),
        }
    )
    insert_event(
        db,
        batch_id,
        "cases_added",
        {"count": len(new_attempts), "new_keys": [a["_key"] for a in new_attempts]},
    )
    return new_attempts


def cmd_add_cases(args: argparse.Namespace) -> int:
    """往运行中批次追加题目。"""
    db = connect_db()
    ensure_schema(db)
    selected = load_file_cases(db, args.case)
    if not selected:
        print("No cases supplied.", file=sys.stderr)
        return 1
    batch = load_batch(db, args.batch_id)
    new_attempts = add_cases_to_batch(db, args.batch_id, selected, batch.get("model", "glm-5-2"))
    print(f"Added {len(new_attempts)} cases to batch {args.batch_id}")
    for a in new_attempts:
        print(f"  #{a['ordinal']:02d} p{a['progress_key']} {a['problem_id']} → queued")
    print(f"  (monitor will launch them on its next poll cycle, respecting concurrency limit)")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    db = connect_db()
    batch = load_batch(db, args.batch_id)
    batch_dir = Path(batch["paths"]["batch_dir"])
    attempts = load_attempts(db, args.batch_id)
    for attempt in attempts:
        if attempt.get("status") not in TERMINAL_STATUSES:
            stop_attempt(db, attempt, batch_dir, decode=False)
            observability = observe_attempt_files(attempt, old_observability=attempt.get("observability") or {})
            final_status, verdict = classify_finished(attempt, observability)
            if final_status == "failed_no_proof":
                final_status = "stopped"
                verdict = make_verdict(
                    "stopped",
                    "batch stop command stopped this attempt",
                    confidence="high",
                    needs_human_math_review=False,
                )
            db.collection(ATTEMPT_COLLECTION).update(
                {
                    "_key": attempt["_key"],
                    "status": final_status,
                    "ended_at": utc_now(),
                    "end_reason": "batch_stop_command",
                    "observability": observability,
                    "verdict": verdict,
                    "updated_at": utc_now(),
                }
            )
    if not args.no_decode:
        decode_all(db, args.batch_id, batch_dir)
    write_final_report(db, args.batch_id, batch_dir)
    update_batch_status(db, args.batch_id, "stopped", {"stopped_at": utc_now()})
    insert_event(db, args.batch_id, "batch_stopped", {"decode": not args.no_decode})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch Devin Solver runner backed by solver_harness.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_selection_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--limit", type=int, default=10, help="number of problems to select")
        p.add_argument("--tier", type=int, default=1, help="difficulty_tier filter; default Tier 1")
        p.add_argument("--start-seq", type=int, help="minimum global_sequence")
        p.add_argument("--end-seq", type=int, help="maximum global_sequence")
        p.add_argument("--bare-ai-expected", action="append", help="filter problem_profiles.bare_ai_expected, e.g. fail")
        p.add_argument("--problem-id", action="append", help="explicit problem_id; repeatable")
        p.add_argument("--progress-key", action="append", help="explicit problem_extraction_progress _key; repeatable")
        p.add_argument("--allow-repeat", action="store_true", help="allow problems already present in devin_problem_runs")

    def add_run_control_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--batch-id", help="explicit batch id")
        p.add_argument("--label", help="short label included in generated batch id")
        p.add_argument("--model", default="glm-5-2")
        p.add_argument("--concurrency", type=int, default=10)
        p.add_argument("--poll-seconds", type=int, default=60)
        p.add_argument("--max-runtime-seconds", type=int, default=7200)
        p.add_argument("--stall-seconds", type=int, default=900)
        p.add_argument("--stop-on-stall", action="store_true")
        p.add_argument("--launch-interval-seconds", type=int, default=8, help="seconds to wait between Solver launches; 361号§7")
        p.add_argument("--no-rate-limit-pause", action="store_true", help="disable auto-pause when a running attempt shows rate-limit markers")
        p.add_argument("--no-monitor", action="store_true", help="launch one observation pass and exit")
        p.add_argument("--no-decode", action="store_true", help="skip final decode-all")

    p_select = sub.add_parser("select", help="print candidate problems without launching")
    add_selection_options(p_select)
    p_select.set_defaults(func=cmd_select)

    p_run = sub.add_parser("run", help="create a batch and launch/monitor Solver attempts")
    add_selection_options(p_run)
    add_run_control_options(p_run)
    p_run.set_defaults(func=cmd_run)

    p_run_files = sub.add_parser("run-files", help="run explicit local problem files with numeric progress keys")
    p_run_files.add_argument(
        "--case",
        action="append",
        required=True,
        help="progress_key:path[:problem_id]; repeatable. The file is fed to Solver, not profile.solution_text.",
    )
    add_run_control_options(p_run_files)
    p_run_files.set_defaults(func=cmd_run_files)

    p_monitor = sub.add_parser("monitor", help="monitor an existing batch")
    p_monitor.add_argument("--batch-id", required=True)
    p_monitor.add_argument("--poll-seconds", type=int, default=60)
    p_monitor.add_argument("--max-runtime-seconds", type=int, default=7200)
    p_monitor.add_argument("--stall-seconds", type=int, default=900)
    p_monitor.add_argument("--stop-on-stall", action="store_true")
    p_monitor.add_argument("--launch-interval-seconds", type=int, default=8)
    p_monitor.add_argument("--no-rate-limit-pause", action="store_true")
    p_monitor.add_argument("--no-launch-queued", action="store_true")
    p_monitor.add_argument("--no-decode", action="store_true")
    p_monitor.add_argument("--once", action="store_true")
    p_monitor.set_defaults(func=cmd_monitor)

    p_status = sub.add_parser("status", help="show batch status")
    p_status.add_argument("--batch-id", required=True)
    p_status.add_argument("--refresh", action="store_true", help="refresh file/tmux observations before printing")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="stop all non-terminal attempts in a batch")
    p_stop.add_argument("--batch-id", required=True)
    p_stop.add_argument("--no-decode", action="store_true")
    p_stop.set_defaults(func=cmd_stop)

    p_setc = sub.add_parser("set-concurrency", help="dynamically change concurrency of a running batch")
    p_setc.add_argument("--batch-id", required=True)
    p_setc.add_argument("--concurrency", type=int, required=True, help="new concurrency limit")
    p_setc.set_defaults(func=cmd_set_concurrency)

    p_addc = sub.add_parser("add-cases", help="add problems to a running batch (monitor auto-launches them)")
    p_addc.add_argument("--batch-id", required=True)
    p_addc.add_argument(
        "--case",
        action="append",
        required=True,
        help="progress_key:path[:problem_id]; repeatable. New problems are queued and auto-launched.",
    )
    p_addc.set_defaults(func=cmd_add_cases)

    p_cont = sub.add_parser("run-continuous", help="continuous worker pool: keep N concurrent, auto-feed from DB until tier exhausted")
    p_cont.add_argument("--feed-tier", type=int, default=1, help="difficulty tier to feed from (1=hardest)")
    p_cont.add_argument("--concurrency", type=int, default=30, help="target concurrency (always-on slots)")
    p_cont.add_argument("--feed-batch-size", type=int, default=10, help="max problems to add per feed cycle")
    p_cont.add_argument("--batch-id", help="explicit batch id")
    p_cont.add_argument("--label", help="short label for generated batch id")
    p_cont.add_argument("--model", default="glm-5-2")
    p_cont.add_argument("--poll-seconds", type=int, default=60)
    p_cont.add_argument("--max-runtime-seconds", type=int, default=7200)
    p_cont.add_argument("--stall-seconds", type=int, default=900)
    p_cont.add_argument("--stop-on-stall", action="store_true")
    p_cont.add_argument("--launch-interval-seconds", type=int, default=5)
    p_cont.add_argument("--no-rate-limit-pause", action="store_true")
    p_cont.add_argument("--no-decode", action="store_true")
    p_cont.set_defaults(func=cmd_run_continuous)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
