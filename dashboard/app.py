"""智慧用电 AI 监控平台 · Streamlit 看板

设计取舍：
  · 直连 PostgreSQL 而不是走自家 API —— 看板属内部工具，直连少一跳、SQL 更灵活，
    也不需要为它加一套鉴权。对外暴露的是 Nginx 反代 + 后续的访问控制。
  · 用 cache_resource 缓存连接池（每次刷新复用），但不用 cache_data 缓存查询结果，
    否则"实时"看板会显示过期数据。
  · 自动刷新用 st.autorefresh 的等价实现：一个刷新间隔下拉框 + st.rerun()。
    Streamlit 原生没有定时重跑，靠 time.sleep + rerun 会阻塞交互，所以让用户选间隔。

本地运行：
    streamlit run dashboard/app.py
容器内运行：见 docker-compose.yml 的 dashboard 服务。
"""
from __future__ import annotations

import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import create_engine, text

st.set_page_config(page_title="智慧用电 AI 监控平台", page_icon="⚡", layout="wide")

DB_URL = (
    f"postgresql+psycopg2://{os.getenv('DB_USER', 'power')}:{os.getenv('DB_PASSWORD', 'power')}"
    f"@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}"
    f"/{os.getenv('DB_NAME', 'power')}"
)

SEVERITY_LABEL = {1: "提示", 2: "警告", 3: "紧急"}
SEVERITY_COLOR = {1: "#f59e0b", 2: "#f97316", 3: "#ef4444"}

# LLM 诊断走 API 而不是直连数据库 —— 诊断逻辑在服务端，
# 而且这个接口要花钱，必须由服务端统一控制。
API_BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")

# 异常区间在曲线上的底色（按类型区分，便于一眼分辨）
KIND_FILL = {
    "过载": "rgba(239,68,68,0.13)",
    "漏电": "rgba(168,85,247,0.15)",
    "欠压": "rgba(59,130,246,0.13)",
    "过压": "rgba(234,179,8,0.13)",
    "过温": "rgba(249,115,22,0.13)",
}


@st.cache_resource
def get_engine():
    """连接池缓存一次即可，不要每次刷新都新建 engine"""
    return create_engine(DB_URL, pool_size=3, max_overflow=2, pool_pre_ping=True)


def query(sql: str, **params) -> pd.DataFrame:
    with get_engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


# ---------------------------------------------------------------------------
# 与后端 API 的交互
# ---------------------------------------------------------------------------
def api_get(path: str, timeout: float = 20.0) -> dict | None:
    """调用后端 GET 接口。失败返回 None 并提示，不抛异常中断看板。"""
    import httpx

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{API_BASE}{path}")
        if resp.status_code == 200:
            return resp.json()
        st.warning(f"接口返回 {resp.status_code}：{resp.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        st.warning(f"调用后端失败（{API_BASE}）：{exc}")
    return None


def api_post(path: str, payload: dict, timeout: float = 90.0) -> dict | None:
    """调用后端 POST 接口。诊断要调 LLM，超时给得宽一些。"""
    import httpx

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{API_BASE}{path}", json=payload)
        if resp.status_code == 200:
            return resp.json()
        st.error(f"接口返回 {resp.status_code}：{resp.text[:300]}")
    except Exception as exc:  # noqa: BLE001
        st.error(f"调用后端失败（{API_BASE}）：{exc}")
    return None


@st.cache_data(ttl=30, show_spinner=False)
def load_llm_status() -> dict:
    """LLM 配置状态。缓存 30 秒，不必每次刷新都问。"""
    import httpx

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{API_BASE}/api/llm/status")
        if resp.status_code == 200:
            return resp.json()
    except Exception:  # noqa: BLE001
        pass
    return {"configured": False, "target": "未知", "note": "后端不可达"}


def render_diagnosis(result: dict) -> None:
    """把诊断结果渲染成结构化 UI。"""
    analysis = result.get("analysis")
    intervals = result.get("intervals") or []

    # ---- 本地识别结果（无论 LLM 成功与否都有）----
    if intervals:
        st.markdown("**本地识别到的异常区间**")
        for i, iv in enumerate(intervals, 1):
            sig = iv.get("suggested_signature") or {}
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 2, 3])
                c1.markdown(f"**{i}. {iv.get('kind_label', '异常')}**")
                c1.caption(f"{iv.get('start_time', '')} → "
                           f"{iv.get('end_time', '')}（{iv.get('duration_text', '')}）")
                c2.markdown(f"规则匹配：**{sig.get('label', '—')}**")
                if "current" in iv:
                    c = iv["current"]
                    c2.caption(f"电流 {c.get('peak')}A "
                               f"= {c.get('peak_multiple_of_rated')}× 额定")
                if "leakage" in iv:
                    lk = iv["leakage"]
                    c2.caption(f"漏电峰值 {lk.get('peak_ma')}mA"
                               f"（基线 {lk.get('baseline_before_event_ma')}mA）")
                if "voltage" in iv:
                    v = iv["voltage"]
                    c2.caption(f"电压最低 {v.get('min')}V"
                               f"（偏差 {v.get('min_deviation_percent')}%）")
                c3.caption(f"形态：{(iv.get('current') or {}).get('shape', '—')}")
                if iv.get("within_normal_range"):
                    c3.caption(f"段内正常：{', '.join(iv['within_normal_range'])}")

    # ---- LLM 未成功 ----
    if not result.get("ok"):
        err = result.get("error") or "未知原因"
        if "未配置" in err:
            st.info(err)
        elif "没有识别到异常区间" in err:
            st.info(err)
        else:
            st.error(f"分析失败：{err}")
        for p in result.get("validation_problems") or []:
            st.caption(f"⚠️ {p}")
        return

    # ---- LLM 结论 ----
    a = analysis or {}
    st.markdown("---")
    st.markdown("### 🤖 LLM 成因分析")

    m1, m2, m3 = st.columns(3)
    m1.metric("判定成因", a.get("cause_label", "—"))
    m2.metric("置信度", f"{(a.get('confidence') or 0) * 100:.0f}%")
    m3.metric("严重程度", a.get("severity", "—"))

    if a.get("is_normal_phenomenon"):
        st.success("✅ 模型判断这是**正常现象**，不是故障 —— 不应触发告警")

    if a.get("reasoning"):
        st.markdown("**判断依据**")
        st.markdown(a["reasoning"])

    ca, cb = st.columns(2)
    with ca:
        if a.get("evidence"):
            st.markdown("**引用证据**")
            for e in a["evidence"]:
                st.markdown(f"- {e}")
    with cb:
        if a.get("actions"):
            st.markdown("**处置建议**")
            for i, act in enumerate(a["actions"], 1):
                st.markdown(f"{i}. {act}")

    if a.get("uncertainties"):
        st.markdown("**模型指出的不确定项**")
        for u in a["uncertainties"]:
            st.caption(f"• {u}")

    llm = result.get("llm") or {}
    st.caption(
        f"模型 {llm.get('model', '—')} · 耗时 {llm.get('latency_ms', 0)}ms · "
        f"tokens {llm.get('prompt_tokens', 0)}+{llm.get('completion_tokens', 0)} · "
        f"分析于 {result.get('analyzed_at', '')}"
    )
    for p in result.get("validation_problems") or []:
        st.caption(f"⚠️ 输出校验：{p}")


# ============================ 侧边栏 ============================
st.sidebar.title("⚡ 智慧用电 AI")
st.sidebar.caption("智能断路器异常检测平台")

page = st.sidebar.radio("视图", ["📊 实时监控", "🔔 告警中心", "📋 设备台账"])

st.sidebar.divider()
hours = st.sidebar.select_slider(
    "数据窗口", options=[1, 2, 6, 12, 24, 72], value=6, format_func=lambda h: f"最近 {h} 小时"
)
refresh = st.sidebar.selectbox(
    "自动刷新", [0, 5, 10, 30, 60], index=1, format_func=lambda s: "关闭" if s == 0 else f"{s} 秒"
)

# ============================ 概览指标 ============================
try:
    summary = query(
        """
        SELECT
          (SELECT count(*) FROM devices)                                AS devices,
          (SELECT count(*) FROM readings)                               AS readings,
          (SELECT count(*) FROM alerts WHERE resolved_at IS NULL)       AS open_alerts,
          (SELECT count(*) FROM alerts
            WHERE detected_at > now() - INTERVAL '24 hours')            AS alerts_24h,
          (SELECT max(recorded_at) FROM readings)                       AS latest
        """
    ).iloc[0]
except Exception as exc:  # noqa: BLE001
    st.error(f"数据库连接失败：{exc}")
    st.info("检查 .env 里的 DB_PASSWORD 是否与数据库容器一致。")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("接入设备", f"{int(summary['devices'])} 台")
c2.metric("累计读数", f"{int(summary['readings']):,} 条")
c3.metric("未处理告警", f"{int(summary['open_alerts'])} 条")
c4.metric("近 24h 告警", f"{int(summary['alerts_24h'])} 条")

if pd.notna(summary["latest"]):
    st.caption(f"最新数据时间：{pd.to_datetime(summary['latest']):%Y-%m-%d %H:%M:%S}")

st.divider()


# ============================ 视图一：实时监控 ============================
if page == "📊 实时监控":
    devices = query("SELECT id, device_sn, name FROM devices ORDER BY id")
    if devices.empty:
        st.warning("还没有设备数据。确认 simulator 容器在运行。")
        st.stop()

    label_map = {r.id: f"{r.name}（{r.device_sn[-4:]}）" for r in devices.itertuples()}

    picked = st.multiselect(
        "选择设备", options=list(label_map), default=list(label_map)[:4],
        format_func=lambda i: label_map[i],
    )
    if not picked:
        st.info("至少选一台设备。")
        st.stop()

    # ---------- 电流曲线（用分钟聚合视图，避免拉原始表）----------
    curve = query(
        """
        SELECT device_id, bucket, avg_current, max_current, avg_voltage,
               avg_leakage, max_temperature
        FROM readings_1min
        WHERE bucket > now() - make_interval(hours => :hours)
          AND device_id = ANY(:ids)
        ORDER BY bucket
        """,
        hours=hours,
        ids=picked,
    )

    # ---------- 先取异常区间（曲线要在图上标出这些区间，所以必须先拿到）----------
    # 序列号 -> 区间列表。纯本地计算，不调 LLM。
    anomalies_by_sn: dict[str, list[dict]] = {}
    sn_by_id: dict[int, str] = {}
    for row in devices.itertuples():
        sn_by_id[row.id] = row.device_sn
    for did in picked:
        sn = sn_by_id.get(did)
        if not sn:
            continue
        anom = api_get(f"/api/devices/{sn}/anomalies?minutes={hours * 60}")
        if anom:
            anomalies_by_sn[sn] = anom.get("intervals") or []

    st.subheader(f"电流趋势（最近 {hours} 小时 · 分钟均值）")
    if curve.empty:
        st.info("这个时间窗口内还没有聚合数据。连续聚合视图每分钟刷新一次，稍等即可。")
    else:
        fig = go.Figure()
        for did in picked:
            sub = curve[curve["device_id"] == did]
            if sub.empty:
                continue
            fig.add_trace(
                go.Scatter(
                    x=sub["bucket"], y=sub["avg_current"], mode="lines",
                    name=label_map[did], line=dict(width=1.8),
                )
            )

        # ---- 异常区间底色标注：一眼看出"哪一段出了问题、是什么类型" ----
        # 注意时区：后端返回的是本地时间字符串，需要转成 tz-aware 才能和
        # 数据库读出的 tz-aware 时间戳对齐，否则矩形会画到 8 小时之外。
        tz = curve["bucket"].dt.tz
        marked = 0
        for sn, intervals in anomalies_by_sn.items():
            for iv in intervals:
                try:
                    x0 = pd.to_datetime(iv["start_time"])
                    x1 = pd.to_datetime(iv["end_time"])
                except (KeyError, ValueError, TypeError):
                    continue
                if x0.tzinfo is None and tz is not None:
                    x0, x1 = x0.tz_localize(tz), x1.tz_localize(tz)
                # 单点区间太窄，视觉上撑开一点，否则看不见
                if x1 <= x0:
                    x1 = x0 + pd.Timedelta(minutes=1)

                kind = (iv.get("kinds") or ["异常"])[0]
                fig.add_vrect(
                    x0=x0, x1=x1,
                    fillcolor=KIND_FILL.get(kind, "rgba(148,163,184,0.13)"),
                    line_width=0, layer="below",
                    annotation_text=iv.get("kind_label", kind),
                    annotation_position="top left",
                    annotation_font_size=10,
                )
                marked += 1
        if marked:
            st.caption(f"曲线底色标出了 {marked} 个异常区间；"
                       f"下方「异常区间标注」可查看细节并手动触发 LLM 成因分析")

        # 把告警点叠加在曲线之上 —— 一眼看出"哪台设备、什么时刻出的问题"
        alerts = query(
            """
            SELECT device_id, detected_at, alert_type, severity, value
            FROM alerts
            WHERE detected_at > now() - make_interval(hours => :hours)
              AND device_id = ANY(:ids)
              AND alert_type = '过载'
            ORDER BY detected_at
            """,
            hours=hours,
            ids=picked,
        )
        if not alerts.empty:
            fig.add_trace(
                go.Scatter(
                    x=alerts["detected_at"], y=alerts["value"], mode="markers",
                    name="过载告警",
                    marker=dict(color="#ef4444", size=9, symbol="x",
                                line=dict(width=1, color="white")),
                    hovertemplate="%{x|%m-%d %H:%M}<br>电流 %{y:.1f} A<extra>过载告警</extra>",
                )
            )

        fig.update_layout(
            height=380, margin=dict(l=10, r=10, t=10, b=10),
            yaxis_title="电流 (A)", xaxis_title=None,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
            hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True)

    # ================== 异常区间自动标注 + LLM 手动分析 ==================
    st.divider()
    st.subheader("🔍 异常区间标注")

    llm_status = load_llm_status()
    if not llm_status.get("configured"):
        st.caption(f"LLM 未配置（{llm_status.get('target')}）—— "
                   f"仍可查看异常区间，{llm_status.get('note', '')}")

    # 每台选中设备各算一次区间。这是纯本地计算，不花钱，所以可以随看板刷新。
    # 区间数据在上面画曲线底色时已经取过，这里直接复用，不重复调 API。
    total_intervals = 0
    for did in picked:
        sn = sn_by_id.get(did)
        if not sn:
            continue
        intervals = anomalies_by_sn.get(sn) or []
        total_intervals += len(intervals)
        if not intervals:
            continue

        st.markdown(f"**{label_map.get(did, sn)}** —— 识别到 {len(intervals)} 段异常")

        for idx, iv in enumerate(intervals, 1):
            sig = iv.get("suggested_signature") or {}
            with st.expander(
                f"{idx}. {iv.get('kind_label', '异常')} · "
                f"{iv.get('start_time', '')[11:16]} → {iv.get('end_time', '')[11:16]} · "
                f"{iv.get('duration_text', '')} · 规则匹配：{sig.get('label', '—')}",
                expanded=False,
            ):
                d1, d2, d3 = st.columns(3)
                if "current" in iv:
                    c = iv["current"]
                    d1.metric("电流峰值", f"{c.get('peak')} A",
                              f"{c.get('peak_multiple_of_rated')}× 额定")
                    d1.caption(f"形态 {c.get('shape')}｜"
                               f"超脱扣边界 {c.get('thermal_trip_a')}A："
                               f"{'是' if c.get('exceeds_thermal_trip') else '否'}")
                if "leakage" in iv:
                    lk = iv["leakage"]
                    d2.metric("漏电峰值", f"{lk.get('peak_ma')} mA",
                              f"基线 {lk.get('baseline_before_event_ma')} mA")
                    d2.caption(f"形态 {lk.get('shape')}｜涨幅 {lk.get('rise_ma')}mA")
                if "voltage" in iv:
                    v = iv["voltage"]
                    d3.metric("电压最低", f"{v.get('min')} V",
                              f"{v.get('min_deviation_percent')}%")
                    d3.caption(f"形态 {v.get('shape')}｜"
                               f"允许下限 {v.get('allowed_min')}V")
                if "temperature" in iv:
                    t = iv["temperature"]
                    d3.metric("温度峰值", f"{t.get('peak_c')} ℃",
                              f"+{t.get('rise_c')} ℃")
                if iv.get("within_normal_range"):
                    st.caption(f"段内保持正常的量：{', '.join(iv['within_normal_range'])}")
                st.caption(f"事件前基线：电流 "
                           f"{(iv.get('current') or {}).get('baseline_before_event', '—')}A｜"
                           f"电压 {(iv.get('voltage') or {}).get('baseline_before_event', '—')}V｜"
                           f"漏电 {(iv.get('leakage') or {}).get('baseline_before_event_ma', '—')}mA")

                # ---- 手动确认触发 LLM 分析 ----
                st.markdown("---")
                cache_key = f"diag::{sn}::{iv.get('start_time')}::{iv.get('end_time')}"
                cached = st.session_state.get(cache_key)

                btn_col, note_col = st.columns([1, 3])
                with btn_col:
                    btn_label = "📖 查看已有分析" if cached else "🤖 分析成因"
                    clicked = st.button(btn_label, key=f"btn::{cache_key}",
                                        disabled=not llm_status.get("configured"))
                with note_col:
                    if cached:
                        st.caption("该区间的分析结果已缓存，不会重复调用 API")
                    else:
                        st.caption(
                            "点击后将把**该时段的电气量、设备参数与本地特征**"
                            "发送给 LLM 分析成因（会产生 API 费用）。"
                            "不会发送设备位置等敏感信息。"
                        )
                    if llm_status.get("configured"):
                        st.caption(f"目标：{llm_status.get('target')}")

                if not llm_status.get("configured"):
                    st.info("未配置 LLM_API_KEY，无法进行成因分析。"
                            "请在服务器 .env 中填写后重启 api 容器。")

                if clicked:
                    with st.spinner("正在调用 LLM 分析成因，通常需要 5-15 秒…"):
                        result = api_post(
                            f"/api/devices/{sn}/diagnose",
                            {"minutes": hours * 60, "include_alerts": True},
                        )
                    if result:
                        st.session_state[cache_key] = result

                if cached:
                    render_diagnosis(cached)

    if total_intervals == 0:
        st.info("所选设备在当前时间窗口内没有识别到异常区间。"
                "可以调大「数据窗口」，或注入一次故障后回来看。")

    # ---------- 漏电与温度 ----------
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("漏电电流")
        if not curve.empty:
            fig_l = go.Figure()
            for did in picked:
                sub = curve[curve["device_id"] == did]
                if sub.empty:
                    continue
                fig_l.add_trace(go.Scatter(x=sub["bucket"], y=sub["avg_leakage"],
                                           mode="lines", name=label_map[did],
                                           line=dict(width=1.6)))
            fig_l.add_hline(y=30, line_dash="dash", line_color="#ef4444",
                            annotation_text="30mA 人身安全线", annotation_position="top left")
            fig_l.update_layout(height=280, margin=dict(l=10, r=10, t=30, b=10),
                                yaxis_title="漏电 (mA)", showlegend=False)
            st.plotly_chart(fig_l, use_container_width=True)

    with col_b:
        st.subheader("温度")
        if not curve.empty:
            fig_t = go.Figure()
            for did in picked:
                sub = curve[curve["device_id"] == did]
                if sub.empty:
                    continue
                fig_t.add_trace(go.Scatter(x=sub["bucket"], y=sub["max_temperature"],
                                           mode="lines", name=label_map[did],
                                           line=dict(width=1.6)))
            fig_t.add_hline(y=55, line_dash="dash", line_color="#ef4444",
                            annotation_text="55℃ 告警线", annotation_position="top left")
            fig_t.update_layout(height=280, margin=dict(l=10, r=10, t=30, b=10),
                                yaxis_title="温度 (℃)", showlegend=False)
            st.plotly_chart(fig_t, use_container_width=True)

    # ---------- 最新读数 ----------
    st.subheader("各设备最新读数")
    latest = query(
        """
        SELECT DISTINCT ON (d.id)
               d.name, d.device_sn, r.recorded_at, r.current, r.voltage,
               r.leakage, r.temperature, r.switch_state
        FROM devices d
        LEFT JOIN readings r ON r.device_id = d.id
        ORDER BY d.id, r.recorded_at DESC
        """
    )
    if not latest.empty:
        latest = latest.assign(
            状态=latest["switch_state"].map({1: "合闸", 0: "分闸"}).fillna("未知"),
            时间=pd.to_datetime(latest["recorded_at"]).dt.strftime("%H:%M:%S"),
        )
        st.dataframe(
            latest[["name", "device_sn", "时间", "current", "voltage",
                    "leakage", "temperature", "状态"]]
            .rename(columns={"name": "设备", "device_sn": "序列号",
                             "current": "电流A", "voltage": "电压V",
                             "leakage": "漏电mA", "temperature": "温度℃"}),
            use_container_width=True, hide_index=True,
        )


# ============================ 视图二：告警中心 ============================
elif page == "🔔 告警中心":
    by_type = query(
        """
        SELECT alert_type, count(*) AS cnt, max(severity) AS max_sev
        FROM alerts WHERE detected_at > now() - make_interval(hours => :hours)
        GROUP BY alert_type ORDER BY cnt DESC
        """,
        hours=hours,
    )
    left, right = st.columns([1, 2])

    with left:
        st.subheader("告警类型分布")
        if by_type.empty:
            st.info("该时间窗口内没有告警。")
        else:
            fig_p = go.Figure(
                go.Bar(
                    x=by_type["cnt"], y=by_type["alert_type"], orientation="h",
                    marker_color=["#ef4444", "#f97316", "#f59e0b", "#3b82f6"][: len(by_type)],
                    text=by_type["cnt"], textposition="outside",
                )
            )
            fig_p.update_layout(height=300, margin=dict(l=10, r=30, t=10, b=10),
                                xaxis_title="告警条数", yaxis_title=None)
            st.plotly_chart(fig_p, use_container_width=True)

    with right:
        st.subheader("告警趋势（按小时）")
        trend = query(
            """
            SELECT date_trunc('hour', detected_at) AS h, count(*) AS cnt
            FROM alerts WHERE detected_at > now() - make_interval(hours => :hours)
            GROUP BY h ORDER BY h
            """,
            hours=hours,
        )
        if trend.empty:
            st.info("暂无趋势数据。")
        else:
            fig_tr = go.Figure(
                go.Scatter(x=trend["h"], y=trend["cnt"], mode="lines+markers",
                           fill="tozeroy", line=dict(color="#ef4444", width=2))
            )
            fig_tr.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                                 yaxis_title="告警数", xaxis_title=None)
            st.plotly_chart(fig_tr, use_container_width=True)

    st.divider()
    st.subheader("告警明细")

    filter_col1, filter_col2 = st.columns([1, 1])
    with filter_col1:
        type_options = ["全部"] + sorted(
            query("SELECT DISTINCT alert_type FROM alerts")["alert_type"].tolist()
        )
        pick_type = st.selectbox("按类型过滤", type_options)
    with filter_col2:
        only_open = st.checkbox("只看未处理", value=False)

    sql = """
        SELECT a.detected_at, d.name AS 设备, d.device_sn AS 序列号,
               a.alert_type AS 类型, a.severity, a.value, a.threshold,
               a.reason AS 判定理由, a.resolved_at
        FROM alerts a JOIN devices d ON d.id = a.device_id
        WHERE a.detected_at > now() - make_interval(hours => :hours)
    """
    if pick_type != "全部":
        sql += " AND a.alert_type = :atype"
    if only_open:
        sql += " AND a.resolved_at IS NULL"
    sql += " ORDER BY a.detected_at DESC LIMIT 300"

    detail = query(sql, hours=hours, **({"atype": pick_type} if pick_type != "全部" else {}))
    if detail.empty:
        st.info("没有符合条件的告警。")
    else:
        detail = detail.assign(
            时间=pd.to_datetime(detail["detected_at"]).dt.strftime("%m-%d %H:%M:%S"),
            级别=detail["severity"].map(SEVERITY_LABEL),
        )
        st.dataframe(
            detail[["时间", "设备", "序列号", "类型", "级别", "value",
                    "threshold", "判定理由", "resolved_at"]]
            .rename(columns={"value": "触发值", "threshold": "阈值",
                             "resolved_at": "已处理时间"}),
            use_container_width=True, hide_index=True,
        )
        st.caption(f"共 {len(detail)} 条（最多显示 300 条）")


# ============================ 视图三：设备台账 ============================
else:
    st.subheader("设备健康度")

    health = query(
        """
        SELECT d.id, d.device_sn, d.name, d.location, d.rated_current,
               d.firmware, d.last_seen_at,
               (SELECT count(*) FROM alerts a
                 WHERE a.device_id = d.id AND a.resolved_at IS NULL)      AS open_alerts,
               (SELECT count(*) FROM alerts a
                 WHERE a.device_id = d.id
                   AND a.detected_at > now() - INTERVAL '24 hours')       AS alerts_24h,
               (SELECT count(*) FROM readings r
                 WHERE r.device_id = d.id)                                AS total_readings,
               (SELECT max(r.leakage) FROM readings r
                 WHERE r.device_id = d.id)                                AS peak_leakage,
               (SELECT max(r.temperature) FROM readings r
                 WHERE r.device_id = d.id)                                AS peak_temp
        FROM devices d ORDER BY d.id
        """
    )

    if health.empty:
        st.info("还没有设备。确认 simulator 在运行。")
    else:
        total = len(health)
        at_risk = int((health["open_alerts"] > 0).sum())
        d1, d2, d3 = st.columns(3)
        d1.metric("设备总数", f"{total} 台")
        d2.metric("有未处理告警", f"{at_risk} 台")
        d3.metric("健康设备", f"{total - at_risk} 台")

        display = health.assign(
            状态=["⚠️ 待处理" if n > 0 else "✅ 正常" for n in health["open_alerts"]],
            最近上报=pd.to_datetime(health["last_seen_at"]).dt.strftime("%m-%d %H:%M:%S"),
        )
        st.dataframe(
            display[["device_sn", "name", "状态", "open_alerts", "alerts_24h",
                     "total_readings", "peak_leakage", "peak_temp", "最近上报"]]
            .rename(columns={
                "device_sn": "序列号", "name": "设备名称",
                "open_alerts": "未处理告警", "alerts_24h": "近24h告警",
                "total_readings": "累计读数", "peak_leakage": "漏电峰值mA",
                "peak_temp": "温度峰值℃",
            }),
            use_container_width=True, hide_index=True,
        )

# ============================ 自动刷新 ============================
if refresh:
    import time

    time.sleep(refresh)
    st.rerun()
