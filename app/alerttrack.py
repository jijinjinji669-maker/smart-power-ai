"""告警生命周期追踪：去重与恢复判定的纯逻辑（零依赖，可单测）。

把「哪些告警该新开、哪些该关闭」从 consumer 的数据库逻辑里抽出来，
单独成模块，便于单测，也避免把状态机逻辑埋在一堆 SQL 里。
"""
from __future__ import annotations


def plan_transitions(
    open_types: set[str], current_types: set[str], warm: bool
) -> tuple[list[str], list[str]]:
    """计算一次检测后的告警状态迁移。

    参数：
      open_types    该设备当前处于「开启」状态（尚未恢复）的告警类型集合
      current_types 本次检测命中的告警类型集合
      warm          检测器是否已积累足够窗口样本（冷启动时统计不可靠）

    返回 (to_open, to_resolve)：
      to_open    本次新出现的类型（需要插入一条告警）
      to_resolve 本次消失的类型（需要标记恢复）

    关键取舍：
      · 同一设备同一类型在恢复之前只开一条告警 —— 这是去重的落点；
      · 冷启动时**不判定恢复**：窗口没攒满，这一帧没命中不代表故障消失，
        否则重启后会把仍在持续的故障误判为已恢复。
    """
    to_open = [t for t in current_types if t not in open_types]
    to_resolve: list[str] = []
    if warm:
        to_resolve = [t for t in open_types if t not in current_types]
    return to_open, to_resolve
