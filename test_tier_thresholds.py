"""验证检测器的分档阈值：过载预警起点应随额定电流变化。"""
import sys

sys.path.insert(0, ".")
from app.detector import DetectParams
from simulator.device import TIERS, TIER_AIRCON, TIER_LIGHTING, TIER_MAIN, TIER_SOCKET


def build_stream(minutes=240, tier_key="socket"):
    pass


def main():
    print("=" * 66)
    print("检测器分档阈值验证")
    print("=" * 66)
    print()
    print(f"  {'档位':<12}{'额定':>7}{'保持边界':>10}{'过载预警起点':>14}{'是否一致':>10}")

    base = DetectParams()
    all_ok = True
    for key in (TIER_LIGHTING, TIER_SOCKET, TIER_AIRCON, TIER_MAIN):
        spec = TIERS[key]
        params = base.with_rated_current(spec.rated_current)
        floor = params.overload_floor_a
        expected = 1.13 * spec.rated_current
        ok = abs(floor - expected) < 1e-9 and abs(floor - spec.thermal_hold) < 1e-9
        all_ok = all_ok and ok
        print(f"  {spec.label:<12}{spec.rated_current:>6.0f}A"
              f"{spec.thermal_hold:>9.1f}A{floor:>13.1f}A{'OK' if ok else 'FAIL':>10}")

    print()
    print("  说明：过载预警起点 = 1.13 x 额定电流（IEC 60898-1 热保护保持边界）")
    print("        该值原先被硬编码为 20 A 绝对值，对 16 A 与 63 A 档位都是错的。")
    print()
    print("=" * 66)
    print("分档阈值验证通过" if all_ok else "分档阈值验证失败")
    print("=" * 66)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
