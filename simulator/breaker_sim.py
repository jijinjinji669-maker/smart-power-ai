"""MQTT 发布层：把设备模型产出的数据发到 broker。

设备的行为模型在 simulator/device.py（零依赖，便于单测与冒烟测试）。
本文件只负责 MQTT 连接、发布、重复上报与优雅退出。

运行：python -m simulator.breaker_sim --devices 10 --interval 5
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

from simulator.device import RATED_CURRENT, BreakerDevice

__all__ = ["BreakerDevice", "RATED_CURRENT", "build_client", "main"]


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
    parser.add_argument("--devices", type=int, default=10, help="设备数量")
    parser.add_argument("--interval", type=float, default=5.0, help="每台设备上报间隔（秒）")
    parser.add_argument("--host", default=None, help="MQTT 主机，默认读 MQTT_HOST 环境变量")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--prefix", default=None)
    parser.add_argument("--seed", type=int, default=20260916, help="随机种子，固定后数据可复现")
    args = parser.parse_args()

    host = args.host or os.getenv("MQTT_HOST", "localhost")
    port = args.port or int(os.getenv("MQTT_PORT", "1883"))
    prefix = args.prefix or os.getenv("MQTT_TOPIC_PREFIX", "power/breaker")

    rng = random.Random(args.seed)
    devices = [BreakerDevice(i + 1, rng) for i in range(args.devices)]

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
