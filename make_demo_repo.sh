#!/usr/bin/env bash
# 构造演示仓库并打成 bundle。覆盖案例:
#   重命名(含小改)、变更再撤销、分支合并、同文本独立引入、
#   手工合并解决、删除、二进制新增、歧义重命名(多个同等候选)
set -euo pipefail
cd "$(dirname "$0")"
rm -rf demo demo.bundle
git init -q -b main demo
cd demo
export GIT_AUTHOR_NAME="Demo Author" GIT_AUTHOR_EMAIL="demo@example.com"
export GIT_COMMITTER_NAME="Demo Author" GIT_COMMITTER_EMAIL="demo@example.com"

mkdir -p docs
cat > app.py <<'EOF'
def greeting():
    return "Hello, world"


def main():
    print(greeting())


if __name__ == "__main__":
    main()
EOF
cat > util.py <<'EOF'
def slugify(name):
    return name.strip().lower().replace(" ", "-")


def clamp(value, low, high):
    return max(low, min(high, value))
EOF
cat > README.md <<'EOF'
# Demo Service

A tiny demo service.

## Legacy section

This section describes the v1 deployment.
It is obsolete and will be removed.

## Usage

Run `python app.py`.
EOF
cat > docs/notes.txt <<'EOF'
Release checklist
- write changelog
- tag the release
- notify stakeholders
EOF
cat > config.ini <<'EOF'
[server]
host = 127.0.0.1
port = 8080
EOF
git add -A && git commit -qm "Initial import"
git tag v1.0

# A1: 新增实验性 metrics(稍后被撤销)
cat > app.py <<'EOF'
def greeting():
    return "Hello, world"


def metrics():
    return {"requests": 0, "errors": 0}


def main():
    print(greeting())
    print(metrics())


if __name__ == "__main__":
    main()
EOF
git commit -qam "Add experimental metrics block"

# A2: 撤销(变更再撤销案例: 净变更为零, 仅历史可见)
git revert --no-edit HEAD >/dev/null
A2=$(git rev-parse HEAD)

# A3: 重命名 util.py -> helpers.py 并加一行 docstring(重命名+小改)
git mv util.py helpers.py
cat > helpers.py <<'EOF'
def slugify(name):
    return name.strip().lower().replace(" ", "-")


def clamp(value, low, high):
    """Clamp value into the [low, high] range."""
    return max(low, min(high, value))
EOF
git commit -qam "Rename util.py to helpers.py and document clamp"

# A4: 删除 README 过时段落(删除案例: 需保留 base 路径与范围)
cat > README.md <<'EOF'
# Demo Service

A tiny demo service.

## Usage

Run `python app.py`.
EOF
git commit -qam "Remove obsolete legacy section from README"

# A5: 新增二进制文件(仅报告 blob 身份)
mkdir -p assets
printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x01\x00\x01\x08\x06' > assets/logo.bin
git add assets/logo.bin && git commit -qm "Add binary logo asset"

# A6: 删除 docs/notes.txt, 新增两个内容完全相同的副本(歧义重命名: 同等候选)
git rm -q docs/notes.txt
mkdir -p docs
for f in docs/notes-v2.txt docs/notes-archive.txt; do
cat > "$f" <<'EOF'
Release checklist
- write changelog
- tag the release
- notify stakeholders
EOF
done
git add docs && git commit -qm "Split release notes into v2 and archive copies"

# A7: main 上新增 tuning 配置(同文本独立引入, 主线一侧)
cat >> config.ini <<'EOF'

[tuning]
workers = 4
EOF
git commit -qam "Add tuning section to config"

# A8: main 上修改 greeting(将与 feature 冲突)
sed -i 's/Hello, world/Hello, main/' app.py
git commit -qam "Update greeting for mainline"

# F1/F2: feature 分支(从 A2 分出)
git checkout -q -b feature "$A2"
cat >> config.ini <<'EOF'

[tuning]
workers = 4
EOF
git commit -qam "Add tuning section to config (feature side)"
sed -i 's/Hello, world/Hello, feature/' app.py
git commit -qam "Update greeting for feature"

# M: 合并, greeting 冲突手工解决为 "Hello, everyone"(合并解决引入)
git checkout -q main
git merge --no-ff -m "Merge branch 'feature'" feature || true
cat > app.py <<'EOF'
def greeting():
    return "Hello, everyone"


def main():
    print(greeting())


if __name__ == "__main__":
    main()
EOF
git add app.py
git commit -qm "Merge branch 'feature' (resolve greeting conflict)"
git tag v2.0

cd ..
git -C demo bundle create "$PWD/demo.bundle" --all >/dev/null
git -C demo bundle verify "$PWD/demo.bundle"
echo "== 提交图 =="
git -C demo log --graph --oneline --all
