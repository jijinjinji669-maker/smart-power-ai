"""异常检测：不依赖任何 ML 框架，只用标准库 statistics。

设计取舍（面试必答）：
  · 用 MAD 而非 3-sigma —— 均值与标准差会被异常点自身拉偏，等于给尖峰"打掩护"；
    中位数与 MAD 由数据主体决定，单个尖峰改不动它们。
  · 统计判断必须同时越过物理线 —— 电气安全有硬阈值，数学模型不能违背物理。
  · 连续 N 点确认 —— 负荷启停瞬间本来就会抖，孤立单点不报警。
  · 漂移检测加基线守卫 —— 否则故障结束后 slow-EWMA 追平期间会持续报警，拖出长尾。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from typing import Any

MAD_TO_SIGMA = 1.4826  # 使 MAD 在正态分布下与标准差可比


@dataclass(slots=True)
class Alert:
    alert_type: str
    severity: int          # 1 提示 / 2 警告 / 3 紧急
    value: float
    threshold: float
    reason: str


@dataclass(frozen=True)
class DetectParams:
    """检测器需要的全部参数。

    刻意做成不依赖 pydantic 的纯 dataclass：
    检测是纯数学逻辑，不应该因为「没装配置框架」就跑不起来，
    也方便单元测试里直接构造参数，不必先搭起整个 Settings。

    关于阈值分两类，不要混：
      · **绝对安全阈值**：漏电 30 mA、温度 55 ℃。这些是人身与设备安全线，
        与设备容量无关，任何档位都一样。
      · **相对电气阈值**：过载边界必须由该设备的额定电流推导。
        用一个绝对安培值当门槛是错的 —— 照明回路 18 A 已经过载，
        而 63 A 总开关 25 A 完全正常，同一个数字不可能对两者都对。
    """

    detect_window: int = 60
    detect_mad_k: float = 4.0
    detect_min_confirm: int = 2
    # 绝对安全阈值
    leakage_limit_ma: float = 30.0
    voltage_min_v: float = 198.0
    temp_limit_c: float = 55.0
    # 相对电气阈值：由设备额定电流推导，不再使用固定安培值
    rated_current_a: float = 20.0
    # 过载判定起点：热保护保持边界 1.13 × In（IEC 60898-1）。
    # 低于此值 1 小时内本就不应脱扣，因此连预警都不该报。
    thermal_hold_factor: float = 1.13

    @property
    def overload_floor_a(self) -> float:
        """过载预警起点 = 1.13 × 额定电流。"""
        return self.thermal_hold_factor * self.rated_current_a

    def with_rated_current(self, rated_current: float) -> "DetectParams":
        """按设备额定电流派生一份参数，其余字段不变。"""
        return replace(self, rated_current_a=rated_current)

    @classmethod
    def from_settings(cls, settings: Any) -> "DetectParams":
        """从应用配置（pydantic Settings）构造，保持两者字段名一致。"""
        return cls(
            detect_window=settings.detect_window,
            detect_mad_k=settings.detect_mad_k,
            detect_min_confirm=settings.detect_min_confirm,
            leakage_limit_ma=settings.leakage_limit_ma,
            voltage_min_v=settings.voltage_min_v,
            temp_limit_c=settings.temp_limit_c,
            rated_current_a=getattr(settings, "rated_current_a", 20.0),
            thermal_hold_factor=getattr(settings, "thermal_hold_factor", 1.13),
        )


def _rolling_median(xs: list[float], end: int, window: int) -> float:
    lo = max(0, end - window + 1)
    return statistics.median(xs[lo:end + 1])


def _mad_flags(xs: list[float], window: int, k: float) -> list[bool]:
    """滑窗 MAD 异常标记。

    先把每个位置的滚动中位数预计算一遍（O(n·window)），
    再复用这份缓存计算偏差中位数。否则每个位置都要重算整个窗口的中位数，
    复杂度会劣化到 O(n·window²) —— 对每分钟一次的检测影响不大，
    但设备规模上去后是实打实的性能瓶颈。
    """
    n = len(xs)
    medians = [_rolling_median(xs, i, window) for i in range(n)]

    flags = [False] * n
    for i in range(n):
        lo = max(0, i - window + 1)
        devs = [abs(xs[j] - medians[j]) for j in range(lo, i + 1)]
        mad = statistics.median(devs) if devs else 0.0
        if mad <= 1e-9:
            continue
        flags[i] = abs(xs[i] - medians[i]) > k * MAD_TO_SIGMA * mad
    return flags


def _confirm(flags: list[bool], need: int) -> list[bool]:
    out = [False] * len(flags)
    for i in range(len(flags)):
        lo = max(0, i - need + 1)
        if all(flags[j] for j in range(lo, i + 1)):
            out[i] = True
    return out


def _ewma(xs: list[float], span: int) -> list[float]:
    alpha = 2 / (span + 1)
    out: list[float] = []
    prev = xs[0]
    for x in xs:
        prev = alpha * x + (1 - alpha) * prev
        out.append(prev)
    return out


class AnomalyDetector:
    """滑动窗口检测器。每个设备一个实例，窗口内保存最近 N 个采样。"""

    def __init__(self, params: DetectParams | Any) -> None:
        # 兼容两种入参：DetectParams，或任何字段名一致的配置对象（如 Settings）
        self.s = params if isinstance(params, DetectParams) else DetectParams.from_settings(params)
        self.current: list[float] = []
        self.voltage: list[float] = []
        self.leakage: list[float] = []
        self.temperature: list[float] = []

    def push(self, current: float, voltage: float, leakage: float, temperature: float) -> None:
        cap = self.s.detect_window
        for buf, val in (
            (self.current, current),
            (self.voltage, voltage),
            (self.leakage, leakage),
            (self.temperature, temperature),
        ):
            buf.append(val)
            if len(buf) > cap:
                del buf[0]

    def detect(self) -> list[Alert]:
        s = self.s
        alerts: list[Alert] = []
        # 冷启动：窗口太短时统计量不可靠，只做物理阈值判断
        if len(self.current) < max(10, s.detect_window // 4):
            return self._physical_only()

        cur, vol, lek, tmp = self.current, self.voltage, self.leakage, self.temperature
        latest_cur, latest_vol, latest_lek, latest_tmp = (
            cur[-1], vol[-1], lek[-1], tmp[-1],
        )

        # ---- 过载：统计异常 AND 超过该设备的过载起点 ----
        if (
            _confirm(_mad_flags(cur, s.detect_window, s.detect_mad_k), s.detect_min_confirm)[-1]
            and latest_cur > s.overload_floor_a
        ):
            med = _rolling_median(cur, len(cur) - 1, s.detect_window)
            alerts.append(
                Alert(
                    "过载", 3, latest_cur, s.overload_floor_a,
                    f"电流 {latest_cur:.1f}A 偏离窗口中位数 {med:.1f}A 超过 "
                    f"{s.detect_mad_k} 倍 MAD，且高于该回路 {s.rated_current_a:.0f}A 额定的 "
                    f"{s.thermal_hold_factor} 倍（{s.overload_floor_a:.1f}A）",
                )
            )

        # ---- 欠压 ----
        if (
            _confirm(_mad_flags(vol, s.detect_window, s.detect_mad_k), s.detect_min_confirm)[-1]
            and latest_vol < s.voltage_min_v
        ):
            alerts.append(
                Alert("欠压", 3, latest_vol, s.voltage_min_v,
                      f"电压 {latest_vol:.1f}V 低于 {s.voltage_min_v}V 且偏离自身基线")
            )

        # ---- 漏电：物理红线 或 漂移趋势 ----
        if latest_lek > s.leakage_limit_ma:
            alerts.append(
                Alert("漏电", 3, latest_lek, s.leakage_limit_ma,
                      f"漏电 {latest_lek:.1f}mA 超过人身安全线 {s.leakage_limit_ma}mA")
            )
        else:
            fast = _ewma(lek, 15)
            slow = _ewma(lek, 90)
            drifting = _confirm(
                [(f - sl) > 2.0 for f, sl in zip(fast, slow)], s.detect_min_confirm
            )[-1]
            # 基线守卫：漂移信号必须同时说明"当前确实高于自身历史中位数"
            baseline = _rolling_median(lek, len(lek) - 1, 30)
            if drifting and latest_lek > baseline + 4.0:
                alerts.append(
                    Alert("漏电", 2, latest_lek, baseline + 4.0,
                          f"漏电快速均值高于慢速均值且高于自身基线 "
                          f"{baseline:.1f}mA，疑似渐变漏电")
                )

        # ---- 过温 ----
        if latest_tmp > s.temp_limit_c:
            alerts.append(
                Alert("过温", 2, latest_tmp, s.temp_limit_c,
                      f"温度 {latest_tmp:.1f}℃ 超过 {s.temp_limit_c}℃")
            )

        return alerts

    def _physical_only(self) -> list[Alert]:
        s = self.s
        alerts: list[Alert] = []
        if self.leakage and self.leakage[-1] > s.leakage_limit_ma:
            alerts.append(Alert("漏电", 3, self.leakage[-1], s.leakage_limit_ma,
                                "冷启动期漏电超过安全线"))
        if self.temperature and self.temperature[-1] > s.temp_limit_c:
            alerts.append(Alert("过温", 2, self.temperature[-1], s.temp_limit_c,
                                "冷启动期温度超限"))
        return alerts
