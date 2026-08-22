#!/usr/bin/env python3
"""audit_misclassified_connection.py — 审计所有被误分类为failed_token_limit的连接错误

不重跑任何题，直接查ArangoDB中所有failed_token_limit run的pane_snapshot，
检查哪些含"Connection error"——这些就是被分类顺序bug误分类的run。

同时检查其他可能被误分类的status（failed_no_proof/failed_stall等）。

用法:
  python audit_misclassified_connection.py
"""
import sys
import os
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arango import ArangoClient

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
TRAJ_DIR = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory"

CONNECTION_MARKERS = ["connection error", "econnrefused", "etimedout", "socket hang up", "fetch failed"]
TOKEN_LIMIT_MARKERS = ["token limit", "context limit", "context_length", "maximum context",
                       "response truncated", "max output token", "send a message to continue"]


def read_pane(exp_id):
    """从硬盘读pane_snapshot.txt内容"""
    path = os.path.join(TRAJ_DIR, exp_id, "collector", "pane_snapshot.txt")
    if os.path.exists(path):
        try:
            with open(path, errors="replace") as f:
                return f.read()
        except Exception:
            return ""
    return ""


def main():
    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    # 1. 查所有failed_token_limit的run（不只tier=1，不只最后run，是所有）
    print("查询所有failed_token_limit的run记录...")
    aql = """
FOR r IN devin_problem_runs
  FILTER r.batch_id == "pipe-runner"
  FILTER r.status == "failed_token_limit"
  RETURN {
    key: r._key,
    exp_id: r.exp_id,
    problem_id: r.problem_id,
    status: r.status,
    ended_at: r.ended_at,
    runtime: r.runtime_seconds
  }
"""
    cursor = db.aql.execute(aql, ttl=300, batch_size=2000)
    runs = list(cursor)
    print(f"  failed_token_limit总run数: {len(runs)}")
    print()

    # 2. 检查每个run的pane_snapshot（从硬盘读）
    has_connection = []
    has_token_limit = []
    has_both = []
    has_neither = []
    no_pane_file = []

    for i, r in enumerate(runs):
        if i % 1000 == 0:
            print(f"  进度: {i}/{len(runs)}...", flush=True)
        pane = read_pane(r["exp_id"])
        if not pane:
            no_pane_file.append(r)
            continue
        pane_lower = pane.lower()
        is_connection = any(m in pane_lower for m in CONNECTION_MARKERS)
        is_token_limit = any(m in pane_lower for m in TOKEN_LIMIT_MARKERS) and not is_connection

        if is_connection and is_token_limit:
            has_both.append(r)
        elif is_connection:
            has_connection.append(r)
        elif is_token_limit:
            has_token_limit.append(r)
        else:
            has_neither.append(r)

    print(f"--- 分类结果 ---")
    print(f"  含Connection error（误分类）: {len(has_connection)}")
    print(f"  含真正token limit标记: {len(has_token_limit)}")
    print(f"  同时含两者: {len(has_both)}")
    print(f"  两者都不含（无法判定）: {len(has_neither)}")
    print(f"  无pane文件: {len(no_pane_file)}")
    print()

    # 3. 误分类的run按时间分布
    print(f"--- 误分类run的时间分布（按天）---")
    day_counts = Counter()
    for r in has_connection:
        ended = r.get("ended_at", "")
        if ended:
            day = ended[:10]  # YYYY-MM-DD
            day_counts[day] += 1
    for day, count in sorted(day_counts.items()):
        print(f"  {day}: {count}")
    print()

    # 4. 误分类的run按problem_id去重——多少个独立题目
    misclassified_problems = set(r["problem_id"] for r in has_connection)
    print(f"--- 误分类涉及独立题目数 ---")
    print(f"  误分类run数: {len(has_connection)}")
    print(f"  涉及独立题目: {len(misclassified_problems)}")
    print()

    # 5. 这些题目的最后run是什么status——是否最终被正确处理了
    print(f"--- 这些题目的最后run status ---")
    problem_keys = list(misclassified_problems)
    # 分批查询
    last_status_counts = Counter()
    batch_size = 500
    for i in range(0, len(problem_keys), batch_size):
        batch = problem_keys[i:i+batch_size]
        keys_str = ",".join(f'"{k}"' for k in batch)
        aql2 = f"""
FOR r IN devin_problem_runs
  FILTER r.problem_id IN [{keys_str}]
  FILTER r.batch_id == "pipe-runner"
  FILTER r.status != "running"
  SORT r.ended_at DESC
  RETURN {{problem_id: r.problem_id, status: r.status, ended_at: r.ended_at}}
"""
        cursor2 = db.aql.execute(aql2, ttl=300, batch_size=2000)
        seen = set()
        for r in cursor2:
            if r["problem_id"] not in seen:
                seen.add(r["problem_id"])
                last_status_counts[r["status"]] += 1

    print(f"  最后run status分布:")
    for s, c in sorted(last_status_counts.items(), key=lambda x: -x[1]):
        print(f"    {s}: {c}")
    print()

    # 6. 检查其他status是否也有误分类
    print(f"--- 检查其他status是否也有连接错误误分类 ---")
    other_statuses = ["failed_no_proof", "failed_stall", "dead_session", "ai_gave_up",
                      "failed_thinking_spin", "failed_output_limit", "invalid_tool_use"]
    for status in other_statuses:
        aql3 = f"""
FOR r IN devin_problem_runs
  FILTER r.batch_id == "pipe-runner"
  FILTER r.status == "{status}"
  RETURN r.exp_id
"""
        cursor3 = db.aql.execute(aql3, ttl=300, batch_size=2000)
        conn_count = 0
        total = 0
        for exp_id in cursor3:
            total += 1
            pane = read_pane(exp_id)
            if pane:
                pane_lower = pane.lower()
                if any(m in pane_lower for m in CONNECTION_MARKERS):
                    conn_count += 1
        if conn_count > 0:
            print(f"  {status}: {conn_count}/{total} 含Connection error")
        else:
            print(f"  {status}: 0/{total} ✅")

    print()

    # 7. 总结
    print(f"=== 总结 ===")
    print(f"  failed_token_limit总run: {len(runs)}")
    print(f"  误分类为failed_token_limit的连接错误run: {len(has_connection)}")
    print(f"  涉及独立题目: {len(misclassified_problems)}")
    print(f"  这些题目最后run被正确处理的: {last_status_counts.get('candidate_solved', 0)} solved")
    print(f"  这些题目最后run仍为failed_token_limit的: {last_status_counts.get('failed_token_limit', 0)}")
    print()
    print(f"  需要重新跑的题目数 = 最后run仍为failed_token_limit的 + 最后run为其他失败的")
    needs_rerun = sum(c for s, c in last_status_counts.items()
                      if s not in ("candidate_solved",))
    print(f"  = {needs_rerun}")


if __name__ == "__main__":
    main()
