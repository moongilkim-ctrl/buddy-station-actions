#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本机 WorkBuddy 登录态里的 accessToken 注入 GitHub 仓库 Secret。

为什么要这个脚本
----------------
GitHub Actions 的云端 runner 上没有你的本机登录态，读不到
`CodeBuddyExtension/.../workbuddy-desktop.info`。所以需要在**本机**把 token 取出来，
写进仓库 Secret，Actions 再从 `secrets.WORKBUDDY_TOKEN` 读取。

安全约定
--------
- token 只经 stdin 管道传给 `gh secret set`，**不落盘、不进 shell 历史、不进 git**。
- `gh` 走 `command -v` 动态定位，不写死安装路径。
- 默认只打印 token 的指纹（长度 + 前后 4 位），要看全文必须显式加 `--reveal`。
- 绝不把 token 写进仓库文件；`--print-env` 仅用于本地临时测试，输出到终端即止。

用法
----
    # 1) 先登录 gh（只需一次）
    gh auth login

    # 2) 导出并写入仓库 Secret（在 git 仓库根目录执行）
    python3 scripts/export_token.py --push-to-secret

    # 指定仓库 / 自定义 secret 名
    python3 scripts/export_token.py --push-to-secret --repo yourname/buddy-station --secret-name WORKBUDDY_TOKEN

    # 只看指纹，不写任何东西
    python3 scripts/export_token.py --inspect

前置：本机已登录 WorkBuddy 客户端。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time

AUTH_DIRS = [
    os.path.join("CodeBuddyExtension", "Data", "Public", "auth"),
]
AUTH_FILENAMES = ["workbuddy-desktop.info"]


def find_login_state() -> str | None:
    candidates = []
    for env_key in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env_key)
        if base:
            for ext in AUTH_DIRS:
                candidates.append(os.path.join(base, ext))
    home = os.path.expanduser("~")
    candidates.append(os.path.join(home, "Library", "Application Support",
                                   "CodeBuddyExtension", "Data", "Public", "auth"))
    candidates.append(os.path.join(home, ".config", "CodeBuddyExtension", "Data", "Public", "auth"))
    candidates.append(os.path.join(home, ".workbuddy", "auth"))
    for d in candidates:
        for name in AUTH_FILENAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


def load_token(path: str) -> tuple[str, str, dict]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    auth = data.get("auth", {}) or {}
    token = (auth.get("accessToken") or "").strip()
    domain = (auth.get("domain") or "").strip()
    if not token:
        raise SystemExit("登录态里没有 accessToken，请先在 WorkBuddy 客户端登录。")
    return token, domain, auth


def token_fingerprint(token: str) -> str:
    if len(token) <= 10:
        return "(过短)"
    return "%s…%s（长度 %d）" % (token[:4], token[-4:], len(token))


def token_expiry(token: str) -> str:
    """从 JWT payload 读 exp，转成可读时间；解析不了就返回未知。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        if not exp:
            return "未知（payload 无 exp）"
        left = (exp - int(time.time())) / 86400
        return "%s（剩余约 %.1f 天）" % (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)), left)
    except Exception:  # noqa: BLE001
        return "未知（payload 解析失败）"


def gh_path() -> str | None:
    """动态定位 gh，不写死路径。"""
    return shutil.which("gh")


def ensure_gh_auth() -> bool:
    gh = gh_path()
    if not gh:
        print("[error] 未找到 gh CLI。请先安装：https://cli.github.com/", file=sys.stderr)
        return False
    proc = subprocess.run([gh, "auth", "status"], capture_output=True, text=True)
    if proc.returncode != 0:
        print("[error] gh 未登录，请先执行：gh auth login", file=sys.stderr)
        return False
    print("[info] gh 已登录：" + proc.stdout.strip().splitlines()[0])
    return True


def push_secret(token: str, secret_name: str, repo: str | None) -> bool:
    """经 stdin 管道写入 Secret——token 不出现在命令行参数里。"""
    if not ensure_gh_auth():
        return False
    gh = gh_path()
    cmd = [gh, "secret", "set", secret_name]
    if repo:
        cmd += ["--repo", repo]
    proc = subprocess.run(cmd, input=token, capture_output=True, text=True)
    if proc.returncode != 0:
        print("[error] 写入 Secret 失败：%s" % (proc.stderr.strip() or proc.stdout.strip()), file=sys.stderr)
        return False
    target = repo or "当前仓库（由 origin 推断）"
    print("[ok] 已把 %s 写入 %s 的 Actions Secret（值不会回显）" % (secret_name, target))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="导出本机 WorkBuddy token 并注入 GitHub 仓库 Secret")
    parser.add_argument("--push-to-secret", action="store_true", help="把 token 写入仓库 Actions Secret")
    parser.add_argument("--repo", default=None, help="目标仓库 owner/name，缺省用当前 git 仓库")
    parser.add_argument("--secret-name", default="WORKBUDDY_TOKEN", help="Secret 名称，默认 WORKBUDDY_TOKEN")
    parser.add_argument("--inspect", action="store_true", help="只打印指纹与有效期，不做任何写入")
    parser.add_argument("--reveal", action="store_true", help="打印 token 全文（谨慎，仅本地调试）")
    parser.add_argument("--print-env", action="store_true", help="以 shell 赋值形式打印，便于本地临时测试")
    args = parser.parse_args()

    path = find_login_state()
    if not path:
        print("[error] 未找到本机登录态，请先在 WorkBuddy 客户端登录。", file=sys.stderr)
        return 1
    token, domain, auth = load_token(path)
    print("[info] 登录态：%s" % path)
    print("[info] 域名：%s" % (domain or "(缺失，脚本将回退默认 www.codebuddy.cn)"))
    print("[info] token 指纹：%s" % token_fingerprint(token))
    print("[info] token 有效期：%s" % token_expiry(token))

    if any(v in auth for v in ("refreshToken",)):
        print("[info] 登录态含 refreshToken（本脚本不使用；Actions 仅用 accessToken）")

    if args.reveal:
        print("[warn] 以下为明文 token，仅供本地调试：" % ())
        print(token)
    if args.print_env:
        print("export WORKBUDDY_TOKEN='%s'" % token)
        print("export WORKBUDDY_DOMAIN='%s'" % (domain or "www.codebuddy.cn"))

    if args.push_to_secret:
        return 0 if push_secret(token, args.secret_name, args.repo) else 1

    if not (args.inspect or args.reveal or args.print_env):
        print("\n[提示] 未指定动作。常用：python3 scripts/export_token.py --push-to-secret")
        print("       token 到期后重新执行一次即可刷新 Secret（JWT 过期无法续期，只能重新导出）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
