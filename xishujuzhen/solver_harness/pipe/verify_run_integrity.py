#!/usr/bin/env python3
"""verify_run_integrity.py — 运行完整性验证

验证每次运行是否符合"纯数学thinking"的硬约束：
1. 无工具调用——除了devin cli默认加载的AGENTS.md，不发生任何工具调用
2. 真实thinking——AI确实在思考（有数学内容、有推理步骤）
3. 真实proof——PROOF COMPLETE标记前有实质性的证明内容

用法:
  python verify_run_integrity.py --exp-id <id>
  python verify_run_integrity.py --batch [--limit 100]
  python verify_run_integrity.py --all
"""
import sys
import os
import json
import argparse
import sqlite3
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from arango import ArangoClient

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
ATTEMPT_COLLECTION = "devin_problem_runs"

TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")

PROOF_COMPLETE_MARKER = "PROOF COMPLETE"
MATH_INDICATORS = [
    "\\frac", "\\sum", "\\int", "\\Rightarrow", "therefore", "hence", "thus",
    "prove", "proof", "since", "let", "assume", "suppose", "consider", "we have",
    "因为", "所以", "假设", "令", "考虑", "证明",
    "minimize", "maximize", "optim", "inequal", "constraint", "feasible",
    "denote", "define", "wlog", "without loss", "lemma", "theorem", "corollary",
]

import re

# ANSI转义码清理
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b\[[0-9;]*m|\[\d+m|\[0m|\[K|\[2A|\[2C|\[\?25[a-z]|\[\?2026[a-z]')


def clean_ansi(text: str) -> str:
    """清理ANSI转义码和tmux格式字符"""
    # 清理ANSI转义序列
    text = ANSI_ESCAPE.sub('', text)
    # 清理Unicode braille字符（devin cli的spinner）
    text = re.sub(r'[\u2800-\u28ff]', '', text)
    # 清理其他控制字符
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    return text


def check_no_tool_use(exp_id: str) -> dict:
    """检查是否有工具调用——查sessions.db"""
    traj_dir = TRAJECTORY_BASE / exp_id / "sessions_db"
    if not traj_dir.exists():
        return {"checked": False, "reason": "no sessions_db dir", "has_tool_use": False}

    db_files = list(traj_dir.glob("*.db"))
    if not db_files:
        return {"checked": False, "reason": "no .db files", "has_tool_use": False}

    tool_calls = []
    for db_file in db_files:
        try:
            conn = sqlite3.connect(str(db_file))
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cursor.fetchall()]

            # 查各种可能的工具调用表
            for table in tables:
                if "tool" in table.lower() or "action" in table.lower():
                    cursor.execute(f"SELECT COUNT(*) FROM {table}")
                    count = cursor.fetchone()[0]
                    if count > 0:
                        tool_calls.append({"table": table, "count": count})

            # 查message_nodes中是否有tool类型
            if "message_nodes" in tables:
                cursor.execute("SELECT DISTINCT type FROM message_nodes")
                types = [r[0] for r in cursor.fetchall()]
                for t in types:
                    if t and "tool" in str(t).lower():
                        cursor.execute(f"SELECT COUNT(*) FROM message_nodes WHERE type = ?", (t,))
                        count = cursor.fetchone()[0]
                        tool_calls.append({"table": "message_nodes", "type": t, "count": count})

            conn.close()
        except Exception as e:
            continue

    return {"checked": True, "has_tool_use": len(tool_calls) > 0, "tool_calls": tool_calls}


def get_pane_content(exp_id: str) -> str:
    """获取pane内容——优先用collector保存的snapshot，其次用tmux_pipe.log"""
    # 优先用collector保存的清理版snapshot
    snapshot_clean = TRAJECTORY_BASE / exp_id / "collector" / "pane_snapshot_clean.txt"
    if snapshot_clean.exists():
        return snapshot_clean.read_text(encoding="utf-8", errors="ignore")
    # 其次用collector保存的原始snapshot
    snapshot = TRAJECTORY_BASE / exp_id / "collector" / "pane_snapshot.txt"
    if snapshot.exists():
        raw = snapshot.read_text(encoding="utf-8", errors="ignore")
        return clean_ansi(raw)
    # 最后用tmux_pipe.log
    pipe_log = TRAJECTORY_BASE / exp_id / "tmux" / "tmux_pipe.log"
    if pipe_log.exists():
        raw = pipe_log.read_text(encoding="utf-8", errors="ignore")
        return clean_ansi(raw)
    return ""


def check_real_thinking(exp_id: str) -> dict:
    """检查是否有真实thinking内容"""
    content = get_pane_content(exp_id)
    if not content:
        return {"checked": False, "reason": "no pane content", "has_thinking": False}
    if len(content) < 100:
        return {"checked": True, "has_thinking": False, "reason": "content too short"}

    # 检查数学内容
    math_count = sum(1 for ind in MATH_INDICATORS if ind in content.lower())
    # 检查thinking标记
    thinking_markers = ["Thinking", "thinking"]
    has_thinking_marker = any(m in content for m in thinking_markers)

    return {
        "checked": True,
        "has_thinking": math_count >= 2 or has_thinking_marker,
        "math_indicator_count": math_count,
        "has_thinking_marker": has_thinking_marker,
        "content_length": len(content),
    }


def check_real_proof(exp_id: str) -> dict:
    """检查是否有真实proof内容"""
    content = get_pane_content(exp_id)
    if not content:
        return {"checked": False, "reason": "no pane content", "has_proof": False}

    has_marker = PROOF_COMPLETE_MARKER in content
    if not has_marker:
        return {"checked": True, "has_marker": False, "has_proof": False}

    idx = content.index(PROOF_COMPLETE_MARKER)
    proof_body = content[:idx]
    lines = [l.strip() for l in proof_body.split("\n") if l.strip()]
    content_lines = [l for l in lines if not l.startswith("#") and not l.startswith("###")]
    proof_content = "\n".join(content_lines)

    math_count = sum(1 for ind in MATH_INDICATORS if ind in proof_content.lower())

    return {
        "checked": True,
        "has_marker": True,
        "has_proof": len(proof_content) >= 100 and math_count >= 2,
        "proof_length": len(proof_content),
        "math_indicator_count": math_count,
    }


def verify_single(db, exp_id: str) -> dict:
    """验证单次运行的完整性"""
    result = {
        "exp_id": exp_id,
        "no_tool_use": check_no_tool_use(exp_id),
        "real_thinking": check_real_thinking(exp_id),
        "real_proof": check_real_proof(exp_id),
    }

    # 综合判定
    issues = []
    if result["no_tool_use"]["has_tool_use"]:
        issues.append("tool_use_detected")
    if result["real_thinking"]["checked"] and not result["real_thinking"]["has_thinking"]:
        issues.append("no_real_thinking")
    if result["real_proof"]["checked"] and result["real_proof"].get("has_marker") and not result["real_proof"]["has_proof"]:
        issues.append("marker_without_proof")

    result["issues"] = issues
    result["valid"] = len(issues) == 0
    return result


def verify_batch(db, limit: int) -> dict:
    """批量验证"""
    aql = (
        f"FOR a IN {ATTEMPT_COLLECTION} "
        f"FILTER a.batch_id == 'pipe-runner' "
        f"FILTER a.verdict == 'candidate_solved' "
        f"SORT a.started_at DESC "
    )
    if limit > 0:
        aql += f"LIMIT {limit} "
    aql += "RETURN {exp_id: a.exp_id, _key: a._key, verdict: a.verdict}"

    cursor = db.aql.execute(aql, ttl=300)
    results = []
    valid_count = 0
    invalid_count = 0

    for row in cursor:
        exp_id = row["exp_id"]
        if not exp_id:
            continue
        result = verify_single(db, exp_id)
        result["attempt_key"] = row["_key"]
        if result["valid"]:
            valid_count += 1
        else:
            invalid_count += 1
        results.append(result)

    return {"total": len(results), "valid": valid_count, "invalid": invalid_count, "results": results}


def main():
    parser = argparse.ArgumentParser(description="运行完整性验证")
    parser.add_argument("--exp-id", type=str)
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    if args.exp_id:
        result = verify_single(db, args.exp_id)
        print(f"=== 运行完整性验证: {args.exp_id} ===")
        print(f"  无工具调用:   {result['no_tool_use']}")
        print(f"  真实thinking: {result['real_thinking']}")
        print(f"  真实proof:    {result['real_proof']}")
        print(f"  问题:         {result['issues']}")
        print(f"  VERDICT: {'VALID' if result['valid'] else 'INVALID'}")
        return

    if args.batch or args.all:
        result = verify_batch(db, args.limit if not args.all else 0)
        print(f"=== 批量运行完整性验证 ===")
        print(f"  总数:    {result['total']}")
        print(f"  有效:    {result['valid']}")
        print(f"  无效:    {result['invalid']}")
        print(f"\n无效运行详情:")
        for r in result["results"]:
            if not r["valid"]:
                print(f"  ❌ {r['exp_id']}: {r['issues']}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
