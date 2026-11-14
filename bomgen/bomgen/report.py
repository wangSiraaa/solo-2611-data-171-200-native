"""可读报告(Markdown)。"""
from __future__ import annotations

import json

_STATUS_CN = {"satisfied": "满足", "violated": "违反", "pending": "未决"}


def render_report(result: dict) -> str:
    L: list[str] = []
    cfg = result["config"]
    s = result["summary"]
    L.append("# 配置BOM 报告")
    L.append("")
    L.append(f"- 根定义: `{result['root_definition']}`　根发生: `{result['root_id']}`　配置: `{cfg['name']}`")
    opts = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in cfg["options"].items())
    L.append(f"- 根作用域选项: {opts or '(空)'}")
    if cfg["instance_overrides"]:
        iov = "; ".join(f"`{p}` → {json.dumps(o, ensure_ascii=False, sort_keys=True)}"
                        for p, o in cfg["instance_overrides"].items())
        L.append(f"- 实例覆盖: {iov}")
    if result["releasable"]:
        L.append(f"- 状态: **完整可发布**(发生 {s['occurrences']} 项, 零件 {s['parts']} 种, 无阻断诊断)")
    else:
        L.append(f"- 状态: **partial — 不能发布完整BOM**"
                 f"(错误 {s['errors']} 项, 其中缺口 {s['gaps']} 项; 已确定的发生与物料仍导出)")
    L.append("")

    L.append("## 发生树(按有效配置展开, 数量沿路径相乘)")
    L.append("")
    for o in result["occurrences"]:
        depth = o["path"].count(".")
        indent = "　" * depth
        marks = ""
        if o.get("edge_overrides"):
            marks += f"　边覆盖: {json.dumps(o['edge_overrides'], ensure_ascii=False, sort_keys=True)}"
        if o.get("instance_overrides"):
            marks += f"　实例覆盖: {json.dumps(o['instance_overrides'], ensure_ascii=False, sort_keys=True)}"
        if o["kind"] == "part":
            L.append(f"{indent}- `{o['id']}` = 零件 **{o['ref']}** ×{o['qty']} {o['unit']}{marks}")
        else:
            L.append(f"{indent}- `{o['id']}` [{o['ref']}] 累计×{o['qty']}{marks}")
    L.append("")

    L.append("## 物料需求(同一零件汇总, 保留各发生贡献)")
    L.append("")
    if result["parts"]:
        L.append("| 零件 | 单位 | 总需求 | 发生贡献 |")
        L.append("|---|---|---|---|")
        for p in result["parts"]:
            contrib = "; ".join(f"`{c['path']}`×{c['qty']}" for c in p["contributions"])
            L.append(f"| {p['part']} | {p['unit']} | {p['total']} | {contrib} |")
    else:
        L.append("(无已确定的物料)")
    L.append("")

    L.append("## 跨装配约束(在同一配置实例上检查)")
    L.append("")
    if result["constraints"]:
        for c in result["constraints"]:
            line = f"- [{_STATUS_CN[c['status']]}] `{c['id']}` ({c['op']})"
            if c["status"] == "satisfied":
                line += (f"　{c['basis']['left']['path']}.{c['basis']['left']['option']}="
                         f"{json.dumps(c['basis']['left']['value'], ensure_ascii=False)}, "
                         f"{c['basis']['right']['path']}.{c['basis']['right']['option']}="
                         f"{json.dumps(c['basis']['right']['value'], ensure_ascii=False)}")
            else:
                line += f"　依据: {json.dumps(c['basis'], ensure_ascii=False, sort_keys=True)}"
            L.append(line)
    else:
        L.append("(无约束)")
    L.append("")

    L.append("## 诊断")
    L.append("")
    if result["diagnostics"]:
        for d in result["diagnostics"]:
            sev = "错误" if d["severity"] == "error" else "警告"
            path = f" `{d['path']}`" if d["path"] else ""
            line = f"- [{sev}][{d['code']}]{path} {d['message']}"
            if d.get("basis"):
                line += f"　依据: {json.dumps(d['basis'], ensure_ascii=False, sort_keys=True)}"
            L.append(line)
    else:
        L.append("(无诊断)")
    L.append("")
    if not result["releasable"]:
        L.append("> 存在错误级诊断: 缺口/激活环/约束违反或未决都会阻止声明完整可制造,")
        L.append("> 以上已确定的发生与物料按 partial 导出, 修复后可重新构建。")
        L.append("")
    return "\n".join(L)
