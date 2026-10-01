# 智慧用电 AI 监控平台

面向 **智能断路器 / 智慧用电** 场景的 AI 应用后端：MQTT 接入设备电气量 → 在线异常检测 → 告警落库 → 可视化看板 + LLM 成因诊断。

## 特性

- **零 ML 依赖检测**：滑窗 MAD + 双速 EWMA + 物理边界（IEC 60898-1 / GB/T 13955），可解释、可冷启动、零 GPU
- **四类异常**：过载 / 欠压 / 漏电 / 过温，每条告警带可读中文理由
- **LLM 成因诊断**：异常区间本地标注 + DeepSeek 结构化分析（手动触发，避免误烧 token）
- **真实数据模拟器**：分档断路器 + 泊松故障调度（对齐真实年故障率）+ MQTT 手动故障注入
- TimescaleDB 时序存储（超表 / 连续聚合 / 压缩）、Streamlit 看板、全 Docker Compose 编排

## 架构

```
simulator ──MQTT──▶ consumer ──检测──▶ PostgreSQL/TimescaleDB ──▶ api ──▶ dashboard(Streamlit)
                                      └──▶ 告警 ──▶ Redis(最新值)
```

服务：`db` · `redis` · `mqtt(EMQX)` · `api(FastAPI)` · `consumer` · `simulator` · `dashboard`

## 快速开始

**零依赖冒烟测试**（无需数据库 / MQTT / 任何第三方库）：

```bash
python smoke_test.py
```

**Docker 运行**：

```bash
cp .env.example .env        # 改掉 DB_PASSWORD / EMQX_PASSWORD
docker compose up -d --build
curl http://127.0.0.1:8000/health        # API 健康
# 看板 http://127.0.0.1:8501 · Swagger http://127.0.0.1:8000/docs
```

**服务器部署**：

```bash
./deploy.sh                 # 前置检查 → 构建 → 启动 → 健康等待
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` · `/api/stats/summary` | 健康检查 / 概览 |
| GET | `/api/devices` · `/api/devices/{sn}/readings` | 设备列表 / 读数曲线 |
| GET | `/api/alerts` · `/api/alerts/by-type` | 告警列表 / 按类型聚合 |
| GET | `/api/devices/{sn}/anomalies` | 异常区间标注（本地计算，免费） |
| POST | `/api/devices/{sn}/diagnose` | LLM 成因诊断（需在 `.env` 配 `LLM_API_KEY`） |

## 检测思路

不用深度学习：**MAD 而非 3-sigma**（中位数不被尖峰拉偏）、**统计异常必须同时越过物理线**、**连续 N 点确认**（抗抖动）、**漂移加基线守卫**（防告警拖尾）。详细注释见 `app/detector.py`。

## 目录

```
app/         FastAPI · 检测器 · 告警 · LLM 诊断
simulator/   断路器行为模型 · 故障调度 · 手动注入
sql/         建表 / 超表 / 连续聚合 / 压缩
dashboard/   Streamlit 可视化看板
```

## 测试

```bash
python smoke_test.py            # 核心逻辑冒烟
python test_p0_fixes.py         # 额定电流贯通 / 告警去重恢复
python test_tier_thresholds.py  # 分档阈值
```

## 环境要求

Python 3.10+ · Docker + Compose v2。部署细节见 `deploy.sh`，服务编排见 `docker-compose.yml`。
