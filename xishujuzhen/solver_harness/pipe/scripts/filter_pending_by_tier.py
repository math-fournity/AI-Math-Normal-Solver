#!/usr/bin/env python3
"""filter_pending_by_tier.py — 从Redis pending队列中移除非指定tier的题

用法:
  python filter_pending_by_tier.py --tier 1 --dry-run   # 预览
  python filter_pending_by_tier.py --tier 1              # 执行移除
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import redis
from arango import ArangoClient
from redis_queue import REDIS_HOST, REDIS_PORT, REDIS_DB

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tier', type=int, required=True, help='保留的tier')
    parser.add_argument('--dry-run', action='store_true', help='只预览不执行')
    args = parser.parse_args()

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
    # 取出所有pending key
    all_keys = r.zrange('math:pending', 0, -1)
    all_keys_str = [k.decode() if isinstance(k, bytes) else k for k in all_keys]
    total = len(all_keys_str)
    print(f'当前pending总数: {total}')

    # 连接ArangoDB
    c = ArangoClient(hosts=os.environ.get('ARANGO_HOST', 'http://localhost:8529'))
    db = c.db(os.environ['ARANGO_DB'], username=os.environ.get('ARANGO_USER', 'root'),
              password=os.environ.get('ARANGO_PASS', ''))

    # 分批查tier
    batch_size = 5000
    keep_keys = []
    remove_keys = []
    no_record_keys = []

    for i in range(0, total, batch_size):
        batch = all_keys_str[i:i+batch_size]
        # 查这些key的tier
        placeholders = ', '.join(f'"{k}"' for k in batch)
        aql = f'FOR d IN problem_extraction_progress FILTER d._key IN [{placeholders}] RETURN {{key: d._key, tier: d.difficulty_tier}}'
        results = list(db.aql.execute(aql, ttl=120))

        tier_map = {row['key']: row['tier'] for row in results}
        found_keys = set(tier_map.keys())

        for k in batch:
            if k not in found_keys:
                no_record_keys.append(k)
                continue
            if tier_map[k] == args.tier:
                keep_keys.append(k)
            else:
                remove_keys.append(k)

        print(f'  已处理 {min(i+batch_size, total)}/{total}')

    print(f'\n结果:')
    print(f'  保留 (tier={args.tier}): {len(keep_keys)}')
    print(f'  移除 (其他tier): {len(remove_keys)}')
    print(f'  无DB记录: {len(no_record_keys)}')

    # 移除的按tier统计
    if remove_keys:
        # 再查一次移除的题的tier分布
        batch_size2 = 5000
        tier_dist = {}
        for i in range(0, len(remove_keys), batch_size2):
            batch = remove_keys[i:i+batch_size2]
            placeholders = ', '.join(f'"{k}"' for k in batch)
            aql = f'FOR d IN problem_extraction_progress FILTER d._key IN [{placeholders}] COLLECT tier = d.difficulty_tier WITH COUNT INTO c RETURN {{tier, count: c}}'
            results = list(db.aql.execute(aql, ttl=120))
            for row in results:
                tier_dist[row['tier']] = tier_dist.get(row['tier'], 0) + row['count']
        print(f'  移除题的tier分布: {tier_dist}')

    if args.dry_run:
        print('\n--dry-run模式，未执行移除')
        return

    if remove_keys:
        # 从Redis sorted set中移除（分批用pipeline）
        removed = 0
        for i in range(0, len(remove_keys), 1000):
            batch = remove_keys[i:i+1000]
            pipe = r.pipeline()
            for k in batch:
                pipe.zrem('math:pending', k)
            pipe.execute()
            removed += len(batch)
            print(f'  已移除 {removed}/{len(remove_keys)}')
        print(f'\n已从Redis pending移除 {len(remove_keys)} 个非tier={args.tier}的题')

        # 把这些题的extraction_status改回pending（让feeder下次能重新入队到正确的tier）
        for i in range(0, len(remove_keys), 1000):
            batch = remove_keys[i:i+1000]
            for k in batch:
                db.collection('problem_extraction_progress').update({
                    '_key': k, 'extraction_status': 'pending'
                })
        print(f'已将 {len(remove_keys)} 个题的extraction_status改回pending')

    # 验证
    final = r.zcard('math:pending')
    print(f'\n最终pending: {final}')

if __name__ == '__main__':
    main()
