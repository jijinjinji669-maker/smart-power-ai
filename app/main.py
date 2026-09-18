"""FastAPI 服务层：读数查询 / 告警列表 / 设备健康概览。

分页与聚合都带上限，避免有人把 from 设成 1970 年把数据库打死。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session

settings = get_settings()
app = FastAPI(
    title="智慧用电 AI 监控平台",
    description="智能断路器数据采集 · 异常检测 · 告警查询",
    version="0.1.0",
)

# 小带宽服务器必配：JSON 响应压完通常只有 1/5
app.add_middleware(GZipMiddleware, minimum_size=1024)


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
