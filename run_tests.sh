#!/usr/bin/env bash
# 运行全部单元测试（仅依赖标准库 unittest 与已安装的 mido）。
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD:${PYTHONPATH:-}:$PWD/tests"
./.venv/bin/python -m unittest discover -s tests -v
