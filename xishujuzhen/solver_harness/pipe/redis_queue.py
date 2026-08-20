#!/usr/bin/env python3
"""redis_queue.py — Redis队列操作封装

封装对Redis队列的所有操作，4个服务共用。
队列结构：
  math:pending    — Sorted Set, score=priority, member=problem_key
  math:running    — Hash, field=attempt_id, value=JSON metadata
  math:completed  — List, JSON result
  math:failed     — List, JSON result
  math:stats      — Hash, 实时统计
"""
import json
import time
import redis
from typing import Any

REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 0

PENDING_KEY = "math:pending"
RUNNING_KEY = "math:running"
COMPLETED_KEY = "math:completed"
FAILED_KEY = "math:failed"
STATS_KEY = "math:stats"


def get_redis() -> redis.Redis:
    """获取Redis连接（带重连）"""
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)


def enqueue_pending(r: redis.Redis, problem_key: str, priority: int) -> int:
    """将题目加入pending队列（Sorted Set by priority）"""
    return r.zadd(PENDING_KEY, {problem_key: priority})


def dequeue_pending(r: redis.Redis, count: int = 1) -> list[tuple[str, int]]:
    """从pending队列取出最高优先级的题目（ZPOPMIN，原子操作）"""
    results = r.zpopmin(PENDING_KEY, count)
    # results = [(member, score), ...]
    return [(m, int(s)) for m, s in results]


def add_running(r: redis.Redis, attempt_id: str, metadata: dict[str, Any]) -> int:
    """将attempt加入running队列"""
    return r.hset(RUNNING_KEY, attempt_id, json.dumps(metadata))


def get_running(r: redis.Redis, attempt_id: str) -> dict[str, Any] | None:
    """获取单个running attempt的metadata"""
    val = r.hget(RUNNING_KEY, attempt_id)
    return json.loads(val) if val else None


def get_all_running(r: redis.Redis) -> dict[str, dict[str, Any]]:
    """获取所有running attempt"""
    raw = r.hgetall(RUNNING_KEY)
    return {k: json.loads(v) for k, v in raw.items()}


def remove_running(r: redis.Redis, attempt_id: str) -> int:
    """从running队列移除"""
    return r.hdel(RUNNING_KEY, attempt_id)


def add_completed(r: redis.Redis, result: dict[str, Any]) -> int:
    """加入completed队列"""
    return r.lpush(COMPLETED_KEY, json.dumps(result))


def add_failed(r: redis.Redis, result: dict[str, Any]) -> int:
    """加入failed队列"""
    return r.lpush(FAILED_KEY, json.dumps(result))


def pending_count(r: redis.Redis) -> int:
    return r.zcard(PENDING_KEY)


def running_count(r: redis.Redis) -> int:
    return r.hlen(RUNNING_KEY)


def completed_count(r: redis.Redis) -> int:
    return r.llen(COMPLETED_KEY)


def failed_count(r: redis.Redis) -> int:
    return r.llen(FAILED_KEY)


def update_stats(r: redis.Redis):
    """更新实时统计"""
    r.hset(STATS_KEY, mapping={
        "pending": pending_count(r),
        "running": running_count(r),
        "completed": completed_count(r),
        "failed": failed_count(r),
        "updated_at": int(time.time()),
    })


def get_stats(r: redis.Redis) -> dict[str, Any]:
    """获取统计"""
    return r.hgetall(STATS_KEY)


def clear_all(r: redis.Redis):
    """清空所有队列（dry-run测试用）"""
    r.delete(PENDING_KEY, RUNNING_KEY, COMPLETED_KEY, FAILED_KEY, STATS_KEY)


def ping() -> bool:
    """测试Redis连接"""
    try:
        r = get_redis()
        return r.ping()
    except Exception:
        return False
