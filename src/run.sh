#!/usr/bin/env bash
# 一键复现：v1 全量布局 -> v2 增量布局 -> 独立核验 -> 对照 HTML
set -euo pipefail
cd "$(dirname "$0")"
PY=python3

echo "== [1/5] v1 全量层次布局 =="
$PY -m depsvg.cli layout \
  --nodes ../fixtures/nodes-v1.json --edges ../fixtures/edges-v1.csv \
  --plan ../fixtures/display-plan.json \
  --svg ../outputs/v1.svg --json ../outputs/v1.json

echo "== [2/5] v2 增量布局（读取 v1 坐标 JSON） =="
$PY -m depsvg.cli layout \
  --nodes ../fixtures/nodes-v2.json --edges ../fixtures/edges-v2.csv \
  --plan ../fixtures/display-plan.json --old-json ../outputs/v1.json \
  --svg ../outputs/v2.svg --json ../outputs/v2.json

echo "== [3/5] 独立核验 v1 / v2（解析实际 SVG 文本与 JSON 逐项比对） =="
$PY -m depsvg.cli verify \
  --nodes ../fixtures/nodes-v1.json --edges ../fixtures/edges-v1.csv \
  --plan ../fixtures/display-plan.json \
  --svg ../outputs/v1.svg --json ../outputs/v1.json \
  --report ../outputs/v1-verify.json
$PY -m depsvg.cli verify \
  --nodes ../fixtures/nodes-v2.json --edges ../fixtures/edges-v2.csv \
  --plan ../fixtures/display-plan.json --old-json ../outputs/v1.json \
  --svg ../outputs/v2.svg --json ../outputs/v2.json \
  --report ../outputs/v2-verify.json

echo "== [4/5] 生成可直接打开的对照 HTML =="
$PY -m depsvg.cli report \
  --v1-json ../outputs/v1.json --v1-svg ../outputs/v1.svg \
  --v2-json ../outputs/v2.json --v2-svg ../outputs/v2.svg \
  --out ../outputs/compare.html

echo "== [5/5] 回归与对抗测试 =="
$PY ../tests/test_pipeline.py
echo "全部完成，产物见 ../outputs/"
