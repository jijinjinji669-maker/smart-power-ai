#!/usr/bin/env python3
"""在服务器上应用 Decimal 序列化修复。逐处精确替换，每步断言成功。

背景：asyncpg 对 PostgreSQL 的 NUMERIC 列返回 decimal.Decimal，
而 Decimal 不能被 json.dumps 序列化，拼 prompt 时会抛
"Object of type Decimal is not JSON serializable"。
"""
import ast
import pathlib
import sys

root = pathlib.Path.home() / "smart-power-ai"
changed: list[str] = []

# ---------------- 修复 1：main.py 的 _f() 显式转 float ----------------
p = root / "app" / "main.py"
src = p.read_text(encoding="utf-8")

old_f = '''def _f(value) -> float | None:
    return None if value is None else float(value)'''
new_f = '''def _f(value) -> float | None:
    """把数据库返回的 NUMERIC 转成原生 float。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None'''

if old_f in src:
    src = src.replace(old_f, new_f)
    changed.append("main.py: _f() 显式转 float")
elif "return None if value is None else float(value)" not in src:
    print("   main.py 的 _f() 已修复过")
else:
    print("!! main.py 未找到 _f() 目标代码", file=sys.stderr)
    sys.exit(1)

# ---------------- 修复 2：main.py 告警字段显式转换 ----------------
old_alerts = '''        alerts = [
            {**dict(r), "detected_at": r["detected_at"].isoformat()} for r in rows
        ]'''
new_alerts = '''        alerts = [
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
        ]'''

if old_alerts in src:
    src = src.replace(old_alerts, new_alerts)
    changed.append("main.py: 告警字段显式转换")
elif '"severity": int(r["severity"])' in src:
    print("   main.py 的告警转换已修复过")
else:
    print("!! main.py 未找到告警构造代码", file=sys.stderr)
    sys.exit(1)

p.write_text(src, encoding="utf-8")

# ---------------- 修复 3：diagnosis.py 的 json.dumps 加 default=str ----------------
p2 = root / "app" / "diagnosis.py"
src2 = p2.read_text(encoding="utf-8")
old_dump = "+ json.dumps(payload, ensure_ascii=False, indent=2)"
new_dump = "+ json.dumps(payload, ensure_ascii=False, indent=2, default=str)"

if old_dump in src2:
    src2 = src2.replace(old_dump, new_dump)
    p2.write_text(src2, encoding="utf-8")
    changed.append("diagnosis.py: json.dumps 加 default=str")
elif "default=str" in src2:
    print("   diagnosis.py 已修复过")
else:
    print("!! diagnosis.py 未找到 json.dumps 调用", file=sys.stderr)
    sys.exit(1)

# ---------------- 语法校验 ----------------
for rel in ("app/main.py", "app/diagnosis.py"):
    ast.parse((root / rel).read_text(encoding="utf-8"))
    print(f"   ✓ {rel} 语法通过")

print()
if changed:
    print("已应用：")
    for c in changed:
        print(f"  ✓ {c}")
else:
    print("无需修改（全部已修复）")
