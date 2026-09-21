"""LLM 客户端 —— 调用 OpenAI 兼容接口，强制结构化输出。

设计要点：

1. **不自由发挥**。成因只能从给定的故障签名集合里选，并必须给出判断依据。
   否则 LLM 会编出听起来专业但在电气上错误的结论，而且很难发现错。

2. **强制 JSON 输出**。用 response_format={"type": "json_object"}，
   再用 Pydantic 二次校验。解析失败就报错，不猜测。

3. **不泄露密钥**。密钥只从配置读，任何异常信息、日志、返回值里都不带密钥。
   base_url 在前端展示时会被剥离。

4. **可控超时与重试**。超时 30 秒，只重试一次，失败就把原因原样返回，
   由调用方决定要不要提示用户。

只用 httpx 一个依赖（它已在 requirements.txt 里）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("llm")

DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 1

# 系统提示：把模型钉死在「基于给定特征做判定」这件事上
SYSTEM_PROMPT = """你是一名低压配电电气工程师，负责分析智能断路器记录到的异常曲线。

你的任务：根据给定的结构化特征，判断最可能的成因，并给出排查建议。

严格约束：
1. 成因**只能**从「候选签名」列表里选择一个 key，不要发明新的成因类型。
2. 如果候选签名都不合适，选 "unknown"，并在 reasoning 里说明缺少什么信息。
3. 判断必须基于给定的数值特征，**不要臆造数据里没有的信息**。
4. 特别注意区分「故障」和「正常但像故障」：例如压缩机启动浪涌是正常现象，
   它电流很高但持续仅数百毫秒且会快速回落，温度也不会明显上升。
5. 涉及电气安全时明确指出：30 mA 是人身安全阈值（GB/T 13955-2017），
   硬件保护动作不可由软件替代。

参考的电气知识：
- 断路器脱扣特性（IEC 60898-1）：1.13×In 一小时内不应脱扣；1.45×In 一小时内必须脱扣；
  C 曲线瞬时磁脱扣区间为 5~10×In。这里的 In 是断路器额定电流。
- 过载导致温度上升遵循焦耳定律，温升与电流平方成正比，且有热惯性延迟。
- 漏电缓慢爬升通常是绝缘受潮或老化；突变到 30 mA 以上多为线路破损接地。
- 多台设备同时出现电压跌落，说明是电源侧问题而非单台设备问题。

只输出 JSON，不要任何解释性文字。格式：
{
  "cause_key": "候选签名里的 key",
  "cause_label": "对应签名的中文名",
  "confidence": 0.0 到 1.0 之间的小数,
  "reasoning": "为什么这样判断，必须引用给定的具体数值特征",
  "evidence": ["引用的特征，每条一句，最多 5 条"],
  "is_normal_phenomenon": true 或 false,
  "severity": "提示" 或 "警告" 或 "紧急",
  "actions": ["建议的处置动作，按优先级排序，最多 5 条"],
  "uncertainties": ["哪些信息不足或有歧义，最多 3 条"]
}
"""


@dataclass
class LLMResult:
    """一次 LLM 调用的结果。ok=False 时 error 说明原因。"""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    model: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_text: str = ""

    def usage_text(self) -> str:
        if not self.ok:
            return ""
        return (
            f"{self.model} · {self.latency_ms}ms · "
            f"{self.prompt_tokens}+{self.completion_tokens} tokens"
        )


class LLMClient:
    """OpenAI 兼容接口的极简客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.timeout = timeout

    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        """是否已配置密钥。未配置时调用方应给出友好提示而不是报错。"""
        return bool(self.api_key)

    def safe_target(self) -> str:
        """可供前端展示的目标描述 —— 不含密钥。"""
        return f"{self.base_url} · {self.model}"

    # ------------------------------------------------------------------
    def chat_json(self, user_prompt: str) -> LLMResult:
        """发一次对话，要求返回 JSON 对象，并解析成 dict。"""
        if not self.configured:
            return LLMResult(
                ok=False,
                error="未配置 LLM_API_KEY。请在 .env 中填写后再使用分析功能。",
            )
        if not self.base_url:
            return LLMResult(ok=False, error="未配置 LLM_BASE_URL")

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,          # 诊断任务要稳定，不要发散
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error = ""
        for attempt in range(MAX_RETRIES + 1):
            result = self._once(url, headers, payload, attempt)
            if result.ok or not result.error.startswith("RETRY:"):
                return result
            last_error = result.error[6:]
            log.warning("LLM 调用第 %d 次失败，准备重试：%s", attempt + 1, last_error)

        return LLMResult(ok=False, error=last_error or "调用失败", model=self.model)

    def _once(self, url: str, headers: dict, payload: dict, attempt: int) -> LLMResult:
        started = _now_ms()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            return LLMResult(ok=False, error=f"RETRY:请求超时（{self.timeout:.0f} 秒）",
                             model=self.model)
        except httpx.RequestError as exc:
            # 注意：exc 消息可能包含 URL，但不含 Authorization 头
            return LLMResult(ok=False, error=f"RETRY:网络错误 {exc.__class__.__name__}",
                             model=self.model)

        latency = _now_ms() - started

        if resp.status_code == 401:
            return LLMResult(ok=False, error="密钥无效或已过期（HTTP 401）", model=self.model)
        if resp.status_code == 429:
            return LLMResult(ok=False, error="RETRY:触发限流（HTTP 429）", model=self.model)
        if resp.status_code >= 500:
            return LLMResult(
                ok=False, error=f"RETRY:服务端错误 HTTP {resp.status_code}", model=self.model
            )
        if resp.status_code != 200:
            return LLMResult(
                ok=False,
                error=f"请求失败 HTTP {resp.status_code}：{_short(resp.text)}",
                model=self.model,
            )

        try:
            body = resp.json()
        except json.JSONDecodeError:
            return LLMResult(ok=False, error="响应不是合法 JSON", model=self.model)

        choices = body.get("choices") or []
        if not choices:
            return LLMResult(ok=False, error="响应中没有 choices 字段", model=self.model)

        text = (choices[0].get("message") or {}).get("content") or ""
        usage = body.get("usage") or {}

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return LLMResult(
                ok=False,
                error=f"模型返回的内容不是合法 JSON：{exc}",
                model=body.get("model", self.model),
                latency_ms=latency,
                raw_text=text[:2000],
            )

        if not isinstance(data, dict):
            return LLMResult(
                ok=False, error="模型返回的 JSON 顶层不是对象",
                model=body.get("model", self.model), latency_ms=latency,
                raw_text=text[:2000],
            )

        return LLMResult(
            ok=True,
            data=data,
            model=body.get("model", self.model),
            latency_ms=latency,
            prompt_tokens=usage.get("prompt_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or 0,
        )


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _short(text: str, limit: int = 200) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------------------------------------------------------------------------
# 输出校验：只信任符合契约的字段
# ---------------------------------------------------------------------------
VALID_SEVERITY = {"提示", "警告", "紧急"}


def validate_diagnosis(data: dict[str, Any], allowed_keys: set[str]) -> tuple[dict, list[str]]:
    """校验并规范化模型输出。返回 (规范化结果, 问题列表)。

    不抛异常 —— 模型输出有瑕疵时，尽量保留可用部分并记录问题，
    而不是整条丢弃让用户白等一次调用。
    """
    problems: list[str] = []
    out: dict[str, Any] = {}

    key = str(data.get("cause_key", "")).strip()
    if key not in allowed_keys:
        problems.append(f"返回的 cause_key {key!r} 不在候选签名中，已改为 unknown")
        key = "unknown"
    out["cause_key"] = key

    out["cause_label"] = str(data.get("cause_label") or "").strip() or key

    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
        problems.append("confidence 不是数字，已置为 0")
    out["confidence"] = round(min(1.0, max(0.0, conf)), 2)

    out["reasoning"] = str(data.get("reasoning") or "").strip()
    if not out["reasoning"]:
        problems.append("缺少 reasoning（判断依据）")

    out["evidence"] = _str_list(data.get("evidence"), 5, problems, "evidence")
    out["actions"] = _str_list(data.get("actions"), 5, problems, "actions")
    out["uncertainties"] = _str_list(data.get("uncertainties"), 3, problems, "uncertainties")

    out["is_normal_phenomenon"] = bool(data.get("is_normal_phenomenon", False))

    severity = str(data.get("severity", "")).strip()
    if severity not in VALID_SEVERITY:
        if severity:
            problems.append(f"severity {severity!r} 不合法，已改为「警告」")
        severity = "警告"
    out["severity"] = severity

    return out, problems


def _str_list(value: Any, limit: int, problems: list[str], name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"{name} 不是数组，已忽略")
        return []
    items = [str(v).strip() for v in value if str(v).strip()]
    if len(items) > limit:
        items = items[:limit]
    return items
