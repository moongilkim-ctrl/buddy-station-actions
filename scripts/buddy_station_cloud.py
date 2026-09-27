#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy「Buddy 加油站」云端每日任务：签到 + 派猫猫旅行闭环。

设计要点（对应需求）
--------------------
1. 一次跑完：签到与派猫猫旅行在同一次运行内完成。
2. 先领后派：先领掉已到家的旅行积分；再判断能否派新的一趟；今日已派过（traveling /
   daily_limit_reached）则跳过派遣。
3. 结果落盘 + 写 GitHub Step Summary（运行结论看运行日志与 Step Summary，本项目不发任何通知）；
   落盘前经 `sanitize_for_output()` 脱敏，因为公开仓库的 artifact 任何人可下载。
4. 猫猫失败不影响签到结论：整体退出码只由签到决定（签到成功/今日已签 => 退出码 0）。

安全边界
--------
- 纯标准库，无第三方依赖。
- 云端无本机登录态：token 只从环境变量 `WORKBUDDY_TOKEN` 读取，不读取任何本地文件、不落盘。
- 任何输出（stdout / stderr / artifact / GitHub Step Summary）都不含明文 token，
  统一经 `redact()` 处理。
- **无邮件功能**：不含 SMTP 客户端、不读发信凭证、不写收件箱，从根上不存在邮箱泄漏面。
- 写操作仅 3 个已验证接口：daily-checkin / travel/claim / travel/depart。

接口（均为实测确认的真实路径，勿改用网上流传的其他名称）
----------------------------------------------------------
- POST /v2/billing/meter/checkin-activity-status   状态查询（域名 = 登录态 domain）
- POST /v2/billing/meter/daily-checkin             签到（幂等）
- GET  /activity/growth/buddy/travel/status        旅行状态
- GET  /activity/growth/buddy/travel/config        地点配置
- POST /activity/growth/buddy/travel/claim         领取旅行奖励
- POST /activity/growth/buddy/travel/depart        派出旅行
（后 4 个走 https://www.workbuddy.cn，路径不带 /v2 前缀）
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

# ===== 接口常量（真实路径，实测确认）=====
DEFAULT_DOMAIN = "www.codebuddy.cn"          # 登录态 auth.domain，可用 WORKBUDDY_DOMAIN 覆盖
TRAVEL_DOMAIN = "www.workbuddy.cn"           # 旅行接口固定走官网域

CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"

TRAVEL_STATE_TEXT = {
    "idle": "空闲（可派遣）",
    "traveling": "旅行中",
    "arrived": "已到达（待领取）",
}

CST = timezone(timedelta(hours=8))           # 报告用中国标准时间，便于对齐日常作息

# ===== 全局：脱敏与原始报文捕获 =====
_SECRETS: list[str] = []
RAW_CALLS: list[dict] = []


def register_secret(value: str | None) -> None:
    """登记需要在输出中屏蔽的敏感串（token 等）。"""
    if value and len(value) >= 8 and value not in _SECRETS:
        _SECRETS.append(value)


def redact(text) -> str:
    """屏蔽所有已登记敏感串；并兜底掩掉疑似 JWT 的长串。"""
    if text is None:
        return ""
    out = str(text)
    for secret in _SECRETS:
        if secret in out:
            out = out.replace(secret, "<REDACTED>")
    # 兜底：形如 xxx.yyy.zzz 的长 base64 JWT
    import re as _re
    out = _re.sub(r"\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\b",
                  "<JWT_REDACTED>", out)
    return out


def now_cst() -> datetime:
    return datetime.now(CST)


# 落盘/上 artifact 前需要抹掉的具体字段（公开仓库的 artifact 任何人可下载）
# 只保留「可证明流程跑通」的信息，不留下账号资产与个人标识。
_MASK = "<REDACTED>"
_SENSITIVE_KEYS = {
    # 账号资产
    "total_credits", "streak_days", "today_credit", "reward_credit",
    "record_id", "credited", "claimed_credit",
    # 个人标识
    "domain", "detail", "to", "from", "user", "location_name",
}
# 邮箱地址一律打码，无论出现在哪个字段
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def sanitize_for_output(node):
    """递归脱敏，用于写 result.json（公开仓库的 artifact 任何人可下载）。

    规则：
      · 键名命中 _SENSITIVE_KEYS → 整值替换为 <REDACTED>；
      · 字符串里出现邮箱 → 抹掉邮箱片段；
      · 原始 HTTP 报文（带 http_status 的条目）→ 整条只留 label 与状态码，
        不留 request/response —— 需要看报文时在本机用 `--print-raw`。
    """
    if isinstance(node, list):
        out = []
        for item in node:
            if isinstance(item, dict) and "http_status" in item:
                out.append({"label": item.get("label"), "http_status": item.get("http_status")})
            else:
                out.append(sanitize_for_output(item))
        return out
    if isinstance(node, dict):
        clean = {}
        for key, value in node.items():
            if key in _SENSITIVE_KEYS:
                clean[key] = _MASK
            elif isinstance(value, (dict, list)):
                clean[key] = sanitize_for_output(value)
            elif isinstance(value, str):
                clean[key] = _EMAIL_RE.sub(_MASK, value)
            else:
                clean[key] = value
        return clean
    return node


def fmt_local(value) -> str | None:
    """把 unix 时间戳格式化为北京时间的可读串。"""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value, CST).strftime("%Y-%m-%d %H:%M:%S")


# ===== HTTP =====
def api_call(domain: str, token: str, path: str, method: str = "POST",
             payload=None, timeout: int = 20, label: str = "") -> tuple[int, dict]:
    """发起一次请求并记录脱敏后的原始报文（供验收时贴出）。

    domain 若已带 http(s):// 前缀则原样使用（便于对接自建网关 / 本地端到端测试）。
    """
    if domain.startswith("http://") or domain.startswith("https://"):
        url = domain.rstrip("/") + path
    else:
        url = "https://" + domain + path
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    else:
        body = b"{}" if method == "POST" else None
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "buddy-station-actions/1.0")
    status, parsed = 0, {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            parsed = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            parsed = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            parsed = {"code": -1, "msg": "HTTP %s" % exc.code}
    except Exception as exc:  # noqa: BLE001
        status, parsed = 0, {"code": -1, "msg": str(exc)}
    RAW_CALLS.append({
        "label": label or path,
        "request": "%s %s" % (method, url),
        "payload": redact(json.dumps(payload, ensure_ascii=False)) if payload else ("{}" if method == "POST" else None),
        "http_status": status,
        "response": redact(json.dumps(parsed, ensure_ascii=False)),
    })
    return status, parsed


# ===== 签到 =====
def fetch_status(domain: str, token: str) -> dict:
    status, body = api_call(domain, token, CHECKIN_STATUS_PATH, label="① 状态查询")
    if status != 200 or body.get("code") != 0:
        raise RuntimeError("查询状态失败：HTTP %s, %s" % (status, redact(body.get("msg"))))
    return body.get("data", {}) or {}


def do_checkin(domain: str, token: str, dry_run: bool = False) -> dict:
    """签到（幂等）。返回结论字典：action ∈ checked_in / skipped / failed。"""
    try:
        before = fetch_status(domain, token)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "failed", "message": "签到前置状态查询失败：%s" % redact(exc)}

    if before.get("today_checked_in"):
        return {"ok": True, "action": "skipped", "already_checked_in": True,
                "message": "今日已签到（幂等跳过，未发写请求）",
                "before": {"total_credits": before.get("total_credits"),
                           "streak_days": before.get("streak_days"),
                           "today_credit": before.get("today_credit")}}

    if dry_run:
        return {"ok": True, "action": "dry_run", "message": "dry-run：已跳过签到写请求"}

    status, body = api_call(domain, token, CHECKIN_PATH, label="② 签到（写）")
    code, msg = body.get("code"), body.get("msg")
    if status == 200 and code == 0:
        return {"ok": True, "action": "checked_in", "message": msg or "签到成功", "data": body.get("data")}
    if code == 10001 and "已签到" in (msg or ""):
        return {"ok": True, "action": "skipped", "already_checked_in": True, "message": msg or "今日已签到"}
    return {"ok": False, "action": "failed", "message": redact(msg) or ("HTTP %s" % status), "code": code}


# ===== 派猫猫旅行 =====
def travel_get(token: str, path: str, label: str = "") -> dict | None:
    status, body = api_call(TRAVEL_DOMAIN, token, path, method="GET", label=label or path)
    if status != 200 or not isinstance(body, dict) or body.get("code") != 0:
        return None
    return body.get("data")


def travel_claim(token: str, dry_run: bool = False) -> dict:
    if dry_run:
        return {"ok": True, "action": "dry_run", "message": "dry-run：已跳过领取写请求"}
    status, body = api_call(TRAVEL_DOMAIN, token, TRAVEL_CLAIM_PATH, method="POST",
                            payload={}, label="④ 领取旅行奖励（写）")
    if status == 200 and isinstance(body, dict) and body.get("code") == 0:
        data = body.get("data") or {}
        return {"ok": True, "action": "claimed", "reward_credit": data.get("reward_credit"),
                "message": "已领取旅行积分 +%s" % (data.get("reward_credit")
                                                 if data.get("reward_credit") is not None else "?")}
    return {"ok": False, "action": "failed",
            "message": redact(body.get("msg")) if isinstance(body, dict) else ("HTTP %s" % status),
            "code": body.get("code") if isinstance(body, dict) else None}


def travel_depart(token: str, location_id: int | None, locations: list, dry_run: bool = False) -> dict:
    chosen = location_id
    if chosen is None:
        ids = [loc.get("id") for loc in (locations or []) if isinstance(loc, dict) and loc.get("id") is not None]
        if not ids:
            return {"ok": False, "action": "failed", "message": "未取到可选地点列表，已跳过派遣"}
        chosen = random.choice(ids)
    if dry_run:
        return {"ok": True, "action": "dry_run", "location_id": chosen,
                "message": "dry-run：已跳过派遣写请求（原计划地点 %s）" % chosen}
    status, body = api_call(TRAVEL_DOMAIN, token, TRAVEL_DEPART_PATH, method="POST",
                            payload={"location_id": chosen}, label="⑤ 派出旅行（写）")
    if status == 200 and isinstance(body, dict) and body.get("code") == 0:
        data = body.get("data") or {}
        loc = data.get("location") or {}
        return {"ok": True, "action": "departed", "location_id": chosen,
                "location_name": loc.get("name"), "arrive_at": data.get("arrive_at"),
                "arrive_at_text": fmt_local(data.get("arrive_at")),
                "message": "已派出 Buddy 前往【%s】" % (loc.get("name") or ("地点 %s" % chosen))}
    return {"ok": False, "action": "failed", "location_id": chosen,
            "message": redact(body.get("msg")) if isinstance(body, dict) else ("HTTP %s" % status),
            "code": body.get("code") if isinstance(body, dict) else None}


def travel_auto(token: str, location_id: int | None = None, dry_run: bool = False) -> dict:
    """派猫猫旅行闭环：先领（已到家的）→ 再判能否派新一趟 → 今日已派过就跳过。"""
    log: list[str] = []
    status = travel_get(token, TRAVEL_STATUS_PATH, label="③ 旅行状态")
    if status is None:
        return {"ok": False, "available": False, "state": None, "log": ["查询旅行状态失败，已跳过全部写操作"],
                "claimed": None, "departed": None, "final": None}

    initial_state = status.get("state")
    claimed = departed = None

    # 步骤 1：先领掉已经到家的旅行积分
    if initial_state == "arrived":
        claimed = travel_claim(token, dry_run=dry_run)
        log.append(claimed.get("message") or claimed.get("action"))
        if claimed.get("action") == "claimed":
            fresh = travel_get(token, TRAVEL_STATUS_PATH, label="③b 领取后复查状态")
            if fresh is not None:
                status = fresh
    else:
        log.append("当前无待领取积分（state=%s）" % (initial_state or "未知"))

    # 步骤 2：判断能不能派新的一趟
    if status.get("daily_limit_reached"):
        log.append("今日派遣次数已达上限，跳过派遣（今天已经派过，不重复派）")
    elif status.get("state") in (None, "", "idle"):
        config = travel_get(token, TRAVEL_CONFIG_PATH, label="③c 地点配置") or {}
        departed = travel_depart(token, location_id, config.get("locations") or [], dry_run=dry_run)
        log.append(departed.get("message") or departed.get("action"))
    elif status.get("state") == "traveling":
        log.append("Buddy 正在旅行中，无需派遣（今日已派）")
    else:
        log.append("状态 %s 不可派遣，已跳过" % status.get("state"))

    final = travel_get(token, TRAVEL_STATUS_PATH, label="⑥ 结果复查状态") or status
    remaining = None
    arrive, server_now = final.get("arrive_at"), final.get("server_now")
    if final.get("state") == "traveling" and isinstance(arrive, int) and isinstance(server_now, int):
        remaining = max(0, arrive - server_now)
    return {
        "ok": True, "available": True,
        "state": final.get("state"),
        "state_text": TRAVEL_STATE_TEXT.get(final.get("state"), final.get("state")),
        "location_name": (final.get("location") or {}).get("name") if isinstance(final.get("location"), dict) else None,
        "reward_credit": final.get("reward_credit"),
        "remaining_seconds": remaining,
        "remaining_text": ("%d 小时 %d 分" % divmod(remaining // 60, 60)) if remaining is not None else None,
        "daily_limit_reached": final.get("daily_limit_reached"),
        "record_id": final.get("record_id"),
        "claimed": claimed, "departed": departed, "final": final, "log": log,
    }


# ===== 主流程 =====
def write_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:  # noqa: BLE001
        pass


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s=%s\n" % (name, value))
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkBuddy Buddy 加油站云端每日任务")
    parser.add_argument("--location", type=int, default=None, help="指定派遣地点 1-4，缺省随机")
    parser.add_argument("--dry-run", action="store_true", help="只读演练：不发送任何写请求")
    parser.add_argument("--json-out", default=None, help="把结构化结果写入指定路径")
    parser.add_argument("--print-raw", action="store_true", help="向 stdout 打印脱敏后的原始返回")
    args = parser.parse_args()

    token = (os.environ.get("WORKBUDDY_TOKEN") or "").strip()
    domain = (os.environ.get("WORKBUDDY_DOMAIN") or DEFAULT_DOMAIN).strip()
    register_secret(token)
    day = now_cst().strftime("%Y-%m-%d")

    if not token:
        msg = ("未取到 WORKBUDDY_TOKEN。云端没有本机登录态，请先在本机运行 "
               "`python3 scripts/export_token.py --push-to-secret` 把凭证写入仓库 Secret。")
        print("[error] " + msg, file=sys.stderr)
        write_step_summary("## ❌ 任务未执行\n\n" + msg + "\n")
        return 2

    print("[info] 开始执行 · %s · 域名 %s%s" % (day, domain, " · dry-run" if args.dry_run else ""))

    # ---- 一、签到（决定整体成败）----
    checkin = do_checkin(domain, token, dry_run=args.dry_run)
    sign_txt = {"checked_in": "✅ 签到成功", "skipped": "✅ 今日已签到（幂等跳过）",
                "dry_run": "⏸ dry-run 未执行", "failed": "❌ 签到失败"}.get(checkin.get("action"), "❓ 未知")
    print("[checkin] %s · %s" % (sign_txt, checkin.get("message")))

    # ---- 二、猫猫旅行（失败不影响签到结论）----
    travel = travel_auto(token, location_id=args.location, dry_run=args.dry_run)
    for line in travel.get("log") or []:
        print("[travel] " + str(line))
    if not travel.get("ok"):
        print("[travel] ⚠️ 猫猫环节失败，但按约定不影响签到结论", file=sys.stderr)

    # ---- 三、落盘与汇总 ----
    raw_calls = RAW_CALLS
    payload = {
        "day": day,
        "dry_run": args.dry_run,
        "domain": domain,
        "checkin": {k: v for k, v in checkin.items() if k != "data"},
        "travel": {k: v for k, v in travel.items() if k != "final"},
        "raw_calls": raw_calls,
        "interfaces": {
            "status": "POST /v2/billing/meter/checkin-activity-status",
            "checkin": "POST /v2/billing/meter/daily-checkin",
            "travel_status": "GET /activity/growth/buddy/travel/status",
            "travel_config": "GET /activity/growth/buddy/travel/config",
            "travel_claim": "POST /activity/growth/buddy/travel/claim",
            "travel_depart": "POST /activity/growth/buddy/travel/depart",
        },
    }
    if args.json_out:
        try:
            with open(args.json_out, "w", encoding="utf-8") as handle:
                json.dump(sanitize_for_output(payload), handle, ensure_ascii=False, indent=2)
            print("[info] 结构化结果已写入 %s（已脱敏）" % args.json_out)
        except Exception as exc:  # noqa: BLE001
            print("[warn] 结果落盘失败：%s" % redact(exc), file=sys.stderr)
    if args.print_raw:
        print("\n===== 原始返回（脱敏）=====")
        for call in raw_calls:
            print("%s\n  %s -> HTTP %s\n  %s" % (call["label"], call["request"],
                                                 call["http_status"], call["response"]))

    # 运行结论写进 GitHub Step Summary：在 Actions 页面即可看到当天结果
    write_step_summary("## %s · Buddy 加油站每日任务\n\n"
                       "| 环节 | 结论 | 说明 |\n|---|---|---|\n"
                       "| 签到 | %s | %s |\n| 猫猫旅行 | %s | %s |\n"
                       % (day, sign_txt, str(checkin.get("message"))[:60],
                          "✅ 正常" if travel.get("ok") else "❌ 异常",
                          "；".join(str(x) for x in (travel.get("log") or []))[:120]))
    print("[summary] 签到 %s · 猫猫 %s" % (sign_txt, "正常" if travel.get("ok") else "异常"))

    set_output("checkin_status", str(checkin.get("action")))
    set_output("travel_status", "ok" if travel.get("ok") else "failed")

    # 退出码只由签到决定：签到成功/已签 = 成功；猫猫失败不影响
    return 0 if checkin.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
