#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动触发 Buddy 加油站云端工作流（等价于外部 cron 服务所做的事）。

用途
----
本仓库是 **私有仓库 + 免费账号**，GitHub 原生 `schedule` 触发器不生效，
因此定时改由外部 cron 服务调用 GitHub dispatch API 完成。
本脚本就是那次调用的等价实现 —— 既可用来手工补跑，也可用来验证 PAT 权限是否正确。

用法
----
    export GH_CRON_PAT="<细粒度PAT，仅需 Actions: Read and write>"
    python3 scripts/trigger_dispatch.py                     # 触发正式运行
    python3 scripts/trigger_dispatch.py --dry-run           # 触发只读演练
    python3 scripts/trigger_dispatch.py --location 1        # 指定派遣地点
    python3 scripts/trigger_dispatch.py --check             # 只看权限是否可用，不触发

安全
----
PAT 只从环境变量读取，不回显、不落盘、不写入任何文件。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_REPO = "moongilkim-ctrl/buddy-station-actions"
WORKFLOW_FILE = "buddy-station.yml"
REF = "main"


def _mask(value: str) -> str:
    """回显用：只露首尾，中间打码。"""
    if not value:
        return "(未提供)"
    if len(value) <= 10:
        return value[:2] + "*" * (len(value) - 2)
    return "%s...%s（长度 %d）" % (value[:8], value[-4:], len(value))


def dispatch(repo: str, token: str, dry_run: bool, location: str) -> int:
    url = "https://api.github.com/repos/%s/actions/workflows/%s/dispatches" % (repo, WORKFLOW_FILE)
    payload: dict = {"ref": REF}
    inputs: dict = {}
    if dry_run:
        inputs["dry_run"] = "true"
    if location:
        inputs["location"] = location
    if inputs:
        payload["inputs"] = inputs

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "buddy-station-trigger/1.0")

    print("目标工作流 : %s" % url)
    print("请求体     : %s" % json.dumps(payload, ensure_ascii=False))
    print("PAT        : %s" % _mask(token))
    print()

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        detail = exc.read().decode("utf-8", "replace")[:300]
        print("❌ 触发失败：HTTP %s" % code)
        print("   %s" % detail)
        if code == 401:
            print("   排查：PAT 无效或已过期。")
        elif code == 403:
            print("   排查：PAT 缺少本仓库的 Actions: Read and write 权限。")
        elif code == 404:
            print("   排查：PAT 未授予本仓库访问权，或仓库名/工作流文件名有误。")
        return 1
    except Exception as exc:  # noqa: BLE001
        print("❌ 请求异常：%s" % exc)
        return 1

    if code == 204:
        print("✅ 已触发（HTTP 204，无正文即成功）")
        print("   查看运行：gh run list --repo %s --limit 3" % repo)
        return 0
    print("⚠️ 非预期状态码：HTTP %s" % code)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="触发 Buddy 加油站云端工作流")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo，默认 %s" % DEFAULT_REPO)
    parser.add_argument("--dry-run", action="store_true", help="触发只读演练（不发写请求）")
    parser.add_argument("--location", default="", help="派遣地点 1/2/3/4（留空随机）")
    parser.add_argument("--check", action="store_true", help="只校验 PAT 是否存在，不真正触发")
    args = parser.parse_args()

    token = os.environ.get("GH_CRON_PAT", "").strip()
    if not token:
        print("缺少环境变量 GH_CRON_PAT。请先创建细粒度 PAT（仅需本仓库 Actions: Read and write），然后：")
        print('  export GH_CRON_PAT="<你的PAT>" && python3 scripts/trigger_dispatch.py')
        print("详见 scripts/setup_external_cron.md")
        return 2

    if args.check:
        print("PAT 已提供：%s" % _mask(token))
        print("（--check 只做本地校验，未发起网络请求）")
        return 0

    return dispatch(args.repo, token, args.dry_run, args.location)


if __name__ == "__main__":
    raise SystemExit(main())
