#!/usr/bin/env python3
"""shared_logger.py — 管道化系统共享日志基础设施

所有pipe脚本通过本模块获取logger，统一写入D盘日志目录。

日志规范：
- 日志目录: /Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_pipe/logs/
- 单文件分片: 1MB
- 总量限制: 10GB（约10000个分片）
- 循环滚动: 达到上限后覆盖最老的文件
- 格式: [时间] [级别] [模块] 消息
- 同时输出到文件和stdout（方便tmux中查看）

用法:
  from shared_logger import get_logger
  logger = get_logger("runner")
  logger.info("启动Runner, concurrency=30")
  logger.error("连接失败", exc_info=True)
"""
import logging
import logging.handlers
import os
from pathlib import Path

# 日志目录
LOG_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_pipe/logs")
LOG_BASE.mkdir(parents=True, exist_ok=True)

# 日志参数
MAX_BYTES_PER_FILE = 1 * 1024 * 1024  # 1MB per file
MAX_TOTAL_BYTES = 10 * 1024 * 1024 * 1024  # 10GB total
# RotatingFileHandler的backupCount = 总量/单文件 = 10240
BACKUP_COUNT = MAX_TOTAL_BYTES // MAX_BYTES_PER_FILE  # 10240

# 日志格式
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 已创建的logger缓存
_loggers = {}


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """获取一个模块的logger。

    每个模块（runner/collector/feeder等）有自己的日志文件，
    同时也写入一个统一的pipe.log。

    Args:
        name: 模块名（如"runner", "collector"）
        level: 日志级别

    Returns:
        配置好的logger
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(f"pipe.{name}")
    logger.setLevel(level)
    logger.propagate = False  # 不传播到root logger

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # 1. 模块专属日志文件（1MB分片，循环滚动）
    module_log = LOG_BASE / f"{name}.log"
    file_handler = logging.handlers.RotatingFileHandler(
        str(module_log),
        maxBytes=MAX_BYTES_PER_FILE,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 2. 统一日志文件（所有模块写入pipe.log，方便查看全局状态）
    pipe_log = LOG_BASE / "pipe.log"
    pipe_handler = logging.handlers.RotatingFileHandler(
        str(pipe_log),
        maxBytes=MAX_BYTES_PER_FILE,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    pipe_handler.setLevel(level)
    pipe_handler.setFormatter(formatter)
    # 用filter标记来源模块
    pipe_handler.addFilter(lambda record: setattr(record, 'module', name) or True)
    logger.addHandler(pipe_handler)

    # 3. stdout输出（方便tmux中实时查看）
    stdout_handler = logging.StreamHandler()
    stdout_handler.setLevel(level)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    _loggers[name] = logger
    return logger


def get_log_dir() -> Path:
    """返回日志目录路径"""
    return LOG_BASE


def get_log_size_info() -> dict:
    """返回当前日志目录的大小信息"""
    total_size = 0
    file_count = 0
    files = []
    for f in LOG_BASE.glob("*.log*"):
        size = f.stat().st_size
        total_size += size
        file_count += 1
        files.append({"name": f.name, "size": size})
    return {
        "log_dir": str(LOG_BASE),
        "total_size_mb": round(total_size / 1024 / 1024, 1),
        "total_size_gb": round(total_size / 1024 / 1024 / 1024, 3),
        "file_count": file_count,
        "limit_gb": 10,
        "limit_mb_per_file": 1,
        "files": sorted(files, key=lambda x: -x["size"])[:20],
    }


if __name__ == "__main__":
    # 自测
    logger = get_logger("test")
    logger.info("测试日志")
    logger.warning("测试警告")
    logger.error("测试错误")
    info = get_log_size_info()
    print(f"日志目录: {info['log_dir']}")
    print(f"总大小: {info['total_size_mb']}MB ({info['total_size_gb']}GB)")
    print(f"文件数: {info['file_count']}")
    print(f"限制: {info['limit_gb']}GB总量, {info['limit_mb_per_file']}MB/文件")
