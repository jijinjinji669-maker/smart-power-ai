"""健康检查探针：读取 Redis 里的心跳时间戳。

解决的问题：
  Dockerfile 里的 HEALTHCHECK 是给 api 服务检查 8000 端口的，
  而 consumer / simulator 与 api 共用同一镜像，会继承这条探针，
  并因为不监听 8000 端口而永远显示 unhealthy —— 那只是探针问错了问题。

正确做法：
  长驻型消费者服务应该暴露「我最近还在干活」的信号。
  本模块让 consumer 每写入一批就更新 Redis 里的心跳时间戳，
  再由本探针判断该时间戳是否足够新。

这样检查的就不是"进程还在不在"（进程活着但消费停滞才是真故障），
而是"这个消费者最近真的处理过数据吗"。

用法（容器内）：
    python -m app.healthcheck --service consumer --max-age 180
退出码 0 = 健康，1 = 不健康。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import redis


def get_client() -> redis.Redis:
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        socket_connect_timeout=3,
        socket_timeout=3,
        decode_responses=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="基于 Redis 心跳的服务健康检查")
    parser.add_argument("--service", default="consumer", help="服务名，用于拼心跳 key")
    parser.add_argument("--max-age", type=float, default=180.0, help="心跳允许的最大延迟（秒）")
    args = parser.parse_args()

    key = f"heartbeat:{args.service}"

    try:
        client = get_client()
        raw = client.get(key)
    except Exception as exc:  # noqa: BLE001
        # 连不上 Redis 本身就算不健康
        print(f"UNHEALTHY: Redis 不可达 ({exc})", file=sys.stderr)
        return 1

    if raw is None:
        print(f"UNHEALTHY: 心跳 {key} 不存在，服务可能从未成功启动", file=sys.stderr)
        return 1

    try:
        last = float(raw)
    except (TypeError, ValueError):
        print(f"UNHEALTHY: 心跳 {key} 内容不是时间戳: {raw!r}", file=sys.stderr)
        return 1

    age = time.time() - last
    if age > args.max_age:
        print(
            f"UNHEALTHY: 心跳已过期 {age:.0f}s（阈值 {args.max_age:.0f}s），"
            f"服务可能消费停滞",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {key} 延迟 {age:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
