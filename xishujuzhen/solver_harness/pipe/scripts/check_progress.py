#!/usr/bin/env python3
"""check_progress.py — 检查解题系统进度

用法:
  python check_progress.py              # 基本进度
  python check_progress.py --verbose    # 详细（含running详情）
"""
import sys, os, argparse, time, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from arango import ArangoClient
from redis_queue import get_redis
from datetime import datetime, timezone, timedelta

def main():
    parser = argparse.ArgumentParser(description="检查解题系统进度")
    parser.add_argument('--verbose', action='store_true', help='显示running详情')
    args = parser.parse_args()

    r = get_redis()
    pending = r.zcard('math:pending')
    running = r.hlen('math:running')

    c = ArangoClient(hosts=os.environ.get('ARANGO_HOST', 'http://localhost:8529'))
    db = c.db(os.environ['ARANGO_DB'], username=os.environ.get('ARANGO_USER', 'root'),
              password=os.environ.get('ARANGO_PASS', ''))

    # tier=1 extraction_status分布
    status_dist = list(db.aql.execute(
        'FOR d IN problem_extraction_progress FILTER d.difficulty_tier == 1 '
        'COLLECT status = d.extraction_status WITH COUNT INTO c '
        'SORT c DESC RETURN {status, count: c}', ttl=30))

    # 最近1小时按status统计
    now = datetime.now(timezone.utc)
    when = (now - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
    stats = list(db.aql.execute(
        'FOR r IN devin_problem_runs FILTER r.ended_at >= @when '
        'COLLECT status = r.status WITH COUNT INTO c '
        'SORT c DESC RETURN {status, count: c}',
        bind_vars={'when': when}, ttl=30))
    total_1h = sum(s['count'] for s in stats)
    solved_1h = sum(s['count'] for s in stats if s['status'] == 'candidate_solved')
    stall_1h = sum(s['count'] for s in stats if s['status'] == 'failed_stall')
    rl_1h = sum(s['count'] for s in stats if s['status'] == 'rate_limited')

    # 并发配置
    conc = r.get('math:config:concurrency')

    print(f'Redis: pending={pending} running={running} concurrency={conc}')
    print()
    print('tier=1 extraction_status:')
    for s in status_dist:
        print(f'  {s["status"]}: {s["count"]}')
    print()
    print(f'最近1小时: 总{total_1h} solved={solved_1h} stall={stall_1h} rate_limited={rl_1h}')
    for s in stats:
        print(f'  {s["status"]}: {s["count"]}')

    # 剩余时间估算
    if total_1h > 0:
        hours_left = pending / total_1h
        print(f'\n按{total_1h}题/小时估算，剩余{pending}题 ≈ {hours_left:.1f}小时')

    # rate limit风暴预警
    if rl_1h > 50:
        print(f'\n⚠️ rate_limited={rl_1h}，rate limit风暴迹象！建议降并发')

    # verbose: running详情
    if args.verbose:
        print()
        print('running详情:')
        running_data = r.hgetall('math:running')
        now_ts = time.time()
        buckets = {'<60s': 0, '60-300s': 0, '300-600s': 0, '600+s': 0}
        for k, v in running_data.items():
            data = json.loads(v)
            elapsed = now_ts - data.get('start_time', now_ts)
            if elapsed < 60: buckets['<60s'] += 1
            elif elapsed < 300: buckets['60-300s'] += 1
            elif elapsed < 600: buckets['300-600s'] += 1
            else: buckets['600+s'] += 1
        for b, cnt in buckets.items():
            print(f'  {b}: {cnt}')

if __name__ == '__main__':
    main()
