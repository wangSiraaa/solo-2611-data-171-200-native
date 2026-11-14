#!/usr/bin/env python3
"""独立 VLQ/mappings 预言机（与 Node 实现完全独立编写，仅用于交叉核验）。

读取一张 Source Map v3（basic 或含 inline sections 的 indexed），
解码全部 mappings，打印每个片段的生成列与（可选的）原始位置。
Node 侧报告的片段表应与本脚本输出逐字段一致。
"""
import json
import sys

B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
IDX = {c: i for i, c in enumerate(B64)}


def decode_vlqs(text):
    out = []
    i = 0
    while i < len(text):
        result = 0
        shift = 0
        while True:
            digit = IDX[text[i]]
            i += 1
            result += (digit & 31) << shift
            shift += 5
            if not (digit & 32):
                break
        negative = result & 1
        result >>= 1
        out.append(-result if negative else result)
    return out


def decode_mappings(mappings):
    lines = []
    src = sline = scol = name = 0  # 源相关增量跨行保持
    for raw_line in mappings.split(";"):
        gen_col = 0  # 每个生成行重置生成列
        segments = []
        for token in raw_line.split(","):
            if token == "":
                continue
            vals = decode_vlqs(token)
            gen_col += vals[0]
            seg = {"genCol": gen_col, "fields": len(vals)}
            if len(vals) >= 4:
                src += vals[1]
                sline += vals[2]
                scol += vals[3]
                seg.update(sourceIndex=src, sourceLine=sline, sourceColumn=scol)
                if len(vals) == 5:
                    name += vals[4]
                    seg["nameIndex"] = name
            segments.append(seg)
        lines.append(segments)
    return lines


def emit(title, mappings):
    print(f"== {title} ==")
    for li, segs in enumerate(decode_mappings(mappings)):
        if not segs:
            print(f"  line {li}: <空，整行无映射>")
        for s in segs:
            extra = ""
            if "sourceIndex" in s:
                extra = f" -> src#{s['sourceIndex']} {s['sourceLine']}:{s['sourceColumn']}"
                if "nameIndex" in s:
                    extra += f" name#{s['nameIndex']}"
            print(f"  line {li} genCol {s['genCol']} fields={s['fields']}{extra}")


def main(path):
    data = json.load(open(path, encoding="utf-8"))
    if "sections" in data:
        for i, sec in enumerate(data["sections"]):
            emit(f"{path} section#{i} offset={sec['offset']}", sec["map"]["mappings"])
    else:
        emit(path, data["mappings"])


if __name__ == "__main__":
    main(sys.argv[1])
