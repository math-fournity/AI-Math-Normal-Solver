#!/usr/bin/env python3
"""从源文件提取题目文本到独立txt文件，供batch_problem_runner.py --case模式使用。

支持三种源文件格式：
  - .parquet (mathnet): 按original_index行号读problem_markdown列
  - .jsonl (numina_math/omni_math/olympiadbench): 按original_index行号读problem字段
  - .lean (compfiles): 提取problem语句部分

用法：
  # 按tier选N道无profile题，提取题面到outdir
  python extract_problem_text.py --tier 1 --limit 10 --outdir runs/batch_problem_files/batch_t1
  python extract_problem_text.py --tier 2 --limit 10 --outdir runs/batch_problem_files/batch_t2
  python extract_problem_text.py --tier 3 --limit 10 --outdir runs/batch_problem_files/batch_t3

  # 按progress_key指定
  python extract_problem_text.py --progress-key 382056,382075 --outdir runs/batch_problem_files/batch_custom

  # 输出：
  #   outdir/p<key>_<problem_id>.txt  — 每题一个文件（可直接用作--case的path）
  #   outdir/manifest.jsonl           — 每行一个JSON：{progress_key, problem_id, path, tier, source_dataset}
  #
  # 注意：outdir必须放在持久化目录（如runs/下），不要用/tmp——
  # /tmp重启后清空，会导致manifest丢失（虽然题面文件会被run-files复制到batch_dir持久化）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from arango import ArangoClient


# ---------------------------------------------------------------------------
# DB连接
# ---------------------------------------------------------------------------

def connect_db() -> Any:
    host = os.environ.get("ARANGO_HOST", "http://localhost:8529")
    dbname = os.environ.get("ARANGO_DB", "xishujuzhen_math_glm52")
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "")
    client = ArangoClient(hosts=host)
    return client.db(dbname, username=user, password=password)


# ---------------------------------------------------------------------------
# 从源文件读题面
# ---------------------------------------------------------------------------

# parquet缓存：避免重复读大文件
_parquet_cache: dict[str, Any] = {}


def read_parquet_row(path: str, index: int) -> str:
    """从parquet文件读指定行的problem_markdown列。"""
    if path not in _parquet_cache:
        _parquet_cache[path] = pq.read_table(path)
    table = _parquet_cache[path]
    if index >= len(table):
        raise IndexError(f"parquet row {index} out of range (len={len(table)})")
    row = table.slice(index, 1).to_pydict()
    # mathnet用problem_markdown列
    for col in ("problem_markdown", "problem", "question", "prompt"):
        if col in row and row[col]:
            val = row[col]
            if isinstance(val, list):
                val = val[0] if val else ""
            return str(val).strip()
    raise KeyError(f"no problem column found in {path}")


def read_jsonl_row(path: str, index: int) -> str:
    """从jsonl文件读指定行的problem字段。"""
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i == index:
                d = json.loads(line)
                for col in ("problem", "question", "prompt", "problem_text"):
                    if col in d and d[col]:
                        return str(d[col]).strip()
                raise KeyError(f"no problem field in {path} row {index}: keys={list(d.keys())}")
    raise IndexError(f"jsonl row {index} not found in {path}")


def read_lean_problem(path: str) -> str:
    """从compfiles .lean文件提取problem语句。"""
    text = Path(path).read_text(encoding="utf-8")

    # 安全截断关键词——防止解答/提示泄漏
    CUT_KEYWORDS = ["Formalization notes", "## Formalization", "solution sketch", "The proof follows"]

    # 方法1：提取/-! ... -/块注释（compfiles标准格式，题面在这里）
    match = re.search(r"/-!\s*(.*?)\s*-/", text, re.DOTALL)
    if match:
        block = match.group(1).strip()
        # 去掉markdown标题行（# 开头），保留题面正文
        lines = []
        for line in block.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # 跳过"# International Mathematical Olympiad..."
            if stripped:
                lines.append(stripped)
        if lines:
            result = "\n".join(lines)
            for kw in CUT_KEYWORDS:
                cut_idx = result.find(kw)
                if cut_idx > 0:
                    result = result[:cut_idx].strip()
            return result

    # 方法2：提取/- ... -/块注释（非!变体）
    match = re.search(r"/-\s*(.*?)\s*-/", text, re.DOTALL)
    if match:
        block = match.group(1).strip()
        # 跳过Copyright/license块
        if "Copyright" not in block and "license" not in block.lower():
            lines = [l.strip() for l in block.split("\n") if l.strip() and not l.strip().startswith("#")]
            if lines:
                result = "\n".join(lines)
                for kw in CUT_KEYWORDS:
                    cut_idx = result.find(kw)
                    if cut_idx > 0:
                        result = result[:cut_idx].strip()
                return result

    # 方法3：提取problem声明后的内容
    match = re.search(r"problem\s+\w+\s*:\s*(.+?)\s*:=\s*by", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # 方法4：fallback
    cutoff = text.find(":= by")
    if cutoff > 0:
        return text[:cutoff].strip()
    return text[:500].strip()


def load_problem_text(progress: dict[str, Any]) -> str:
    """从progress记录的external_ref读源文件提取题面。"""
    ext = progress.get("external_ref") or {}
    path = ext.get("local_path", "")
    idx = ext.get("original_index", 0)

    if not path:
        raise ValueError(f"no local_path in progress {progress.get('_key')}")

    # 相对路径转绝对路径（相对于repo根）
    if not os.path.isabs(path):
        # 找repo根——脚本在xishujuzhen/solver_harness/下
        repo_root = Path(__file__).resolve().parents[2]
        full_path = repo_root / path
    else:
        full_path = Path(path)

    if not full_path.exists():
        raise FileNotFoundError(f"source file not found: {full_path}")

    suffix = full_path.suffix.lower()

    if suffix == ".parquet":
        return read_parquet_row(str(full_path), idx)
    elif suffix == ".jsonl":
        return read_jsonl_row(str(full_path), idx)
    elif suffix == ".lean":
        return read_lean_problem(str(full_path))
    else:
        raise ValueError(f"unsupported source format: {suffix} ({full_path})")


# ---------------------------------------------------------------------------
# 从DB选题
# ---------------------------------------------------------------------------

def select_by_progress(
    db: Any,
    *,
    limit: int,
    tier: int | None,
    source_dataset: str | None,
    progress_keys: list[str],
    allow_repeat: bool,
    exclude_profiled: bool = True,
) -> list[dict[str, Any]]:
    """从progress表选题，不要求有profile。"""
    query = """
    FOR p IN problem_extraction_progress
      FILTER @tier == null OR p.difficulty_tier == @tier
      FILTER @source_dataset == null OR p.source_dataset == @source_dataset
      FILTER LENGTH(@progress_keys) == 0 OR p._key IN @progress_keys
      FILTER p.external_ref != null
      FILTER p.external_ref.local_path != null
      FILTER p.external_ref.local_path != ""
      FILTER @exclude_profiled == false OR LENGTH(
        FOR prof IN problem_profiles
          FILTER prof._key == p.problem_id
          LIMIT 1
          RETURN 1
      ) == 0
      FILTER @allow_repeat OR LENGTH(
        FOR r IN devin_problem_runs
          FILTER r.progress_key == p._key
          LIMIT 1
          RETURN 1
      ) == 0
      SORT p.global_sequence ASC, p._key ASC
      LIMIT @limit
      RETURN p
    """
    cursor = db.aql.execute(
        query,
        bind_vars={
            "limit": limit,
            "tier": tier,
            "source_dataset": source_dataset,
            "progress_keys": progress_keys,
            "allow_repeat": allow_repeat,
            "exclude_profiled": exclude_profiled,
        },
    )
    return list(cursor)


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

def slug(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_")
    return s[:maxlen] or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description="从源文件提取题目文本")
    parser.add_argument("--tier", type=int, default=None, help="难度tier (1=最难, 5=最易)")
    parser.add_argument("--source-dataset", default=None, help="按来源筛选 (compfiles/mathnet/omni_math/...)")
    parser.add_argument("--limit", type=int, default=10, help="选取题数")
    parser.add_argument("--progress-key", nargs="*", default=[], help="直接指定progress_key列表")
    parser.add_argument("--outdir", required=True, help="输出目录")
    parser.add_argument("--allow-repeat", action="store_true", help="允许选已跑过的题")
    parser.add_argument("--include-profiled", action="store_true", help="包含已有profile的题（默认排除）")
    args = parser.parse_args()

    db = connect_db()
    selected = select_by_progress(
        db,
        limit=args.limit,
        tier=args.tier,
        source_dataset=args.source_dataset,
        progress_keys=args.progress_key,
        allow_repeat=args.allow_repeat,
        exclude_profiled=not args.include_profiled,
    )

    if not selected:
        print("No candidates selected.", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    manifest_path = outdir / "manifest.jsonl"
    cases: list[str] = []
    success = 0
    failed = 0

    with open(manifest_path, "w", encoding="utf-8") as manifest:
        for prog in selected:
            key = prog["_key"]
            problem_id = prog.get("problem_id", f"p{key}")
            tier = prog.get("difficulty_tier", "?")
            ds = prog.get("source_dataset", "unknown")
            gseq = prog.get("global_sequence", 0)

            try:
                text = load_problem_text(prog)
                if not text or len(text.strip()) < 10:
                    raise ValueError(f"empty problem text ({len(text)} chars)")

                # 写题面文件
                fname = f"p{key}_{slug(problem_id, 40)}.txt"
                fpath = outdir / fname
                fpath.write_text(text.strip() + "\n", encoding="utf-8")

                # 写manifest行
                entry = {
                    "progress_key": key,
                    "problem_id": problem_id,
                    "path": str(fpath),
                    "tier": tier,
                    "source_dataset": ds,
                    "global_sequence": gseq,
                    "chars": len(text),
                }
                manifest.write(json.dumps(entry, ensure_ascii=False) + "\n")

                # 生成--case参数
                cases.append(f"{key}:{fpath}:{problem_id}")
                success += 1
                print(f"  OK p{key} {problem_id} tier={tier} {ds} ({len(text)} chars)")

            except Exception as e:
                failed += 1
                print(f"  FAIL p{key} {problem_id}: {e}", file=sys.stderr)

    print(f"\nDone: {success} extracted, {failed} failed")
    print(f"Manifest: {manifest_path}")

    # 输出run-files命令
    if cases:
        print(f"\n=== run-files command ===")
        cmd_parts = [
            ".venv/bin/python xishujuzhen/solver_harness/batch_problem_runner.py run-files",
        ]
        for c in cases:
            cmd_parts.append(f"--case {c}")
        cmd_parts.append("--concurrency 10 --max-runtime-seconds 3600 --poll-seconds 60 --stall-seconds 900 --stop-on-stall")
        print(" ".join(cmd_parts))

    return 0 if success > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
