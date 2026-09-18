-- 智慧用电 AI 平台 · 数据库初始化
-- 该文件由 docker-entrypoint-initdb.d 在容器首次启动时自动执行
-- 注意：只有在 pgdata 卷为空时才会执行。改了这里要 docker compose down -v 重建

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- ============ 设备档案 ============
CREATE TABLE IF NOT EXISTS devices (
    id            SERIAL PRIMARY KEY,
    device_sn     VARCHAR(64) UNIQUE NOT NULL,
    name          VARCHAR(128),
    location      VARCHAR(128),
    rated_current NUMERIC(6,2)  DEFAULT 40,
    rated_voltage NUMERIC(6,2)  DEFAULT 220,
    firmware      VARCHAR(32),
    last_seen_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ   DEFAULT now()
);

-- ============ 时序读数 ============
CREATE TABLE IF NOT EXISTS readings (
    recorded_at  TIMESTAMPTZ   NOT NULL,
    device_id    INTEGER       NOT NULL REFERENCES devices(id),
    current      NUMERIC(8,3),
    voltage      NUMERIC(8,3),
    power        NUMERIC(10,3),
    power_factor NUMERIC(5,3),
    frequency    NUMERIC(5,2),
    leakage      NUMERIC(8,3),
    temperature  NUMERIC(6,2),
    switch_state SMALLINT,
    arc_flag     SMALLINT      DEFAULT 0
);

-- 转成超表：按天分块，查询只扫相关块
SELECT create_hypertable('readings', 'recorded_at',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);

-- 设备 + 时间倒序是最高频查询模式
CREATE INDEX IF NOT EXISTS idx_readings_device_time
    ON readings (device_id, recorded_at DESC);

-- 唯一约束：网络重传会产生完全相同的 (设备, 采集时间)，
-- 靠 ON CONFLICT DO NOTHING 做幂等去重，由数据库兜底而不是靠代码去猜
CREATE UNIQUE INDEX IF NOT EXISTS uq_readings_device_time
    ON readings (device_id, recorded_at);

-- 冷数据列式压缩，磁盘占用通常降到 1/10
ALTER TABLE readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id',
    timescaledb.compress_orderby   = 'recorded_at DESC'
);
SELECT add_compression_policy('readings', INTERVAL '7 days', if_not_exists => TRUE);

-- 每分钟聚合：看板查这个视图，不扫原始表
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_1min
WITH (timescaledb.continuous) AS
SELECT device_id,
       time_bucket('1 minute', recorded_at) AS bucket,
       avg(current)      AS avg_current,
       max(current)      AS max_current,
       avg(voltage)      AS avg_voltage,
       min(voltage)      AS min_voltage,
       avg(leakage)      AS avg_leakage,
       max(leakage)      AS max_leakage,
       avg(temperature)  AS avg_temperature,
       max(temperature)  AS max_temperature,
       count(*)          AS sample_count
FROM readings
GROUP BY device_id, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('readings_1min',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE);

-- ============ 告警 ============
CREATE TABLE IF NOT EXISTS alerts (
    id          BIGSERIAL PRIMARY KEY,
    device_id   INTEGER      NOT NULL REFERENCES devices(id),
    alert_type  VARCHAR(32)  NOT NULL,   -- 过载 / 欠压 / 漏电 / 过温
    severity    SMALLINT     NOT NULL,   -- 1 提示 2 警告 3 紧急
    value       NUMERIC(10,3),
    threshold   NUMERIC(10,3),
    reason      TEXT,                    -- 可解释性：为什么报的警
    detected_at TIMESTAMPTZ  NOT NULL,
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_alerts_device_time
    ON alerts (device_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_unresolved
    ON alerts (detected_at DESC) WHERE resolved_at IS NULL;
