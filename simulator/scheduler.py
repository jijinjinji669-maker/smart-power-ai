"""故障事件调度器 —— 用泊松过程按真实故障率生成事件。

为什么要换掉「每次采样掷骰子」：

  旧做法是每次采样以固定概率触发一个单点故障。这带来两个问题：
    1. 故障率无法与真实世界对齐 —— 旧参数折算下来是每台设备 25 分钟一次，
       而真实住宅断路器的年故障率是「每年数次」，差了约 25000 倍
    2. 故障之间彼此独立、均匀散布，而真实故障是稀少的独立事件，
       事件之间有很长的平静期

  本模块改为：为每台设备维护「下一次事件的发生时刻」，按指数分布采样间隔
  （等价于泊松过程），到点就创建一个 FaultEvent 放进设备的事件队列。

三档故障率，用「每天期望事件数 λ」统一表达，切换只改 λ：

    realistic  对齐真实年故障率      —— 用于验证误报率
    demo       演示用                —— 几分钟内能看到告警
    off        零自动故障            —— 纯手动注入（阶段 4 提供）

参考：断路器脱扣特性见 simulator/device.py 中引用的 IEC 60898-1 阈值。
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from simulator.device import FAULT_PROFILES, BreakerDevice, FaultEvent, FaultProfile

SECONDS_PER_DAY = 86400.0

# ---------------------------------------------------------------------------
# 故障率档位：每天每台设备的期望事件数 λ
# ---------------------------------------------------------------------------
FAULT_LEVELS: dict[str, dict[str, float]] = {
    # 对齐真实年故障率：
    #   过载 2 次/年 → 2/365 ≈ 0.0055 次/天
    #   欠压 0.5 次/年 → 0.0014 次/天
    #   漏电 0.3 次/年 → 0.0008 次/天（事件少，但每个事件持续数小时到数天）
    "realistic": {
        "multi_appliance": 0.0035,
        "socket_overload": 0.0015,
        "motor_stall": 0.0005,
        "damp_creep": 0.0005,
        "insulation_aging": 0.0003,
        "sag_from_load": 0.0014,
        "compressor_inrush": 4.0,      # 正常现象，本来就很频繁（每天数次）
    },
    # 演示档：几分钟到几十分钟能看到一次告警
    "demo": {
        "multi_appliance": 0.35,
        "socket_overload": 0.20,
        "motor_stall": 0.10,
        "damp_creep": 0.20,
        "insulation_aging": 0.10,
        "sag_from_load": 0.25,
        "compressor_inrush": 6.0,
    },
    # 关闭自动故障，全部依赖手动注入（阶段 4）
    "off": {k: 0.0 for k in FAULT_PROFILES},
}


@dataclass
class SchedulerStats:
    """调度统计，用于运行时观察故障率是否符合预期。"""

    scheduled: int = 0
    started: int = 0
    by_profile: dict[str, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def record_scheduled(self, profile_key: str) -> None:
        self.scheduled += 1

    def record_started(self, profile_key: str) -> None:
        self.started += 1
        self.by_profile[profile_key] = self.by_profile.get(profile_key, 0) + 1

    def summary(self, device_count: int) -> str:
        elapsed_h = max((time.time() - self.started_at) / 3600.0, 1e-6)
        per_device_day = self.started / max(device_count, 1) / (elapsed_h / 24.0)
        top = sorted(self.by_profile.items(), key=lambda kv: -kv[1])[:3]
        parts = "，".join(f"{FAULT_PROFILES[k].label} {v}" for k, v in top)
        return (
            f"已调度 {self.scheduled} / 已触发 {self.started}；"
            f"折算每台每天 {per_device_day:.3f} 次"
            + (f"；主要类型：{parts}" if parts else "")
        )


class FaultScheduler:
    """为每台设备按泊松过程调度故障事件。"""

    def __init__(
        self,
        devices: list[BreakerDevice],
        level: str = "demo",
        rng: random.Random | None = None,
    ) -> None:
        if level not in FAULT_LEVELS:
            raise ValueError(f"未知故障率档位 {level!r}，可选：{', '.join(FAULT_LEVELS)}")

        self.devices = devices
        self.level = level
        self.rng = rng or random.Random(20260916)
        self.rates = FAULT_LEVELS[level]
        self.stats = SchedulerStats()

        # 每台设备的下一次事件时刻（墙钟秒）。用 -1 表示尚未初始化。
        self._next_at: dict[str, float] = {d.sn: -1.0 for d in devices}

        # 预先把所有有非零发生率的场景摊平成一个候选池，按 λ 加权抽样
        self._pool: list[tuple[FaultProfile, float]] = [
            (FAULT_PROFILES[key], rate)
            for key, rate in self.rates.items()
            if rate > 0.0 and key in FAULT_PROFILES
        ]
        self._total_rate = sum(rate for _, rate in self._pool)

    # ------------------------------------------------------------------
    def _pick_profile(self) -> FaultProfile | None:
        """按 λ 加权随机选一个故障场景。"""
        if not self._pool or self._total_rate <= 0:
            return None
        r = self.rng.random() * self._total_rate
        acc = 0.0
        for profile, rate in self._pool:
            acc += rate
            if r <= acc:
                return profile
        return self._pool[-1][0]

    def _schedule_next(self, device: BreakerDevice, now: float) -> None:
        """安排该设备的下一次事件时刻。

        泊松过程的事件间隔服从指数分布：Δt ~ Exp(rate)，其中 rate 是
        「该设备所有故障类型的总发生率」（次/秒）。

        关键点：在 dt 秒的窗口内发生至少一次事件的概率是 1 - e^(-λ·dt)，
        而不是 λ·dt。旧实现直接用 λ·dt 当每次采样的概率，
        所以间隔越密误差越大，也解释不了为什么"频率失控"。
        """
        if self._total_rate <= 0:
            self._next_at[device.sn] = math.inf
            return

        # 单台设备的总发生率（次/秒）
        rate_per_second = self._total_rate / SECONDS_PER_DAY
        # 指数分布采样间隔
        interval = self.rng.expovariate(rate_per_second) if rate_per_second > 0 else math.inf

        # 太密集时给一个下限，避免同一时刻堆叠大量事件
        interval = max(interval, 30.0)
        self._next_at[device.sn] = now + interval

    def _spawn(self, device: BreakerDevice, profile: FaultProfile, now: float) -> FaultEvent:
        """按 profile 的取值区间生成一个具体事件。"""
        lo, hi = profile.peak_multiple
        peak = lo if hi <= lo else self.rng.uniform(lo, hi)

        d_lo, d_hi = profile.duration_seconds
        duration = self.rng.uniform(d_lo, d_hi)

        # 演示档下把长事件压缩，否则漏电事件要几小时才看得到结果
        if self.level == "demo" and duration > 600:
            scale = min(1.0, 600.0 / duration)
            duration = max(60.0, duration * max(scale, 0.02))

        return FaultEvent(
            fault_type=profile.fault_type,
            profile=profile,
            start_at=now,
            duration=duration,
            peak_multiple=peak,
            source="auto",
            device_sn=device.sn,
        )

    # ------------------------------------------------------------------
    def tick(self, now: float) -> list[tuple[BreakerDevice, FaultEvent]]:
        """推进调度器。返回本次新触发的事件列表，供日志输出。

        应在模拟器主循环里每个节拍调用一次。
        """
        started: list[tuple[BreakerDevice, FaultEvent]] = []

        for device in self.devices:
            nxt = self._next_at.get(device.sn, -1.0)

            # 首次初始化：立刻安排一个未来时刻（不马上触发，避免启动瞬间一堆故障）
            if nxt < 0.0:
                self._schedule_next(device, now)
                continue

            if now < nxt:
                continue

            # 到点了：如果该设备已有生效中的事件，就顺延，避免事件叠加
            if any(e.is_active(now) for e in device.events):
                self._next_at[device.sn] = now + 60.0
                continue

            profile = self._pick_profile()
            if profile is None:
                self._next_at[device.sn] = math.inf
                continue

            event = self._spawn(device, profile, now)
            device.events.append(event)
            self.stats.record_scheduled(profile.key)
            self.stats.record_started(profile.key)
            started.append((device, event))

            self._schedule_next(device, now)

        return started
