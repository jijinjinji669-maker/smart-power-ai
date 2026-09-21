"""MQTT 发布层：把设备模型产出的数据发到 broker。

设备的行为模型在 simulator/device.py（零依赖，便于单测与冒烟测试）。
本文件只负责 MQTT 连接、发布、重复上报与优雅退出。

运行：python -m simulator.breaker_sim --devices 20 --interval 5
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
from datetime import datetime

import paho.mqtt.client as mqtt

from simulator.device import TIERS, BreakerDevice

__all__ = ["BreakerDevice", "TIERS", "build_client", "main"]

# 默认设备构成：4 台照明 + 6 台插座 + 6 台空调 + 4 台总开关 = 20 台
DEFAULT_TIER_MIX = [
    "lighting", "lighting", "lighting", "lighting",
    "socket", "socket", "socket", "socket", "socket", "socket",
    "aircon", "aircon", "aircon", "aircon", "aircon", "aircon",
    "main", "main", "main", "main",
]


def build_client(host: str, port: int) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="breaker-simulator")

    def on_connect(c, userdata, flags, reason_code, properties=None):
        print(f"[sim] MQTT connected rc={reason_code}", flush=True)

    def on_disconnect(c, userdata, flags, reason_code, properties=None):
        print(f"[sim] MQTT disconnected rc={reason_code}, 将自动重连", flush=True)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    # 指数退避，避免 broker 没起来时死循环打爆连接
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(host, port, keepalive=30)
    return client


def main() -> int:
    parser = argparse.ArgumentParser(description="智能断路器数据模拟器")
    parser.add_argument("--devices", type=int, default=20, help="设备数量（默认 20）")
    parser.add_argument("--interval", type=float, default=5.0, help="每台设备上报间隔（秒）")
    parser.add_argument("--host", default=None, help="MQTT 主机，默认读 MQTT_HOST 环境变量")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--prefix", default=None)
    parser.add_argument("--seed", type=int, default=20260916, help="随机种子，固定后数据可复现")
    parser.add_argument(
        "--tiers", default=None,
        help="档位构成，逗号分隔。默认 4 照明 + 6 插座 + 6 空调 + 4 总开关",
    )
    args = parser.parse_args()

    host = args.host or os.getenv("MQTT_HOST", "localhost")
    port = args.port or int(os.getenv("MQTT_PORT", "1883"))
    prefix = args.prefix or os.getenv("MQTT_TOPIC_PREFIX", "power/breaker")

    rng = random.Random(args.seed)

    # 按档位构成创建设备：每台设备是哪种回路决定了它的额定电流与保护边界
    if args.tiers:
        mix = [t.strip() for t in args.tiers.split(",") if t.strip()]
    else:
        mix = DEFAULT_TIER_MIX[: args.devices]

    # 设备数超出预设构成时循环补齐
    while len(mix) < args.devices:
        mix.append(DEFAULT_TIER_MIX[len(mix) % len(DEFAULT_TIER_MIX)])
    mix = mix[: args.devices]

    unknown = [t for t in mix if t not in TIERS]
    if unknown:
        parser.error(f"未知档位 {unknown}，可选：{', '.join(TIERS)}")

    devices = [
        BreakerDevice(i + 1, rng, TIERS[tier_key])
        for i, tier_key in enumerate(mix)
    ]

    tier_summary: dict[str, int] = {}
    for d in devices:
        tier_summary[d.tier.label] = tier_summary.get(d.tier.label, 0) + 1

    client = build_client(host, port)
    client.loop_start()

    running = True

    def stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    started = time.time()
    sent = faults = offline = 0
    next_tick = {d.sn: started for d in devices}

    print(f"[sim] {len(devices)} 台设备开始上报 -> {host}:{port} topic={prefix}/<sn>", flush=True)
    print(
        "[sim] 档位构成：" + "，".join(f"{k} {v} 台" for k, v in tier_summary.items()),
        flush=True,
    )
    try:
        while running:
            now = time.time()
            now_dt = datetime.now().astimezone()
            drift_hours = (now - started) / 3600.0
            for dev in devices:
                if now < next_tick[dev.sn]:
                    continue
                next_tick[dev.sn] = now + args.interval
                payload = dev.sample(now, now_dt, drift_hours)
                if payload is None:
                    offline += 1
                    continue
                topic = f"{prefix}/{dev.sn}"
                body = json.dumps(payload, ensure_ascii=False)
                client.publish(topic, body, qos=1)
                sent += 1
                if payload["injected_fault"]:
                    faults += 1
                # 重复上报：模拟网络重传，考验入库幂等
                if rng.random() < 0.01:
                    client.publish(topic, body, qos=1)
                    sent += 1
            time.sleep(0.2)
    finally:
        client.loop_stop()
        client.disconnect()
        print(
            f"[sim] 停止。已发送 {sent} 条（含重复），注入故障 {faults} 条，离线跳过 {offline} 次",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
