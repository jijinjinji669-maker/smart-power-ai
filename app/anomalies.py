"""异常区间识别与故障签名匹配 —— 纯本地计算，不调用任何外部服务。

为什么需要它：
  看板现在只在告警时刻打一个 ✕ 点，看不出「这段异常从哪开始、到哪结束、
  持续多久、峰值多少」。而人要判断成因，靠的正是这些区间特征。

  更重要的是：把数值特征提取交给规则引擎（它擅长算），
  再把「成因判定」交给 LLM（它擅长解释）—— 而不是把原始曲线丢给 LLM 让它做除法。

设计原则：
  · 只做算术，不做推断。这里不判断"是不是过载"，只输出"电流在 09:02-09:06
    达到 2.0 倍额定，形态是阶梯上升"。
  · 阈值全部来自 IEC 60898-1 与 GB/T 13955-2017，与 detector.py 保持一致。
  · 零第三方依赖，可单独测试。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

# 与 detector / device 保持一致的阈值来源
THERMAL_HOLD_FACTOR = 1.13      # IEC 60898-1 热保护保持边界
THERMAL_TRIP_FACTOR = 1.45      # IEC 60898-1 热保护动作边界
LEAKAGE_HARDWARE_MA = 30.0      # GB/T 13955-2017 直接接触保护
TEMP_LIMIT_C = 55.0             # 设备过温告警线

# 电压偏差容忍度（额定 220V 的 ±10% 是国标供电电压允许范围）
VOLTAGE_NOMINAL = 220.0
VOLTAGE_TOLERANCE = 0.10


@dataclass
class SeriesPoint:
    """一个时间点上的一组电气量。缺测的量为 None。"""

    t: float                                  # 距窗口起点的秒数，便于计算
    ts: datetime | None = None                # 原始时间戳，用于展示
    current: float | None = None
    voltage: float | None = None
    leakage: float | None = None
    temperature: float | None = None
    switch_state: int | None = None


@dataclass
class AnomalyInterval:
    """一段连续异常。"""

    start: float                              # 距窗口起点秒数
    end: float
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    kinds: set[str] = field(default_factory=set)      # 过载 / 欠压 / 过压 / 漏电 / 过温
    peak_current: float | None = None
    peak_leakage: float | None = None
    peak_temperature: float | None = None
    min_voltage: float | None = None
    sample_count: int = 0
    # 区间开始前最后一个正常点。用来判断"是从低位跳上来的"还是"本来就在高位"
    baseline: SeriesPoint | None = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def kind_label(self) -> str:
        order = ["过载", "欠压", "过压", "漏电", "过温"]
        return " / ".join(k for k in order if k in self.kinds) or "未知"


@dataclass
class WindowSpec:
    """设备规格与阈值，用于把原始值换算成"相对额定的倍数"这样的可读特征。"""

    device_sn: str
    name: str = ""
    tier: str = ""
    rated_current: float = 20.0
    rated_voltage: float = VOLTAGE_NOMINAL
    leakage_limit_ma: float = LEAKAGE_HARDWARE_MA
    temp_limit_c: float = TEMP_LIMIT_C

    @property
    def thermal_hold(self) -> float:
        return THERMAL_HOLD_FACTOR * self.rated_current

    @property
    def thermal_trip(self) -> float:
        return THERMAL_TRIP_FACTOR * self.rated_current

    @property
    def voltage_min(self) -> float:
        return self.rated_voltage * (1 - VOLTAGE_TOLERANCE)

    @property
    def voltage_max(self) -> float:
        return self.rated_voltage * (1 + VOLTAGE_TOLERANCE)


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------
def classify_point(point: SeriesPoint, spec: WindowSpec) -> set[str]:
    """判断单个时间点属于哪几类异常。返回空集合表示正常。

    注意：这里的阈值判定留了 1% 的余量，避免「刚好等于阈值」这种边界值
    被算作异常 —— 例如漏电正好 30.0 mA 时，物理上应是「到达阈值」而非「超过」。
    """
    kinds: set[str] = set()

    if point.current is not None and point.current > spec.thermal_hold * 1.01:
        kinds.add("过载")

    if point.voltage is not None:
        if point.voltage < spec.voltage_min * 0.999:
            kinds.add("欠压")
        elif point.voltage > spec.voltage_max * 1.001:
            kinds.add("过压")

    if point.leakage is not None and point.leakage > spec.leakage_limit_ma * 1.01:
        kinds.add("漏电")

    if point.temperature is not None and point.temperature > spec.temp_limit_c * 1.01:
        kinds.add("过温")

    return kinds


def find_intervals(
    points: Iterable[SeriesPoint],
    spec: WindowSpec,
    merge_gap_seconds: float = 0.0,
) -> list[AnomalyInterval]:
    """把逐点分类结果合并成连续异常区间。

    merge_gap_seconds > 0 时，间隔小于该值的两段异常会被合并 ——
    真实数据常有单点抖动，不合并会把一次异常切成很多碎片。
    """
    intervals: list[AnomalyInterval] = []
    current: AnomalyInterval | None = None
    last_normal: SeriesPoint | None = None
    pending_gap_normal: SeriesPoint | None = None

    for point in points:
        kinds = classify_point(point, spec)

        if not kinds:
            if current is not None:
                if (
                    merge_gap_seconds > 0
                    and point.t - current.end <= merge_gap_seconds
                ):
                    # 间隙足够小，视为同一段异常，先不断开；
                    # 记下这个正常点，若之后不再续上，它就是下一段的基线
                    pending_gap_normal = point
                    continue
                intervals.append(current)
                current = None
                pending_gap_normal = None
            last_normal = point
            continue

        if current is None:
            # 新一段异常：基线取「上一个正常点」
            current = AnomalyInterval(
                start=point.t, end=point.t,
                start_ts=point.ts, end_ts=point.ts,
                baseline=last_normal,
            )

        current.end = point.t
        current.end_ts = point.ts
        current.kinds |= kinds
        current.sample_count += 1
        pending_gap_normal = None

        if point.current is not None:
            current.peak_current = max(current.peak_current or 0.0, point.current)
        if point.leakage is not None:
            current.peak_leakage = max(current.peak_leakage or 0.0, point.leakage)
        if point.temperature is not None:
            current.peak_temperature = max(
                current.peak_temperature or 0.0, point.temperature
            )
        if point.voltage is not None:
            current.min_voltage = min(
                current.min_voltage if current.min_voltage is not None else point.voltage,
                point.voltage,
            )

    if current is not None:
        intervals.append(current)
    _ = pending_gap_normal

    return intervals


# ---------------------------------------------------------------------------
# 形态特征提取
# ---------------------------------------------------------------------------
def segment_shape(values: list[float], baseline: float | None = None) -> str:
    """判断一段数值的形态。

    baseline 是「区间开始之前的值」。有它才能区分两种看起来一样的情况：
      · 突变：从低位一步跳到高位并维持（例如电机堵转）
      · 高位平台：本来就在高位，没有跳变（例如持续满载）

    没有 baseline 时只能说「高位」，不能说「突变」。
    """
    if len(values) < 3:
        return "样本过少"

    first, last = values[0], values[-1]
    peak = max(values)
    span = last - first
    flat = peak > 0 and (peak - min(values)) <= peak * 0.05

    if flat:
        # 整段基本持平：只有知道前值才能判断是不是跳上来的
        if baseline is not None and baseline > 0 and first >= baseline * 1.5:
            return "突变跳升（起点即高位）"
        return "高位平台（无明显趋势）"

    # 起点就在峰值附近且随后回落 → 尖峰型
    if peak > 0 and first >= peak * 0.9 and last <= peak * 0.9:
        return "尖峰后回落"

    if span <= 0:
        return "波动平台（无明显趋势）"

    # 数一下有几个明显的台阶
    steps = 0
    for i in range(1, len(values)):
        if values[i] - values[i - 1] > span * 0.25:
            steps += 1

    if steps >= 2:
        return f"阶梯上升（{steps + 1} 级）"
    return "线性上升"


def describe_interval(
    interval: AnomalyInterval,
    points: list[SeriesPoint],
    spec: WindowSpec,
) -> dict[str, Any]:
    """把一个异常区间翻译成结构化特征描述。这是要交给 LLM 的输入。

    重要：**只报告真正异常的量**。如果不加区分地把电流、电压、漏电、温度
    都列进去，LLM 会看到一堆正常值被当作"异常证据"，从而给出错误结论。
    """
    seg = [p for p in points if interval.start <= p.t <= interval.end]
    base_point = interval.baseline

    def series(attr: str) -> list[float]:
        return [getattr(p, attr) for p in seg if getattr(p, attr) is not None]

    features: dict[str, Any] = {
        "kinds": sorted(interval.kinds),
        "kind_label": interval.kind_label,
        "duration_seconds": round(interval.duration_seconds, 1),
        "duration_text": _human_duration(interval.duration_seconds),
        "sample_count": interval.sample_count,
    }

    if interval.start_ts:
        features["start_time"] = interval.start_ts.strftime("%Y-%m-%d %H:%M:%S")
    if interval.end_ts:
        features["end_time"] = interval.end_ts.strftime("%Y-%m-%d %H:%M:%S")

    # ---------------- 电流 ----------------
    if "过载" in interval.kinds:
        currents = series("current")
        if currents:
            base_current = base_point.current if base_point else None
            features["current"] = {
                "peak": round(max(currents), 2),
                "min": round(min(currents), 2),
                "median": round(statistics.median(currents), 2),
                "peak_multiple_of_rated": round(max(currents) / spec.rated_current, 2),
                "rated_current": spec.rated_current,
                "baseline_before_event": (
                    round(base_current, 2) if base_current is not None else None
                ),
                "thermal_hold_a": round(spec.thermal_hold, 1),
                "thermal_trip_a": round(spec.thermal_trip, 1),
                "shape": segment_shape(currents, baseline=base_current),
                "exceeds_thermal_trip": max(currents) >= spec.thermal_trip,
            }

    # ---------------- 电压 ----------------
    if interval.kinds & {"欠压", "过压"}:
        voltages = series("voltage")
        if voltages:
            base_voltage = base_point.voltage if base_point else None
            features["voltage"] = {
                "min": round(min(voltages), 2),
                "max": round(max(voltages), 2),
                "median": round(statistics.median(voltages), 2),
                "nominal": spec.rated_voltage,
                "baseline_before_event": (
                    round(base_voltage, 2) if base_voltage is not None else None
                ),
                "min_deviation_percent": round(
                    (min(voltages) - spec.rated_voltage) / spec.rated_voltage * 100, 1
                ),
                "allowed_min": round(spec.voltage_min, 1),
                "allowed_max": round(spec.voltage_max, 1),
                "shape": segment_shape(voltages, baseline=base_voltage),
            }

    # ---------------- 漏电 ----------------
    if "漏电" in interval.kinds:
        leakages = series("leakage")
        if leakages:
            base_leakage = base_point.leakage if base_point else None
            features["leakage"] = {
                "peak_ma": round(max(leakages), 2),
                "min_ma": round(min(leakages), 2),
                "median_ma": round(statistics.median(leakages), 2),
                "baseline_before_event_ma": (
                    round(base_leakage, 2) if base_leakage is not None else None
                ),
                "hardware_limit_ma": spec.leakage_limit_ma,
                "rise_ma": round(
                    max(leakages) - (base_leakage if base_leakage is not None else min(leakages)),
                    2,
                ),
                "shape": segment_shape(leakages, baseline=base_leakage),
                "exceeds_limit": max(leakages) > spec.leakage_limit_ma * 1.01,
            }

    # ---------------- 温度 ----------------
    if "过温" in interval.kinds:
        temps = series("temperature")
        if temps:
            base_temp = base_point.temperature if base_point else None
            features["temperature"] = {
                "peak_c": round(max(temps), 2),
                "median_c": round(statistics.median(temps), 2),
                "baseline_before_event_c": (
                    round(base_temp, 2) if base_temp is not None else None
                ),
                "rise_c": round(
                    max(temps) - (base_temp if base_temp is not None else temps[0]), 2
                ),
                "limit_c": spec.temp_limit_c,
                "shape": segment_shape(temps, baseline=base_temp),
            }

    # 同段里其余电气量确实正常时，明确写出来，避免 LLM 误以为没测
    normal_note = []
    if "过载" not in interval.kinds and any(p.current is not None for p in seg):
        normal_note.append("电流")
    if not (interval.kinds & {"欠压", "过压"}) and any(p.voltage is not None for p in seg):
        normal_note.append("电压")
    if "漏电" not in interval.kinds and any(p.leakage is not None for p in seg):
        normal_note.append("漏电")
    if "过温" not in interval.kinds and any(p.temperature is not None for p in seg):
        normal_note.append("温度")
    if normal_note:
        features["within_normal_range"] = normal_note

    return features


def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.1f} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


# ---------------------------------------------------------------------------
# 故障签名匹配（封闭集合，防止 LLM 自由发挥）
# ---------------------------------------------------------------------------
# 每个签名描述「哪几个量一起变化」，LLM 只能从这个集合里挑，并给出依据。
FAULT_SIGNATURES: list[dict[str, Any]] = [
    {
        "key": "multi_appliance",
        "label": "多台大功率电器同时运行",
        "indicators": "电流阶梯上升至 1.5~2.2 倍额定；电压轻微下降；温度随电流平方上升",
        "evidence_fields": ["current", "temperature", "voltage"],
    },
    {
        "key": "socket_overload",
        "label": "插座串接大功率负载",
        "indicators": "电流线性缓慢上升到 1.2~1.6 倍额定；温度缓慢上升；持续时间较长",
        "evidence_fields": ["current", "temperature"],
    },
    {
        "key": "motor_stall",
        "label": "电机堵转（洗衣机/水泵被卡）",
        "indicators": "电流突然跃升到 3~5 倍额定，几乎无爬升过程；持续到保护动作",
        "evidence_fields": ["current"],
    },
    {
        "key": "damp_creep",
        "label": "线路受潮导致漏电爬升",
        "indicators": "漏电指数型爬升；电流与电压基本不变；事件时长远大于其他类型",
        "evidence_fields": ["leakage"],
    },
    {
        "key": "insulation_aging",
        "label": "电器绝缘老化",
        "indicators": "漏电线性缓慢上升；变化率极小；时间跨度达数天",
        "evidence_fields": ["leakage"],
    },
    {
        "key": "sag_from_load",
        "label": "大功率设备启动导致电压跌落",
        "indicators": "电压快速跌落 15%~25%；电流可能上升；多台同配电箱设备同时出现",
        "evidence_fields": ["voltage"],
    },
    {
        "key": "compressor_inrush",
        "label": "压缩机启动浪涌（正常现象）",
        "indicators": "电流瞬间达到 5~7 倍稳态后迅速回落；持续仅数百毫秒到 2 秒；不伴随温度上升",
        "evidence_fields": ["current"],
    },
    {
        "key": "unknown",
        "label": "无法匹配已知签名",
        "indicators": "特征组合不符合任何已知模式，需要更多数据或人工排查",
        "evidence_fields": [],
    },
]


def _metric_is_significant(features: dict[str, Any], metric: str) -> bool:
    """判断某个电气量在这段异常里是否**显著**异常。

    为什么要这个：只看"kinds 里有没有过载"是不够的。
    一段 30 秒的轻微越线，和一段 5 分钟的 2 倍过载，是完全不同的两件事，
    用同一个结论去回答会误导 LLM。
    """
    if metric == "current":
        c = features.get("current") or {}
        peak = c.get("peak_multiple_of_rated") or 0.0
        duration = features.get("duration_seconds", 0.0)
        # 要么倍数够高，要么持续时间够长（热积累需要时间）
        return peak >= 1.5 or (peak >= 1.13 and duration >= 60.0)

    if metric == "leakage":
        lk = features.get("leakage") or {}
        if not lk.get("exceeds_limit"):
            return False
        base = lk.get("baseline_before_event_ma")
        peak = lk.get("peak_ma") or 0.0
        # 相对基线明显抬升才算（避免刚好越线一点点就被当成强证据）
        if base is None:
            return True
        return (peak - base) >= 5.0 or peak >= (lk.get("hardware_limit_ma") or 30.0) * 1.2

    if metric == "voltage":
        v = features.get("voltage") or {}
        deviation = abs(v.get("min_deviation_percent") or 0.0)
        return deviation >= 12.0

    if metric == "temperature":
        t = features.get("temperature") or {}
        return (t.get("rise_c") or 0.0) >= 5.0

    return False


def match_signature(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """按「显著异常的电气量组合 + 形态」把特征匹配到签名。

    保守匹配：只根据「哪些量显著异常」和「电流形态」判定，
    不做概率推断。目的是给 LLM 一个候选集合，而不是直接下结论。
    """
    if not candidates:
        return _signature("unknown")

    kinds: set[str] = set()
    for c in candidates:
        kinds |= set(c.get("kinds", []))

    sig_current = any(_metric_is_significant(c, "current") for c in candidates)
    sig_leakage = any(_metric_is_significant(c, "leakage") for c in candidates)
    sig_voltage = any(_metric_is_significant(c, "voltage") for c in candidates)
    has_temp = "过温" in kinds

    current_shapes = " ".join(
        (c.get("current") or {}).get("shape", "") for c in candidates
    )
    duration = sum(c.get("duration_seconds", 0.0) for c in candidates)

    # 漏电类：只有漏电显著、其他量正常
    if sig_leakage and not sig_current and not sig_voltage:
        # 用时长区分受潮与老化：前者以小时计，后者以天计
        if duration >= 86400 * 0.8:
            return _signature("insulation_aging")
        return _signature("damp_creep")

    # 电压类：电压显著异常且电流没有显著过载
    if sig_voltage and not sig_current:
        return _signature("sag_from_load")

    # 电流类：用形态区分
    if sig_current:
        peak_multiple = max(
            (c.get("current") or {}).get("peak_multiple_of_rated", 0.0)
            for c in candidates
        )
        if "突变跳升" in current_shapes or "尖峰后回落" in current_shapes:
            if peak_multiple >= 3.0:
                return _signature("motor_stall")
            if duration <= 5.0:
                return _signature("compressor_inrush")
        if "阶梯" in current_shapes:
            return _signature("multi_appliance")
        if "线性" in current_shapes:
            return _signature("socket_overload")
        # 高位平台或缺形态信息：按持续时间在两者间取较长者
        if duration >= 300:
            return _signature("multi_appliance")
        return _signature("socket_overload")

    if has_temp:
        # 过温通常由过载派生，单独的过温说明散热有问题
        return _signature("multi_appliance")

    return _signature("unknown")


def _signature(key: str) -> dict[str, Any]:
    for sig in FAULT_SIGNATURES:
        if sig["key"] == key:
            return dict(sig)
    return dict(FAULT_SIGNATURES[-1])
