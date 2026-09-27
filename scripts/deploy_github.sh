#!/usr/bin/env bash
# 一键把 buddy-station-actions 推到 GitHub 并完成首次跑通
#
# 用法（在项目根目录执行）：
#   bash scripts/deploy_github.sh                 # 推送到已有仓库（origin 已配置）
#   bash scripts/deploy_github.sh my-repo         # 新建私有仓库 my-repo 并推送
#   bash scripts/deploy_github.sh my-repo --run   # 上面 + 立刻触发一次并打印运行结论
#
# 前置：gh 已登录（gh auth login，或 echo <PAT> | gh auth login --with-token）
# 特性：解释器/工具路径一律用 command -v 动态解析，不写死版本号；
#       token 只经 stdin 管道写入 Secret，不出现在命令行参数与任何输出中。

set -uo pipefail

REPO_NAME="${1:-}"
DO_RUN="${2:-}"

say()  { printf '\033[36m[deploy]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[ok]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- 1. 动态定位工具 ----
GH="$(command -v gh || true)"
GIT="$(command -v git || true)"
PY="$(command -v python3 || command -v python || true)"
[ -n "$GIT" ] || die "找不到 git，请先安装 Git。"
[ -n "$GH" ]  || die "找不到 gh CLI，请先安装：https://cli.github.com/"
[ -n "$PY" ]  || die "找不到 python（需要它导出 token）。"
say "gh  -> $GH"
say "git -> $GIT"
say "py  -> $PY ($($PY --version 2>&1))"

# ---- 2. 校验登录态 ----
"$GH" auth status >/dev/null 2>&1 || die "gh 未登录。请先执行： gh auth login"
GH_USER="$("$GH" api user --jq .login 2>/dev/null || true)"
[ -n "$GH_USER" ] || die "无法读取 GitHub 账号，请重新 gh auth login"
ok "GitHub 账号：$GH_USER"

# ---- 3. 建仓库 / 配置 remote ----
if [ -n "$REPO_NAME" ]; then
  if "$GH" repo view "$GH_USER/$REPO_NAME" >/dev/null 2>&1; then
    say "仓库 $GH_USER/$REPO_NAME 已存在，直接复用"
  else
    say "创建私有仓库 $GH_USER/$REPO_NAME"
    "$GH" repo create "$REPO_NAME" --private \
      --description "WorkBuddy Buddy 加油站每日签到 + 派猫猫旅行自动化" >/dev/null \
      || die "创建仓库失败"
  fi
  SLUG="$GH_USER/$REPO_NAME"
else
  SLUG="$("$GH" repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
  [ -n "$SLUG" ] || die "当前目录未关联 GitHub 仓库。请传仓库名新建： bash scripts/deploy_github.sh <仓库名>"
  say "使用当前仓库：$SLUG"
fi

if "$GIT" remote get-url origin >/dev/null 2>&1; then
  "$GIT" remote set-url origin "https://github.com/$SLUG.git"
else
  "$GIT" remote add origin "https://github.com/$SLUG.git"
fi
"$GIT" branch -M main 2>/dev/null || true

# ---- 4. 提交并推送 ----
if [ -n "$("$GIT" status --porcelain)" ]; then
  "$GIT" add -A
  "$GIT" -c user.name="${GH_USER}" -c user.email="${GH_USER}@users.noreply.github.com" \
    commit -q -m "chore: 部署 Buddy 加油站自动化" || true
  say "已提交待推送的改动"
fi
say "推送到 origin/main ..."
"$GH" auth setup-git >/dev/null 2>&1 || true
"$GIT" push -u origin main --quiet || die "推送失败"
ok "代码已推送：https://github.com/$SLUG"

# ---- 5. 写入 Secret（token 只走 stdin，绝不回显）----
say "导出本机登录态并写入 Secret WORKBUDDY_TOKEN ..."
"$PY" scripts/export_token.py --push-to-secret --repo "$SLUG" \
  || die "写入 Secret 失败（请确认已在 WorkBuddy 客户端登录）"
ok "Secret 已写入"

# ---- 6. 可选：立刻实跑一次并打印结论 ----
if [ "$DO_RUN" = "--run" ]; then
  say "触发一次实跑（正式模式，会真正签到 / 领派）..."
  "$GH" workflow run buddy-station.yml --repo "$SLUG" >/dev/null 2>&1 \
    || "$GH" workflow run "Buddy 加油站每日任务" --repo "$SLUG" >/dev/null 2>&1 \
    || die "触发失败，可改用 dry-run 先验证：gh workflow run buddy-station.yml -f dry_run=true"
  sleep 8
  RUN_ID="$("$GH" run list --repo "$SLUG" --limit 1 --json databaseId --jq '.[0].databaseId')"
  say "等待运行 $RUN_ID 结束 ..."
  "$GH" run watch "$RUN_ID" --repo "$SLUG" --exit-status >/dev/null 2>&1 || true
  echo
  say "===== 运行结论 ====="
  "$GH" run view "$RUN_ID" --repo "$SLUG" --log | tail -40
  echo
  say "完整日志： https://github.com/$SLUG/actions/runs/$RUN_ID"
fi

echo
ok "部署完成。定时任务：每天 09:30 北京时间自动执行（cron 30 1 * * * UTC）"
say "仓库地址：https://github.com/$SLUG"
say "手动 dry-run 演练： gh workflow run buddy-station.yml --repo $SLUG -f dry_run=true"
