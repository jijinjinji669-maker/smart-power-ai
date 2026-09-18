# 智慧用电 AI 监控平台

智能断路器数据采集 · 在线异常检测 · 告警查询 API

面向 **智能断路器 / 智慧用电 / 计量用电** 业务场景的 AI 应用后端：
通过 MQTT 接入设备电气量数据，实时检测过载、欠压、漏电、过温四类异常，
并把结果落库供看板与告警系统消费。

> **设计取舍：不依赖任何机器学习框架做异常检测。**
> 用滑窗 MAD + 双速 EWMA 漂移检测 + 物理边界约束，
> 换取可解释性（运维要知道为什么报警）、冷启动能力（新设备无需历史数据）和零 GPU 部署成本。

---

## 目录结构

```
smart-power-ai/
├── docker-compose.yml          # 六服务编排：db / redis / mqtt / api / consumer / simulator
├── Dockerfile                  # 多阶段构建，非 root 运行
├── deploy.sh                   # 一键部署（含前置检查与健康等待）
├── smoke_test.py               # ★ 零依赖冒烟测试，验证核心逻辑
├── .env.example                # 配置模板（复制为 .env 后填值）
├── requirements.txt
├── sql/init.sql                # 建表 + 超表 + 连续聚合 + 压缩策略
├── nginx/smart-power.conf      # 反向代理 + gzip + 安全响应头
├── app/
│   ├── config.py               # 全部参数从环境变量来，无魔法数字
│   ├── db.py                   # 异步引擎与会话
│   ├── detector.py             # ★ 异常检测核心（零第三方依赖）
│   ├── consumer.py             # MQTT -> 攒批入库 -> 在线检测
│   └── main.py                 # FastAPI：健康/统计/设备/读数/告警
└── simulator/
    ├── device.py               # 设备行为模型（零依赖，可单测）
    └── breaker_sim.py          # MQTT 发布层 + 重复上报模拟
```

---

## 快速开始

### 最快验证：零依赖冒烟测试

不需要数据库、消息队列或任何第三方库，直接验证核心逻辑：

```bash
python smoke_test.py
```

预期输出：三类故障全部检出，精确率与召回率均高于 60%，最后打印
`冒烟测试通过 ✅`。详细数字见下文「实测效果」。

### 完整运行（Docker）

```bash
cp .env.example .env
# 编辑 .env，至少改掉 DB_PASSWORD 和 EMQX_PASSWORD

docker compose up -d --build

# 看采集与检测日志
docker compose logs -f consumer

# 验证
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/stats/summary
open http://127.0.0.1:8000/docs          # Swagger 文档
open http://127.0.0.1:8501               # 可视化看板
```

### 服务器部署

```bash
git clone <你的仓库地址> smart-power-ai
cd smart-power-ai
cp .env.example .env
vim .env                                  # 填强密码
chmod +x deploy.sh
./deploy.sh
```

---

## 可视化看板

Streamlit 看板（容器名 `dashboard`，监听 `127.0.0.1:8501`），三个视图：

| 视图 | 内容 |
|---|---|
| 📊 实时监控 | 多设备电流趋势（分钟聚合）、==告警点叠加在曲线上==、漏电曲线（含 30mA 安全线）、温度曲线（含 55℃ 告警线）、各设备最新读数 |
| 🔔 告警中心 | 按类型分布柱状图、按小时告警趋势、可按类型/状态过滤的告警明细（含**判定理由**） |
| 📋 设备台账 | 每台设备的健康状态、累计读数、漏电峰值、温度峰值、最近上报时间 |

看板特性：

- **直连数据库**读取分钟聚合视图 `readings_1min`，不扫原始表
- 时间窗口 1h ~ 72h 可调，自动刷新 0/5/10/30/60 秒可调
- 与 API 一样只监听回环地址，公网访问经 Nginx 反代（配置见 `nginx/smart-power.conf`）

```bash
# 只看板单独重启
docker compose restart dashboard

# 看板日志
docker compose logs -f dashboard

# 本机验证（服务器上执行）
curl -s http://127.0.0.1:8501/_stcore/health
```

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（供 docker healthcheck / Nginx 探活） |
| GET | `/api/stats/summary` | 设备数、读数总量、未处理告警、24h 告警数 |
| GET | `/api/devices` | 设备列表 + 在线状态 + 未处理告警数 |
| GET | `/api/devices/{sn}/readings` | 读数曲线，支持 `bucket=raw\|1min`、`minutes`、`limit` |
| GET | `/api/alerts` | 告警列表，支持按设备、类型、时间过滤 |
| GET | `/api/alerts/by-type` | 按类型聚合，供看板画图 |

```bash
# 查某台设备最近 2 小时的分钟聚合曲线
curl "http://127.0.0.1:8000/api/devices/AJS-BRK-2026-0001/readings?minutes=120&bucket=1min"

# 查最近 24 小时的漏电告警
curl "http://127.0.0.1:8000/api/alerts?alert_type=漏电&hours=24"
```

---

## 异常检测的设计说明

### 为什么不用机器学习

| 维度 | 统计方法（本方案） | 深度模型（LSTM/AE） |
|---|---|---|
| 可解释性 | 能说明"偏离中位数 4 倍 MAD 且超过 20A" | 只能给出一个分数，运维不接受 |
| 冷启动 | 新装设备当天可用 | 需积累数周数据才能训练 |
| 部署成本 | 消费者进程内，无 GPU、无模型版本管理 | 需推理服务、GPU 或 ONNX 运行时 |

**扩展点**：积累 3-6 个月运行数据后，可将当前告警作为标注换成有监督模型，
只需替换 `detector.detect()` 一个函数，上下游接口不变。

### 四类异常与对应策略

| 异常 | 策略 | 理由 |
|---|---|---|
| 过载 | 滑窗 MAD + 物理下限 | MAD 不被异常值自身污染；3-sigma 会被尖峰拉偏而"掩护"异常 |
| 欠压 | 滑窗 MAD + 绝对欠压线 | 统计异常必须同时越过物理线，避免正常负荷波动误报 |
| 漏电 | 双速 EWMA 漂移 + 基线守卫 | 渐变故障不产生尖峰；但漂移信号必须叠加"高于自身基线"防止告警拖尾 |
| 过温 | 绝对阈值 | 温度有明确安全线，无需统计判断 |

### 实测效果

项目自带零依赖冒烟测试，可直接复现（不需要装任何第三方库）：

```bash
python smoke_test.py
```

240 个逐分钟样本（注入过载 / 漏电 / 欠压三段故障）上的实测结果：

| 指标 | 数值 |
|---|---|
| 精确率 Precision | **100.0%**（17 条告警，0 条误报） |
| 召回率 Recall | **88.9%**（18 个故障样本检出 16 个） |
| F1 | **94.1%** |

**告警示例**（每条都带可读理由，运维能直接判断该不该处理）：

```
09:01  [过载] sev=3  电流 28.99A 偏离窗口中位数 11.05A 超过 4.0 倍 MAD，且高于 20.0A
10:00  [漏电] sev=3  漏电 52.6mA 超过人身安全线 30.0mA
11:01  [欠压] sev=3  电压 177.5V 低于 198.0V 且偏离自身基线
```

> **诚实说明**：以上为合成数据（固定随机种子、人为注入故障）上的结果，
> 真实设备数据噪声更大，需要重新调参。
> 另：检测存在 1-2 个采样周期的延迟，源于「连续点确认」策略 ——
> 这是准确率与响应及时性之间的刻意取舍。

**算法演进过程**（早期独立脚本验证，记录了误报是怎么被一步步消掉的）：

| 版本 | 策略 | 精确率 | 召回率 | F1 |
|---|---|---|---|---|
| v1 | 滑窗 MAD(window=30, k=3.5) | 56.0% | 100% | 71.8% |
| v2 | 扩窗 60 + 物理边界 + 连续确认 | 57.1% | 85.7% | 68.6% |
| **v3** | **+ 漂移基线守卫** | **92.3%** | **85.7%** | **88.9%** |

> v1 误报根因：早高峰负荷正常爬坡（6A → 10A）被判定为异常。
> v3 修复的第二个问题：漏电故障结束后，慢速 EWMA 需较长时间追平，
> `fast - slow` 持续为正导致告警拖出 8 分钟长尾。
> 解决办法是要求漂移信号同时满足「当前值显著高于滚动中位数」。

---

## 模拟器复现的真实数据问题

真实设备数据不是干净表格。模拟器刻意产出四类脏数据：

| 问题 | 模拟方式 | 系统如何处理 |
|---|---|---|
| 传感器漂移 | 每台设备漏电基线随机且每小时缓慢上移 | 检测用「相对自身基线」而非绝对阈值 |
| 离线缺口 | 15% 概率进入 1-5 分钟离线 | 该时段不写入记录，==绝不记 0==（0A 会被误判为停电） |
| 重复上报 | 1% 概率重发同一时刻数据 | 数据库唯一索引 + `ON CONFLICT DO NOTHING` 幂等去重 |
| 时钟偏移 | `recorded_at` 与 `gateway_ts` 分开记录 | 以采集时间为准，保留网关时间便于排查 |

```bash
# 调参：设备数 / 上报间隔 / 随机种子（固定种子数据可复现）
python -m simulator.breaker_sim --devices 10 --interval 5 --seed 20260916
```

---

## 运维要点

```bash
docker compose ps                       # 服务状态
docker compose logs -f consumer         # 采集与检测日志（最常用）
docker compose up -d --build            # 改代码后必须加 --build
docker compose down                     # 停止（保留数据卷）
docker compose down -v                  # 停止并删除数据（慎用）
```

**小内存机器（3.5G）已做的适配**：

- PostgreSQL `shared_buffers=512MB`、`max_connections=60`，避免默认配置吃光内存
- `wal_compression=on` 降低磁盘写入
- 容器统一 `json-file` 日志上限（10MB × 3），防止日志写满 40G 磁盘
- 消费者队列满时**主动丢弃并告警**，宁可丢数据不让内存无限增长
- 所有容器 `restart: always` + healthcheck，进程异常自动恢复

**安全**：

- 数据库与 MQTT **不对宿主机暴露端口**，仅在 compose 内部网络可达
- API 只监听 `127.0.0.1:8000`，公网访问一律经 Nginx
- 容器以非 root 用户（uid 1000）运行
- `.env` 已加入 `.gitignore`，密钥不进版本库

---

## 后续规划

- [ ] 分时电价与负荷预测，输出可节省电费估算
- [ ] Streamlit 实时看板（曲线 + 告警列表 + AI 问答框）
- [ ] Text2SQL 自然语言查数（只读数据库角色 + Pydantic 校验 + 超时与行数上限）
- [ ] 故障知识库 RAG 问答
- [ ] Prometheus 指标 + Grafana 看板
- [ ] pytest 覆盖检测器与 API
- [ ] GitHub Actions 自动构建部署
