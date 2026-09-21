"""诊断编排：把异常特征组装成 prompt，调用 LLM，产出结构化结论。

分工：
  · app/anomalies.py  算特征（数值计算，本地）
  · app/llm.py        调模型（结构化输出，校验）
  · 本模块            串起来 + 组装 prompt + 落库缓存

为什么不让 LLM 自由发挥成因：
  它可以编出听起来专业但在电气上错误的结论，而且很难发现错。
  所以成因被限定在 FAULT_SIGNATURES 这个封闭集合里，
  LLM 只负责「从候选里选 + 给依据 + 给处置建议」。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.anomalies import (
    AnomalyInterval,
    FAULT_SIGNATURES,
    SeriesPoint,
    WindowSpec,
    describe_interval,
    match_signature,
)
from app.llm import LLMClient, validate_diagnosis

log = logging.getLogger("diagnosis")

ALLOWED_CAUSE_KEYS = {sig["key"] for sig in FAULT_SIGNATURES}


@dataclass
class DiagnosisInput:
    """一次诊断所需的全部输入。"""

    spec: WindowSpec
    points: list[SeriesPoint]
    alerts: list[dict[str, Any]] = field(default_factory=list)
    window_minutes: int = 120


@dataclass
class DiagnosisOutput:
    """诊断结果。ok=False 时 error 说明原因，其余字段可能为空。"""

    ok: bool
    error: str = ""
    # 本地算出的区间与签名（无论 LLM 是否成功都有）
    intervals: list[dict[str, Any]] = field(default_factory=list)
    overall_signature: dict[str, Any] = field(default_factory=dict)
    # LLM 结论
    cause_key: str = ""
    cause_label: str = ""
    confidence: float = 0.0
    reasoning: str = ""
    evidence: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    is_normal_phenomenon: bool = False
    severity: str = ""
    # 元信息
    llm_configured: bool = False
    llm_target: str = ""
    model: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_preview: str = ""
    validation_problems: list[str] = field(default_factory=list)
    analyzed_at: str = ""


# ---------------------------------------------------------------------------
# 上下文构建
# ---------------------------------------------------------------------------
def extract_interval_features(
    data: DiagnosisInput,
    merge_gap_seconds: float = 120.0,
) -> tuple[list[dict[str, Any]], list[AnomalyInterval]]:
    """识别异常区间并提取特征。"""
    from app.anomalies import find_intervals

    intervals = find_intervals(data.points, data.spec, merge_gap_seconds)
    features = [describe_interval(iv, data.points, data.spec) for iv in intervals]
    return features, intervals


def build_prompt(data: DiagnosisInput) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """组装用户 prompt。返回 (prompt 文本, 区间特征列表, 综合签名)。"""
    spec = data.spec
    features, _intervals = extract_interval_features(data)

    overall = match_signature(features) if features else match_signature([])

    # ---- 设备规格 ----
    device_block = {
        "device_sn": spec.device_sn,
        "name": spec.name,
        "tier": spec.tier,
        "rated_current_a": spec.rated_current,
        "rated_voltage_v": spec.rated_voltage,
        "thermal_hold_a": round(spec.thermal_hold, 1),
        "thermal_trip_a": round(spec.thermal_trip, 1),
        "voltage_normal_range_v": [round(spec.voltage_min, 1), round(spec.voltage_max, 1)],
        "leakage_hardware_limit_ma": spec.leakage_limit_ma,
        "temperature_limit_c": spec.temp_limit_c,
    }

    # ---- 候选签名（封闭集合）----
    candidates = [
        {
            "key": sig["key"],
            "label": sig["label"],
            "indicators": sig["indicators"],
        }
        for sig in FAULT_SIGNATURES
    ]

    # ---- 相关告警 ----
    alert_block = data.alerts[:10]

    payload = {
        "分析窗口": f"最近 {data.window_minutes} 分钟",
        "设备规格": device_block,
        "识别到的异常区间": features,
        "本地规则匹配的建议签名": {
            "key": overall["key"],
            "label": overall["label"],
            "说明": "这是按「哪些电气量显著异常 + 电流形态」做的保守匹配，仅供参考，你可以否决它",
        },
        "同一时段的历史告警": alert_block,
        "候选签名": candidates,
    }

    prompt = (
        "请分析下面这台智能断路器记录到的异常，判断最可能的成因。\n\n"
        "注意：\n"
        "- 只能从「候选签名」里选 cause_key，或选 unknown\n"
        "- reasoning 必须引用「异常区间」里的具体数值，不要泛泛而谈\n"
        "- 如果特征看起来像正常现象（例如启动浪涌），请把 is_normal_phenomenon 设为 true\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return prompt, features, overall


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def diagnose(data: DiagnosisInput, client: LLMClient) -> DiagnosisOutput:
    """执行一次诊断。不抛异常 —— 失败信息通过返回值传递。"""
    out = DiagnosisOutput(
        ok=False,
        llm_configured=client.configured,
        llm_target=client.safe_target(),
        analyzed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    # 1) 本地特征提取（即使没有 LLM 也有价值）
    try:
        prompt, features, overall = build_prompt(data)
    except Exception as exc:  # noqa: BLE001
        out.error = f"特征提取失败：{exc}"
        log.exception("特征提取失败")
        return out

    out.intervals = features
    out.overall_signature = overall
    out.prompt_preview = prompt[:1500]

    if not features:
        out.error = (
            "该时间窗口内没有识别到异常区间。"
            "可能原因：窗口太短、数据缺失、或该时段确实正常。"
        )
        return out

    # 2) 未配置密钥时提前返回，把本地结果给出去
    if not client.configured:
        out.error = "未配置 LLM_API_KEY，仅返回本地特征与规则匹配结果。"
        return out

    # 3) 调 LLM
    result = client.chat_json(prompt)
    out.model = result.model
    out.latency_ms = result.latency_ms
    out.prompt_tokens = result.prompt_tokens
    out.completion_tokens = result.completion_tokens

    if not result.ok:
        out.error = result.error
        return out

    cleaned, problems = validate_diagnosis(result.data, ALLOWED_CAUSE_KEYS)
    out.validation_problems = problems
    out.cause_key = cleaned["cause_key"]
    out.cause_label = cleaned["cause_label"]
    out.confidence = cleaned["confidence"]
    out.reasoning = cleaned["reasoning"]
    out.evidence = cleaned["evidence"]
    out.actions = cleaned["actions"]
    out.uncertainties = cleaned["uncertainties"]
    out.is_normal_phenomenon = cleaned["is_normal_phenomenon"]
    out.severity = cleaned["severity"]
    out.ok = True
    return out


def to_dict(out: DiagnosisOutput) -> dict[str, Any]:
    """转成可 JSON 序列化的字典，供 API 返回。"""
    return {
        "ok": out.ok,
        "error": out.error,
        "intervals": out.intervals,
        "overall_signature": out.overall_signature,
        "llm": {
            "configured": out.llm_configured,
            "target": out.llm_target,
            "model": out.model,
            "latency_ms": out.latency_ms,
            "prompt_tokens": out.prompt_tokens,
            "completion_tokens": out.completion_tokens,
        },
        "analysis": {
            "cause_key": out.cause_key,
            "cause_label": out.cause_label,
            "confidence": out.confidence,
            "reasoning": out.reasoning,
            "evidence": out.evidence,
            "actions": out.actions,
            "uncertainties": out.uncertainties,
            "is_normal_phenomenon": out.is_normal_phenomenon,
            "severity": out.severity,
        }
        if out.cause_key
        else None,
        "validation_problems": out.validation_problems,
        "analyzed_at": out.analyzed_at,
    }
