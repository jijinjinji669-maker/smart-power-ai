"""手动注入故障的客户端工具。

模拟器订阅 `power/control/inject`，本工具负责发指令并打印执行结果。

用法示例：

    # 列出可用场景与设备
    python -m tools.inject list
    python -m tools.inject devices

    # 注入「多台大功率同时开」到指定设备，持续 5 分钟
    python -m tools.inject overload --device AJS-BRK-2026-0003 --duration 300

    # 按场景精确注入，并覆盖峰值倍数
    python -m tools.inject --profile motor_stall --peak 4.0 --duration 30

    # 随机挑一台设备注入漏电劣化
    python -m tools.inject leakage

    # 清掉某台设备生效中的事件
    python -m tools.inject clear --device AJS-BRK-2026-0003

连接参数默认取环境变量 MQTT_HOST / MQTT_PORT，也可用 --host / --port 指定。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid

# paho-mqtt 采用延迟导入：本模块的「构造指令 / 打印结果」是纯逻辑，
# 不依赖 MQTT 库，这样没有装 paho 的环境也能跑 --help 和单元级验证。
# 真正发送时才导入（见 main）。

# 按故障类型注入时，映射到该类型下的默认场景
TYPE_DEFAULT_PROFILE = {
    "overload": "multi_appliance",
    "leakage": "damp_creep",
    "voltage": "sag_from_load",
    "inrush": "compressor_inrush",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="向模拟器发送故障注入指令",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "action", nargs="?", default="inject",
        choices=["inject", "list", "devices", "clear", "overload", "leakage", "voltage", "inrush"],
        help="动作：inject 注入 / list 列场景 / devices 列设备 / clear 清除 / "
             "或直接写故障类型（overload/leakage/voltage/inrush）",
    )
    parser.add_argument("--profile", default=None, help="场景名，如 multi_appliance")
    parser.add_argument("--device", default=None, help="设备序列号；缺省则随机挑一台")
    parser.add_argument("--peak", type=float, default=None, help="覆盖峰值倍数")
    parser.add_argument("--duration", type=float, default=None, help="覆盖持续秒数")
    parser.add_argument("--host", default=None, help="MQTT 主机")
    parser.add_argument("--port", type=int, default=None, help="MQTT 端口")
    parser.add_argument("--topic", default=None, help="控制主题")
    parser.add_argument("--timeout", type=float, default=10.0, help="等待结果的秒数")
    return parser


def build_payload(args) -> dict:
    action = args.action

    if action in ("list", "devices", "clear"):
        payload = {"action": action}
        if args.device:
            payload["device_sn"] = args.device
        return payload

    payload: dict = {"action": "inject", "request_id": uuid.uuid4().hex[:8]}

    if args.profile:
        payload["profile"] = args.profile
    elif action in TYPE_DEFAULT_PROFILE:
        payload["fault_type"] = action

    if args.device:
        payload["device_sn"] = args.device
    if args.peak is not None:
        payload["peak_multiple"] = args.peak
    if args.duration is not None:
        payload["duration_seconds"] = args.duration

    return payload


def main() -> int:
    args = build_parser().parse_args()

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print(
            "缺少依赖 paho-mqtt。本地调试可执行：\n"
            "    pip install paho-mqtt\n"
            "生产环境该依赖已在主镜像里（见 requirements.txt）。",
            file=sys.stderr,
        )
        return 2

    host = args.host or os.getenv("MQTT_HOST", "localhost")
    port = args.port or int(os.getenv("MQTT_PORT", "1883"))
    control_topic = args.topic or os.getenv("MQTT_CONTROL_TOPIC", "power/control/inject")
    result_prefix = os.getenv("MQTT_RESULT_PREFIX", "power/control/result")

    payload = build_payload(args)
    request_id = payload.get("request_id", "unknown")
    result_topic = f"{result_prefix}/{request_id}"

    got: dict = {}
    done = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        # paho-mqtt v2 的回调参数是 ReasonCode 对象，不是 int，
        # 直接 int(reason_code) 会抛 TypeError。取 .value 才是数值。
        rc = getattr(reason_code, "value", reason_code)
        if rc != 0:
            print(f"连接失败 rc={rc}（{reason_code}）", file=sys.stderr)
            got["error"] = f"MQTT 连接失败 rc={rc}"
            done.set()
            return
        client.subscribe(result_topic, qos=1)
        client.publish(control_topic, json.dumps(payload, ensure_ascii=False), qos=1)

    def on_message(client, userdata, msg):
        try:
            got.update(json.loads(msg.payload.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            got["error"] = f"结果解析失败：{exc}"
        done.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"inject-{request_id}")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=20)
    client.loop_start()

    print(f"→ {control_topic}")
    print(f"  请求：{json.dumps(payload, ensure_ascii=False)}")

    ok = done.wait(args.timeout)
    client.loop_stop()
    client.disconnect()

    if not ok:
        print(f"\n✗ {args.timeout:.0f} 秒内未收到结果", file=sys.stderr)
        print("  排查：模拟器在跑吗？主题对吗？（--topic 可指定）", file=sys.stderr)
        return 1

    if not got.get("ok"):
        print(f"\n✗ 执行失败：{got.get('error', '未提供原因')}", file=sys.stderr)
        return 1

    action = got.get("action", "")
    print("\n✓ 执行成功")

    if action == "inject":
        print(f"  设备      {got['device_sn']}（{got['device_name']}）")
        print(f"  回路档位  {got['tier']}  额定 {got['rated_current']:.0f}A")
        print(f"  场景      {got['profile_label']}（{got['profile']}）")
        print(f"  峰值      {got['peak_multiple']:.2f} 倍")
        print(f"  持续      {got['duration_seconds']:.0f} 秒")

        if got.get("is_normal"):
            print("  注意      这是正常现象，不是故障 —— 用来考验检测器会不会误报")
        if "target_current_a" in got:
            verdict = "会脱扣" if got.get("will_trip") else "不脱扣"
            print(f"  目标电流  {got['target_current_a']:.1f}A  "
                  f"（热脱扣边界 {got['thermal_trip_a']:.1f}A，"
                  f"磁脱扣下界 {got['magnetic_min_a']:.1f}A）→ {verdict}")
    elif action == "list_profiles":
        print(f"  共 {len(got['profiles'])} 个场景：")
        for p in got["profiles"]:
            flag = "（正常现象）" if p["is_normal"] else ""
            print(f"    {p['key']:<20} {p['label']}{flag}")
            print(f"      {p['description']}")
    elif action == "list_devices":
        print(f"  共 {len(got['devices'])} 台设备：")
        for d in got["devices"]:
            print(f"    {d['device_sn']}  {d['name']:<14} "
                  f"额定 {d['rated_current']:.0f}A  脱扣边界 {d['thermal_trip']:.1f}A  "
                  f"生效事件 {d['active_events']}")
    elif action == "clear":
        print(f"  已清除 {got.get('removed_events', 0)} 个生效中的事件")

    return 0


if __name__ == "__main__":
    sys.exit(main())
