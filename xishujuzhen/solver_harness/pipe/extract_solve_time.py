#!/usr/bin/env python3
"""extract_solve_time.py — 精确解题时间提取

从多个数据源提取AI实际解题时间，不是粗略的elapsed。

时间分解：
  runner.start_time (Runner入队列)
    → session_info.start_timestamp (devin cli启动)       = launch_overhead
    → conversation第一个step (AI开始工作)                  = init_overhead
    → conversation最后一个step (AI完成最后输出)            = solve_time ← 核心指标
    → collector判定时间                                    = judge_delay

数据源优先级：
  1. tmux/tmux_pipe.log的文件创建/修改时间（最可靠——覆盖完整解题过程）
  2. exports/conversation.json的steps时间戳（ATIF-v1.7，毫秒级ISO格式）
  3. sessions_db/trajectory.jsonl的created_at（Unix timestamp）

注意：conversation.json的steps时间戳可能不覆盖完整解题过程
（devin cli的export可能在session结束时一次性写入，steps时间戳只覆盖部分）。
tmux_pipe.log从tmux session创建到结束持续写入，文件时间最可靠。

用法:
  python extract_solve_time.py --exp-id <id>
  python extract_solve_time.py --batch [--limit 100]
  python extract_solve_time.py --update-db  # 提取并写入DB
"""
import sys
import os
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from arango import ArangoClient

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
ATTEMPT_COLLECTION = "devin_problem_runs"

TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")


def parse_iso(ts_str: str) -> datetime | None:
    """解析ISO格式时间戳"""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def parse_unix(ts: int | float) -> datetime | None:
    """解析Unix timestamp"""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None


def extract_from_conversation(exp_dir: Path) -> dict | None:
    """从exports/conversation.json提取时间——最精确的来源"""
    conv_file = exp_dir / "exports" / "conversation.json"
    if not conv_file.exists():
        return None
    try:
        conv = json.load(open(conv_file, encoding="utf-8"))
    except Exception:
        return None

    steps = conv.get("steps", [])
    if not steps:
        return None

    # 提取首尾step的时间戳
    first_ts = None
    last_ts = None
    for step in steps:
        ts_str = step.get("timestamp") or step.get("created_at") or step.get("ts")
        ts = parse_iso(ts_str) if isinstance(ts_str, str) else parse_unix(ts_str)
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

    if not first_ts or not last_ts:
        return None

    # token统计
    fm = conv.get("final_metrics", {})

    return {
        "source": "conversation.json",
        "first_step_ts": first_ts.isoformat(),
        "last_step_ts": last_ts.isoformat(),
        "solve_time_seconds": (last_ts - first_ts).total_seconds(),
        "total_steps": len(steps),
        "prompt_tokens": fm.get("total_prompt_tokens"),
        "completion_tokens": fm.get("total_completion_tokens"),
        "cached_tokens": fm.get("total_cached_tokens"),
    }


def extract_from_trajectory(exp_dir: Path) -> dict | None:
    """从sessions_db/trajectory.jsonl提取时间——备选来源"""
    traj_file = exp_dir / "sessions_db" / "trajectory.jsonl"
    if not traj_file.exists():
        return None

    first_ts = None
    last_ts = None
    line_count = 0
    try:
        with open(traj_file, encoding="utf-8") as f:
            for line in f:
                line_count += 1
                d = json.loads(line)
                ts_val = d.get("created_at") or d.get("timestamp") or d.get("ts")
                ts = parse_unix(ts_val) if isinstance(ts_val, (int, float)) else parse_iso(ts_val)
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
    except Exception:
        return None

    if not first_ts or not last_ts:
        return None

    return {
        "source": "trajectory.jsonl",
        "first_step_ts": first_ts.isoformat(),
        "last_step_ts": last_ts.isoformat(),
        "solve_time_seconds": (last_ts - first_ts).total_seconds(),
        "total_steps": line_count,
    }


def extract_from_file_mtime(exp_dir: Path) -> dict | None:
    """从tmux_pipe.log的文件时间提取——最粗略的备选"""
    pipe_log = exp_dir / "tmux" / "tmux_pipe.log"
    if not pipe_log.exists():
        return None
    import os
    stat = pipe_log.stat()
    created_ts = datetime.fromtimestamp(stat.st_birthtime, tz=timezone.utc)
    modified_ts = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return {
        "source": "tmux_pipe.log mtime",
        "first_step_ts": created_ts.isoformat(),
        "last_step_ts": modified_ts.isoformat(),
        "solve_time_seconds": (modified_ts - created_ts).total_seconds(),
        "total_steps": None,
    }


def extract_from_session_info(exp_dir: Path) -> dict | None:
    """从session_info.json提取start_timestamp和updated_at"""
    si_file = exp_dir / "session_info.json"
    if not si_file.exists():
        return None
    try:
        si = json.load(open(si_file, encoding="utf-8"))
    except Exception:
        return None
    start_ts = parse_iso(si.get("start_timestamp"))
    updated_ts = parse_iso(si.get("updated_at"))
    if not start_ts:
        return None
    return {
        "start_timestamp": start_ts.isoformat(),
        "updated_at": updated_ts.isoformat() if updated_ts else None,
        "session_duration_seconds": (updated_ts - start_ts).total_seconds() if updated_ts else None,
    }


def extract_solve_time(exp_id: str) -> dict:
    """提取一次运行的完整时间信息"""
    exp_dir = TRAJECTORY_BASE / exp_id
    if not exp_dir.exists():
        return {"exp_id": exp_id, "error": "trajectory dir not found"}

    result = {"exp_id": exp_id}

    # session_info.json（启动时间）
    si = extract_from_session_info(exp_dir)
    if si:
        result["session_info"] = si

    # 解题时间——按优先级尝试
    # tmux_pipe.log最可靠（覆盖完整解题过程），conversation.json可能不完整
    for extractor in [extract_from_file_mtime, extract_from_conversation, extract_from_trajectory]:
        solve_info = extractor(exp_dir)
        if solve_info:
            result["solve_time"] = solve_info
            break

    # 计算时间分解
    if result.get("session_info") and result.get("solve_time"):
        si_start = parse_iso(result["session_info"]["start_timestamp"])
        solve_first = parse_iso(result["solve_time"]["first_step_ts"])
        solve_last = parse_iso(result["solve_time"]["last_step_ts"])
        si_updated = parse_iso(result["session_info"]["updated_at"]) if result["session_info"].get("updated_at") else None

        if si_start and solve_first:
            result["time_breakdown"] = {
                "init_overhead_seconds": (solve_first - si_start).total_seconds(),
                "solve_time_seconds": (solve_last - solve_first).total_seconds(),
                "tail_overhead_seconds": (si_updated - solve_last).total_seconds() if si_updated else None,
                "total_session_seconds": (si_updated - si_start).total_seconds() if si_updated else None,
            }

    return result


def update_db_solve_time(db, exp_id: str, time_info: dict) -> bool:
    """将精确解题时间写入DB"""
    # 找到attempt记录
    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.exp_id == '{exp_id}' LIMIT 1 RETURN a._key"
    cursor = db.aql.execute(aql, ttl=60)
    keys = list(cursor)
    if not keys:
        return False

    attempt_key = keys[0]
    solve_time = time_info.get("solve_time", {})
    breakdown = time_info.get("time_breakdown", {})

    update_doc = {"_key": attempt_key}
    if solve_time.get("solve_time_seconds") is not None:
        update_doc["solve_time_seconds"] = round(solve_time["solve_time_seconds"], 1)
    if solve_time.get("source"):
        update_doc["solve_time_source"] = solve_time["source"]
    if solve_time.get("total_steps"):
        update_doc["total_steps"] = solve_time["total_steps"]
    if solve_time.get("prompt_tokens"):
        update_doc["prompt_tokens"] = solve_time["prompt_tokens"]
    if solve_time.get("completion_tokens"):
        update_doc["completion_tokens"] = solve_time["completion_tokens"]
    if solve_time.get("cached_tokens"):
        update_doc["cached_tokens"] = solve_time["cached_tokens"]
    if breakdown.get("init_overhead_seconds") is not None:
        update_doc["init_overhead_seconds"] = round(breakdown["init_overhead_seconds"], 1)
    if breakdown.get("tail_overhead_seconds") is not None:
        update_doc["tail_overhead_seconds"] = round(breakdown["tail_overhead_seconds"], 1)

    try:
        db.collection(ATTEMPT_COLLECTION).update(update_doc)
        return True
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="精确解题时间提取")
    parser.add_argument("--exp-id", type=str)
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--update-db", action="store_true", help="提取并写入DB")
    args = parser.parse_args()

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    if args.exp_id:
        result = extract_solve_time(args.exp_id)
        print(f"=== 解题时间: {args.exp_id} ===")
        if result.get("error"):
            print(f"  ❌ {result['error']}")
            return
        si = result.get("session_info", {})
        st = result.get("solve_time", {})
        tb = result.get("time_breakdown", {})
        print(f"  数据来源:          {st.get('source', 'N/A')}")
        print(f"  devin cli启动:     {si.get('start_timestamp', 'N/A')}")
        print(f"  第一个step:        {st.get('first_step_ts', 'N/A')}")
        print(f"  最后一个step:      {st.get('last_step_ts', 'N/A')}")
        print(f"  updated_at:        {si.get('updated_at', 'N/A')}")
        print(f"\n  === 时间分解（秒） ===")
        print(f"  init_overhead:     {tb.get('init_overhead_seconds', 'N/A')}")
        print(f"  solve_time:        {tb.get('solve_time_seconds', 'N/A')}  ← AI实际解题时间")
        print(f"  tail_overhead:     {tb.get('tail_overhead_seconds', 'N/A')}")
        print(f"  total_session:     {tb.get('total_session_seconds', 'N/A')}")
        if st.get("prompt_tokens"):
            print(f"\n  === token统计 ===")
            print(f"  prompt_tokens:     {st['prompt_tokens']}")
            print(f"  completion_tokens: {st['completion_tokens']}")
            print(f"  cached_tokens:     {st['cached_tokens']}")
            print(f"  total_steps:       {st['total_steps']}")
        if args.update_db:
            ok = update_db_solve_time(db, args.exp_id, result)
            print(f"\n  DB更新: {'✅' if ok else '❌'}")
        return

    if args.batch:
        aql = (
            f"FOR a IN {ATTEMPT_COLLECTION} "
            f"FILTER a.batch_id == 'pipe-runner' "
            f"SORT a.started_at DESC "
        )
        if args.limit > 0:
            aql += f"LIMIT {args.limit} "
        aql += "RETURN {exp_id: a.exp_id, _key: a._key, verdict: a.verdict}"
        cursor = db.aql.execute(aql, ttl=300)

        results = []
        updated = 0
        for row in cursor:
            exp_id = row["exp_id"]
            if not exp_id:
                continue
            time_info = extract_solve_time(exp_id)
            tb = time_info.get("time_breakdown", {})
            solve_time = tb.get("solve_time_seconds")
            source = time_info.get("solve_time", {}).get("source", "N/A")
            results.append({
                "exp_id": exp_id,
                "verdict": row["verdict"],
                "solve_time": solve_time,
                "source": source,
            })
            if args.update_db and solve_time is not None:
                if update_db_solve_time(db, exp_id, time_info):
                    updated += 1

        print(f"=== 批量解题时间提取 ({len(results)}个运行) ===")
        for r in results:
            st = f"{r['solve_time']:.1f}s" if r["solve_time"] else "N/A"
            print(f"  {r['exp_id'][:25]:25s}  verdict={str(r['verdict']):20s}  solve_time={st:>8s}  source={r['source']}")
        if args.update_db:
            print(f"\nDB更新: {updated}/{len(results)}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
