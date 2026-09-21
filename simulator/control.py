"""MQTT 手动注入：通过控制主题按需注入故障场景。

为什么用 MQTT 而不是加一个 REST 接口：
  ① 不依赖服务器 SSH —— 目标环境在阿里云上，连 GitHub 都不稳定
  ② 可以同时从看板、脚本、甚至手机发指令
  ③ 它和生产链路走同一个 broker，本身就是真实架构的一部分
  ④ 改动最小：模拟器本来就连着 broker，只要多订阅一个主题

主题约定：
  power/control/inject          接收指令
  power/control/result/<req_id> 回传执行结果（带 request_id 便于对号）

指令格式（JSON）：
  {"action": "inject",
   "request_id": "demo-1",                 // 可选，用于匹配结果
   "device_sn": "AJS-BRK-2026-0003",       // 可选，缺省表示随机一台
   "fault_type": "overload",               // 可选，按类型筛选
   "profile": "multi_appliance",           // 可选，指定具体场景
   "peak_multiple": 2.0,                   // 可选，覆盖场景默认峰值
   "duration_seconds": 300}                // 可选，覆盖场景默认时长

其他 action：
  {"action": "list_profiles"}              列出可用场景
  {"action": "list_devices"}               列出设备
  {"action": "clear", "device_sn": "..."}  清掉某台设备生效中的事件
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field

from simulator.device import FAULT_PROFILES, BreakerDevice, FaultEvent, FaultProfile

DEFAULT_CONTROL_TOPIC = "power/control/inject"
DEFAULT_RESULT_PREFIX = "power/control/result"


@dataclass
class Command:
    """一条已解析并通过校验的控制指令。"""

    action: str
    request_id: str = ""
    device_sn: str = ""
    fault_type: str = ""
    profile_key: str = ""
    peak_multiple: float | None = None
    duration_seconds: float | None = None
    raw: dict = field(default_factory=dict)


class ControlHandler:
    """解析并执行 MQTT 控制指令。"""

    def __init__(
        self,
        devices: list[BreakerDevice],
        rng: random.Random | None = None,
        fault_type_map: dict[str, str] | None = None,
    ) -> None:
        self.devices = devices
        self.rng = rng or random.Random()
        self.by_sn = {d.sn: d for d in devices}
        # 故障类型 -> 该类型下的场景名列表；不传则从 FAULT_PROFILES 现算
        self.fault_type_map = fault_type_map or self._build_type_map()
        self.injected = 0

    @staticmethod
    def _build_type_map() -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for key, profile in FAULT_PROFILES.items():
            result.setdefault(profile.fault_type, []).append(key)
        return result

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def parse(self, payload: bytes | str) -> Command | tuple[None, str]:
        """解析一条指令。成功返回 Command，失败返回 (None, 错误说明)。"""
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return None, "payload 不是合法的 UTF-8"

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            return None, f"JSON 解析失败：{exc}"

        if not isinstance(data, dict):
            return None, "指令必须是 JSON 对象"

        action = str(data.get("action", "inject")).strip().lower()
        if action not in ("inject", "list_profiles", "list_devices", "clear"):
            return None, f"未知 action {action!r}"

        cmd = Command(
            action=action,
            request_id=str(data.get("request_id", "")),
            device_sn=str(data.get("device_sn", "")).strip(),
            fault_type=str(data.get("fault_type", "")).strip(),
            profile_key=str(data.get("profile", "")).strip(),
            raw=data,
        )

        for field_name in ("peak_multiple", "duration_seconds"):
            if field_name in data and data[field_name] is not None:
                try:
                    setattr(cmd, field_name, float(data[field_name]))
                except (TypeError, ValueError):
                    return None, f"{field_name} 必须是数字"

        # ---- 校验 ----
        if cmd.action == "inject":
            if cmd.profile_key and cmd.profile_key not in FAULT_PROFILES:
                return None, (
                    f"未知场景 {cmd.profile_key!r}。可用：{', '.join(FAULT_PROFILES)}"
                )
            if cmd.fault_type and cmd.fault_type not in self.fault_type_map:
                return None, (
                    f"未知故障类型 {cmd.fault_type!r}。"
                    f"可用：{', '.join(sorted(self.fault_type_map))}"
                )
            if not cmd.profile_key and not cmd.fault_type:
                return None, "必须指定 profile 或 fault_type 之一"
            if cmd.device_sn and cmd.device_sn not in self.by_sn:
                return None, f"未知设备 {cmd.device_sn!r}"

        if cmd.action == "clear" and cmd.device_sn and cmd.device_sn not in self.by_sn:
            return None, f"未知设备 {cmd.device_sn!r}"

        return cmd

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def execute(self, cmd: Command) -> dict:
        if cmd.action == "list_profiles":
            return self._list_profiles()
        if cmd.action == "list_devices":
            return self._list_devices()
        if cmd.action == "clear":
            return self._clear(cmd)
        return self._inject(cmd)

    def _list_profiles(self) -> dict:
        return {
            "ok": True,
            "profiles": [
                {
                    "key": p.key,
                    "label": p.label,
                    "fault_type": p.fault_type,
                    "peak_multiple": list(p.peak_multiple),
                    "duration_seconds": list(p.duration_seconds),
                    "is_normal": p.is_normal,
                    "description": p.description,
                }
                for p in FAULT_PROFILES.values()
            ],
        }

    def _list_devices(self) -> dict:
        return {
            "ok": True,
            "devices": [
                {
                    "device_sn": d.sn,
                    "name": d.name,
                    "tier": d.tier.key,
                    "rated_current": d.tier.rated_current,
                    "thermal_trip": round(d.tier.thermal_trip, 1),
                    "active_events": len(d.events),
                }
                for d in self.devices
            ],
        }

    def _clear(self, cmd: Command) -> dict:
        targets = [self.by_sn[cmd.device_sn]] if cmd.device_sn else self.devices
        removed = 0
        for device in targets:
            removed += len(device.events)
            device.events.clear()
        return {"ok": True, "action": "clear", "removed_events": removed,
                "request_id": cmd.request_id}

    def _resolve_profile(self, cmd: Command) -> FaultProfile:
        if cmd.profile_key:
            return FAULT_PROFILES[cmd.profile_key]
        candidates = [FAULT_PROFILES[k] for k in self.fault_type_map[cmd.fault_type]]
        return self.rng.choice(candidates)

    def _pick_device(self, cmd: Command) -> BreakerDevice:
        if cmd.device_sn:
            return self.by_sn[cmd.device_sn]
        # 未指定设备时优先挑当前空闲的，避免覆盖别人的演示
        idle = [d for d in self.devices if not d.events]
        return self.rng.choice(idle or self.devices)

    def _inject(self, cmd: Command) -> dict:
        now = time.time()
        # 未指定故障类型时，按场景名反查它属于哪一类
        profile = self._resolve_profile(cmd)
        device = self._pick_device(cmd)

        lo, hi = profile.peak_multiple
        peak = cmd.peak_multiple if cmd.peak_multiple is not None else (
            lo if hi <= lo else self.rng.uniform(lo, hi)
        )

        d_lo, d_hi = profile.duration_seconds
        duration = cmd.duration_seconds if cmd.duration_seconds is not None else (
            self.rng.uniform(d_lo, d_hi) if d_hi > d_lo else d_lo
        )
        duration = max(1.0, float(duration))

        event = FaultEvent(
            fault_type=profile.fault_type,
            profile=profile,
            start_at=now,
            duration=duration,
            peak_multiple=max(0.0, float(peak)),
            source="manual",
            device_sn=device.sn,
        )
        device.events.append(event)
        self.injected += 1

        spec = device.tier
        result = {
            "ok": True,
            "action": "inject",
            "request_id": cmd.request_id,
            "device_sn": device.sn,
            "device_name": device.name,
            "tier": spec.key,
            "rated_current": spec.rated_current,
            "profile": profile.key,
            "profile_label": profile.label,
            "fault_type": profile.fault_type,
            "is_normal": profile.is_normal,
            "peak_multiple": round(event.peak_multiple, 3),
            "duration_seconds": round(duration, 1),
            "starts_at": now,
            "ends_at": now + duration,
        }

        # 给出「这个故障会不会真的触发保护」的判断，便于验证注入是否合理
        if profile.fault_type == "overload":
            target_current = event.peak_multiple * spec.rated_current
            result["target_current_a"] = round(target_current, 1)
            result["thermal_trip_a"] = round(spec.thermal_trip, 1)
            result["magnetic_min_a"] = round(spec.magnetic_min, 1)
            result["will_trip"] = target_current >= spec.thermal_trip

        return result

    # ------------------------------------------------------------------
    @staticmethod
    def result_topic(prefix: str, request_id: str) -> str:
        return f"{prefix}/{request_id or 'unknown'}"
