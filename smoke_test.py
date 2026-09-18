"""本地冒烟测试：零第三方依赖验证项目核心逻辑。

不需要 PostgreSQL / Redis / MQTT / pydantic —— 只验证「设备模型 + 异常检测」主链路。
目的：在投入装环境之前，先确认代码逻辑真的能跑、检测真的能抓到故障。

运行（在 smart-power-ai 目录下）：
    python smoke_test.py
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from app.detector import AnomalyDetector, DetectParams
from simulator.device import BreakerDevice


def build_stream(minutes: int = 240):
    """生成逐分钟样本，并注入三段已知故障作为真值标注。"""
    rng = random.Random(20260916)
    start = datetime(2026, 9, 16, 8, 0)
    dev = BreakerDevice(1, rng)
    dev.profile = "commercial"          # 固定档位，避免随机性影响可复现性
    dev.leakage_baseline = 8.0
    dev.drift_per_hour = 0.05
    dev.next_offline_check = 1e18       # 冒烟测试里关掉随机离线，减少干扰

    rows, truth = [], []
    for minute in range(minutes):
        now = start + timedelta(minutes=minute)
        current = dev.base_current(now) + rng.gauss(0, 0.35)
        voltage = 220.0 + rng.gauss(0, 1.2)
        leakage = max(0.0, dev.leakage_baseline + rng.gauss(0, 1.5))
        temperature = 26.0 + current * 1.25 + rng.gauss(0, 0.7)

        fault = None
        if 60 <= minute < 66:           # 过载
            current += 18.0
            temperature += 14.0
            fault = "过载"
        elif 120 <= minute < 128:       # 漏电爬升
            leakage += 45.0
            fault = "漏电"
        elif 180 <= minute < 184:       # 电压骤降
            voltage -= 42.0
            fault = "欠压"

        rows.append(
            {
                "t": now,
                "current": max(0.0, current),
                "voltage": voltage,
                "leakage": leakage,
                "temperature": temperature,
            }
        )
        truth.append(fault is not None)
    return rows, truth


def main() -> int:
    print("=" * 70)
    print("智慧用电 AI 平台 · 本地冒烟测试（零依赖）")
    print("=" * 70)

    params = DetectParams()
    print(f"\n[配置] 窗口={params.detect_window}  MAD系数k={params.detect_mad_k}  "
          f"连续确认={params.detect_min_confirm}")
    print(f"       漏电红线={params.leakage_limit_ma}mA  "
          f"欠压线={params.voltage_min_v}V  过载物理下限={params.overload_floor_a}A")

    rows, truth = build_stream()
    print(f"\n[数据] {len(rows)} 个逐分钟样本（08:00 起），注入过载/漏电/欠压三段故障")

    detector = AnomalyDetector(params)
    pred, hits = [], []
    for row, is_fault in zip(rows, truth):
        detector.push(row["current"], row["voltage"], row["leakage"], row["temperature"])
        alerts = detector.detect()
        pred.append(len(alerts) > 0)
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

    print("\n[明细] 告警样本")
    if not hits:
        print("  == 没有任何告警，说明检测器没工作 ==")
    for t, kind, sev, reason in hits[:15]:
        print(f"  {t:%H:%M}  [{kind}] sev={sev}  {reason}")
    if len(hits) > 15:
        print(f"  ... 另有 {len(hits) - 15} 条")

    kinds = {h[1] for h in hits}
    checks = [
        ("检测器产生了告警", len(hits) > 0),
        ("过载/漏电/欠压三类都被检出", {"过载", "漏电", "欠压"}.issubset(kinds)),
        (f"召回率 >= 60%（实际 {recall:.1%}）", recall >= 0.60),
        (f"精确率 >= 60%（实际 {precision:.1%}）", precision >= 0.60),
        ("每条告警都有可读的中文理由", all(len(h[3]) > 10 for h in hits)),
        ("告警理由里带具体数值", any(any(c.isdigit() for c in h[3]) for h in hits)),
    ]

    print("\n[校验] 关键断言")
    failed = 0
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed += 0 if ok else 1

    print("\n" + "=" * 70)
    if failed == 0:
        print("冒烟测试通过 ✅ 核心逻辑可用，可以进入部署阶段")
    else:
        print(f"冒烟测试有 {failed} 项失败 ❌ 先修这些再部署")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
