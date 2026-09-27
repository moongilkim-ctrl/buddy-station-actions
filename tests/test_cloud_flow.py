#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地端到端测试：用 mock 服务器验证闭环逻辑与失败隔离。

覆盖 4 个场景：
  A. arrived  → 先领，领完 idle 未达上限 → 再派新一趟（一次运行完成「先领后派」）
  B. traveling → 今日已派过，不重复派；无待领积分
  C. idle + daily_limit_reached=True → 跳过派遣（不越权发写请求）
  D. 旅行接口整段挂掉 → 猫猫失败，但签到仍成功、退出码 0（失败隔离）

用法：python3 tests/test_cloud_flow.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import buddy_station_cloud as m  # noqa: E402

CALLS = []


class Handler(BaseHTTPRequestHandler):
    scenario = "A"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # 显式关闭连接：避免 Windows 下 keep-alive 连接被 shutdown 中断（WinError 10053）
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        CALLS.append(("GET", self.path))
        sc = Handler.scenario
        if self.path.endswith("/travel/status"):
            # A 场景：第一次 arrived，领取后变 idle；之后派出去变 traveling
            if sc == "A":
                claimed = any(c[1].endswith("/travel/claim") and c[0] == "POST" for c in CALLS)
                departed = any(c[1].endswith("/travel/depart") and c[0] == "POST" for c in CALLS)
                if not claimed:
                    return self._send(200, {"code": 0, "data": {"state": "arrived", "reward_credit": 9,
                                                               "record_id": 1, "server_now": 1000}})
                if not departed:
                    return self._send(200, {"code": 0, "data": {"state": "idle", "reward_credit": 0,
                                                               "daily_limit_reached": False,
                                                               "server_now": 1000}})
                return self._send(200, {"code": 0, "data": {"state": "traveling", "reward_credit": 0,
                                                           "daily_limit_reached": True, "record_id": 2,
                                                           "location": {"id": 1, "name": "咖啡馆"},
                                                           "arrive_at": 8200, "server_now": 1000}})
            if sc == "B":
                return self._send(200, {"code": 0, "data": {"state": "traveling", "reward_credit": 10,
                                                           "daily_limit_reached": True, "record_id": 9,
                                                           "location": {"id": 3, "name": "健身房"},
                                                           "arrive_at": 8200, "server_now": 1000}})
            return self._send(200, {"code": 0, "data": {"state": "idle", "reward_credit": 0,
                                                       "daily_limit_reached": True, "server_now": 1000}})
        if self.path.endswith("/travel/config"):
            return self._send(200, {"code": 0, "data": {"locations": [
                {"id": 1, "code": "coffee", "name": "咖啡馆"},
                {"id": 2, "code": "mall", "name": "商场店铺"}]}})
        if self.path.endswith("/travel/records"):
            return self._send(200, {"code": 0, "data": {"records": [], "total": 0}})
        return self._send(404, {"code": -1, "msg": "not found"})

    def do_POST(self):
        CALLS.append(("POST", self.path))
        if self.path.endswith("/daily-checkin"):
            return self._send(200, {"code": 0, "msg": "签到成功", "data": None})
        if self.path.endswith("/checkin-activity-status"):
            return self._send(200, {"code": 0, "msg": "OK", "data": {
                "total_credits": 1100, "streak_days": 11, "today_checked_in": False,
                "daily_credit": 100, "checkin_dates": ["2026-09-25"]}})
        if self.path.endswith("/travel/claim"):
            return self._send(200, {"code": 0, "data": {"state": "idle", "reward_credit": 9}})
        if self.path.endswith("/travel/depart"):
            return self._send(200, {"code": 0, "data": {"state": "traveling", "record_id": 2,
                                                       "location": {"id": 1, "name": "咖啡馆"},
                                                       "arrive_at": 8200, "daily_limit_reached": True}})
        return self._send(404, {"code": -1, "msg": "not found"})


def run_scenario(name, scenario):
    CALLS.clear()
    Handler.scenario = scenario
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    m.RAW_CALLS.clear()
    m._SECRETS.clear()
    m.register_secret("test-token-abcdefgh")
    m.DEFAULT_DOMAIN = "http://127.0.0.1:%d" % port
    m.TRAVEL_DOMAIN = "http://127.0.0.1:%d" % port
    os.environ["WORKBUDDY_TOKEN"] = "test-token-abcdefgh"

    checkin = m.do_checkin(m.DEFAULT_DOMAIN, "test-token-abcdefgh")
    travel = m.travel_auto("test-token-abcdefgh")
    server.shutdown()
    server.server_close()

    writes = [c[1].split("/")[-1] for c in CALLS if c[0] == "POST"]
    print("\n=== 场景 %s ===" % name)
    print("  签到：%s / %s" % (checkin.get("action"), checkin.get("message")))
    print("  猫猫：ok=%s state=%s" % (travel.get("ok"), travel.get("state")))
    for line in travel.get("log") or []:
        print("    · " + line)
    print("  写请求序列：%s" % writes)
    return checkin, travel, writes


def main():
    failures = []

    # A. 先领后派
    ck, tv, writes = run_scenario("A 已到家 → 先领 → 再派新一趟", "A")
    if "daily-checkin" not in writes:
        failures.append("A: 未执行签到")
    if not tv.get("claimed", {}).get("ok") if tv.get("claimed") else True:
        failures.append("A: 领取失败")
    if writes.index("claim") > writes.index("depart") if ("claim" in writes and "depart" in writes) else True:
        failures.append("A: 领取未在派遣之前")
    if "claim" not in writes or "depart" not in writes:
        failures.append("A: 未完成先领后派")

    # B. 旅行中：不重复派
    ck, tv, writes = run_scenario("B 旅行中 → 不重复派", "B")
    if "depart" in writes:
        failures.append("B: 今日已派过却仍发起派遣")
    if "claim" in writes:
        failures.append("B: 无待领取却发起了领取")

    # C. idle 但达上限
    ck, tv, writes = run_scenario("C 空闲但今日已达上限 → 跳过派遣", "C")
    if "depart" in writes:
        failures.append("C: 已达上限却仍发起派遣")

    # D. 失败隔离：猫猫整段挂掉，签到仍成功
    os.environ["WORKBUDDY_TOKEN"] = "test-token-abcdefgh"
    m.RAW_CALLS.clear()
    m.TRAVEL_DOMAIN = "http://127.0.0.1:1"          # 必然连接失败
    Handler.scenario = "B"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    m.DEFAULT_DOMAIN = "http://127.0.0.1:%d" % server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ck = m.do_checkin(m.DEFAULT_DOMAIN, "test-token-abcdefgh")
    tv = m.travel_auto("test-token-abcdefgh")
    server.shutdown()
    server.server_close()
    print("\n=== 场景 D 旅行接口不可用 → 失败隔离 ===")
    print("  签到：%s（应为 checked_in）" % ck.get("action"))
    print("  猫猫：ok=%s log=%s" % (tv.get("ok"), tv.get("log")))
    if ck.get("action") != "checked_in":
        failures.append("D: 猫猫故障影响了签到结论")
    if tv.get("ok"):
        failures.append("D: 猫猫应报告失败")

    # 脱敏校验
    m.register_secret("test-token-abcdefgh")
    if "test-token-abcdefgh" in m.redact("Bearer test-token-abcdefgh"):
        failures.append("脱敏失效：token 泄漏")

    # result.json 落盘脱敏（公开仓库的 artifact 任何人可下载，必须零泄漏）
    # 场景 E：账号资产与个人标识不得出现在上 artifact 的文件里
    # 注意：探针一律用 IANA 保留域名（example.com），不得出现任何真实邮箱。
    probe_mail = "someone@example.com"
    sample = {
        "day": "2026-09-27", "domain": "www.codebuddy.cn",
        "checkin": {"ok": True, "action": "checked_in", "message": "ok",
                    "before": {"total_credits": 1200, "streak_days": 12, "today_credit": 100}},
        "travel": {"ok": True, "reward_credit": 9, "record_id": 9955743, "location_name": "咖啡馆"},
        "note": "如出现 %s 也必须被打码" % probe_mail,
        "raw_calls": [{"label": "① 状态查询", "request": "POST https://x/y",
                       "payload": {"a": 1}, "http_status": 200, "response": {"credits": 1200}}],
    }
    blob = json.dumps(m.sanitize_for_output(sample), ensure_ascii=False)
    for probe, why in ((probe_mail, "邮箱地址"), ("1200", "积分余额"),
                       ("9955743", "记录 ID"), ("www.codebuddy.cn", "账号域名")):
        if probe in blob:
            failures.append("落盘脱敏失效：%s（%s）泄漏" % (probe, why))
    if '"http_status"' not in blob or '"request"' in blob:
        failures.append("落盘脱敏失效：原始报文应只保留 label 与状态码")
    # 关键运行结论仍须保留，否则 artifact 失去排查价值
    if '"action": "checked_in"' not in blob or '"label"' not in blob:
        failures.append("落盘脱敏过度：必要的运行结论被误删")

    print("\n" + "=" * 52)
    if failures:
        print("❌ 失败项：")
        for f in failures:
            print("   - " + f)
        return 1
    print("✅ 全部场景通过：先领后派 / 不重复派 / 上限保护 / 失败隔离 / 脱敏 / 落盘脱敏")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
