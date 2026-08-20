#!/usr/bin/env python3
"""check_progress.py — 检查解题系统进度（含真实时间和估算细节）

报告内容：
  1. 真实时间：系统启动时间、当前时间、运行时长
  2. Redis队列状态：pending/running/concurrency
  3. tier=1 extraction_status分布
  4. 吞吐情况：从启动到现在的总处理量、每小时速率（按实际运行时长计算）
  5. 最近1小时细分（如果运行超过1小时）
  6. 剩余时间估算（含计算依据）
  7. rate limit风暴预警
  8. --verbose: running详情（按运行时长分桶）

系统启动时间来源（按优先级）：
  1. Redis math:system:start_time（pipe_start.sh启动时写入）
  2. DB中最早的devin_problem_runs记录的started_at（fallback）
  3. 都没有则标注"未知"

用法:
  python check_progress.py              # 基本进度
  python check_progress.py --verbose    # 详细（含running详情）
"""
import sys, os, argparse, time, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from arango import ArangoClient
from redis_queue import get_redis
from datetime import datetime, timezone, timedelta


def get_system_start_time(r, db):
    """获取系统启动时间，返回 (unix_ts, iso_str, source)"""
    # 1. Redis中记录的启动时间
    val = r.get('math:system:start_time')
    if val:
        ts = float(val)
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return ts, iso, "Redis math:system:start_time"
    # 2. fallback: DB中最早的started_at
    try:
        aql = 'FOR r IN devin_problem_runs FILTER r.started_at != null SORT r.started_at ASC LIMIT 1 RETURN r.started_at'
        earliest = list(db.aql.execute(aql, ttl=30))
        if earliest:
            # 解析ISO时间
            dt = datetime.fromisoformat(earliest[0].replace('Z', '+00:00'))
            ts = dt.timestamp()
            return ts, earliest[0], "DB最早started_at（fallback，可能不精确）"
    except Exception:
        pass
    # 3. 未知
    return None, None, "未知"


def format_duration(seconds):
    """把秒数格式化为人类可读的时长"""
    if seconds < 60:
        return f"{seconds:.0f}秒"
    if seconds < 3600:
        return f"{seconds/60:.1f}分钟"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}小时{m}分"


def main():
    parser = argparse.ArgumentParser(description="检查解题系统进度（含真实时间和估算细节）")
    parser.add_argument('--verbose', action='store_true', help='显示running详情')
    args = parser.parse_args()

    r = get_redis()
    pending = r.zcard('math:pending')
    running = r.hlen('math:running')
    conc = r.get('math:config:concurrency')

    c = ArangoClient(hosts=os.environ.get('ARANGO_HOST', 'http://localhost:8529'))
    db = c.db(os.environ['ARANGO_DB'], username=os.environ.get('ARANGO_USER', 'root'),
              password=os.environ.get('ARANGO_PASS', ''))

    # === 真实时间 ===
    now_ts = time.time()
    now_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    start_ts, start_iso, start_source = get_system_start_time(r, db)

    print(f'当前时间(UTC): {now_iso}')
    if start_ts:
        elapsed = now_ts - start_ts
        print(f'系统启动时间(UTC): {start_iso}  (来源: {start_source})')
        print(f'运行时长: {format_duration(elapsed)}')
    else:
        elapsed = None
        print(f'系统启动时间: 未知  (来源: {start_source})')
    print()

    # === Redis队列 ===
    print(f'Redis: pending={pending} running={running} concurrency={conc}')
    print()

    # === tier=1 extraction_status分布 ===
    status_dist = list(db.aql.execute(
        'FOR d IN problem_extraction_progress FILTER d.difficulty_tier == 1 '
        'COLLECT status = d.extraction_status WITH COUNT INTO c '
        'SORT c DESC RETURN {status, count: c}', ttl=30))
    print('tier=1 extraction_status:')
    for s in status_dist:
        print(f'  {s["status"]}: {s["count"]}')
    print()

    # === 从启动到现在的总处理量 ===
    # 查DB中从启动时间到现在的所有run
    if start_ts and start_iso:
        stats_total = list(db.aql.execute(
            'FOR r IN devin_problem_runs FILTER r.ended_at >= @when '
            'COLLECT status = r.status WITH COUNT INTO c '
            'SORT c DESC RETURN {status, count: c}',
            bind_vars={'when': start_iso}, ttl=30))
    else:
        # 无启动时间，查全部
        stats_total = list(db.aql.execute(
            'FOR r IN devin_problem_runs '
            'COLLECT status = r.status WITH COUNT INTO c '
            'SORT c DESC RETURN {status, count: c}', ttl=30))

    total_all = sum(s['count'] for s in stats_total)
    solved_all = sum(s['count'] for s in stats_total if s['status'] == 'candidate_solved')

    print(f'从启动到现在: 总{total_all} solved={solved_all}')
    for s in stats_total:
        print(f'  {s["status"]}: {s["count"]}')

    # === 每小时速率（按实际运行时长计算）===
    print()
    if elapsed and elapsed > 0:
        if elapsed < 3600:
            # 不足1小时
            rate_per_hour = total_all / (elapsed / 3600)
            print(f'运行不足1小时（{format_duration(elapsed)}），'
                  f'按实际时长折算速率: {total_all}题 / {elapsed/60:.1f}分钟 = {rate_per_hour:.0f}题/小时')
        else:
            rate_per_hour = total_all / (elapsed / 3600)
            print(f'平均速率: {total_all}题 / {format_duration(elapsed)} = {rate_per_hour:.0f}题/小时')
    else:
        rate_per_hour = 0
        print('平均速率: 无法计算（运行时长未知）')

    # === 最近1小时细分（仅当运行超过1小时时）===
    print()
    if elapsed and elapsed >= 3600:
        now = datetime.now(timezone.utc)
        when_1h = (now - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
        stats_1h = list(db.aql.execute(
            'FOR r IN devin_problem_runs FILTER r.ended_at >= @when '
            'COLLECT status = r.status WITH COUNT INTO c '
            'SORT c DESC RETURN {status, count: c}',
            bind_vars={'when': when_1h}, ttl=30))
        total_1h = sum(s['count'] for s in stats_1h)
        solved_1h = sum(s['count'] for s in stats_1h if s['status'] == 'candidate_solved')
        stall_1h = sum(s['count'] for s in stats_1h if s['status'] == 'failed_stall')
        rl_1h = sum(s['count'] for s in stats_1h if s['status'] == 'rate_limited')

        print(f'最近1小时 ({when_1h} ~ {now_iso}): 总{total_1h} solved={solved_1h} '
              f'stall={stall_1h} rate_limited={rl_1h}')
        for s in stats_1h:
            print(f'  {s["status"]}: {s["count"]}')
    elif elapsed:
        print(f'最近1小时: 跳过（系统运行仅{format_duration(elapsed)}，不足1小时）')
    else:
        print('最近1小时: 跳过（系统启动时间未知）')

    # === 剩余时间估算（含计算依据）===
    print()
    if rate_per_hour > 0 and pending > 0:
        hours_left = pending / rate_per_hour
        print(f'剩余时间估算:')
        print(f'  pending={pending}题 / 速率={rate_per_hour:.0f}题/小时 = {hours_left:.1f}小时')
        print(f'  预计完成: {(datetime.now(timezone.utc) + timedelta(hours=hours_left)).strftime("%Y-%m-%dT%H:%M:%SZ")}')
        # 注意：如果feeder还在补充pending，这个估算会偏小
        feeder_running = True  # 简单假设feeder在运行
        if feeder_running and pending < 2000:
            print(f'  ⚠️ feeder仍在运行，pending可能被补充，实际完成时间可能更晚')
    elif pending == 0:
        print(f'剩余时间估算: pending=0，无剩余题')
    else:
        print(f'剩余时间估算: 无法计算（速率未知）')

    # === rate limit风暴预警 ===
    if elapsed and elapsed >= 3600:
        if rl_1h > 50:
            print(f'\n⚠️ rate_limited={rl_1h}，rate limit风暴迹象！建议降并发')
    else:
        # 不足1小时，查全部rate_limited
        rl_all = sum(s['count'] for s in stats_total if s['status'] == 'rate_limited')
        if rl_all > 10:
            print(f'\n⚠️ rate_limited={rl_all}（运行{format_duration(elapsed) or "?"}内），注意rate limit')

    # === verbose: running详情 ===
    if args.verbose:
        print()
        print('running详情:')
        running_data = r.hgetall('math:running')
        now_ts_v = time.time()
        buckets = {'<60s': 0, '60-300s': 0, '300-600s': 0, '600+s': 0}
        for k, v in running_data.items():
            data = json.loads(v)
            elapsed_v = now_ts_v - data.get('start_time', now_ts_v)
            if elapsed_v < 60: buckets['<60s'] += 1
            elif elapsed_v < 300: buckets['60-300s'] += 1
            elif elapsed_v < 600: buckets['300-600s'] += 1
            else: buckets['600+s'] += 1
        for b, cnt in buckets.items():
            print(f'  {b}: {cnt}')


if __name__ == '__main__':
    main()
