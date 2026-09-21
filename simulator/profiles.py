"""场景库加载：从 YAML 覆盖 device.py 里 FAULT_PROFILES 的默认值。

设计取舍：
  · 只覆盖、不新增 —— YAML 里的 key 必须是 device.py 已定义的场景名。
    这样避免「配置里有个场景但代码不知道怎么渲染」的静默失败。
  · 启动时就加载并合并，运行期不再读文件。
  · YAML 由 PyYAML 解析，它已经是 uvicorn 的依赖，容器里本来就有，
    不需要额外装包（simulator 镜像是主镜像，含 uvicorn）。
  · 解析失败不致命：退回代码里的默认值，但会打印告警 ——
    宁可场景参数用默认值，也不要因为一个配置文件写错就整个模拟器起不来。
"""
from __future__ import annotations

import pathlib
from dataclasses import replace

from simulator.device import FAULT_PROFILES, FaultProfile

DEFAULT_PROFILE_FILE = pathlib.Path(__file__).with_name("fault_profiles.yaml")

# 允许被 YAML 覆盖的字段（其余字段不允许改，避免破坏渲染逻辑）
OVERRIDABLE = {
    "label",
    "peak_multiple",
    "duration_seconds",
    "ramp",
    "steps",
    "cross_fraction",
    "reversible_probability",
    "affects_panel",
    "is_normal",
    "description",
}


def load_overrides(path: pathlib.Path | None = None) -> tuple[dict[str, dict], list[str]]:
    """读取 YAML，返回 (覆盖字典, 问题列表)。"""
    path = path or DEFAULT_PROFILE_FILE
    problems: list[str] = []

    if not path.exists():
        return {}, [f"场景库文件不存在：{path}"]

    try:
        import yaml
    except ImportError:
        return {}, ["未安装 PyYAML，跳过场景库覆盖"]

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        return {}, [f"场景库解析失败：{exc}"]

    if not isinstance(raw, dict):
        return {}, ["场景库顶层必须是「场景名 -> 参数」的映射"]

    overrides: dict[str, dict] = {}
    for key, value in raw.items():
        if key not in FAULT_PROFILES:
            problems.append(f"未知场景 {key!r}，已忽略（可用：{', '.join(FAULT_PROFILES)}）")
            continue
        if not isinstance(value, dict):
            problems.append(f"场景 {key!r} 的值必须是映射，已忽略")
            continue

        clean: dict = {}
        for field_name, field_value in value.items():
            if field_name not in OVERRIDABLE:
                problems.append(f"场景 {key!r} 的字段 {field_name!r} 不允许覆盖，已忽略")
                continue
            # 元组字段在 YAML 里是列表，转回元组以匹配 dataclass
            if field_name in ("peak_multiple", "duration_seconds", "cross_fraction"):
                if field_value is None:
                    clean[field_name] = None
                elif isinstance(field_value, (list, tuple)) and len(field_value) == 2:
                    clean[field_name] = (float(field_value[0]), float(field_value[1]))
                else:
                    problems.append(
                        f"场景 {key!r} 的 {field_name!r} 需要两个元素的区间，已忽略"
                    )
                    continue
            elif field_name == "steps":
                clean[field_name] = int(field_value)
            elif field_name in ("reversible_probability",):
                clean[field_name] = float(field_value)
            elif field_name in ("affects_panel", "is_normal"):
                clean[field_name] = bool(field_value)
            else:
                clean[field_name] = field_value

        if clean:
            overrides[key] = clean

    return overrides, problems


def apply_overrides(path: pathlib.Path | None = None, verbose: bool = True) -> list[str]:
    """把 YAML 覆盖合并进 FAULT_PROFILES（原地修改）。返回问题列表。"""
    overrides, problems = load_overrides(path)

    for key, fields in overrides.items():
        base: FaultProfile = FAULT_PROFILES[key]
        FAULT_PROFILES[key] = replace(base, **fields)

    if verbose:
        if overrides:
            print(
                f"[profiles] 已从场景库覆盖 {len(overrides)} 个场景："
                + "，".join(overrides),
                flush=True,
            )
        for p in problems:
            print(f"[profiles] 警告：{p}", flush=True)

    return problems
