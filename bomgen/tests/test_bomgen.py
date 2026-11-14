"""不变量测试(无外部依赖, 可直接运行: python3.11 tests/test_bomgen.py)。

覆盖:
- 合法配置完整可发布, 汇总数值与手工核算一致
- 每个汇总零件可由发生贡献重新累加(六个配置全部)
- 跨模块冲突(接口不一致 / 双模式同开)被违反并阻断
- 未定选项记缺口且不藏物料, 约束未决给依据
- 激活环阻断; 潜在环未激活不阻断; 复用同一定义不是环
- 交换定义输入顺序不改变规范结果
- 改变一个局部选项只影响其作用域及相关约束
- 共享子装配在不同局部覆盖下不串配置(缓存键正确性)
"""
import copy
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bomgen.build import run_build, verify_result
from bomgen.model import load_config, load_config_data, load_json, load_model, load_model_data

FIX = Path(__file__).resolve().parents[2] / "fixtures"
ALL_CONFIGS = ["valid", "both_turbo", "conflicting_ports",
               "unknown_turbo", "active_cycle", "inactive_cycle"]


def build(config_name):
    model, md = load_model(FIX / "definitions.json", FIX / "options.json",
                           FIX / "constraints.json")
    assert not md, f"模型校验不应有诊断: {[d.to_json() for d in md]}"
    cfg, cd = load_config(model, FIX / "configs" / f"{config_name}.json")
    assert not cd, f"配置校验不应有诊断: {[d.to_json() for d in cd]}"
    return run_build(model, cfg, md + cd)


def parts_map(result):
    return {p["part"]: p for p in result["parts"]}


def occ_paths(result):
    return {o["path"] for o in result["occurrences"]}


def diag_codes(result):
    return [d["code"] for d in result["diagnostics"]]


# ---------------------------------------------------------------- 合法配置

def test_valid_complete_and_totals():
    r = build("valid")
    assert r["status"] == "complete" and r["releasable"], r["diagnostics"]
    assert r["diagnostics"] == []
    pm = parts_map(r)
    # 手工核算: left(steel,turbo=true) qty1; right(painted) qty2
    assert pm["shell"]["total"] == 3        # left.frame 1 + right.frame 2
    assert pm["coat"]["total"] == 4         # 仅 right(finish=painted): 2*2
    assert pm["motor_a"]["total"] == 3      # interface=A: left 1 + right 2
    assert pm["bolt"]["total"] == 14        # left.drive 4 + right.drive 8 + left.turbo 2
    assert pm["rotor"]["total"] == 1        # 仅 left.turbo
    assert "motor_b" not in pm and "service_kit" not in pm
    assert all(c["status"] == "satisfied" for c in r["constraints"])


def test_reaggregation_all_configs():
    for name in ALL_CONFIGS:
        r = build(name)
        problems = verify_result(r)
        assert not problems, f"{name}: {problems}"
        for p in r["parts"]:
            assert sum(c["qty"] for c in p["contributions"]) == p["total"]


# ---------------------------------------------------------------- 冲突配置

def test_both_turbo_violated():
    r = build("both_turbo")
    assert r["status"] == "partial" and not r["releasable"]
    assert "CONSTRAINT_VIOLATED" in diag_codes(r)
    c = next(c for c in r["constraints"] if c["id"] == "not_both_turbo")
    assert c["status"] == "violated"
    pm = parts_map(r)
    assert pm["rotor"]["total"] == 3 and pm["bolt"]["total"] == 18  # 两侧 turbo 都发生


def test_conflicting_ports_violated():
    r = build("conflicting_ports")
    assert r["status"] == "partial"
    c = next(c for c in r["constraints"] if c["id"] == "ports_match")
    assert c["status"] == "violated"
    assert c["basis"]["left"]["value"] == "A" and c["basis"]["right"]["value"] == "B"
    pm = parts_map(r)
    assert pm["motor_b"]["total"] == 2 and pm["motor_a"]["total"] == 1  # 右侧 interface=B


# ---------------------------------------------------------------- 未定选项

def test_unknown_turbo_gaps_and_pending():
    r = build("unknown_turbo")
    assert r["status"] == "partial"
    gaps = [d for d in r["diagnostics"] if d["code"] == "GAP_UNKNOWN_CONDITION"]
    # 受影响路径被记录, 不多不少
    assert {d["path"] for d in gaps} == {"root.left.turbo", "root.right.turbo"}
    assert all(d["basis"]["unknown_options"] == ["turbo"] for d in gaps)
    # 未确定的边不展开: rotor/bolt 的 turbo 贡献不出现, 但已确定物料仍导出
    paths = occ_paths(r)
    assert "root.left.turbo" not in paths and "root.right.turbo" not in paths
    pm = parts_map(r)
    assert "rotor" not in pm
    assert pm["bolt"]["total"] == 12 and pm["shell"]["total"] == 3
    # 约束未决且给出依据
    c = next(c for c in r["constraints"] if c["id"] == "not_both_turbo")
    assert c["status"] == "pending"
    assert c["basis"]["left"]["status"] == "unknown"
    assert "CONSTRAINT_PENDING" in diag_codes(r)
    # 已确定的接口约束仍正常满足
    assert next(c for c in r["constraints"] if c["id"] == "ports_match")["status"] == "satisfied"


# ---------------------------------------------------------------- 环

def test_active_cycle_blocked():
    r = build("active_cycle")
    assert r["status"] == "partial"
    cyc = [d for d in r["diagnostics"] if d["code"] == "CYCLE_ACTIVE"]
    assert len(cyc) == 1 and cyc[0]["path"] == "root.service.again"
    loop = [e["definition"] for e in cyc[0]["basis"]["cycle"]]
    assert loop == ["service", "service"]
    # 已确定的发生(service_kit)仍导出
    assert parts_map(r)["service_kit"]["total"] == 1


def test_inactive_cycle_not_blocked():
    r = build("inactive_cycle")
    assert r["status"] == "complete", r["diagnostics"]  # 潜在环未激活不阻断
    assert "CYCLE_ACTIVE" not in diag_codes(r)
    assert parts_map(r)["service_kit"]["total"] == 1


def test_reuse_is_not_cycle():
    # module/frame/drive 在 left 与 right 两处复用: 两次发生都完整展开,
    # 不存在全局 visited 跳过第二次发生
    r = build("valid")
    paths = occ_paths(r)
    for p in ("root.left.frame.shell", "root.right.frame.shell",
              "root.left.drive.bolt", "root.right.drive.bolt"):
        assert p in paths, p
    assert "CYCLE_ACTIVE" not in diag_codes(r)


# ---------------------------------------------------------------- 顺序无关

def test_definition_order_independence():
    defs = load_json(FIX / "definitions.json")
    opts = load_json(FIX / "options.json")
    cons = load_json(FIX / "constraints.json")
    cfg_data = load_json(FIX / "configs" / "valid.json")
    baseline = json.dumps(build("valid"), ensure_ascii=False, sort_keys=True)
    for seed in range(6):
        defs2 = copy.deepcopy(defs)
        items = list(defs2.items())
        random.Random(seed).shuffle(items)
        defs2 = dict(items)
        for body in defs2.values():  # 子项顺序也不应影响规范结果
            if isinstance(body, dict) and isinstance(body.get("children"), list):
                random.Random(seed + 100).shuffle(body["children"])
        model, md = load_model_data(defs2, opts, cons)
        cfg, cd = load_config_data(model, copy.deepcopy(cfg_data), "valid")
        r = run_build(model, cfg, md + cd)
        assert json.dumps(r, ensure_ascii=False, sort_keys=True) == baseline, f"seed={seed}"


# ---------------------------------------------------------------- 局部覆盖作用域

def test_instance_override_scope_confinement():
    base = build("valid")
    model, md = load_model(FIX / "definitions.json", FIX / "options.json",
                           FIX / "constraints.json")
    cfg_data = load_json(FIX / "configs" / "valid.json")
    cfg_data["instance_overrides"]["root.left"]["turbo"] = False  # 只改 root.left 一处
    cfg, cd = load_config_data(model, cfg_data, "valid_left_turbo_off")
    r = run_build(model, cfg, md + cd)

    base_paths, new_paths = occ_paths(base), occ_paths(r)
    removed = base_paths - new_paths
    added = new_paths - base_paths
    assert removed and all(p.startswith("root.left.turbo") for p in removed), removed
    assert not added, added
    # 右侧与接口约束完全不受影响
    assert {p for p in new_paths if p.startswith("root.right")} == \
           {p for p in base_paths if p.startswith("root.right")}
    assert [c["status"] for c in r["constraints"]] == [c["status"] for c in base["constraints"]]
    pm = parts_map(r)
    assert "rotor" not in pm and pm["bolt"]["total"] == 12


def test_edge_override_scope_confinement():
    defs = load_json(FIX / "definitions.json")
    # 把 product.left 的边覆盖 finish: steel -> painted, 只应影响 left 子树的 coat
    left = next(c for c in defs["product"]["children"] if c["id"] == "left")
    left["overrides"]["finish"] = "painted"
    opts = load_json(FIX / "options.json")
    cons = load_json(FIX / "constraints.json")
    cfg_data = load_json(FIX / "configs" / "valid.json")
    model, md = load_model_data(defs, opts, cons)
    cfg, cd = load_config_data(model, cfg_data, "valid_left_painted")
    r = run_build(model, cfg, md + cd)
    base = build("valid")

    added = occ_paths(r) - occ_paths(base)
    assert added == {"root.left.frame.coat"}, added
    pm = parts_map(r)
    assert pm["coat"]["total"] == 2 + 4  # left.frame.coat 2 + right.frame.coat 4
    assert {c["path"] for c in pm["coat"]["contributions"]} == \
           {"root.left.frame.coat", "root.right.frame.coat"}
    # 其余零件与约束不变
    for name, p in parts_map(base).items():
        if name != "coat":
            assert pm[name] == p, name
    assert [c["status"] for c in r["constraints"]] == [c["status"] for c in base["constraints"]]


# ---------------------------------------------------------------- 缓存与共享子装配

def test_shared_definition_no_config_bleed():
    # 同一 module/frame 定义在不同局部覆盖(steel/painted)下展开结果不同,
    # 若按定义编号缓存会串配置: coat 只能出现在 painted 的 right 下
    r = build("valid")
    paths = occ_paths(r)
    assert "root.right.frame.coat" in paths
    assert "root.left.frame.coat" not in paths
    # 接口选项沿路径继承: 两侧 drive 都选中 motor_a
    assert "root.left.drive.motor_a" in paths and "root.right.drive.motor_a" in paths
    bolt = parts_map(r)["bolt"]
    assert {c["path"] for c in bolt["contributions"]} == \
           {"root.left.drive.bolt", "root.right.drive.bolt", "root.left.turbo.bolt"}


def test_cache_hit_consistency():
    # 同一 (定义, 有效选项状态) 的两次发生: 第二次命中缓存, 结果必须与现场展开一致
    defs = {
        "top": {"kind": "assembly", "children": [
            {"id": "a1", "ref": "sub", "qty": 2},
            {"id": "a2", "ref": "sub", "qty": 3},
        ]},
        "sub": {"kind": "assembly", "children": [
            {"id": "w", "ref": "widget", "qty": 2},
        ]},
        "widget": {"kind": "part", "unit": "piece"},
    }
    model, md = load_model_data(defs, {"flag": [False, True]}, [])
    cfg, cd = load_config_data(model, {"root_definition": "top", "root_id": "root",
                                       "options": {}}, "twin")
    r = run_build(model, cfg, md + cd)
    assert r["status"] == "complete", r["diagnostics"]
    pm = parts_map(r)
    assert pm["widget"]["total"] == 2 * 2 + 3 * 2
    assert {c["path"]: c["qty"] for c in pm["widget"]["contributions"]} == \
           {"root.a1.w": 4, "root.a2.w": 6}


def test_cache_does_not_hide_cycle():
    # 定义图含潜在环 a<->b; 配置激活后, 缓存不得掩盖环
    defs = {
        "top": {"kind": "assembly", "children": [{"id": "x", "ref": "a", "qty": 1}]},
        "a": {"kind": "assembly", "children": [
            {"id": "go", "ref": "b", "qty": 1, "when": {"eq": [{"option": "loop"}, True]}},
        ]},
        "b": {"kind": "assembly", "children": [
            {"id": "back", "ref": "a", "qty": 1, "when": {"eq": [{"option": "loop"}, True]}},
        ]},
    }
    model, md = load_model_data(defs, {"loop": [False, True]}, [])
    cfg, cd = load_config_data(model, {"root_definition": "top", "root_id": "root",
                                       "options": {"loop": True}}, "loop_on")
    r = run_build(model, cfg, md + cd)
    assert "CYCLE_ACTIVE" in diag_codes(r)
    cfg2, cd2 = load_config_data(model, {"root_definition": "top", "root_id": "root",
                                         "options": {"loop": False}}, "loop_off")
    r2 = run_build(model, cfg2, md + cd2)
    assert r2["status"] == "complete", r2["diagnostics"]


# ---------------------------------------------------------------- 运行器

def _main():
    tests = sorted((k, v) for k, v in globals().items() if k.startswith("test_"))
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
