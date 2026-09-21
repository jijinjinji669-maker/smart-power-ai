"""MQTT 发布层：把设备模型产出的数据发到 broker，并处理手动注入指令。

职责划分：
  · simulator/device.py     设备行为模型（零依赖）
  · simulator/scheduler.py  自动故障调度（泊松过程）
  · simulator/control.py    手动注入指令的解析与执行
  · simulator/profiles.py   场景库 YAML 覆盖
  · 本文件                   MQTT 连接、发布、订阅、优雅退出

运行：python -m simulator.breaker_sim --devices 20 --interval 5 --fault-level demo
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import random
import signal
import sys
import time
from datetime import datetime

import paho.mqtt.client as mqtt

from simulator import profiles as profile_loader
from simulator.control import DEFAULT_CONTROL_TOPIC, DEFAULT_RESULT_PREFIX, ControlHandler
from simulator.device import TIERS, BreakerDevice
from simulator.scheduler import FAULT_LEVELS, FaultScheduler

__all__ = [
    "BreakerDevice", "TIERS", "FAULT_LEVELS", "FaultScheduler",
    "ControlHandler", "build_client", "main",
]

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
    parser.add_argument(
        "--fault-level", default="demo", choices=sorted(FAULT_LEVELS),
        help="自动故障率档位：realistic 对齐真实年故障率 / demo 演示 / off 关闭（纯手动注入）",
    )
    parser.add_argument(
        "--control-topic", default=None,
        help=f"手动注入指令主题，默认 {DEFAULT_CONTROL_TOPIC}",
    )
    parser.add_argument(
        "--result-prefix", default=None,
        help=f"注入结果回传主题前缀，默认 {DEFAULT_RESULT_PREFIX}/<request_id>",
    )
    args = parser.parse_args()

    host = args.host or os.getenv("MQTT_HOST", "localhost")
    port = args.port or int(os.getenv("MQTT_PORT", "1883"))
    prefix = args.prefix or os.getenv("MQTT_TOPIC_PREFIX", "power/breaker")
    control_topic = args.control_topic or os.getenv("MQTT_CONTROL_TOPIC", DEFAULT_CONTROL_TOPIC)
    result_prefix = args.result_prefix or os.getenv(
        "MQTT_RESULT_PREFIX", DEFAULT_RESULT_PREFIX
    )

    # 场景库覆盖必须在创建设备之前应用，否则设备读到的是旧参数
    profile_loader.apply_overrides()

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

    # 手动注入：控制指令经队列从 MQTT 回调线程转到主线程执行，
    # 避免在回调线程里直接改设备状态（与采样线程竞争）。
    control_queue: queue.Queue = queue.Queue(maxsize=256)
    control = ControlHandler(devices, rng=rng)

    client = build_client(host, port)

    def on_message(c, userdata, msg):
        """只做最轻的事：把原始 payload 塞进队列，立即返回。"""
        try:
            control_queue.put_nowait((msg.topic, msg.payload))
        except queue.Full:
            print("[sim] 控制指令队列已满，丢弃一条", flush=True)

    client.on_message = on_message
    client.subscribe(control_topic, qos=1)
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

    # 故障事件调度器：按泊松过程自动产生故障（阶段 2）。
    # level=off 时完全不产生自动故障，全部交给阶段 4 的手动注入。
    scheduler = FaultScheduler(devices, level=args.fault_level, rng=rng)
    last_report = started
    active_count = 0

    print(f"[sim] {len(devices)} 台设备开始上报 -> {host}:{port} topic={prefix}/<sn>", flush=True)
    print(
        "[sim] 档位构成：" + "，".join(f"{k} {v} 台" for k, v in tier_summary.items()),
        flush=True,
    )
    print(f"[sim] 故障调度档位：{args.fault_level}", flush=True)
    print(f"[sim] 手动注入主题：{control_topic}（结果回传 {result_prefix}/<request_id>）",
          flush=True)
    try:
        while running:
            now = time.time()
            now_dt = datetime.now().astimezone()
            drift_hours = (now - started) / 3600.0

            # ---------- 处理手动注入指令（在主线程执行）----------
            while True:
                try:
                    _topic, raw = control_queue.get_nowait()
                except queue.Empty:
                    break

                parsed = control.parse(raw)
                if isinstance(parsed, tuple):
                    _, err = parsed
                    result = {"ok": False, "error": err}
                    # 解析失败时无法拿到 request_id，用 unknown 主题回传
                    client.publish(
                        f"{result_prefix}/unknown",
                        json.dumps(result, ensure_ascii=False),
                        qos=1,
                    )
                    print(f"[sim] 注入指令被拒绝：{err}", flush=True)
                    continue

                result = control.execute(parsed)
                client.publish(
                    control.result_topic(result_prefix, parsed.request_id),
                    json.dumps(result, ensure_ascii=False),
                    qos=1,
                )
                if result.get("ok") and result.get("action") == "inject":
                    print(
                        f"[sim] 手动注入 {result['device_sn']} "
                        f"{result['profile_label']} "
                        f"峰值 {result['peak_multiple']}x "
                        f"持续 {result['duration_seconds']:.0f}s"
                        + (
                            f" → 目标电流 {result['target_current_a']}A，"
                            f"脱扣边界 {result['thermal_trip_a']}A，"
                            f"{'会脱扣' if result['will_trip'] else '不脱扣'}"
                            if "target_current_a" in result else ""
                        ),
                        flush=True,
                    )
                else:
                    print(f"[sim] 控制指令 {parsed.action} → {result}", flush=True)

            # 推进调度器：到点的事件会被放进对应设备的事件队列
            for dev, ev in scheduler.tick(now):
                print(
                    f"[sim] 事件开始 {dev.sn}({dev.tier.label}) "
                    f"{ev.profile.label} 持续 {ev.duration:.0f}s "
                    f"峰值 {ev.peak_multiple:.2f}",
                    flush=True,
                )

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

            # 每 5 分钟打一次运行统计，便于观察故障率是否符合预期
            if now - last_report >= 300:
                last_report = now
                active_count = sum(
                    1 for d in devices if any(e.is_active(now) for e in d.events)
                )
                print(
                    f"[sim] 运行统计：已发送 {sent} 条，注入故障 {faults} 条，"
                    f"离线 {offline} 次，当前生效事件 {active_count} 个 | "
                    f"{scheduler.stats.summary(len(devices))}",
                    flush=True,
                )
            time.sleep(0.2)
    finally:
        client.loop_stop()
        client.disconnect()
        print(
            f"[sim] 停止。已发送 {sent} 条（含重复），注入故障 {faults} 条，"
            f"离线跳过 {offline} 次 | {scheduler.stats.summary(len(devices))}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
