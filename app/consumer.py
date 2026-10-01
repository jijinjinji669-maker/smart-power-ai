"""MQTT 消费者：订阅设备上报 -> 攒批入库 -> 在线检测 -> 告警落库。

三个工程要点：
  1. 攒批写入：单条 INSERT 在小内存机器上是灾难，按"条数或时间"双阈值刷盘。
  2. 幂等去重：网络重传会产生重复记录，交给数据库唯一索引 + ON CONFLICT 兜底。
  3. 检测与入库解耦：入库失败不能让检测线程崩，反之亦然。
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime
from typing import Any

import paho.mqtt.client as mqtt
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.alerttrack import plan_transitions
from app.config import get_settings
from app.db import SessionLocal, engine
from app.detector import AnomalyDetector, DetectParams

# 心跳 key：与 app/healthcheck.py 中的命名保持一致，否则探针会永远判定不健康
HEARTBEAT_KEY = "heartbeat:consumer"

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("consumer")
settings = get_settings()

INSERT_READINGS = text(
    """
    INSERT INTO readings (recorded_at, device_id, current, voltage, power,
                          power_factor, frequency, leakage, temperature,
                          switch_state, arc_flag)
    VALUES (:recorded_at, :device_id, :current, :voltage, :power,
            :power_factor, :frequency, :leakage, :temperature,
            :switch_state, :arc_flag)
    ON CONFLICT (device_id, recorded_at) DO NOTHING
    """
)

INSERT_ALERT = text(
    """
    INSERT INTO alerts (device_id, alert_type, severity, value, threshold, reason, detected_at)
    VALUES (:device_id, :alert_type, :severity, :value, :threshold, :reason, :detected_at)
    RETURNING id
    """
)

RESOLVE_ALERT = text(
    """
    UPDATE alerts SET resolved_at = :resolved_at WHERE id = :id
    """
)


def _payload_rating(payload: dict[str, Any]) -> float | None:
    """从上报 payload 取额定电流；缺失或非法时返回 None（由调用方回退默认值）。"""
    try:
        value = payload.get("rated_current")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class IngestPipeline:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=20000)
        self.device_ids: dict[str, int] = {}
        self.detectors: dict[str, AnomalyDetector] = {}
        # 只在启动时从 pydantic Settings 转换一次，避免每条上报都构造一遍参数对象
        self.detect_params = DetectParams.from_settings(settings)
        self.redis: aioredis.Redis | None = None
        # 每台设备的额定电流（由上报 payload 落库后缓存），用于按设备派生检测阈值
        self.device_ratings: dict[str, float] = {}
        # 尚未恢复的告警：device_sn -> {alert_type: alert_id}，用于去重与恢复
        self.open_alerts: dict[str, dict[str, int]] = {}
        self.stats = {
            "received": 0, "written": 0, "duplicated": 0,
            "alerts": 0, "resolved": 0, "bad": 0,
        }

    async def start(self) -> None:
        self.redis = aioredis.Redis(
            host=settings.redis_host, port=settings.redis_port, decode_responses=True
        )
        await self._load_devices()
        await self._load_open_alerts()

    async def _load_devices(self) -> None:
        async with SessionLocal() as session:
            rows = (await session.execute(
                text("SELECT device_sn, id, rated_current FROM devices")
            )).all()
        self.device_ids = {sn: pk for sn, pk, _ in rows}
        self.device_ratings = {
            sn: float(rc) if rc is not None else settings.rated_current_a
            for sn, _, rc in rows
        }
        log.info("已加载 %d 台设备", len(self.device_ids))

    async def _load_open_alerts(self) -> None:
        """启动时把数据库里尚未恢复的告警载入内存。

        这样重启后对「仍在持续」的故障不会重复开新告警；若故障其实已恢复，
        也会在下一帧检测到「该类型不再命中」时被正常关闭。
        """
        async with SessionLocal() as session:
            rows = (await session.execute(
                text(
                    "SELECT d.device_sn, a.alert_type, a.id "
                    "FROM alerts a JOIN devices d ON d.id = a.device_id "
                    "WHERE a.resolved_at IS NULL"
                )
            )).all()
        for sn, alert_type, alert_id in rows:
            self.open_alerts.setdefault(sn, {})[alert_type] = alert_id
        log.info("已加载 %d 条未恢复告警", len(rows))

    async def _ensure_device(
        self, session, sn: str, name: str | None, rated_current: float | None
    ) -> int:
        """确保设备已落库并缓存其额定电流。

        额定电流以设备上报为准；缺省时回退到配置默认值。
        若同一设备上报的额定电流与缓存不一致（例如旧数据落库成了 40），
        就顺带纠正数据库与缓存 —— 这是一次性自愈，不会每条消息都写库。
        """
        if sn in self.device_ids:
            cached = self.device_ratings.get(sn)
            if (
                rated_current is not None
                and cached is not None
                and abs(cached - rated_current) > 1e-6
            ):
                await session.execute(
                    text("UPDATE devices SET rated_current = :r WHERE device_sn = :sn"),
                    {"r": rated_current, "sn": sn},
                )
                self.device_ratings[sn] = rated_current
                self.detectors.pop(sn, None)  # 用新额定值重建检测器
            return self.device_ids[sn]

        rating = rated_current if rated_current is not None else settings.rated_current_a
        result = await session.execute(
            text(
                "INSERT INTO devices (device_sn, name, rated_current) "
                "VALUES (:sn, :name, :rated) "
                "ON CONFLICT (device_sn) DO UPDATE SET "
                "name = EXCLUDED.name, rated_current = EXCLUDED.rated_current "
                "RETURNING id, rated_current"
            ),
            {"sn": sn, "name": name or sn, "rated": rating},
        )
        row = result.mappings().one()
        device_id = row["id"]
        self.device_ids[sn] = device_id
        self.device_ratings[sn] = (
            float(row["rated_current"]) if row["rated_current"] is not None else rating
        )
        log.info(
            "发现新设备 %s -> id=%s rated=%.1fA", sn, device_id, self.device_ratings[sn]
        )
        return device_id

    async def put(self, payload: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(payload)
            self.stats["received"] += 1
        except asyncio.QueueFull:
            # 背压：宁可丢数据也不能让内存无限增长把机器打挂
            self.stats["bad"] += 1
            log.warning("队列已满，丢弃一条上报（device=%s）", payload.get("device_sn"))

    async def writer_loop(self) -> None:
        """攒批写库：条数或超时任一触发即刷盘"""
        batch: list[dict[str, Any]] = []
        last_flush = asyncio.get_running_loop().time()
        while True:
            timeout = max(0.1, settings.batch_flush_seconds - (asyncio.get_running_loop().time() - last_flush))
            try:
                batch.append(await asyncio.wait_for(self.queue.get(), timeout=timeout))
            except asyncio.TimeoutError:
                pass
            due = (
                len(batch) >= settings.batch_size
                or asyncio.get_running_loop().time() - last_flush >= settings.batch_flush_seconds
            )
            if not batch or not due:
                continue
            await self._flush(batch)
            batch = []
            last_flush = asyncio.get_running_loop().time()

    async def _flush(self, batch: list[dict[str, Any]]) -> None:
        rows = []
        n_opened = 0
        n_resolved = 0
        try:
            async with SessionLocal() as session:
                for payload in batch:
                    try:
                        sn = payload["device_sn"]
                        recorded_at = datetime.fromisoformat(payload["recorded_at"])
                        rating = _payload_rating(payload)
                        device_id = await self._ensure_device(
                            session, sn, payload.get("name"), rating
                        )
                    except (KeyError, ValueError) as exc:
                        self.stats["bad"] += 1
                        log.warning("字段异常，跳过：%s", exc)
                        continue

                    rows.append(
                        {
                            "recorded_at": recorded_at,
                            "device_id": device_id,
                            "current": payload.get("current"),
                            "voltage": payload.get("voltage"),
                            "power": payload.get("power"),
                            "power_factor": payload.get("power_factor"),
                            "frequency": payload.get("frequency"),
                            "leakage": payload.get("leakage"),
                            "temperature": payload.get("temperature"),
                            "switch_state": payload.get("switch_state", 1),
                            "arc_flag": payload.get("arc_flag", 0),
                        }
                    )

                    # 按该设备额定电流派生检测参数，而不是用全局 20A
                    rating = self.device_ratings.get(sn, settings.rated_current_a)
                    detector = self.detectors.setdefault(
                        sn, AnomalyDetector(self.detect_params.with_rated_current(rating))
                    )
                    detector.push(
                        float(payload.get("current") or 0.0),
                        float(payload.get("voltage") or 0.0),
                        float(payload.get("leakage") or 0.0),
                        float(payload.get("temperature") or 0.0),
                    )

                    detected = detector.detect()
                    by_type = {a.alert_type: a for a in detected}
                    open_for_sn = self.open_alerts.setdefault(sn, {})
                    to_open, to_resolve = plan_transitions(
                        set(open_for_sn), set(by_type), detector.warm
                    )

                    # 1) 新开告警（去重：同类型未恢复前只插一条）
                    for alert_type in to_open:
                        alert = by_type[alert_type]
                        alert_id = (
                            await session.execute(
                                INSERT_ALERT,
                                {
                                    "device_id": device_id,
                                    "alert_type": alert.alert_type,
                                    "severity": alert.severity,
                                    "value": alert.value,
                                    "threshold": alert.threshold,
                                    "reason": alert.reason,
                                    "detected_at": recorded_at,
                                },
                            )
                        ).scalar_one()
                        open_for_sn[alert_type] = alert_id
                        n_opened += 1

                    # 2) 恢复（关闭）告警：仅窗口足够（统计可靠）时判定
                    for alert_type in to_resolve:
                        await session.execute(
                            RESOLVE_ALERT,
                            {"id": open_for_sn[alert_type], "resolved_at": recorded_at},
                        )
                        del open_for_sn[alert_type]
                        n_resolved += 1

                if rows:
                    await session.execute(INSERT_READINGS, rows)
                await session.commit()

                self.stats["written"] += len(rows)
                self.stats["alerts"] += n_opened
                self.stats["resolved"] += n_resolved
                if n_opened or n_resolved:
                    log.warning(
                        "写入 %d 条读数，开 %d 条告警，恢复 %d 条",
                        len(rows), n_opened, n_resolved,
                    )
                else:
                    log.info("写入 %d 条读数", len(rows))
        except Exception:
            log.exception("批写入失败，丢这一批（%d 条），进程继续", len(batch))

        # 更新 Redis 最新值，供看板秒级刷新（失败不影响主流程）
        if self.redis is not None and rows:
            try:
                pipe = self.redis.pipeline()
                for r in rows:
                    key = f"latest:{r['device_id']}"
                    pipe.hset(key, mapping={k: str(v) for k, v in r.items() if k != "device_id"})
                    pipe.expire(key, 3600)
                # 心跳：每成功入库一批就刷新时间戳，
                # 供 app.healthcheck 判断"消费是否停滞"，而不是只判断"进程是否活着"
                pipe.set(HEARTBEAT_KEY, str(time.time()), ex=600)
                await pipe.execute()
            except Exception:
                log.warning("Redis 更新失败（不影响入库）")

    def summary(self) -> str:
        s = self.stats
        return (
            f"收到 {s['received']} / 入库 {s['written']} / 告警 {s['alerts']} / "
            f"恢复 {s['resolved']} / 脏数据 {s['bad']} / 队列 {self.queue.qsize()}"
        )


async def main() -> int:
    pipeline = IngestPipeline()
    await pipeline.start()

    writer = asyncio.create_task(pipeline.writer_loop())
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pipeline.stats["bad"] += 1
            return
        asyncio.run_coroutine_threadsafe(pipeline.put(payload), loop)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ingest-consumer")
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        topic = f"{settings.mqtt_topic_prefix}/+"
        c.subscribe(topic, qos=1)
        log.info("MQTT 已连接 rc=%s，订阅 %s", reason_code, topic)

    client.on_connect = on_connect
    client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)

    def request_stop(signum, frame):
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    client.loop_start()

    async def reporter():
        while not stop_event.is_set():
            await asyncio.sleep(60)
            log.info("运行统计：%s", pipeline.summary())

    reporter_task = asyncio.create_task(reporter())
    try:
        await stop_event.wait()
    finally:
        log.info("退出中，最终统计：%s", pipeline.summary())
        reporter_task.cancel()
        writer.cancel()
        client.loop_stop()
        client.disconnect()
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
