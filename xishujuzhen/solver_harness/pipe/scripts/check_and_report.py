#!/usr/bin/env python3
"""check_and_report.py — 系统统计 + export文件结构完整性检查

功能：
  1. 全局统计：从系统启动到现在，处理了多少题、解决多少、未解决多少
  2. export文件统计：多少处理过的题有export、多少没有
  3. export结构完整性检查：逐个验证JSON格式是否完整
     - 检查过的在DB中标记 export_check 字段，下次只查未检查的
  4. 汇总报告

export结构完整性检查标准（ATIF-v1.7格式）：
  - 顶层5个key齐全：schema_version, session_id, agent, steps, final_metrics
  - steps是非空list
  - 至少1个source=agent的step
  - agent step有message字段
  - final_metrics.total_steps == len(steps)

DB标记：
  - export_check: "passed" | "failed:原因" | "missing" | null(未检查)
  - export_check_at: 检查时间(ISO)
  - export_size: 文件大小(字节)

用法:
  python check_and_report.py                  # 统计+增量检查未检查过的export
  python check_and_report.py --recheck        # 强制重新检查所有export
  python check_and_report.py --stats-only     # 只统计不检查export
  python check_and_report.py --limit 100      # 只检查100个（测试用）
  python check_and_report.py --verbose        # 显示检查详情
"""
import sys
import os
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from arango import ArangoClient
from redis_queue import get_redis

TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")
ATTEMPT_COLLECTION = "devin_problem_runs"


def get_system_start_time(r, db):
    """获取系统启动时间"""
    val = r.get('math:system:start_time')
    if val:
        ts = float(val)
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return ts, iso, "Redis math:system:start_time"
    try:
        aql = 'FOR r IN devin_problem_runs FILTER r.started_at != null SORT r.started_at ASC LIMIT 1 RETURN r.started_at'
        earliest = list(db.aql.execute(aql, ttl=30))
        if earliest:
            dt = datetime.fromisoformat(earliest[0].replace('Z', '+00:00'))
            return dt.timestamp(), earliest[0], "DB最早started_at（fallback）"
    except Exception:
        pass
    return None, None, "未知"


def format_duration(seconds):
    if seconds < 60: return f"{seconds:.0f}秒"
    if seconds < 3600: return f"{seconds/60:.1f}分钟"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}小时{m}分"


def check_export_structure(file_path: Path) -> tuple[str, int]:
    """检查export文件结构完整性，返回 (结果, 文件大小)
    结果: "passed" | "failed:原因" | "missing"
    """
    if not file_path.exists():
        return "missing", 0

    size = file_path.stat().st_size
    if size < 100:
        return f"failed:文件过小({size}B)", size

    try:
        data = json.loads(file_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        return f"failed:JSON解析失败({str(e)[:50]})", size
    except Exception as e:
        return f"failed:读取失败({str(e)[:50]})", size

    if not isinstance(data, dict):
        return "failed:顶层不是dict", size

    # 检查5个顶层key
    required_keys = {'schema_version', 'session_id', 'agent', 'steps', 'final_metrics'}
    missing_keys = required_keys - set(data.keys())
    if missing_keys:
        return f"failed:缺少顶层key({','.join(missing_keys)})", size

    # steps是非空list
    steps = data.get('steps')
    if not isinstance(steps, list):
        return "failed:steps不是list", size
    if len(steps) == 0:
        return "failed:steps为空", size

    # 至少1个agent step
    agent_steps = [s for s in steps if isinstance(s, dict) and s.get('source') == 'agent']
    if not agent_steps:
        return "failed:无agent step", size

    # agent step有message字段
    for s in agent_steps:
        if 'message' not in s:
            return f"failed:agent step({s.get('step_id','?')})无message字段", size

    # final_metrics.total_steps == len(steps)
    fm = data.get('final_metrics', {})
    total_steps = fm.get('total_steps')
    if isinstance(total_steps, int) and total_steps != len(steps):
        return f"failed:total_steps({total_steps})!=len(steps)({len(steps)})", size

    return "passed", size


def main():
    parser = argparse.ArgumentParser(description="系统统计 + export结构完整性检查")
    parser.add_argument('--recheck', action='store_true', help='强制重新检查所有export（忽略DB标记）')
    parser.add_argument('--stats-only', action='store_true', help='只统计不检查export')
    parser.add_argument('--limit', type=int, default=0, help='只检查N个（0=全部未检查的）')
    parser.add_argument('--verbose', action='store_true', help='显示检查详情')
    args = parser.parse_args()

    r = get_redis()
    client = ArangoClient(hosts=os.environ.get("ARANGO_HOST", "http://localhost:8529"), request_timeout=300)
    db = client.db(os.environ['ARANGO_DB'], username=os.environ.get('ARANGO_USER', 'root'),
                   password=os.environ.get('ARANGO_PASS', ''))

    now_ts = time.time()
    now_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    start_ts, start_iso, start_source = get_system_start_time(r, db)

    # ============================================================
    # 第一部分：全局统计
    # ============================================================
    print("=" * 70)
    print("  系统统计报告")
    print("=" * 70)
    print()

    print(f"当前时间(UTC): {now_iso}")
    if start_ts:
        elapsed = now_ts - start_ts
        print(f"系统启动时间(UTC): {start_iso}  (来源: {start_source})")
        print(f"运行时长: {format_duration(elapsed)}")
    else:
        elapsed = None
        print(f"系统启动时间: 未知  (来源: {start_source})")
    print()

    # 全局统计——按status分组
    print("--- 处理量统计 ---")
    status_stats = list(db.aql.execute(
        'FOR r IN devin_problem_runs '
        'FILTER r.batch_id == "pipe-runner" '
        'COLLECT status = r.status WITH COUNT INTO c '
        'SORT c DESC RETURN {status, count: c}', ttl=120))
    total_processed = sum(s['count'] for s in status_stats)
    solved = sum(s['count'] for s in status_stats if s['status'] == 'candidate_solved')
    not_solved = total_processed - solved

    print(f"  总处理题数:     {total_processed}")
    print(f"  已解决(solved): {solved} ({solved/total_processed*100:.1f}%)" if total_processed else "  已解决: 0")
    print(f"  未解决:         {not_solved} ({not_solved/total_processed*100:.1f}%)" if total_processed else "  未解决: 0")
    print()
    print("  按status细分:")
    for s in status_stats:
        print(f"    {s['status']}: {s['count']}")
    print()

    # 速率
    if elapsed and elapsed > 0 and total_processed > 0:
        rate = total_processed / (elapsed / 3600)
        print(f"  平均处理速率: {total_processed}题 / {format_duration(elapsed)} = {rate:.0f}题/小时")
        solved_rate = solved / (elapsed / 3600)
        print(f"  平均解决速率: {solved}题 / {format_duration(elapsed)} = {solved_rate:.0f}题/小时")
    print()

    # ============================================================
    # 第二部分：export文件统计
    # ============================================================
    print("--- export文件统计 ---")

    # 按export_check字段统计
    export_stats = list(db.aql.execute(
        'FOR r IN devin_problem_runs '
        'FILTER r.batch_id == "pipe-runner" '
        'FILTER r.status == "candidate_solved" '
        'COLLECT ec = r.export_check WITH COUNT INTO c '
        'SORT c DESC RETURN {export_check: ec, count: c}', ttl=120))
    print("  candidate_solved题的export检查状态:")
    total_solved = 0
    checked_passed = 0
    checked_failed = 0
    unchecked = 0
    for s in export_stats:
        ec = s['export_check']
        cnt = s['count']
        total_solved += cnt
        if ec == 'passed':
            checked_passed += cnt
            print(f"    checked=passed:    {cnt}")
        elif ec and ec.startswith('failed'):
            checked_failed += cnt
            print(f"    checked=failed:    {cnt}")
        elif ec == 'missing':
            checked_failed += cnt
            print(f"    checked=missing:   {cnt}")
        else:
            unchecked += cnt
            print(f"    unchecked(null):   {cnt}")
    print(f"    合计:              {total_solved}")
    print()

    if args.stats_only:
        print("(--stats-only 模式，跳过export检查)")
        print()
        return

    # ============================================================
    # 第三部分：export结构完整性检查（增量）
    # ============================================================
    print("=" * 70)
    print("  Export结构完整性检查")
    print("=" * 70)
    print()

    if args.recheck:
        print("模式: 强制重新检查所有candidate_solved题的export")
        aql = (
            'FOR r IN devin_problem_runs '
            'FILTER r.batch_id == "pipe-runner" '
            'FILTER r.status == "candidate_solved" '
            'SORT r.ended_at ASC '
        )
        bind_vars = {}
    else:
        print(f"模式: 增量检查（只查未检查过的，当前unchecked={unchecked}）")
        aql = (
            'FOR r IN devin_problem_runs '
            'FILTER r.batch_id == "pipe-runner" '
            'FILTER r.status == "candidate_solved" '
            'FILTER r.export_check == null '
            'SORT r.ended_at ASC '
        )
        bind_vars = {}

    if args.limit > 0:
        aql += 'LIMIT @limit '
        bind_vars['limit'] = args.limit

    aql += 'RETURN {_key: r._key, exp_id: r.exp_id, problem_id: r.problem_id, ended_at: r.ended_at}'

    records = list(db.aql.execute(aql, bind_vars=bind_vars, ttl=600))
    to_check = len(records)
    print(f"待检查: {to_check}个")
    print()

    if to_check == 0:
        print("没有需要检查的export文件，全部已检查过。")
        print()
        # 汇总最终结果
        print("--- export检查汇总 ---")
        print(f"  passed:  {checked_passed}")
        print(f"  failed:  {checked_failed}")
        print(f"  unchecked: {unchecked}")
        return

    # 逐个检查
    passed = 0
    failed_list = []
    missing_list = []
    batch_updates = []
    check_count = 0

    for rec in records:
        exp_id = rec['exp_id']
        export_path = TRAJECTORY_BASE / exp_id / "exports" / "conversation.json"
        result, size = check_export_structure(export_path)
        check_count += 1

        if check_count % 500 == 0:
            print(f"  进度: {check_count}/{to_check} ...", flush=True)

        if args.verbose or (not result.startswith('passed') and (len(failed_list) + len(missing_list)) < 20):
            if result != 'passed':
                print(f"  ❌ {exp_id} problem={rec['problem_id']}: {result}")

        # 准备DB更新
        update_doc = {
            '_key': rec['_key'],
            'export_check': result,
            'export_check_at': now_iso,
            'export_size': size,
        }
        batch_updates.append(update_doc)

        if result == 'passed':
            passed += 1
        elif result == 'missing':
            missing_list.append((exp_id, rec['problem_id']))
        else:
            failed_list.append((exp_id, rec['problem_id'], result))

        # 批量写入DB（每100个一批）
        if len(batch_updates) >= 100:
            db.collection(ATTEMPT_COLLECTION).update_many(batch_updates)
            batch_updates = []

    # 写入剩余
    if batch_updates:
        db.collection(ATTEMPT_COLLECTION).update_many(batch_updates)

    # === 检查结果 ===
    print()
    print(f"--- 本次检查结果 ---")
    print(f"  检查总数:  {to_check}")
    print(f"  passed:    {passed}")
    print(f"  failed:    {len(failed_list)}")
    print(f"  missing:   {len(missing_list)}")
    print()

    if failed_list:
        print(f"  ❌ 结构完整性失败 ({len(failed_list)}个):")
        for eid, pid, reason in failed_list[:20]:
            print(f"    {eid} problem={pid}: {reason}")
        if len(failed_list) > 20:
            print(f"    ... 共{len(failed_list)}个")
        print()

    if missing_list:
        print(f"  ❌ export文件缺失 ({len(missing_list)}个):")
        for eid, pid in missing_list[:20]:
            print(f"    {eid} problem={pid}")
        if len(missing_list) > 20:
            print(f"    ... 共{len(missing_list)}个")
        print()

    # === 全局汇总（含历史+本次） ===
    print("=" * 70)
    print("  全局export检查汇总（含历史+本次）")
    print("=" * 70)
    print()
    final_stats = list(db.aql.execute(
        'FOR r IN devin_problem_runs '
        'FILTER r.batch_id == "pipe-runner" '
        'FILTER r.status == "candidate_solved" '
        'COLLECT ec = r.export_check WITH COUNT INTO c '
        'SORT c DESC RETURN {export_check: ec, count: c}', ttl=120))

    total_final = 0
    final_passed = 0
    final_failed = 0
    final_unchecked = 0
    for s in final_stats:
        ec = s['export_check']
        cnt = s['count']
        total_final += cnt
        if ec == 'passed':
            final_passed += cnt
        elif ec is None:
            final_unchecked += cnt
        else:
            final_failed += cnt

    print(f"  candidate_solved总数:    {total_final}")
    print(f"  export检查passed:        {final_passed} ({final_passed/total_final*100:.1f}%)" if total_final else "")
    print(f"  export检查failed/missing: {final_failed} ({final_failed/total_final*100:.1f}%)" if total_final else "")
    print(f"  未检查:                  {final_unchecked}")
    print()
    print("  按检查结果细分:")
    for s in final_stats:
        ec = s['export_check'] or 'null(未检查)'
        print(f"    {ec}: {s['count']}")


if __name__ == "__main__":
    main()
