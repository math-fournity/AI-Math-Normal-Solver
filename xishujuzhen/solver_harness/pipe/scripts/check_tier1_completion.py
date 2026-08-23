#!/usr/bin/env python3
"""check_tier1_completion.py — tier=1题目完成度权威检查（统一DB+硬盘+Redis三数据源）

定义7级完成度分类标准，融合三个数据源给出每道题的权威状态：

| 级别 | 名称 | 判定条件 | 需要做什么 |
|---|---|---|---|
| 1 | completed | candidate_solved + 硬盘文件完整 | 无需操作 |
| 2 | solved_missing_data | candidate_solved + 缺关键文件 | 重跑补全数据 |
| 3 | infra_failure | last run = 基础设施失败类 | 重跑 |
| 4 | model_failure | last run = AI能力失败类 | Profile数据 |
| 5 | empty_problem_text | 无run + Redis verdict=empty_problem_text | 修复题目数据 |
| 6 | data_issue | last run = answer_leak类 | 不应重跑 |
| 7 | never_run | 无run + 不在Redis队列 | 需入队 |

本脚本是"题目完成度"的权威定义。其他脚本（check_tier1_remaining.py / check_tier1_retention.py）
是本脚本的子集视图，只看单一维度。判断"还有多少题需要处理"时，以本脚本为准。

用法:
  python check_tier1_completion.py --tier 1
  python check_tier1_completion.py --tier 1 --verbose
  python check_tier1_completion.py --tier 1 --export FILE
  python check_tier1_completion.py --tier 1 --list-level solved_missing_data
"""
import sys
import os
import json
import argparse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arango import ArangoClient
import redis

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
TRAJ_DIR = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory"
BATCH_ID = "pipe-runner"

INFRA_FAILURE_STATUSES = {
    "rate_limited", "dead_session", "crash_recovered",
    "failed_connection", "launch_error",
}
MODEL_FAILURE_STATUSES = {
    "failed_token_limit", "ai_gave_up", "failed_thinking_spin",
    "failed_tool_stall", "failed_no_proof", "failed_stall",
}
DATA_ISSUE_STATUSES = {
    "answer_leak", "answer_leak_in_input",
}

# 文件完整性豁免规则（与check_tier1_retention.py一致）
EXEMPT_EXPORT = {"rate_limited", "failed_stall"}
EXEMPT_PANE = {"failed_stall"}

# 完成度级别
LEVEL_COMPLETED = "completed"
LEVEL_SOLVED_MISSING = "solved_missing_data"
LEVEL_INFRA_FAILURE = "infra_failure"
LEVEL_MODEL_FAILURE = "model_failure"
LEVEL_EMPTY_TEXT = "empty_problem_text"
LEVEL_DATA_ISSUE = "data_issue"
LEVEL_NEVER_RUN = "never_run"

# 级别→中文说明
LEVEL_DESC = {
    LEVEL_COMPLETED: "已完整解决（candidate_solved + 文件完整）",
    LEVEL_SOLVED_MISSING: "已解决但留存文件缺失（candidate_solved + 缺export/pane）",
    LEVEL_INFRA_FAILURE: "基础设施失败（rate_limited/dead_session/crash_recovered等）",
    LEVEL_MODEL_FAILURE: "AI能力失败（token_limit/ai_gave_up/stall等，Profile数据）",
    LEVEL_EMPTY_TEXT: "题目文本为空（无run记录 + Redis verdict=empty_problem_text）",
    LEVEL_DATA_ISSUE: "数据问题（answer_leak类，题目含答案）",
    LEVEL_NEVER_RUN: "从未运行且不在Redis队列（需入队）",
}

# 级别→是否需要重跑
LEVEL_ACTION = {
    LEVEL_COMPLETED: "无需操作",
    LEVEL_SOLVED_MISSING: "重跑补全数据",
    LEVEL_INFRA_FAILURE: "重跑",
    LEVEL_MODEL_FAILURE: "Profile数据（token_limit看情况，其他不重跑）",
    LEVEL_EMPTY_TEXT: "修复题目数据（非重跑）",
    LEVEL_DATA_ISSUE: "不应重跑",
    LEVEL_NEVER_RUN: "需入队",
}


def check_file_completeness(exp_id, status):
    """检查单个run的硬盘文件完整性，返回 (is_complete, missing_files)"""
    path = os.path.join(TRAJ_DIR, exp_id)
    if not os.path.exists(path):
        return False, ["no_dir"]

    files = set()
    for root, dirs, fnames in os.walk(path):
        for f in fnames:
            rel = os.path.relpath(os.path.join(root, f), path)
            files.add(rel)

    missing = []

    # session_info.json：所有status都必须有
    if "session_info.json" not in files:
        missing.append("session_info")

    # exports/conversation.json：除豁免外都必须有
    if status not in EXEMPT_EXPORT:
        if not any(f.startswith("exports/") for f in files):
            missing.append("export")

    # collector/pane_snapshot：除豁免外都必须有
    if status not in EXEMPT_PANE:
        if not any(f.startswith("collector/") for f in files):
            missing.append("pane")

    # tmux/tmux_pipe.log：所有status都必须有
    if not any(f.startswith("tmux/") for f in files):
        missing.append("tmux")

    return len(missing) == 0, missing


def classify_problem(p_key, last_run, redis_verdict, check_files=True):
    """对单道题分类完成度级别

    Args:
        p_key: 题目_key
        last_run: 最后一条终态run的dict（含status/exp_id），或None
        redis_verdict: Redis队列中该题的verdict，或None
        check_files: 是否检查硬盘文件完整性（candidate_solved时需要）

    Returns:
        (level, detail) - detail含missing_files等额外信息
    """
    if last_run is None:
        # 无run记录
        if redis_verdict == "empty_problem_text":
            return LEVEL_EMPTY_TEXT, {"redis_verdict": redis_verdict}
        elif redis_verdict is not None:
            # 在Redis队列中但无run记录且非empty_problem_text——异常情况
            return LEVEL_NEVER_RUN, {"redis_verdict": redis_verdict, "note": "在Redis中但无run记录"}
        else:
            return LEVEL_NEVER_RUN, {"redis_verdict": None}

    status = last_run["status"]

    if status == "candidate_solved":
        if not check_files:
            return LEVEL_COMPLETED, {}
        is_complete, missing = check_file_completeness(last_run["exp_id"], status)
        if is_complete:
            return LEVEL_COMPLETED, {}
        else:
            return LEVEL_SOLVED_MISSING, {"missing_files": missing, "exp_id": last_run["exp_id"]}

    elif status in INFRA_FAILURE_STATUSES:
        return LEVEL_INFRA_FAILURE, {"status": status}

    elif status in MODEL_FAILURE_STATUSES:
        return LEVEL_MODEL_FAILURE, {"status": status}

    elif status in DATA_ISSUE_STATUSES:
        return LEVEL_DATA_ISSUE, {"status": status}

    else:
        # 未知status（如running/stopped等）
        return LEVEL_NEVER_RUN, {"status": status, "note": "未知status"}


def main():
    parser = argparse.ArgumentParser(description="tier=1题目完成度权威检查（统一DB+硬盘+Redis）")
    parser.add_argument("--tier", type=int, default=1, help="难度档位（默认1）")
    parser.add_argument("--verbose", action="store_true", help="列出各级别详细题目ID")
    parser.add_argument("--export", type=str, default=None, help="导出完整分析结果到JSON文件")
    parser.add_argument("--list-level", type=str, default=None,
                        help="只列出指定级别的题目ID（如--list-level solved_missing_data）")
    parser.add_argument("--no-file-check", action="store_true",
                        help="跳过硬盘文件检查（快速模式，solved题不检查文件完整性）")
    args = parser.parse_args()

    tier = args.tier
    check_files = not args.no_file_check

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    # === 1. 加载tier=N题目 ===
    print(f"加载tier={tier}题目...")
    aql = (
        f"FOR p IN problem_extraction_progress "
        f"FILTER p.difficulty_tier == {tier} "
        f"RETURN {{key: p._key, extraction_status: p.extraction_status, source_dataset: p.source_dataset}}"
    )
    tier1_problems = list(db.aql.execute(aql, ttl=300, batch_size=5000))
    tier1_map = {p["key"]: p for p in tier1_problems}
    tier1_keys = set(tier1_map.keys())
    print(f"  tier={tier}总数: {len(tier1_keys)}")

    # === 2. 加载所有pipe-runner终态run，取最后一条 ===
    print("加载所有pipe-runner终态run...")
    aql = (
        "FOR r IN devin_problem_runs "
        'FILTER r.batch_id == "pipe-runner" '
        'FILTER r.status != "running" '
        "SORT r.problem_id, r.ended_at DESC "
        "RETURN {problem_id: r.problem_id, status: r.status, ended_at: r.ended_at, exp_id: r.exp_id}"
    )
    all_runs = list(db.aql.execute(aql, ttl=300, batch_size=5000))
    last_run_by_pid = {}
    for r in all_runs:
        if r["problem_id"] not in last_run_by_pid:
            last_run_by_pid[r["problem_id"]] = r
    print(f"  总run数: {len(all_runs)}, 有终态run的题: {len(last_run_by_pid)}")

    # === 3. 加载Redis队列verdict ===
    print("加载Redis队列...")
    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
    completed = r.lrange("math:completed", 0, -1)
    failed = r.lrange("math:failed", 0, -1)
    redis_verdict_by_pid = {}
    redis_verdict_counts = Counter()
    for item in completed + failed:
        try:
            d = json.loads(item)
            pk = d.get("problem_key")
            verdict = d.get("verdict", "<unknown>")
            if pk:
                redis_verdict_by_pid[pk] = verdict
                redis_verdict_counts[verdict] += 1
        except (json.JSONDecodeError, TypeError):
            pass
    print(f"  Redis中unique题数: {len(redis_verdict_by_pid)}")

    # === 4. 逐题分类 ===
    print(f"分类题目（{'含硬盘文件检查' if check_files else '快速模式，跳过文件检查'}）...")
    results = {}  # p_key -> (level, detail)
    level_counts = Counter()
    level_pids = defaultdict(list)

    for p_key in tier1_keys:
        last_run = last_run_by_pid.get(p_key)
        redis_verdict = redis_verdict_by_pid.get(p_key)
        level, detail = classify_problem(p_key, last_run, redis_verdict, check_files)
        results[p_key] = (level, detail)
        level_counts[level] += 1
        level_pids[level].append(p_key)

    # === 5. 报告 ===
    print()
    print(f"=== tier={tier} 题目完成度权威报告 ===")
    print(f"（融合DB run记录 + 硬盘文件完整性 + Redis队列verdict三数据源）")
    print()

    total = len(tier1_keys)
    # 按级别编号排序输出
    level_order = [
        (LEVEL_COMPLETED, "1"),
        (LEVEL_SOLVED_MISSING, "2"),
        (LEVEL_INFRA_FAILURE, "3"),
        (LEVEL_MODEL_FAILURE, "4"),
        (LEVEL_EMPTY_TEXT, "5"),
        (LEVEL_DATA_ISSUE, "6"),
        (LEVEL_NEVER_RUN, "7"),
    ]

    print(f"{'级别':>4} {'名称':<25} {'数量':>8} {'占比':>7}  {'需要做什么'}")
    print(f"{'-' * 100}")
    for level, num in level_order:
        cnt = level_counts.get(level, 0)
        pct = cnt / total * 100 if total > 0 else 0
        action = LEVEL_ACTION[level]
        print(f"  {num}   {level:<25} {cnt:>8} {pct:>6.1f}%  {action}")
    print(f"{'-' * 100}")
    print(f"      {'合计':<25} {total:>8} {'100.0%':>7}")
    print()

    # 汇总：需要重跑的题
    should_rerun = level_counts.get(LEVEL_INFRA_FAILURE, 0) + level_counts.get(LEVEL_SOLVED_MISSING, 0)
    should_fix_data = level_counts.get(LEVEL_EMPTY_TEXT, 0)
    should_enqueue = level_counts.get(LEVEL_NEVER_RUN, 0)
    no_action = level_counts.get(LEVEL_COMPLETED, 0) + level_counts.get(LEVEL_MODEL_FAILURE, 0) + level_counts.get(LEVEL_DATA_ISSUE, 0)

    print(f"=== 汇总 ===")
    print(f"  无需操作（已完成+AI能力失败+数据问题）: {no_action}")
    print(f"  需要重跑（基础设施失败+已解决缺文件）:  {should_rerun}")
    print(f"  需修复数据（empty_problem_text）:       {should_fix_data}")
    print(f"  需入队（从未运行且不在Redis）:          {should_enqueue}")
    print()

    # solved_missing_data的详细缺失文件统计
    if level_counts.get(LEVEL_SOLVED_MISSING, 0) > 0:
        print(f"=== solved_missing_data 缺失文件详情 ===")
        missing_file_counts = Counter()
        for p_key in level_pids[LEVEL_SOLVED_MISSING]:
            _, detail = results[p_key]
            for mf in detail.get("missing_files", []):
                missing_file_counts[mf] += 1
        for mf, cnt in sorted(missing_file_counts.items(), key=lambda x: -x[1]):
            print(f"  缺{mf}: {cnt}题")
        print()

    # empty_problem_text的source_dataset分布
    if level_counts.get(LEVEL_EMPTY_TEXT, 0) > 0:
        print(f"=== empty_problem_text 的 source_dataset 分布 ===")
        ds_counts = Counter()
        for p_key in level_pids[LEVEL_EMPTY_TEXT]:
            ds_counts[tier1_map[p_key]["source_dataset"]] += 1
        for ds, cnt in sorted(ds_counts.items(), key=lambda x: -x[1]):
            print(f"  {ds}: {cnt}")
        print()

    # infra_failure的status分布
    if level_counts.get(LEVEL_INFRA_FAILURE, 0) > 0:
        print(f"=== infra_failure 的 status 分布 ===")
        status_counts = Counter()
        for p_key in level_pids[LEVEL_INFRA_FAILURE]:
            _, detail = results[p_key]
            status_counts[detail.get("status", "<unknown>")] += 1
        for s, cnt in sorted(status_counts.items(), key=lambda x: -x[1]):
            print(f"  {s}: {cnt}")
        print()

    # model_failure的status分布
    if level_counts.get(LEVEL_MODEL_FAILURE, 0) > 0:
        print(f"=== model_failure 的 status 分布 ===")
        status_counts = Counter()
        for p_key in level_pids[LEVEL_MODEL_FAILURE]:
            _, detail = results[p_key]
            status_counts[detail.get("status", "<unknown>")] += 1
        for s, cnt in sorted(status_counts.items(), key=lambda x: -x[1]):
            print(f"  {s}: {cnt}")
        print()

    # === 6. --list-level 输出 ===
    if args.list_level:
        level = args.list_level
        if level not in LEVEL_DESC:
            print(f"未知级别: {level}")
            print(f"可用级别: {', '.join(l for l, _ in level_order)}")
            sys.exit(1)
        pids = level_pids.get(level, [])
        print(f"=== {level} 级别题目列表（{len(pids)}题）===")
        print(f"说明: {LEVEL_DESC[level]}")
        print(f"需要做什么: {LEVEL_ACTION[level]}")
        print()
        for p_key in sorted(pids):
            p = tier1_map[p_key]
            _, detail = results[p_key]
            extra = ""
            if "status" in detail:
                extra += f" status={detail['status']}"
            if "missing_files" in detail:
                extra += f" missing={','.join(detail['missing_files'])}"
            if "redis_verdict" in detail and detail["redis_verdict"]:
                extra += f" redis_verdict={detail['redis_verdict']}"
            print(f"  {p_key} | dataset={p['source_dataset']}{extra}")
        print()

    # === 7. --verbose 输出 ===
    if args.verbose:
        print(f"=== 各级别详细题目ID（前20个/级别）===")
        for level, num in level_order:
            pids = level_pids.get(level, [])
            if pids:
                print(f"\n  {level} ({len(pids)}题): {LEVEL_DESC[level]}")
                for p_key in sorted(pids)[:20]:
                    p = tier1_map[p_key]
                    _, detail = results[p_key]
                    extra = ""
                    if "status" in detail:
                        extra += f" status={detail['status']}"
                    if "missing_files" in detail:
                        extra += f" missing={','.join(detail['missing_files'])}"
                    print(f"    {p_key} | dataset={p['source_dataset']}{extra}")
                if len(pids) > 20:
                    print(f"    ... 共{len(pids)}题")
        print()

    # === 8. 导出 ===
    if args.export:
        export_data = {
            "tier": tier,
            "total": total,
            "level_counts": dict(level_counts),
            "level_descriptions": LEVEL_DESC,
            "level_actions": LEVEL_ACTION,
            "summary": {
                "no_action": no_action,
                "should_rerun": should_rerun,
                "should_fix_data": should_fix_data,
                "should_enqueue": should_enqueue,
            },
            "redis_verdict_counts": dict(redis_verdict_counts),
            "problems": {
                p_key: {"level": results[p_key][0], "detail": results[p_key][1]}
                for p_key in tier1_keys
            },
        }
        with open(args.export, "w") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        print(f"分析结果已导出到: {args.export}")


if __name__ == "__main__":
    main()
