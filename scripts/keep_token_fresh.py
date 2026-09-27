#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保持仓库 Secret WORKBUDDY_TOKEN 常新，让云端定时任务永不过期。

先读这段：为什么不是"纯云端互刷"
--------------------------------
客户端里确实有一条续期链路，我已经把它挖出来了：

    POST /v2{auth.prefixPath}/auth/token/refresh
    Header: X-Refresh-Token: <refreshToken>
    Header: X-Auth-Refresh-Source: plugin

但它的 **base URL 来自远端下发的产品配置**（`authentication.endpoint` /
`attributes.prefixPath`），本机登录态文件里没有；实测 www.codebuddy.cn、
api.codebuddy.cn、console.codebuddy.cn 下的全部候选路径都是 404。
也就是说"云端拿着一枚 refreshToken 自己续期"这条路当前走不通。

于是换一个更简单、也更稳的思路：
WorkBuddy 桌面端大约每 24 小时自动刷新一次会话，并把**新签发**的 accessToken
写回本机登录态文件 `workbuddy-desktop.info` —— 该令牌有效期约 55 天。
只要本机偶尔开着客户端，这个文件里就永远躺着一枚"新鲜"的令牌。

本脚本只做一件事：**发现本机令牌变了，就把它同步进仓库 Secret。**
配合每日计划任务，效果等同于自动续期。

用法
----
    python3 scripts/keep_token_fresh.py --check          # 只看状态，不写任何东西
    python3 scripts/keep_token_fresh.py --push           # 令牌有变化才同步
    python3 scripts/keep_token_fresh.py --push --force   # 无条件同步一次
    python3 scripts/keep_token_fresh.py --install-task   # 注册本地计划任务（每日+登录时）
    python3 scripts/keep_token_fresh.py --uninstall-task

安全
----
- 令牌只经 stdin 管道交给 `gh secret set`，不落盘、不进命令行参数、不进 git。
- 状态文件只记录指纹（首尾 4 位 + 长度）与时间，**不含明文令牌**。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from export_token import (  # noqa: E402
    ensure_gh_auth, find_login_state, gh_path, load_token,
    token_expiry, token_fingerprint,
)

SECRET_NAME = "WORKBUDDY_TOKEN"
STATE_PATH = os.path.join(os.path.expanduser("~"), ".workbuddy",
                          "buddy-station-token-sync.json")
TASK_DAILY = "BuddyStationTokenSync-Daily"
TASK_LOGON = "BuddyStationTokenSync-Logon"
DAILY_AT = "09:00"   # 早于云端任务（09:30），确保当天用的是新令牌


# ===== 状态文件（只存指纹，不存令牌） =====
def read_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def write_state(data: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


# ===== 仓库推断 =====
def infer_repo() -> str:
    gh = gh_path()
    if not gh:
        return ""
    try:
        out = subprocess.run([gh, "repo", "view", "--json", "nameWithOwner",
                              "-q", ".nameWithOwner"],
                             capture_output=True, text=True, timeout=20)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


# ===== 推送 Secret =====
def push_secret(token: str, repo: str) -> bool:
    if not ensure_gh_auth():
        return False
    gh = gh_path()
    cmd = [gh, "secret", "set", SECRET_NAME]
    if repo:
        cmd += ["--repo", repo]
    proc = subprocess.run(cmd, input=token, capture_output=True, text=True)
    if proc.returncode != 0:
        print("[error] 写入 Secret 失败：%s"
              % (proc.stderr.strip() or proc.stdout.strip()), file=sys.stderr)
        return False
    return True


# ===== 计划任务（PowerShell cmdlet，避免 schtasks 引号地狱） =====
def _powershell(script: str) -> tuple[int, str]:
    """执行 PowerShell。注意 Windows 下其输出非 UTF-8（通常是 GBK），必须容错解码。"""
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _task_exists(name: str) -> bool:
    rc, out = _powershell(
        "if (Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue) "
        "{ Write-Output PRESENT } else { Write-Output ABSENT }" % name)
    return "PRESENT" in out


def install_task(script_path: str, repo: str) -> int:
    """注册计划任务。注意：注册后必须回查确认，否则会把失败当成功。"""
    python_exe = sys.executable
    args = '"%s" --push' % script_path
    if repo:
        args += " --repo %s" % repo
    rc_all = 0
    for name, trigger in (
        (TASK_DAILY, "New-ScheduledTaskTrigger -Daily -At %s" % DAILY_AT),
        (TASK_LOGON, "New-ScheduledTaskTrigger -AtLogOn"),
    ):
        ps = (
            "$a = New-ScheduledTaskAction -Execute '{py}' -Argument '{ar}'; "
            "$t = {tr}; "
            "Register-ScheduledTask -TaskName '{nm}' -Action $a -Trigger $t "
            "-Description 'Sync WorkBuddy token to GitHub Secret' -Force "
            "-ErrorAction Stop | Out-Null"
        ).format(py=python_exe, ar=args, tr=trigger, nm=name)
        rc, out = _powershell(ps)
        if rc == 0 and _task_exists(name):          # 回查，不信 rc
            print("  ✅ 已注册计划任务：%s" % name)
        else:
            rc_all = 1
            detail = out.strip().splitlines()[0] if out.strip() else "（无输出）"
            print("  ❌ 注册失败：%s → %s" % (name, detail[:160]), file=sys.stderr)
    if rc_all == 0:
        print("\n已完成。触发时机：每天 %s + 每次登录。" % DAILY_AT)
        print("移除：python3 scripts/keep_token_fresh.py --uninstall-task")
    else:
        print("\n[提示] 本机环境可能禁止注册计划任务（如受限沙箱）。", file=sys.stderr)
        print("       可改用 WorkBuddy 自动化每日定时运行：", file=sys.stderr)
        print("         python3 scripts/keep_token_fresh.py --push", file=sys.stderr)
    return rc_all


def uninstall_task() -> int:
    rc_all = 0
    for name in (TASK_DAILY, TASK_LOGON):
        rc, out = _powershell(
            "Unregister-ScheduledTask -TaskName '%s' -Confirm:$false "
            "-ErrorAction SilentlyContinue; Write-Output OK" % name)
        if rc == 0:
            print("  ✅ 已移除：%s" % name)
        else:
            rc_all = 1
            print("  ⚠️ 移除异常：%s → %s" % (name, out[:160]), file=sys.stderr)
    return rc_all


# ===== 主流程 =====
def main() -> int:
    ap = argparse.ArgumentParser(description="保持 WORKBUDDY_TOKEN Secret 常新")
    ap.add_argument("--check", action="store_true", help="只报告状态，不做任何写入")
    ap.add_argument("--push", action="store_true", help="令牌有变化时同步到仓库 Secret")
    ap.add_argument("--force", action="store_true", help="无条件同步，忽略指纹比对")
    ap.add_argument("--repo", default="", help="目标仓库 owner/name，缺省自动推断")
    ap.add_argument("--install-task", action="store_true", help="注册本地计划任务")
    ap.add_argument("--uninstall-task", action="store_true", help="移除本地计划任务")
    args = ap.parse_args()

    script_path = os.path.abspath(__file__)

    if args.install_task:
        repo = args.repo or infer_repo()
        print("注册本地计划任务（仓库：%s）" % (repo or "自动推断"))
        return install_task(script_path, repo)
    if args.uninstall_task:
        return uninstall_task()

    # --- 读本机登录态 ---
    path = find_login_state()
    if not path:
        print("[error] 未找到本机登录态，请先在 WorkBuddy 客户端登录。", file=sys.stderr)
        return 1
    token, domain, auth = load_token(path)
    fp = token_fingerprint(token)
    state = read_state()

    print("=== WorkBuddy 令牌同步 ===")
    print("  登录态      : %s" % path)
    print("  accessToken : %s" % fp)
    print("  有效期      : %s" % token_expiry(token))
    rtx = auth.get("refreshToken") or ""
    if rtx:
        print("  refreshToken: %s（有效期 %s）"
              % (token_fingerprint(rtx), token_expiry(rtx)))
    last_fp = state.get("fingerprint", "")
    last_at = state.get("pushed_at", 0)
    print("  上次同步    : %s"
          % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_at))
             if last_at else "（从未）"))

    changed = bool(args.force) or (fp != last_fp)
    if args.check and not args.push:
        print("\n  结论：%s"
              % ("令牌已更新，建议同步（加 --push）" if changed else "令牌未变，Secret 已是最新"))
        return 0

    if not args.push:
        print("\n[提示] 未指定动作。常用：--check / --push / --install-task")
        return 0

    if not changed:
        print("\n  令牌未变，跳过同步（如需强制请加 --force）")
        return 0

    repo = args.repo or infer_repo()
    if push_secret(token, repo):
        state.update({"fingerprint": fp, "pushed_at": int(time.time()),
                      "repo": repo, "expiry": token_expiry(token)})
        write_state(state)
        print("\n  ✅ 已同步到 %s 的 Secret %s" % (repo or "当前仓库", SECRET_NAME))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
