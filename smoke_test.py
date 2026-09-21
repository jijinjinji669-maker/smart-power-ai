"""本地冒烟测试：零第三方依赖验证核心逻辑。

不需要 PostgreSQL / Redis / MQTT / pydantic —— 只验证「设备模型 + 异常检测」。

阶段 1 的验收重点：**过载时电流必须真的达到断路器的动作条件。**
初版用绝对增量（+16 A / 40 A 额定 = 0.72 倍），按 IEC 60898-1 连热保护门槛
（1.13 倍）都不到，所以那些"过载"在物理上不成立。本测试把这个断言固化下来。

运行（在 smart-power-ai 目录下）：
    python smoke_test.py
"""
from __future__ import annotations

import random
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from app.detector import AnomalyDetector, DetectParams
from simulator.device import (
    FAULT_PROFILES,
    TIERS,
    TIER_AIRCON,
    TIER_LIGHTING,
    TIER_MAIN,
    TIER_SOCKET,
    BreakerDevice,
    FaultEvent,
)


def build_stream(minutes: int = 240, tier_key: str = TIER_SOCKET):
    """生成逐分钟样本，并在固定时点插入三段故障事件。"""
    rng = random.Random(20260916)
    start = datetime(2026, 9, 16, 8, 0)
    now0 = time.time()

    dev = BreakerDevice(1, rng, TIERS[tier_key])
    dev.next_offline_check = 1e18          # 冒烟测试里关掉随机离线

    # 三段故障事件（用 profile 定义的倍数，不再手写绝对值）
    dev.events = [
        FaultEvent(
            fault_type="overload",
            profile=FAULT_PROFILES["multi_appliance"],
            start_at=now0 + 60 * 60,
            duration=6 * 60,
            peak_multiple=2.0,
            source="manual",
        ),
        FaultEvent(
            fault_type="leakage",
            profile=FAULT_PROFILES["damp_creep"],
            start_at=now0 + 120 * 60,
            duration=8 * 60,
            peak_multiple=0.0,
            source="manual",
        ),
        FaultEvent(
            fault_type="voltage",
            profile=FAULT_PROFILES["sag_from_load"],
            start_at=now0 + 180 * 60,
            duration=4 * 60,
            peak_multiple=0.20,
            source="manual",
        ),
    ]

    rows, truth = [], []
    for minute in range(minutes):
        t = now0 + minute * 60
        now = start + timedelta(minutes=minute)
        payload = dev.sample(t, now, drift_hours=minute / 60.0)
        if payload is None:
            continue
        rows.append({"t": now, **payload})
        truth.append(payload["injected_fault"] is not None)
    return rows, truth, dev


def main() -> int:
    print("=" * 74)
    print("智慧用电 AI 平台 · 本地冒烟测试（零依赖）")
    print("=" * 74)

    base_params = DetectParams()

    # ---------------- 设备规格表 ----------------
    print("\n[设备档位] 脱扣阈值由额定值与曲线推导，不手写")
    print(f"  {'档位':<12}{'额定':>7}{'曲线':>5}{'保持':>9}{'动作':>9}{'磁脱扣':>10}")
    for key in (TIER_LIGHTING, TIER_SOCKET, TIER_AIRCON, TIER_MAIN):
        s = TIERS[key]
        print(f"  {s.label:<12}{s.rated_current:>6.0f}A{s.curve:>5}"
              f"{s.thermal_hold:>8.1f}A{s.thermal_trip:>8.1f}A{s.magnetic_min:>9.0f}A")

    rows, truth, dev = build_stream()
    spec = dev.tier

    # 关键：检测器的过载阈值必须由该设备的额定电流派生，不能用全局绝对值
    params = base_params.with_rated_current(spec.rated_current)
    print(f"\n[配置] 窗口={params.detect_window}  MAD系数k={params.detect_mad_k}  "
          f"连续确认={params.detect_min_confirm}")
    print(f"       漏电红线={params.leakage_limit_ma}mA（绝对安全阈值）  "
          f"欠压线={params.voltage_min_v}V（绝对）")
    print(f"       过载起点={params.overload_floor_a:.1f}A "
          f"（= 1.13 × 额定 {spec.rated_current:.0f}A，由设备档位推导）")

    print(f"\n[数据] {len(rows)} 个逐分钟样本（{spec.label} {spec.rated_current:.0f}A，"
          f"08:00 起），注入过载 / 漏电 / 欠压三段事件")

    # ---------------- 检测 ----------------
    detector = AnomalyDetector(params)
    pred, hits, peak_current = [], [], 0.0
    for row in rows:
        detector.push(row["current"], row["voltage"], row["leakage"], row["temperature"])
        alerts = detector.detect()
        pred.append(len(alerts) > 0)
        peak_current = max(peak_current, row["current"])
        for a in alerts:
            hits.append((row["t"], a.alert_type, a.severity, a.reason))

    tp = sum(1 for p, t in zip(pred, truth) if p and t)
    fp = sum(1 for p, t in zip(pred, truth) if p and not t)
    fn = sum(1 for p, t in zip(pred, truth) if not p and t)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    print("\n[结果] 检测性能")
    print(f"  命中TP={tp}   误报FP={fp}   漏报FN={fn}   告警总数={len(hits)}")
    print(f"  精确率 Precision = {precision:.1%}")
    print(f"  召回率 Recall    = {recall:.1%}")
    print(f"  F1              = {f1:.1%}")

    print("\n[明细] 告警样本（前 12 条）")
    if not hits:
        print("  == 没有任何告警，检测器没工作 ==")
    for t, kind, sev, reason in hits[:12]:
        print(f"  {t:%H:%M}  [{kind}] sev={sev}  {reason}")
    if len(hits) > 12:
        print(f"  ... 另有 {len(hits) - 12} 条")

    # ---------------- 断言 ----------------
    _, trip_reason = dev.would_trip(peak_current)
    kinds = {h[1] for h in hits}

    checks = [
        # 阶段 1 的核心验收：过载必须真的达到动作条件
        (f"过载峰值 {peak_current:.1f}A >= 热脱扣边界 {spec.thermal_trip:.1f}A",
         peak_current >= spec.thermal_trip),
        (f"峰值为额定的 {peak_current / spec.rated_current:.2f} 倍"
         f"（初版只有 0.72 倍）", peak_current / spec.rated_current >= 1.45),
        ("检测器产生了告警", len(hits) > 0),
        ("过载 / 漏电 / 欠压三类都被检出", {"过载", "漏电", "欠压"}.issubset(kinds)),
        (f"召回率 >= 60%（实际 {recall:.1%}）", recall >= 0.60),
        ("每条告警都有可读的中文理由", all(len(h[3]) > 10 for h in hits)),
    ]

    print("\n[校验] 关键断言")
    failed = 0
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed += 0 if ok else 1

    print(f"\n  [脱扣判断] {trip_reason}")

    print("\n" + "=" * 74)
    if failed == 0:
        print("冒烟测试通过 ✅ 物理错误已修正，可以进入阶段 2")
    else:
        print(f"冒烟测试有 {failed} 项失败 ❌")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
