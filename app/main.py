"""FastAPI 服务层：读数查询 / 告警列表 / 设备健康概览 / 异常诊断。

分页与聚合都带上限，避免有人把 from 设成 1970 年把数据库打死。

关于 LLM 诊断接口的设计取舍：
  · 异常区间标注用 GET（纯本地计算，免费，可随便刷）
  · LLM 成因分析用 POST 且需显式指定时间窗口（要花钱，必须手动触发）
  这样能避免「看板一刷新就烧 token」。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.anomalies import SeriesPoint, WindowSpec
from app.config import get_settings
from app.db import get_session
from app.diagnosis import DiagnosisInput, diagnose, to_dict
from app.llm import LLMClient

log = logging.getLogger("api")

settings = get_settings()
app = FastAPI(
    title="智慧用电 AI 监控平台",
    description="智能断路器数据采集 · 异常检测 · 告警查询 · LLM 成因诊断",
    version="0.2.0",
)

# 小带宽服务器必配：JSON 响应压完通常只有 1/5
app.add_middleware(GZipMiddleware, minimum_size=1024)

# 诊断用的默认时间窗口（分钟）。上限设死，避免有人传一年导致拖库。
DIAGNOSE_DEFAULT_MINUTES = 120
DIAGNOSE_MAX_MINUTES = 24 * 60


@app.get("/health", tags=["运维"])
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    """给 docker healthcheck 和 Nginx 探活用，必须足够轻"""
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    if not db_ok:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok", "service": "smart-power-ai"}


@app.get("/api/stats/summary", tags=["运维"])
async def summary(session: AsyncSession = Depends(get_session)) -> dict:
    """首页概览数字"""
    row = (
        await session.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM devices)                        AS device_count,
                  (SELECT count(*) FROM readings)                       AS reading_count,
                  (SELECT count(*) FROM alerts WHERE resolved_at IS NULL) AS open_alerts,
                  (SELECT count(*) FROM alerts
                    WHERE detected_at > now() - INTERVAL '24 hours')     AS alerts_24h,
                  (SELECT max(recorded_at) FROM readings)               AS latest_at
                """
            )
        )
    ).mappings().one()
    return dict(row)


@app.get("/api/devices", tags=["设备"])
async def list_devices(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (
        await session.execute(
            text(
                """
                SELECT d.id, d.device_sn, d.name, d.location, d.rated_current,
                       d.firmware, d.last_seen_at,
                       (SELECT count(*) FROM alerts a
                         WHERE a.device_id = d.id AND a.resolved_at IS NULL) AS open_alerts,
                       CASE
                         WHEN d.last_seen_at IS NULL THEN 'unknown'
                         WHEN d.last_seen_at > now() - INTERVAL '10 minutes' THEN 'online'
                         ELSE 'offline'
                       END AS status
                FROM devices d
                ORDER BY d.id
                """
            )
        )
    ).mappings().all()
    return [dict(r) for r in rows]


@app.get("/api/devices/{sn}/readings", tags=["读数"])
async def get_readings(
    sn: str,
    minutes: int = Query(60, ge=1, le=60 * 24 * 7, description="回看多少分钟"),
    bucket: Literal["raw", "1min"] = Query("1min", description="raw=原始点，1min=分钟聚合"),
    limit: int = Query(500, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    device = (
        await session.execute(
            text("SELECT id, device_sn, name FROM devices WHERE device_sn = :sn"), {"sn": sn}
        )
    ).mappings().first()
    if device is None:
        raise HTTPException(status_code=404, detail=f"设备 {sn} 不存在")

    since = datetime.now().astimezone() - timedelta(minutes=minutes)

    if bucket == "raw":
        sql = text(
            """
            SELECT recorded_at, current, voltage, power, leakage, temperature, switch_state
            FROM readings
            WHERE device_id = :device_id AND recorded_at >= :since
            ORDER BY recorded_at DESC
            LIMIT :limit
            """
        )
    else:
        # 查连续聚合视图而不是扫原始表，这是小机器上能扛住的关键
        sql = text(
            """
            SELECT bucket AS recorded_at, avg_current AS current, avg_voltage AS voltage,
                   NULL::numeric AS power, avg_leakage AS leakage,
                   avg_temperature AS temperature, NULL::smallint AS switch_state
            FROM readings_1min
            WHERE device_id = :device_id AND bucket >= :since
            ORDER BY bucket DESC
            LIMIT :limit
            """
        )

    rows = (
        await session.execute(sql, {"device_id": device["id"], "since": since, "limit": limit})
    ).mappings().all()

    return {
        "device": dict(device),
        "bucket": bucket,
        "minutes": minutes,
        "count": len(rows),
        "points": [dict(r) for r in rows],
    }


@app.get("/api/alerts", tags=["告警"])
async def list_alerts(
    sn: str | None = Query(None, description="按设备序列号过滤"),
    alert_type: str | None = Query(None, description="过载/欠压/漏电/过温"),
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sql = text(
        """
        SELECT a.id, d.device_sn, d.name AS device_name, a.alert_type, a.severity,
               a.value, a.threshold, a.reason, a.detected_at, a.resolved_at
        FROM alerts a
        JOIN devices d ON d.id = a.device_id
        WHERE a.detected_at >= now() - make_interval(hours => :hours)
          AND (:sn IS NULL OR d.device_sn = :sn)
          AND (:alert_type IS NULL OR a.alert_type = :alert_type)
        ORDER BY a.detected_at DESC
        LIMIT :limit
        """
    )
    rows = (
        await session.execute(
            sql, {"hours": hours, "sn": sn, "alert_type": alert_type, "limit": limit}
        )
    ).mappings().all()
    return {"count": len(rows), "alerts": [dict(r) for r in rows]}


@app.get("/api/alerts/by-type", tags=["告警"])
async def alerts_by_type(
    hours: int = Query(24, ge=1, le=24 * 30),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """给看板画柱状图用"""
    rows = (
        await session.execute(
            text(
                """
                SELECT alert_type, count(*) AS cnt
                FROM alerts
                WHERE detected_at >= now() - make_interval(hours => :hours)
                GROUP BY alert_type ORDER BY cnt DESC
                """
            ),
            {"hours": hours},
        )
    ).mappings().all()
    return {"hours": hours, "items": [dict(r) for r in rows]}


# ===========================================================================
# 异常区间标注（纯本地计算，不调 LLM，可以随便刷）
# ===========================================================================
async def _load_device(session: AsyncSession, sn: str) -> dict:
    row = (
        await session.execute(
            text(
                """
                SELECT id, device_sn, name, location, rated_current
                FROM devices WHERE device_sn = :sn
                """
            ),
            {"sn": sn},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"设备 {sn} 不存在")
    return dict(row)


async def _load_points(
    session: AsyncSession, device_id: int, minutes: int, limit: int = 2000
) -> list[dict]:
    """读取原始读数，按时间正序，用于区间识别。

    走原始表而不是分钟聚合视图 —— 区间起止时刻需要点级精度。
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT recorded_at, current, voltage, leakage, temperature, switch_state
                FROM readings
                WHERE device_id = :device_id
                  AND recorded_at >= now() - make_interval(mins => :minutes)
                ORDER BY recorded_at DESC
                LIMIT :limit
                """
            ),
            {"device_id": device_id, "minutes": minutes, "limit": limit},
        )
    ).mappings().all()
    # 查询用倒序取最近 limit 条，再翻正用于顺序分析
    return [dict(r) for r in reversed(rows)]


def _build_window_spec(device: dict, readings: list[dict]) -> WindowSpec:
    """从设备档案构造规格。额定电流缺失时按 payload 里的 tier 推断。"""
    rated = float(device.get("rated_current") or 20.0)
    return WindowSpec(
        device_sn=device["device_sn"],
        name=device.get("name") or "",
        tier="",
        rated_current=rated,
        rated_voltage=220.0,
        leakage_limit_ma=settings.leakage_limit_ma,
        temp_limit_c=settings.temp_limit_c,
    )


def _to_series_points(readings: list[dict]) -> list[SeriesPoint]:
    """把数据库行转成 SeriesPoint。时间转成「距窗口起点的秒数」便于计算。"""
    if not readings:
        return []
    first = readings[0]["recorded_at"]
    points: list[SeriesPoint] = []
    for r in readings:
        ts = r["recorded_at"]
        delta = (ts - first).total_seconds()
        points.append(
            SeriesPoint(
                t=delta,
                ts=ts,
                current=_f(r.get("current")),
                voltage=_f(r.get("voltage")),
                leakage=_f(r.get("leakage")),
                temperature=_f(r.get("temperature")),
                switch_state=r.get("switch_state"),
            )
        )
    return points


def _f(value) -> float | None:
    """把数据库返回的 NUMERIC 转成原生 float。

    注意：asyncpg 对 PostgreSQL 的 NUMERIC 列返回的是 decimal.Decimal，
    而 Decimal 不能被 json.dumps 序列化 —— 拼 prompt 时会直接抛
    "Object of type Decimal is not JSON serializable"。所以这里必须显式转 float。
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@app.get("/api/devices/{sn}/anomalies", tags=["诊断"])
async def device_anomalies(
    sn: str,
    minutes: int = Query(DIAGNOSE_DEFAULT_MINUTES, ge=5, le=DIAGNOSE_MAX_MINUTES),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """自动标注异常区间 —— 纯本地计算，不调用 LLM，因此可以频繁调用。

    返回每段异常的时间、持续时长、峰值、形态，以及匹配到的故障签名。
    """
    from app.diagnosis import extract_interval_features

    device = await _load_device(session, sn)
    readings = await _load_points(session, device["id"], minutes)
    if not readings:
        return {
            "device": device,
            "minutes": minutes,
            "point_count": 0,
            "interval_count": 0,
            "intervals": [],
        }

    spec = _build_window_spec(device, readings)
    data = DiagnosisInput(spec=spec, points=_to_series_points(readings),
                          window_minutes=minutes)
    features, _ = extract_interval_features(data)

    from app.anomalies import match_signature

    for feats in features:
        sig = match_signature([feats])
        feats["suggested_signature"] = {"key": sig["key"], "label": sig["label"]}

    return {
        "device": device,
        "minutes": minutes,
        "point_count": len(readings),
        "interval_count": len(features),
        "intervals": features,
    }


# ===========================================================================
# LLM 成因诊断（要花钱，必须手动触发）
# ===========================================================================
class DiagnoseRequest(BaseModel):
    """诊断请求。时间窗口必须显式给出 —— 这个接口不会被自动调用。"""

    minutes: int = Field(
        default=DIAGNOSE_DEFAULT_MINUTES,
        ge=5,
        le=DIAGNOSE_MAX_MINUTES,
        description="分析最近多少分钟的数据",
    )
    include_alerts: bool = Field(default=True, description="是否把同段告警一起送给模型")


@app.post("/api/devices/{sn}/diagnose", tags=["诊断"])
async def device_diagnose(
    sn: str,
    req: DiagnoseRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """调用 LLM 分析异常成因。

    ⚠️ 这个接口会产生 API 费用，且不缓存结果 —— 前端必须让用户显式确认后才调用。
    设计成 POST 而不是 GET，就是为了避免被浏览器预取或误触发。
    """
    device = await _load_device(session, sn)
    readings = await _load_points(session, device["id"], req.minutes)
    if not readings:
        raise HTTPException(
            status_code=422,
            detail=f"设备 {sn} 最近 {req.minutes} 分钟没有读数，无法分析",
        )

    alerts: list[dict] = []
    if req.include_alerts:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT alert_type, severity, value, threshold, reason, detected_at
                    FROM alerts
                    WHERE device_id = :device_id
                      AND detected_at >= now() - make_interval(mins => :minutes)
                    ORDER BY detected_at DESC LIMIT 10
                    """
                ),
                {"device_id": device["id"], "minutes": req.minutes},
            )
        ).mappings().all()
        alerts = [
            {
                "alert_type": r["alert_type"],
                "severity": int(r["severity"]) if r["severity"] is not None else None,
                "value": _f(r["value"]),
                "threshold": _f(r["threshold"]),
                "reason": r["reason"],
                "detected_at": r["detected_at"].isoformat()
                if r["detected_at"] is not None
                else None,
            }
            for r in rows
        ]

    spec = _build_window_spec(device, readings)
    data = DiagnosisInput(
        spec=spec,
        points=_to_series_points(readings),
        alerts=alerts,
        window_minutes=req.minutes,
    )

    client = LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )

    log.info(
        "诊断请求 device=%s minutes=%d points=%d llm_configured=%s",
        sn, req.minutes, len(readings), client.configured,
    )
    result = diagnose(data, client)
    return to_dict(result)


@app.get("/api/llm/status", tags=["诊断"])
async def llm_status() -> dict:
    """给前端判断是否该启用「分析成因」按钮。不返回密钥。"""
    client = LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    return {
        "configured": client.configured,
        "target": client.safe_target(),
        "model": settings.llm_model,
        "note": "未配置时仍可使用异常区间标注（纯本地计算）",
    }
