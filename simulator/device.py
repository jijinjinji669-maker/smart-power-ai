"""断路器设备行为模型 —— 零第三方依赖。

核心设计：**故障一律按额定电流的倍数定义，不用绝对增量。**

为什么：初版用 `current += 16` 这种绝对增量，在 40 A 额定下只到 0.72 倍额定，
按 IEC 60898-1 连热保护门槛（1.13 倍）都不到 —— 那些"过载"在物理上根本不成立。

本模块的阈值全部从脱扣曲线推导，不手写：
  1.13 × In  热保护保持边界（1 小时内不应脱扣）
  1.45 × In  热保护动作边界（1 小时内必须脱扣）
  5    × In  C 曲线瞬时磁脱扣区间下界

断路器分档（贴合真实住宅配电）：
    总开关 63 A / 空调 25 A / 插座 20 A / 照明 16 A
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# IEC 60898-1 脱扣特性系数（标准值，不要改）
# ---------------------------------------------------------------------------
THERMAL_HOLD_FACTOR = 1.13   # 1 小时内不应脱扣
THERMAL_TRIP_FACTOR = 1.45   # 1 小时内必须脱扣

MAGNETIC_FACTOR = {          # 瞬时磁脱扣区间（下限, 上限）
    "B": (3.0, 5.0),
    "C": (5.0, 10.0),
    "D": (10.0, 20.0),
}

# 漏电阈值（GB/T 13955-2017：30 mA 直接接触保护）
LEAKAGE_HARDWARE_MA = 30.0
LEAKAGE_WARN_MA = 22.0       # 预警线：硬件动作前的 73%，留出处置时间


# ---------------------------------------------------------------------------
# 设备档位
# ---------------------------------------------------------------------------
TIER_LIGHTING = "lighting"
TIER_SOCKET = "socket"
TIER_AIRCON = "aircon"
TIER_MAIN = "main"


@dataclass(frozen=True)
class TierSpec:
    """一个设备档位的电气规格。"""

    key: str
    rated_current: float      # 额定电流 A
    curve: str                # 脱扣曲线类型
    label: str                # 中文说明

    # ---- 由额定值与曲线推导，不手写 ----
    @property
    def thermal_hold(self) -> float:
        """热保护保持边界：低于此值 1 小时内不应脱扣。"""
        return THERMAL_HOLD_FACTOR * self.rated_current

    @property
    def thermal_trip(self) -> float:
        """热保护动作边界：高于此值 1 小时内必须脱扣。"""
        return THERMAL_TRIP_FACTOR * self.rated_current

    @property
    def magnetic_min(self) -> float:
        """瞬时磁脱扣区间下界。"""
        lo, _ = MAGNETIC_FACTOR[self.curve]
        return lo * self.rated_current

    @property
    def magnetic_max(self) -> float:
        """瞬时磁脱扣区间上界。"""
        _, hi = MAGNETIC_FACTOR[self.curve]
        return hi * self.rated_current


TIERS: dict[str, TierSpec] = {
    TIER_LIGHTING: TierSpec(TIER_LIGHTING, 16.0, "C", "照明回路"),
    TIER_SOCKET: TierSpec(TIER_SOCKET, 20.0, "C", "插座回路"),
    TIER_AIRCON: TierSpec(TIER_AIRCON, 25.0, "C", "空调回路"),
    TIER_MAIN: TierSpec(TIER_MAIN, 63.0, "C", "配电箱总开关"),
}


@dataclass
class FaultProfile:
    """一个故障场景的形态定义。

    参数全部是**相对额定电流的倍数**，同一套场景可套用到任意档位。
    """

    key: str
    label: str
    fault_type: str                                # overload / leakage / voltage / inrush
    peak_multiple: tuple[float, float]             # 峰值倍数区间（相对 In）
    duration_seconds: tuple[float, float]          # 持续时长区间
    ramp: str = "stepped"                          # stepped / linear / sudden / exponential
    steps: int = 1                                 # 阶梯级数（ramp=stepped 时有效）
    cross_fraction: tuple[float, float] | None = None   # 越过阈值的时点（占事件时长比例）
    reversible_probability: float = 0.0            # 中途回落概率
    affects_panel: bool = False                    # 是否影响同配电箱其他设备
    is_normal: bool = False                        # True = 正常现象，不是故障
    description: str = ""


# ---------------------------------------------------------------------------
# 故障场景库：全部来自真实住宅配电场景
# ---------------------------------------------------------------------------
FAULT_PROFILES: dict[str, FaultProfile] = {
    # ---------------- 过载：负载累积 ----------------
    "multi_appliance": FaultProfile(
        key="multi_appliance",
        label="多台大功率同时开",
        fault_type="overload",
        peak_multiple=(1.5, 2.2),
        duration_seconds=(120.0, 480.0),
        ramp="stepped",
        steps=3,
        description="空调 + 电热水器 + 微波炉同时运行，负载逐台投入",
    ),
    "socket_overload": FaultProfile(
        key="socket_overload",
        label="插排串接大功率",
        fault_type="overload",
        peak_multiple=(1.2, 1.6),
        duration_seconds=(300.0, 1800.0),
        ramp="linear",
        description="一个插座带多台设备，缓慢累积到轻微过载",
    ),
    "motor_stall": FaultProfile(
        key="motor_stall",
        label="电机堵转",
        fault_type="overload",
        peak_multiple=(3.0, 5.0),
        duration_seconds=(5.0, 60.0),
        ramp="sudden",
        description="洗衣机或水泵被卡住，电流骤升到接近磁脱扣区",
    ),
    # ---------------- 漏电：绝缘劣化 ----------------
    "damp_creep": FaultProfile(
        key="damp_creep",
        label="线路受潮",
        fault_type="leakage",
        peak_multiple=(0.0, 0.0),              # 漏电用 mA 增量，不用电流倍数
        duration_seconds=(14400.0, 172800.0),  # 4-48 小时
        ramp="exponential",
        cross_fraction=(0.70, 0.90),
        reversible_probability=0.20,
        description="雨季或浴室线路受潮，漏电缓慢爬升到 30 mA 红线",
    ),
    "insulation_aging": FaultProfile(
        key="insulation_aging",
        label="电器绝缘老化",
        fault_type="leakage",
        peak_multiple=(0.0, 0.0),
        duration_seconds=(172800.0, 2592000.0),  # 2-30 天
        ramp="linear",
        cross_fraction=(0.80, 0.95),
        reversible_probability=0.10,
        description="老旧电热水器或洗衣机绝缘劣化，数月尺度缓慢上升",
    ),
    # ---------------- 电压 ----------------
    "sag_from_load": FaultProfile(
        key="sag_from_load",
        label="大功率启动导致电压跌落",
        fault_type="voltage",
        peak_multiple=(0.15, 0.25),            # 这里表示跌落比例，不是电流倍数
        duration_seconds=(30.0, 300.0),
        ramp="sudden",
        affects_panel=True,
        description="大功率设备启动，同配电箱内多路电压同时跌落",
    ),
    # ---------------- 正常现象（用来考验检测器）----------------
    "compressor_inrush": FaultProfile(
        key="compressor_inrush",
        label="空调或冰箱压缩机启动浪涌",
        fault_type="inrush",
        peak_multiple=(5.0, 7.0),
        duration_seconds=(0.2, 2.0),
        ramp="sudden",
        is_normal=True,
        description="压缩机启动瞬间 5-7 倍稳态电流，数百毫秒后回落。正常现象，但极易误报",
    ),
}


@dataclass
class FaultEvent:
    """一次正在生效的故障事件。"""

    fault_type: str
    profile: FaultProfile
    start_at: float                    # 墙钟开始时间
    duration: float
    peak_multiple: float
    source: str = "auto"               # auto / manual
    device_sn: str = ""

    def progress(self, now: float) -> float:
        """返回 0.0-1.0 的事件进度；已结束返回 1.0。"""
        if self.duration <= 0:
            return 1.0
        return min(1.0, max(0.0, (now - self.start_at) / self.duration))

    def is_active(self, now: float) -> bool:
        return self.start_at <= now < self.start_at + self.duration


@dataclass
class BreakerDevice:
    """单台智能断路器的行为模型。"""

    index: int
    rng: random.Random
    tier: TierSpec = field(default_factory=lambda: TIERS[TIER_SOCKET])

    def __post_init__(self) -> None:
        self.sn = f"AJS-BRK-2026-{self.index:04d}"
        self.name = f"{self.tier.label}-{self.index:02d}"
        rng = self.rng

        # 漏电基线按档位比例缩放（大档位线路更长、寄生电容更大）
        scale = self.tier.rated_current / 20.0
        self.leakage_baseline = rng.uniform(4.0, 9.0) * scale
        self.drift_per_hour = rng.uniform(0.01, 0.06) * scale

        self.switch_state = 1
        self.arc_flag = 0
        self.energy_total = rng.uniform(500, 3000)
        self.offline_until = 0.0
        self.next_offline_check = 0.0
        self._leak_extra = 0.0

        # 事件队列（阶段 2 接入自动调度，阶段 4 接入 MQTT 注入）
        self.events: list[FaultEvent] = []

    # ------------------------------------------------------------------
    # 负荷曲线
    # ------------------------------------------------------------------
    def base_current(self, now: datetime) -> float:
        """按档位与时段算稳态电流。

        形状取自真实住宅日负荷特征：早高峰、午后回落、晚高峰。
        返回值为**相对额定电流的比例**再乘额定值。
        """
        h = now.hour + now.minute / 60

        if self.tier.key == TIER_LIGHTING:
            # 照明：傍晚到夜间为主，白天很低
            ratio = 0.05 + 0.35 * math.exp(-((h - 20.0) ** 2) / 4.0)
        elif self.tier.key == TIER_AIRCON:
            # 空调：午后与晚间两个峰，夜间仍运行
            ratio = (
                0.10
                + 0.45 * math.exp(-((h - 14.0) ** 2) / 6.0)
                + 0.35 * math.exp(-((h - 21.5) ** 2) / 4.0)
            )
        elif self.tier.key == TIER_MAIN:
            # 总开关：各分路之和，整体双峰
            ratio = (
                0.15
                + 0.30 * math.exp(-((h - 7.5) ** 2) / 2.5)
                + 0.35 * math.exp(-((h - 19.5) ** 2) / 3.5)
            )
        else:
            # 插座：早高峰 + 晚高峰
            ratio = (
                0.08
                + 0.30 * math.exp(-((h - 7.5) ** 2) / 2.5)
                + 0.40 * math.exp(-((h - 19.5) ** 2) / 3.5)
            )

        # 叠加测量噪声后换算成安培
        return max(0.0, ratio + self.rng.gauss(0, 0.015)) * self.tier.rated_current

    # ------------------------------------------------------------------
    # 保护动作判断
    # ------------------------------------------------------------------
    def would_trip(self, current: float) -> tuple[bool, str]:
        """判断这个电流会不会让断路器跳闸，以及依据。"""
        spec = self.tier
        if current >= spec.magnetic_min:
            return True, f"{current:.1f}A 超过磁脱扣下界 {spec.magnetic_min:.1f}A"
        if current >= spec.thermal_trip:
            return True, f"{current:.1f}A 超过热脱扣边界 {spec.thermal_trip:.1f}A"
        if current >= spec.thermal_hold:
            return False, (
                f"{current:.1f}A 落在热保护区 "
                f"（{spec.thermal_hold:.1f}-{spec.thermal_trip:.1f}A），暂不脱扣"
            )
        return False, f"{current:.1f}A 低于保持边界 {spec.thermal_hold:.1f}A，属正常运行"

    # ------------------------------------------------------------------
    # 采样
    # ------------------------------------------------------------------
    def sample(self, t: float, now: datetime, drift_hours: float) -> dict | None:
        """产出一条上报数据；返回 None 表示此刻设备离线。"""
        # ---------- 离线缺口 ----------
        if t < self.offline_until:
            return None
        if t >= self.next_offline_check:
            self.next_offline_check = t + self.rng.uniform(1200, 7200)
            if self.rng.random() < 0.05:
                self.offline_until = t + self.rng.uniform(60, 1800)
                return None

        # ---------- 正常工况 ----------
        current = self.base_current(now)
        voltage = 220.0 + self.rng.gauss(0, 1.2)
        fault_label: str | None = None
        self._leak_extra = 0.0

        # ---------- 生效中的故障事件 ----------
        for ev in self.events:
            if not ev.is_active(t):
                continue
            p = ev.progress(t)
            prof = ev.profile

            if prof.fault_type == "overload":
                # ------------------------------------------------------------------
                # 关键：故障电流直接按额定电流的倍数给，**不是**乘在稳态负荷上。
                #
                # 原因：20 A 插座回路在 9:00 的稳态负荷只有约 3.5 A（0.18 倍额定）。
                # 若把倍数乘在稳态负荷上，2 倍也只有 7 A，离热脱扣边界 29 A 差很远 ——
                # 那样"过载"在物理上依然不成立。
                #
                # 真实场景是：用户新接入了大功率电器（空调、电热水器、微波炉），
                # 这部分负载本身就有十几到几十安，叠加在原有负荷之上。
                # 所以故障的"目标电流"应直接由额定电流倍数决定。
                # ------------------------------------------------------------------
                if prof.ramp == "stepped":
                    # 阶梯上升：每一级对应一台大功率电器投入
                    step = min(prof.steps, int(p * prof.steps) + 1)
                    ratio = ev.peak_multiple * step / prof.steps
                elif prof.ramp == "linear":
                    ratio = ev.peak_multiple * p
                else:                                   # sudden：堵转类，立即到位
                    ratio = ev.peak_multiple
                current = max(current, ratio * self.tier.rated_current)
                fault_label = prof.label

            elif prof.fault_type == "leakage":
                # 劣化曲线后段上翘，模拟绝缘加速击穿
                over = max(0.0, p - 0.5) * 2
                self._leak_extra = 40.0 * (p + 0.6 * over * over)
                fault_label = prof.label

            elif prof.fault_type == "voltage":
                # 此处 peak_multiple 表示跌落比例
                depth = ev.peak_multiple * min(1.0, p * 4)     # 快速跌落
                voltage -= 220.0 * depth
                fault_label = prof.label

            elif prof.fault_type == "inrush":
                # 正常现象：短时高电流，随即回落，不算故障
                current *= ev.peak_multiple
                fault_label = None

        # 清理已经结束的事件。
        # 注意：不能用 is_active() 过滤 —— 它对「还没开始」的事件也返回 False，
        # 那会把未来事件在开始之前就误删掉（踩过这个坑）。
        # 正确条件是「尚未结束」，这样待触发的事件会一直留在队列里。
        self.events = [e for e in self.events if t < e.start_at + e.duration]

        # ---------- 漏电（基线漂移 + 事件增量）----------
        leakage = max(
            0.0,
            self.leakage_baseline
            + self.drift_per_hour * drift_hours
            + self._leak_extra
            + self.rng.gauss(0, 1.2),
        )

        # ---------- 派生量（物理上自洽）----------
        current = max(0.0, current)
        spec = self.tier
        load_ratio = current / spec.rated_current
        power_factor = max(0.75, min(0.99, 0.97 - 0.06 * load_ratio))
        power = current * voltage * power_factor / 1000.0

        # 温度 = 环境温度（日周期）+ 焦耳热（∝ 电流平方）
        hour = now.hour + now.minute / 60
        ambient = 25.0 + 4.0 * math.cos((hour - 14.0) / 24.0 * 2 * math.pi)
        rise = 0.02 * (current ** 2) / max(spec.rated_current / 20.0, 1.0)
        temperature = ambient + rise + self.rng.gauss(0, 0.4)

        # ---------- 组装 ----------
        payload = {
            "device_sn": self.sn,
            "name": self.name,
            "tier": spec.key,
            "rated_current": spec.rated_current,
            "curve": spec.curve,
            "recorded_at": now.astimezone(timezone.utc).isoformat(),
            "gateway_ts": datetime.now(timezone.utc).isoformat(),
            "current": round(current, 3),
            "voltage": round(voltage, 3),
            "power": round(power, 3),
            "power_factor": round(power_factor, 3),
            "frequency": round(50.0 + self.rng.gauss(0, 0.05), 2),
            "leakage": round(leakage, 3),
            "temperature": round(temperature, 2),
            "switch_state": self.switch_state,
            "arc_flag": self.arc_flag,
            "energy_total": round(self.energy_total, 3),
            "firmware": "v1.4.2",
            "injected_fault": fault_label,
        }
        self.arc_flag = 0
        self.energy_total += power / 3600.0
        return payload
