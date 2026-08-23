#!/usr/bin/env python3
"""Verify whether tier rows with empty DB problem_text are recoverable on disk.

Unlike the legacy ``extract_problem_text.py``, this audit resolves relative
``external_ref.local_path`` values against the original glm5.2 worktree and
supports the FATE/MathArena ``.json`` formats.  It does not update the database
or write extracted problem text; only hashes and lengths are reported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from tier1_audit_common import (
    ORIGINAL_WORKTREE,
    SOLVER_BASE,
    connect_db,
    json_dump,
    load_nonpipe_runs_for_problems,
    load_tier_problems,
    read_lean_problem,
)


class SourceReader:
    def __init__(self, source_root: Path):
        self.source_root = source_root
        self.json_cache: dict[Path, Any] = {}
        self.jsonl_cache: dict[Path, list[Any]] = {}
        self.parquet_cache: dict[Path, Any] = {}

    def resolve(self, local_path: str) -> Path:
        path = Path(local_path)
        return path if path.is_absolute() else self.source_root / path

    @staticmethod
    def text_from_record(record: Any) -> tuple[str, str]:
        if isinstance(record, str):
            return record.strip(), "record"
        if not isinstance(record, dict):
            return "", "unsupported_record"
        for field in (
            "informal_statement",
            "problem",
            "question",
            "prompt",
            "problem_text",
            "formal_statement",
        ):
            value = record.get(field)
            if value:
                return str(value).strip(), field
        columns = record.get("columns")
        if isinstance(columns, dict):
            for field in ("problem", "question", "prompt", "problem_text"):
                value = columns.get(field)
                if value:
                    return str(value).strip(), f"columns.{field}"
        return "", "no_problem_field"

    @staticmethod
    def record_by_id(rows: list[Any], original_id: Any) -> Any | None:
        if original_id is None:
            return None
        expected = str(original_id)
        for record in rows:
            if not isinstance(record, dict):
                continue
            candidates = [record.get("id"), record.get("problem_id")]
            columns = record.get("columns")
            if isinstance(columns, dict):
                candidates.append(columns.get("problem_idx"))
            if any(value is not None and str(value) == expected for value in candidates):
                return record
        return None

    def read(
        self,
        local_path: str,
        original_index: int,
        original_id: Any = None,
    ) -> tuple[str, str, Path]:
        path = self.resolve(local_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        suffix = path.suffix.lower()
        if suffix == ".lean":
            return read_lean_problem(path), "lean_statement", path
        if suffix == ".jsonl":
            if path not in self.jsonl_cache:
                rows: list[Any] = []
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if line.strip():
                            rows.append(json.loads(line))
                self.jsonl_cache[path] = rows
            rows = self.jsonl_cache[path]
            record = (
                rows[original_index]
                if 0 <= original_index < len(rows)
                else self.record_by_id(rows, original_id)
            )
            if record is None:
                raise IndexError(
                    f"jsonl row {original_index} out of range (len={len(rows)}), "
                    f"original_id={original_id!r} not found"
                )
            text, field = self.text_from_record(record)
            return text, field, path
        if suffix == ".json":
            if path not in self.json_cache:
                self.json_cache[path] = json.loads(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            data = self.json_cache[path]
            if isinstance(data, list):
                if 0 <= original_index < len(data):
                    record = data[original_index]
                else:
                    record = self.record_by_id(data, original_id)
                if record is None:
                    raise IndexError(
                        f"json row {original_index} out of range (len={len(data)}), "
                        f"original_id={original_id!r} not found"
                    )
            elif isinstance(data, dict):
                values = data.get("data") or data.get("records") or data.get("problems")
                if isinstance(values, list):
                    if original_index < 0 or original_index >= len(values):
                        raise IndexError(
                            f"json row {original_index} out of range (len={len(values)})"
                        )
                    record = values[original_index]
                else:
                    record = data
            else:
                record = data
            text, field = self.text_from_record(record)
            return text, field, path
        if suffix == ".parquet":
            try:
                import pyarrow.parquet as pq
            except ImportError as exc:
                raise RuntimeError("pyarrow is required for parquet source audit") from exc
            if path not in self.parquet_cache:
                self.parquet_cache[path] = pq.read_table(path)
            table = self.parquet_cache[path]
            if original_index < 0 or original_index >= len(table):
                raise IndexError(
                    f"parquet row {original_index} out of range (len={len(table)})"
                )
            record = {
                key: (value[0] if isinstance(value, list) and value else value)
                for key, value in table.slice(original_index, 1).to_pydict().items()
            }
            text, field = self.text_from_record(record)
            return text, field, path
        raise ValueError(f"unsupported source suffix {suffix!r}: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit empty DB problem source recovery")
    parser.add_argument("--tier", type=int, default=1)
    parser.add_argument(
        "--source-root",
        default=str(ORIGINAL_WORKTREE),
        help="Root used to resolve relative external_ref.local_path values",
    )
    parser.add_argument("--export", help="Optional JSON output path")
    args = parser.parse_args()

    db = connect_db()
    problems = load_tier_problems(db, args.tier)
    empty = {
        key: problem
        for key, problem in problems.items()
        if not str(problem.get("problem_text") or "").strip()
    }
    reader = SourceReader(Path(args.source_root))
    historical_runs = load_nonpipe_runs_for_problems(db, empty)
    details: list[dict] = []
    for key, problem in empty.items():
        external_ref = problem.get("external_ref") or {}
        local_path = str(external_ref.get("local_path") or "")
        original_index = int(external_ref.get("original_index") or 0)
        original_id = external_ref.get("original_id_in_source")
        row = {
            "problem_key": key,
            "problem_id": problem.get("problem_id"),
            "source_dataset": problem.get("source_dataset"),
            "external_ref_local_path": local_path,
            "external_ref_original_index": original_index,
            "external_ref_original_id_in_source": original_id,
            "external_ref": external_ref,
            "recoverable": False,
        }
        try:
            text, field, resolved = reader.read(
                local_path, original_index, original_id
            )
            normalized = text.strip()
            row.update(
                {
                    "resolved_path": str(resolved),
                    "source_field": field,
                    "chars": len(normalized),
                    "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                    "recoverable": len(normalized) >= 10,
                }
            )
            if len(normalized) < 10:
                row["error"] = f"extracted text shorter than 10 chars ({len(normalized)})"
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        if not row["recoverable"]:
            for run in reversed(historical_runs.get(key, [])):
                exp_id = run.get("exp_id")
                if not exp_id:
                    continue
                historical_problem = SOLVER_BASE / str(exp_id) / "problem.txt"
                if not historical_problem.is_file():
                    continue
                text = historical_problem.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                if len(text) < 10:
                    continue
                row.update(
                    {
                        "resolved_path": str(historical_problem),
                        "source_field": "historical_solver.problem.txt",
                        "chars": len(text),
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "recoverable": True,
                        "raw_source_error": row.pop("error", None),
                    }
                )
                break
        details.append(row)

    result = {
        "audit": "empty DB problem source recovery",
        "tier": args.tier,
        "source_root": str(Path(args.source_root)),
        "summary": {
            "empty_problem_text_rows": len(details),
            "recoverable": sum(row["recoverable"] for row in details),
            "not_recoverable": sum(not row["recoverable"] for row in details),
        },
        "breakdowns": {
            "source_dataset": dict(
                Counter(row["source_dataset"] for row in details)
            ),
            "recoverable_by_source": [
                {
                    "source_dataset": source,
                    "recoverable": recoverable,
                    "count": count,
                }
                for (source, recoverable), count in sorted(
                    Counter(
                        (row["source_dataset"], row["recoverable"]) for row in details
                    ).items()
                )
            ],
            "source_field": dict(
                Counter(row.get("source_field") or "<missing>" for row in details)
            ),
        },
        "problems": sorted(details, key=lambda row: row["problem_key"]),
    }
    json_dump(result, args.export)


if __name__ == "__main__":
    main()
