"""P0 修复的针对性单测：额定电流贯通 + 告警去重/恢复（零依赖）。

运行（在 smart-power-ai 目录下）：
    python test_p0_fixes.py

注意：基线样本带一点确定性抖动，否则 MAD 恒为 0 会被检测器跳过。
"""
from __future__ import annotations

import sys

sys.path.insert(0, '.')

from app.alerttrack import plan_transitions
from app.detector import AnomalyDetector, DetectParams


def _baseline(i: int) -> float:
    """带确定性抖动的基线电流，避免 MAD 恒 0。"""
    return 2.0 + 0.3 * (i % 5)


def test_rated_current_threshold() -> list[str]:
    """P0-1：过载阈值必须随设备额定电流变化，而不是全局 20A。"""
    fails: list[str] = []

    def feed(detector, fault_a: float):
        for i in range(40):
            detector.push(_baseline(i), 220.0, 5.0, 25.0)
        for _ in range(5):
            detector.push(fault_a, 220.0, 5.0, 25.0)

    # 16A 照明回路：过载起点 18.08A，20A 必须触发过载
    d16 = AnomalyDetector(DetectParams().with_rated_current(16.0))
    feed(d16, 20.0)
    kinds16 = {a.alert_type for a in d16.detect()}
    if '过载' not in kinds16:
        fails.append(f'16A 回路在 20A 下未检出过载（实际 {kinds16}）')

    # 63A 总开关：过载起点 71.19A，20A 属正常，不应判过载
    d63 = AnomalyDetector(DetectParams().with_rated_current(63.0))
    feed(d63, 20.0)
    kinds63 = {a.alert_type for a in d63.detect()}
    if '过载' in kinds63:
        fails.append(f'63A 总开关在 20A 下误判过载（实际 {kinds63}）')

    return fails


def test_plan_transitions() -> list[str]:
    """P0-2：告警去重 + 恢复的纯状态迁移逻辑。"""
    fails: list[str] = []
    cases = [
        (set(), {'过载'}, True, ['过载'], []),
        ({'过载'}, {'过载'}, True, [], []),
        ({'过载'}, set(), True, [], ['过载']),
        ({'过载'}, set(), False, [], []),
        ({'过载'}, {'漏电'}, True, ['漏电'], ['过载']),
    ]
    for open_types, current, warm, exp_open, exp_resolve in cases:
        to_open, to_resolve = plan_transitions(open_types, current, warm)
        if sorted(to_open) != sorted(exp_open) or sorted(to_resolve) != sorted(exp_resolve):
            fails.append(
                f'plan_transitions({open_types}, {current}, warm={warm}) = '
                f'({to_open}, {to_resolve})，期望 ({exp_open}, {exp_resolve})'
            )
    return fails


def test_lifecycle_end_to_end() -> list[str]:
    """P0-2 端到端：一段持续故障只开一条告警，故障结束即恢复。"""
    fails: list[str] = []

    detector = AnomalyDetector(DetectParams().with_rated_current(20.0))
    open_types: set[str] = set()
    opened = resolved = 0

    def apply(current: float):
        nonlocal opened, resolved, open_types
        detector.push(current, 220.0, 5.0, 25.0)
        cur = {a.alert_type for a in detector.detect()}
        to_open, to_resolve = plan_transitions(open_types, cur, detector.warm)
        opened += len(to_open)
        resolved += len(to_resolve)
        open_types = (open_types | set(to_open)) - set(to_resolve)

    for i in range(40):            # 基线，让检测器热起来
        apply(_baseline(i))
    for _ in range(10):            # 持续过载 30A（> 1.13×20=22.6A）
        apply(30.0)
    if opened != 1:
        fails.append(f'持续故障应只开 1 条告警，实际开了 {opened} 条')
    if open_types != {'过载'}:
        fails.append(f'故障期间应有 1 条未恢复的过载，实际 {open_types}')

    for i in range(10):            # 回到基线，应恢复
        apply(_baseline(i))
    if resolved != 1:
        fails.append(f'故障恢复后应解析 1 条告警，实际 {resolved} 条')
    if open_types:
        fails.append(f'恢复后不应再有未恢复告警，实际 {open_types}')

    return fails


def main() -> int:
    print('=' * 66)
    print('P0 修复 · 针对性单测（额定电流贯通 + 告警去重/恢复）')
    print('=' * 66)

    suites = [
        ('额定电流贯通（16A 判过载 / 63A 不判）', test_rated_current_threshold),
        ('告警去重/恢复纯逻辑 plan_transitions', test_plan_transitions),
        ('端到端生命周期（持续故障只开一条 + 恢复）', test_lifecycle_end_to_end),
    ]

    total_fails = 0
    for name, fn in suites:
        fails = fn()
        status = 'PASS' if not fails else 'FAIL'
        print(f'\n[{status}] {name}')
        for f in fails:
            print(f'    [x] {f}')
        total_fails += len(fails)

    print('\n' + '=' * 66)
    print('ALL PASS' if total_fails == 0 else f'{total_fails} FAILED')
    print('=' * 66)
    return 1 if total_fails else 0


if __name__ == '__main__':
    raise SystemExit(main())
