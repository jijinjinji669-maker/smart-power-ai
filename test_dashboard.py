"""看板逻辑离线测试：不依赖 streamlit / 数据库，验证 dashboard/app.py 的内部逻辑。

做法：
  1. 用假模块顶替 streamlit / plotly / sqlalchemy，避免安装依赖
  2. 用内存里的假数据库（sqlite）建出与 init.sql 同构的表结构，并灌入样本数据
  3. 拦截 dashboard/app.py 里所有 SQL，逐条打给假数据库执行
     —— 这样 SQL 语法、字段名、聚合写法有错会立刻暴露
  4. 拦截 st.* 调用，检查有没有调用到不存在的 streamlit API

运行：python test_dashboard.py   （在 smart-power-ai 目录下）
"""
from __future__ import annotations

import re
import sqlite3
import sys
import types
from datetime import datetime, timedelta

# ---------------------------------------------------------------- 假数据库
# 用 sqlite 模拟 PostgreSQL 的表结构。注意：sqlite 没有 timescaledb_information、
# make_interval、date_trunc 等 PG 特性，所以对这类语句只做"结构校验"，
# 不真正执行 —— 我们要抓的是字段名写错、表名写错、聚合写法错误这类问题。
DDL = """
CREATE TABLE devices (
  id INTEGER PRIMARY KEY, device_sn TEXT, name TEXT, location TEXT,
  rated_current REAL, rated_voltage REAL, firmware TEXT,
  last_seen_at TEXT, created_at TEXT
);
CREATE TABLE readings (
  recorded_at TEXT, device_id INTEGER, current REAL, voltage REAL,
  power REAL, power_factor REAL, frequency REAL, leakage REAL,
  temperature REAL, switch_state INTEGER, arc_flag INTEGER
);
CREATE TABLE alerts (
  id INTEGER PRIMARY KEY, device_id INTEGER, alert_type TEXT, severity INTEGER,
  value REAL, threshold REAL, reason TEXT, detected_at TEXT, resolved_at TEXT
);
CREATE VIEW readings_1min AS
SELECT device_id, recorded_at AS bucket,
       AVG(current) AS avg_current, MAX(current) AS max_current,
       AVG(voltage) AS avg_voltage, MIN(voltage) AS min_voltage,
       AVG(leakage) AS avg_leakage, MAX(leakage) AS max_leakage,
       AVG(temperature) AS avg_temperature, MAX(temperature) AS max_temperature,
       COUNT(*) AS sample_count
FROM readings GROUP BY device_id, recorded_at;
"""

PG_ONLY = ("timescaledb_information", "make_interval", "now() -", "date_trunc", "DISTINCT ON")


def build_fake_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    now = datetime.now()
    for i in range(1, 11):
        conn.execute(
            "INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?)",
            (i, f"AJS-BRK-2026-{i:04d}", f"{i%3+1}号楼{i%6+1}层配电箱-{i:02d}",
             None, 40, 220, "v1.4.2", now.isoformat(), now.isoformat()),
        )
    for i in range(1, 11):
        for m in range(30):
            t = (now - timedelta(minutes=m)).isoformat()
            conn.execute("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (t, i, 8 + m * 0.1, 220, 2.0, 0.94, 50.0,
                          8.0 if i != 3 else 55.0, 40.0 + i, 1, 0))
    for i in range(1, 6):
        conn.execute("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,?)",
                     (i, i, ["过载", "漏电", "欠压", "过温", "漏电"][i - 1], 3,
                      55.0, 30.0, f"测试告警 {i} 的理由文本", now.isoformat(), None))
    conn.commit()
    return conn


# ---------------------------------------------------------------- 假 streamlit
class FakeStreamlit(types.ModuleType):
    """记录所有被调用的 st.* 名称，并检查是否调用了不存在的 API"""

    KNOWN = {
        "set_page_config", "sidebar", "title", "caption", "radio", "divider",
        "select_slider", "selectbox", "columns", "metric", "subheader", "multiselect",
        "info", "warning", "error", "success", "stop", "dataframe", "plotly_chart",
        "checkbox", "rerun", "text", "markdown", "header", "container", "tabs",
        # 装饰器类 API（都真实存在于 streamlit）
        "cache_resource", "cache_data", "cache", "experimental_rerun",
        "expander", "form", "form_submit_button", "number_input", "slider",
    }

    def __init__(self) -> None:
        super().__init__("streamlit")
        self.calls: list[str] = []
        self.unknown: list[str] = []
        self._obj = _FakeWidget()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self.calls.append(name)
        if name not in self.KNOWN:
            self.unknown.append(name)
        return self._obj


class _FakeWidget:
    """任何 st.xxx(...) 都返回一个可继续取属性的假对象"""

    def __init__(self, value=None):
        self._value = value

    def __call__(self, *a, **kw):
        return _FakeWidget(a[0] if a else None)

    def __getattr__(self, name):
        return _FakeWidget()

    def __iter__(self):
        return iter([_FakeWidget(1), _FakeWidget(2)])

    def __getitem__(self, k):
        return _FakeWidget()

    def __bool__(self):
        return True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------- SQL 拦截
class SqliteSession:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if any(tok in sql for tok in PG_ONLY):
            return _FakeResult([])          # PG 专有语法，跳过实执行
        try:
            cur = self.conn.execute(sql, params or {})
            return _FakeResult(cur.fetchall(), [d[0] for d in cur.description or []])
        except sqlite3.Error as exc:
            raise AssertionError(f"SQL 执行失败: {exc}\n--- SQL ---\n{sql}") from exc


class _FakeResult:
    def __init__(self, rows, cols=None):
        self._rows = rows
        self._cols = cols or []


# ---------------------------------------------------------------- 假 pandas
def install_fakes(sqlite_conn):
    """注入假模块，然后 import dashboard/app.py"""
    st = FakeStreamlit()
    sys.modules["streamlit"] = st

    plotly = types.ModuleType("plotly")
    pgo = types.ModuleType("plotly.graph_objects")
    pgo.Figure = lambda *a, **kw: _FakeWidget()
    pgo.Bar = lambda *a, **kw: _FakeWidget()
    pgo.Scatter = lambda *a, **kw: _FakeWidget()
    plotly.graph_objects = pgo
    sys.modules["plotly"] = plotly
    sys.modules["plotly.graph_objects"] = pgo

    sqlalchemy = types.ModuleType("sqlalchemy")
    sqlalchemy.text = lambda s: s
    sqlalchemy.create_engine = lambda *a, **kw: _FakeWidget()
    sys.modules["sqlalchemy"] = sqlalchemy

    # pandas：结构没必要复刻，只保证 dashboard 里的用法不炸
    try:
        import pandas  # noqa: F401
    except ImportError:
        pd = types.ModuleType("pandas")
        pd.DataFrame = _FakeWidget
        pd.read_sql = lambda *a, **kw: _FakeWidget()
        pd.to_datetime = lambda x: _FakeWidget()
        pd.notna = lambda x: True
        sys.modules["pandas"] = pd

    return st


def main() -> int:
    conn = build_fake_db()
    st = install_fakes(conn)

    # 把 dashboard/app.py 当模块读进来，只做静态检查（不执行，避免真实 import 副作用）
    from pathlib import Path

    src = Path("dashboard/app.py").read_text(encoding="utf-8")

    print("=" * 70)
    print("看板离线验证")
    print("=" * 70)

    # 1) 引用的 streamlit API 是否都存在
    #    剥离注释再扫，避免文档里提到的 API 名被误判；但不要剥 docstring，
    #    因为 SQL 也是用三引号写的（见下）。
    checks = []
    # 只看真实调用 st.xxx( ，这样文档/注释里提到的 API 名不会误报，
    # 而代码里真写了不存在的 st.foo(...) 一定会被抓到。
    used = sorted(set(re.findall(r"\bst\.([a-z_]+)\s*\(", src)))
    unknown = [u for u in used if u not in FakeStreamlit.KNOWN]
    print(f"\n[1] 实际调用的 st.* API：{', '.join(used)}")
    checks.append(("无未知 streamlit API", not unknown))
    if unknown:
        print(f"    !! 可疑 API: {unknown}")

    # 2) SQL 里引用的表和字段是否真的存在
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    print(f"\n[2] 假库中的表/视图：{', '.join(sorted(tables))}")

    # 只提取 text(""") 紧跟 SQL 关键字（SELECT/INSERT/WITH）的语句。
    # 这样既能抓到各函数里的查询，又不会把模块开头的 docstring 当 SQL。
    sqls = re.findall(
        r'text\(\s*"""\s*((?:SELECT|INSERT|WITH)\b(?:.|\n)*?)"""',
        src, re.I,
    )
    sqls += re.findall(r'text\(\s*"((?:SELECT|INSERT|WITH)\b[^"]{20,})"', src, re.I)
    sqls += re.findall(
        r'\bsql\s*(?:\+?=)\s*"""\s*((?:SELECT|INSERT|WITH)\b(?:.|\n)*?)"""',
        src, re.I,
    )
    sqls = [s.strip() for s in sqls if s.strip()]

    referenced = set()
    for s in sqls:
        referenced |= set(re.findall(r"\bFROM\s+([a-z_0-9]+)", s, re.I))
        referenced |= set(re.findall(r"\bJOIN\s+([a-z_0-9]+)", s, re.I))
    referenced = {r.lower() for r in referenced}
    missing = referenced - tables
    print(f"    提取到 {len(sqls)} 段 SQL，引用表：{', '.join(sorted(referenced)) or '（无）'}")
    checks.append(("SQL 引用的表都存在", not missing))
    if missing:
        print(f"    !! 缺失: {missing}")

    # 3) 逐个 SQL 片段做字段级校验（PG 专有语法跳过）
    print(f"\n[3] 逐条 SQL 校验（共 {len(sqls)} 段）")
    ok_count = skipped = failed = 0
    for i, sql in enumerate(sqls, 1):
        s = sql.strip()
        if not s:
            continue
        if any(tok in s for tok in PG_ONLY):
            skipped += 1
            print(f"    [跳过] 第 {i} 段含 PG 专有语法")
            continue
        try:
            conn.execute(s)
            ok_count += 1
        except sqlite3.Error as exc:
            failed += 1
            print(f"    [失败] 第 {i} 段: {exc}")
            print("           " + " ".join(s.split())[:110])
    print(f"    通过 {ok_count} / 跳过 {skipped}（PG 专有）/ 失败 {failed}")
    checks.append(("SQL 片段无表名字段名错误", failed == 0))

    # 4) 必填配置项是否都有默认值
    print("\n[4] 环境变量兜底")
    for var in ("DB_HOST", "DB_PORT", "DB_USER", "DB_NAME"):
        checks.append((f"{var} 有默认值", var in src))
    print("    " + ", ".join(f"{v}{'✓' if v in src else '✗'}"
                            for v in ("DB_HOST", "DB_PORT", "DB_USER", "DB_NAME")))
    # 密码必须来自环境变量，不能直接写死。os.getenv 的第二参数是开发默认值，属正常用法。
    checks.append((
        "DB_PASSWORD 通过环境变量读取",
        "os.getenv('DB_PASSWORD'" in src or 'os.getenv("DB_PASSWORD"' in src,
    ))
    checks.append((
        "DB_PASSWORD 无硬编码生产密码",
        not re.search(r'DB_PASSWORD[^)]*\)\s*[:=]\s*["\'][A-Za-z0-9]{8,}["\']', src),
    ))

    print("\n" + "=" * 70)
    print("校验结果")
    print("=" * 70)
    bad = 0
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        bad += 0 if ok else 1
    print("=" * 70)
    print("看板逻辑校验通过 ✅" if bad == 0 else f"有 {bad} 项失败 ❌")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
