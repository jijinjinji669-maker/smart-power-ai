"""断路器设备行为模型 —— 零第三方依赖。

刻意与 MQTT 发布逻辑分离：这样单元测试和冒烟测试可以直接构造设备、
生成数据，而不需要安装 paho-mqtt。
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone

RATED_CURRENT = 40.0
LOAD_PROFILES = ("residential", "commercial")


class BreakerDevice:
    """单台智能断路器的行为模型。

    复现四类真实数据问题：
      1. 传感器漂移  —— 漏电基线随时间缓慢上移，会让绝对阈值失效
      2. 离线缺口    —— 周期性掉线，该时段必须记为缺失而非 0
      3. 重复上报    —— 网络重传（由发布层决定是否触发）
      4. 时钟偏移    —— 采集时间与网关上报时间分开记录
    """

    def __init__(self, index: int, rng: random.Random) -> None:
        self.index = index
        self.sn = f"AJS-BRK-2026-{index:04d}"
        self.name = f"{index % 3 + 1}号楼{index % 6 + 1}层配电箱-{index:02d}"
        self.rng = rng
        self.profile = rng.choice(LOAD_PROFILES)
        self.leakage_baseline = rng.uniform(6.0, 11.0)
        self.drift_per_hour = rng.uniform(0.02, 0.12)
        self.switch_state = 1
        self.arc_flag = 0
        self.energy_total = rng.uniform(500, 3000)
        self.offline_until = 0.0
        self.next_offline_check = 0.0

    # ---------- 负荷曲线 ----------
    def base_current(self, now: datetime) -> float:
        h = now.hour + now.minute / 60
        if self.profile == "residential":
            return (
                3.0
                + 6.0 * math.exp(-((h - 7.5) ** 2) / 2.2)
                + 9.0 * math.exp(-((h - 19.5) ** 2) / 3.0)
            )
        if 8 <= h <= 19:
            return 11.0 + 2.5 * math.exp(-((h - 13.5) ** 2) / 6.0)
        return 2.0

    def sample(self, t: float, now: datetime, drift_hours: float) -> dict | None:
        """产出一条上报数据；返回 None 表示此刻设备离线。"""
        # ---------- 离线缺口 ----------
        if t < self.offline_until:
            return None
        if t >= self.next_offline_check:
            self.next_offline_check = t + self.rng.uniform(600, 2400)
            if self.rng.random() < 0.15:
                self.offline_until = t + self.rng.uniform(60, 300)
                return None

        # ---------- 正常工况 ----------
        current = self.base_current(now) + self.rng.gauss(0, 0.35)
        voltage = 220.0 + self.rng.gauss(0, 1.2)
        temp_extra = 0.0
        fault = None

        roll = self.rng.random()
        if roll < 0.004:                      # 过载尖峰
            current += 16.0
            temp_extra += 12.0
            fault = "过载"
        elif roll < 0.006:                    # 电压骤降
            voltage -= 40.0
            fault = "欠压"
        elif roll < 0.007:                    # 电弧故障
            self.arc_flag = 1

        # ---------- 传感器漂移 ----------
        leakage = max(
            0.0,
            self.leakage_baseline + self.drift_per_hour * drift_hours + self.rng.gauss(0, 1.5),
        )
        if self.rng.random() < 0.002:         # 漏电爬升
            leakage += self.rng.uniform(35, 60)
            fault = fault or "漏电"

        current = max(0.0, current)
        power = current * voltage * 0.94 / 1000.0
        temperature = 26.0 + current * 1.25 + temp_extra + self.rng.gauss(0, 0.7)

        payload = {
            "device_sn": self.sn,
            "name": self.name,
            "recorded_at": now.astimezone(timezone.utc).isoformat(),
            "gateway_ts": datetime.now(timezone.utc).isoformat(),
            "current": round(current, 3),
            "voltage": round(voltage, 3),
            "power": round(power, 3),
            "power_factor": round(self.rng.uniform(0.90, 0.98), 3),
            "frequency": round(50.0 + self.rng.gauss(0, 0.05), 2),
            "leakage": round(leakage, 3),
            "temperature": round(temperature, 2),
            "switch_state": self.switch_state,
            "arc_flag": self.arc_flag,
            "energy_total": round(self.energy_total, 3),
            "firmware": "v1.4.2",
            "injected_fault": fault,   # 仅供自评对比，真实设备没有这个字段
        }
        self.arc_flag = 0
        self.energy_total += power / 3600.0
        return payload
