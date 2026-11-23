"""geometry 模块的单元测试：python3 -I test_geometry.py"""
import geometry


def make(photos, pieces, board=(100, 100)):
    return {
        "board": {"width": board[0], "height": board[1]},
        "photos": photos,
        "pieces": pieces,
    }


def photo(pid, permit, credit="某"):
    return {"id": pid, "credit": credit,
            "permit": {"x": permit[0], "y": permit[1],
                       "w": permit[2] - permit[0], "h": permit[3] - permit[1]}}


def piece(pid, crop, at):
    return {"photo": pid,
            "crop": {"x": crop[0], "y": crop[1],
                     "w": crop[2] - crop[0], "h": crop[3] - crop[1]},
            "at": {"x": at[0], "y": at[1]}}


def row(report, pid):
    return next(r for r in report["rows"] if r["id"] == pid)


# 1. 单片完全在可刊矩形内：无缺口，可生成 CSV
r = analyze = geometry.analyze(make(
    [photo("P1", (0, 0, 50, 50), "甲")],
    [piece("P1", (10, 10, 40, 40), (0, 0))]))
assert row(r, "P1")["exposed"] == 900
assert row(r, "P1")["unlicensed"] == 0
assert r["clean"] and "甲" in r["csv"] and "P1" in r["csv"]

# 2. 露出超出可刊矩形：缺口面积与示例坐标
r = geometry.analyze(make(
    [photo("P1", (0, 0, 10, 10))],
    [piece("P1", (0, 0, 20, 20), (0, 0))]))
rw = row(r, "P1")
assert rw["exposed"] == 400 and rw["unlicensed"] == 300
px, py = rw["point"]
assert 0 <= px < 20 and 0 <= py < 20          # 在露出范围内
assert not (0 <= px < 10 and 0 <= py < 10)      # 且在可刊矩形外
assert not r["clean"] and r["csv"] is None

# 3. 上层裁片遮住下层：被遮部分不计
r = geometry.analyze(make(
    [photo("P1", (0, 0, 100, 100)), photo("P2", (0, 0, 100, 100))],
    [piece("P1", (0, 0, 30, 30), (0, 0)),      # 下层
     piece("P2", (0, 0, 20, 20), (10, 10))]))  # 上层，落在 P1 的 (10,10)-(30,30)
assert row(r, "P1")["exposed"] == 900 - 400
assert row(r, "P2")["exposed"] == 400

# 4. 同一原片两片、裁框不同：换回原片坐标后重叠区域只算一次
r = geometry.analyze(make(
    [photo("P1", (0, 0, 100, 100))],
    [piece("P1", (0, 0, 10, 10), (0, 0)),
     piece("P1", (5, 5, 15, 15), (5, 5))]))
assert row(r, "P1")["exposed"] == 100 + 100 - 25  # 并集 175

# 4b. 同一原片两片、裁框相同：换回原片坐标后完全重叠，只算一次
r = geometry.analyze(make(
    [photo("P1", (0, 0, 100, 100))],
    [piece("P1", (0, 0, 10, 10), (0, 0)),
     piece("P1", (0, 0, 10, 10), (5, 5))]))
assert row(r, "P1")["exposed"] == 100

# 5. 裁片越出展板：只计展板内部分
r = geometry.analyze(make(
    [photo("P1", (0, 0, 100, 100))],
    [piece("P1", (0, 0, 30, 30), (90, 90))]))
assert row(r, "P1")["exposed"] == 100

# 6. 落版位置映射回原片坐标：缺口坐标应在原片坐标系
r = geometry.analyze(make(
    [photo("P1", (100, 100, 115, 115))],
    [piece("P1", (100, 100, 120, 120), (5, 5))]))
rw = row(r, "P1")
assert rw["exposed"] == 400 and rw["unlicensed"] == 400 - 225
px, py = rw["point"]
assert 100 <= px < 120 and 100 <= py < 120
assert not (100 <= px < 115 and 100 <= py < 115)

# 7. 未露出的原片不进 CSV；整页干净时 CSV 只列露出者
r = geometry.analyze(make(
    [photo("P1", (0, 0, 10, 10), "甲"), photo("P2", (0, 0, 10, 10), "乙"),
     photo("P3", (0, 0, 10, 10), "丙")],
    [piece("P1", (0, 0, 10, 10), (0, 0)),
     piece("P2", (0, 0, 10, 10), (0, 0))]))  # P2 完全盖住 P1
assert r["clean"]
assert "P2" in r["csv"] and "P1" not in r["csv"] and "P3" not in r["csv"]

# 8. 大画布不分配像素内存：100000x100000 也能算
r = geometry.analyze(make(
    [photo("P1", (0, 0, 100000, 100000))],
    [piece("P1", (0, 0, 50000, 50000), (50000, 50000))],
    board=(100000, 100000)))
assert row(r, "P1")["exposed"] == 50000 * 50000

# 9. 输入校验
for bad in [
    {},
    {"board": {"width": 10, "height": 10}, "photos": [], "pieces": []},
    make([photo("P1", (0, 0, 10, 10))], [piece("PX", (0, 0, 5, 5), (0, 0))]),
    make([photo("P1", (0, 0, 10, 10))], [piece("P1", (0, 0, 5.5, 5), (0, 0))]),
]:
    try:
        geometry.analyze(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"应当拒绝：{bad!r}")

print("全部测试通过")
