"""Command line interface: mailarch ingest / mailarch verify."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .export import ingest
from .verify import verify


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="mailarch",
        description="离线 EML 邮件档案整理:生成规范元数据、资源与静态 HTML 索引")
    parser.add_argument("--version", action="version",
                        version=f"mailarch {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="读取 EML 目录,导出档案")
    pi.add_argument("src", help="包含 .eml 文件的目录")
    pi.add_argument("out", help="输出目录(manifest.json + assets + HTML)")

    pv = sub.add_parser("verify", help="依据导出清单独立核对档案")
    pv.add_argument("out", help="ingest 输出目录")
    pv.add_argument("--src", help="可选:原始 EML 目录,一并核对来源字节")

    args = parser.parse_args(argv)
    if args.cmd == "ingest":
        try:
            manifest = ingest(args.src, args.out)
        except FileNotFoundError as exc:
            print(f"ingest: {exc}", file=sys.stderr)
            return 2
        t = manifest["threads"]
        print(f"ingest: {manifest['source']['file_count']} 个文件 -> "
              f"{len(manifest['messages'])} 封顶层邮件 "
              f"(+{len(manifest['attached_messages'])} 封附件邮件), "
              f"{len(t['conversations'])} 个会话, "
              f"{len(manifest['assets'])} 个资源")
        print(f"会话诊断 {len(t['diagnostics'])} 条, "
              f"MIME 解析问题 {len(manifest['mime_issues'])} 条 "
              f"(详见 manifest.json 与 index.html)")
        print(f"输出目录: {args.out}")
        return 0

    ok, problems = verify(args.out, args.src)
    if ok:
        print("verify: OK — 清单、资源字节摘要、页面与会话证据全部一致")
        return 0
    print(f"verify: 发现 {len(problems)} 个问题", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
