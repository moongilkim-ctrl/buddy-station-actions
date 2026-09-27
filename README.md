# Buddy 加油站 · GitHub Actions 自动化

把 WorkBuddy「Buddy 加油站」的**每日签到**与**派猫猫旅行**放到 GitHub 免费定时任务里跑。
云端 runner 执行，本机不需要常开；运行结论看运行日志与 Step Summary。

## 一次运行做什么

单次运行内按顺序完成三步：

| 步骤 | 动作 | 接口 |
|---|---|---|
| 1 | 查签到状态 → 未签则签到（幂等） | `POST /v2/billing/meter/checkin-activity-status`、`POST /v2/billing/meter/daily-checkin` |
| 2 | 先领掉已经到家的旅行积分 | `POST /activity/growth/buddy/travel/claim` |
| 3 | 再判断能否派新的一趟；今日已派过就跳过 | `GET /activity/growth/buddy/travel/status`、`POST /activity/growth/buddy/travel/depart` |

接口名均以**实际请求**为准（已实测确认），未采用网上流传的旧接口名。

## 关键约定

- **先领后派**：到达后状态会一直是 `arrived` 等待领取，所以每次运行先补领、再考虑派遣，不会丢积分。
- **不重复派**：派遣前必查 `daily_limit_reached`；`traveling`（今日已派）或已达上限时**不发写请求**。
- **失败隔离**：猫猫环节任一步失败都不影响签到结论，**签到成功即整体成功**（退出码 0）。
- **token 不进仓库**：只存 GitHub Actions Secret，脚本从环境变量读取，日志/artifact 全部脱敏。
- **解释器路径不写死版本号**：工作流用 `command -v python3 || command -v python` 动态解析。

## 部署

> **本次已部署完成**：仓库 `moongilkim-ctrl/buddy-station-actions`，
> Secret `WORKBUDDY_TOKEN` / `WORKBUDDY_DOMAIN` 已写入，命令行触发与手动运行均已跑通。
> 其余步骤保留供换机 / 重建时使用。
>
> ⚠️ **定时机制现状（2026-09-27 实测）**：**GitHub 原生 `schedule` 当前不投递** ——
> 本账号下所有仓库（含新建的公开隔离仓库）`event=schedule` 计数恒为 **0**，
> 而 `workflow_dispatch` 秒级正常；同期第三方仓库（`nodejs/node`、`home-assistant/core`）
> 的 schedule 正常触发 → 判定为**账号级投递失效**，与仓库可见性、cron 写法、仓库配置均无关。
> **因此本项目当前由外部 cron 服务调 dispatch 接口驱动定时**，配置见 `scripts/setup_external_cron.md`。
> 仓库内保留 `cron: "30 1 * * *"`，待 GitHub 恢复后原生定时会自动接手（两者并存不冲突，接口幂等）。

### 0. 登录 gh（一次性）

推送 `.github/workflows/*` 需要 **`workflow`** scope，缺了会被 GitHub 拒收：

```bash
gh auth login --hostname github.com --git-protocol https --web --scopes "repo,workflow"
```

设备码登录无需手工粘贴 token；`repo` + `workflow` 两个 scope 都必须勾上。

### 1. 建仓库并推代码

在项目根目录执行（会自动建私有仓库、推送代码、写入 Secret、可选立刻实跑）：

```bash
bash scripts/deploy_github.sh buddy-station-actions          # 建私有仓库并推送
bash scripts/deploy_github.sh buddy-station-actions --run    # 上面 + 立刻触发一次实跑并打印结论
```

脚本内所有工具（gh / git / python）都用 `command -v` 动态定位，不写死路径与版本号。
若想手动分步，等价命令为：

```bash
gh repo create buddy-station-actions --private
git remote add origin https://github.com/<owner>/buddy-station-actions.git
git push -u origin main
```

仓库保持 **Public**（纯工具类代码，无密钥）。为降低公开带来的暴露面，已做四项加固：

1. **零密钥**：token 只存仓库 Secret，源码不含任何密钥。
2. **仅本人可触发**：工作流带 `if: github.event_name == 'schedule' || github.actor == '<owner>'` 守卫。
   该流程会消耗签到与派遣积分，这条守卫保证即使公开，别人手动触发也只会被跳过；
   `schedule` 事件单独放行，避免守卫把定时运行静默挡掉。
3. **历史干净**：仓库以单次提交重建（`--orphan`），提交作者用 GitHub 匿名邮箱，旧历史（含真实邮箱）已彻底清除。
4. **产物脱敏**：`result.json` 落盘前统一脱敏（见文末加固清单）。

> 可见性与定时无关：本账号的 schedule 投递失效已实测对公开仓库同样成立，转公开 / 升 Pro 都无效，
> 需改由外部定时服务调用 dispatch 接口（见《定时》）。

### 2. 注入凭证到 Secret

云端没有本机登录态，所以要在**本机**导出 token 写进仓库 Secret：

```bash
gh auth login                                      # 若尚未登录
python3 scripts/export_token.py --push-to-secret   # 读本机登录态 → 写仓库 Secret
```

该脚本只把 token 通过 **stdin 管道**交给 `gh secret set`，不落盘、不进命令行参数、不进 git。
默认只打印指纹（长度 + 首尾 4 位）与有效期，要看全文需显式加 `--reveal`。

> **token 有效期**：accessToken 是 JWT，无法自动续期。到期后重跑一次上面的命令刷新 Secret 即可。
> 脚本每次运行都会打印剩余天数；过期后签到会失败，运行日志与 Step Summary 里会看到「签到失败」，便于察觉。

#### 让 Secret 自动保持新鲜（推荐）

不必记着手动刷新——用 `keep_token_fresh.py` 把这件事自动化：

```bash
python3 scripts/keep_token_fresh.py --check    # 只看状态，不做任何写入
python3 scripts/keep_token_fresh.py --push     # 令牌有变化才同步到 Secret
python3 scripts/keep_token_fresh.py --install-task   # 注册本机计划任务（每日 + 登录时）
```

原理：WorkBuddy 桌面端约每 24 小时自动刷新会话，并把**新签发**的 accessToken 写回本机
`workbuddy-desktop.info`。本脚本只做「发现令牌变了 → 同步进 Secret」，配合定时触发即等价于自动续期。
状态记录在 `~/.workbuddy/buddy-station-token-sync.json`，**只存指纹与时间，不含明文令牌**。

> **为什么不做「纯云端互刷」**：客户端确实有续期接口
> `POST /v2{auth.prefixPath}/auth/token/refresh`（令牌走 `X-Refresh-Token` 头），
> 但它的 base URL 来自**远端下发的产品配置**，本机登录态里没有该字段；
> 实测 `www/api/console.codebuddy.cn` 下全部候选路径均 404。
> 且刷新会**轮换 refreshToken**，云端写下新值、本地却仍持旧值，下次本地刷新反而会失败。
> 因此采用「本机同步」而非「纯云端互刷」。

若本机不允许注册计划任务（受限环境），改用 **WorkBuddy 自动化**每日定时执行
`python3 scripts/keep_token_fresh.py --push --repo <owner>/<repo>` 即可，效果相同。

## 定时

目标节奏：**每天 09:30 北京时间**（工作流内声明 `30 1 * * *` = UTC 01:30）。

### 现状：原生 `schedule` 不投递，改由外部 cron 驱动

**2026-09-27 实测结论：本账号的 GitHub 原生 `schedule` 事件不投递**（账号级故障，非仓库配置问题）。

| 项 | 结果 |
|---|---|
| 本账号仓库（公开 `buddy-station-actions`） | `schedule` 运行数 **0** |
| 本账号新建公开隔离仓库（无守卫、`*/5`） | `schedule` 运行数 **0** |
| 第三方参照仓库 `nodejs/node` / `home-assistant/core` | **正常触发**（分钟级频率） |
| `workflow_dispatch`（本账号） | **秒级成功** |
| 判定 | 平台调度服务健康 → 故障在本账号侧（**账号级投递失效**） |

> ⚠️ 重要：**可见性与这件事无关**。此前「免费私有仓库不触发」的说法已被实测否定——
> 转 Public 后 `schedule` 依然恒为 0。转公开 / 升 Pro **都不是解**，别再在这上面花时间。
> 社区同类账号级案例见 discussion #205984 / #207211。

**判定定时是否真的生效，只看这一条命令**（`active` 与 UI 上的触发器图标都不可信）：

```bash
# 本仓库：必须 > 0 才算原生定时可用
gh api "repos/<owner>/<repo>/actions/runs?per_page=100" \
  --jq '[.workflow_runs[]|select(.event=="schedule")]|length'

# 参照仓库（不属于你）：有近期时间戳 = 平台服务是好的，问题在你账号
gh api "repos/nodejs/node/actions/runs?event=schedule&per_page=1" --jq '.workflow_runs[0].created_at'
```

### 当前方案：外部 cron 调 dispatch 接口（必须配置）

由外部定时服务每天 09:30（北京时间）POST 一次 dispatch 接口，绕过失效的原生调度器。
完整步骤见 **`scripts/setup_external_cron.md`**，要点：

1. 建**细粒度 PAT**（仅本仓库 `Actions: Read and write`）
2. 在 cron-job.org 建任务：POST
   `https://api.github.com/repos/<owner>/<repo>/actions/workflows/buddy-station.yml/dispatches`
   Body `{"ref":"main"}`，Header `Authorization: Bearer <PAT>`、`Accept: application/vnd.github+json`
3. 期望返回 **204**，并在 Actions 页面看到新的 `workflow_dispatch` 运行

> 安全提示：PAT 只授予单一仓库的 Actions 写权限，泄露影响面被限制在「能触发本仓库工作流」。
> 请勿使用 `repo` 全权经典令牌。

> 工作流内保留原生 `cron` 声明：**一旦 GitHub 恢复投递，原生定时会自动接手**。
> 两者并存不会冲突——签到与派遣接口本身幂等，`concurrency` 亦会串行化，不会重复扣次。

### 其他失效原因（已排除，供日后核对）

> 工作流文件不在默认分支、cron 间隔 < 5 分钟、`@daily` 等别名（GitHub 不支持）、
> 公开仓库 60 天无提交被自动禁用、整点 UTC 高负载延迟乃至丢弃（可把分钟偏移到 `:23` 这类非整点值）。

## 手动验证

`Actions → Buddy 加油站每日任务 → Run workflow`：

- 勾 **dry_run** → 只读演练，不发任何写请求，用于确认凭证与接口连通性。
- 填 **location** → 指定派遣地点（1 咖啡馆 / 2 商场店铺 / 3 健身房 / 4 古镇客栈；留空随机）。

命令行等价用法：

```bash
gh workflow run buddy-station.yml --repo <owner>/<repo>              # 正式跑
gh workflow run buddy-station.yml --repo <owner>/<repo> -f dry_run=true
gh run list --repo <owner>/<repo> --limit 3
gh run watch <run-id> --repo <owner>/<repo> --exit-status
gh run download <run-id> --repo <owner>/<repo> -n buddy-station-result -D out/
```

> **取证提示**：artifact 步骤是 `if-no-files-found: ignore`，因此「该步骤为绿」**不等于**产出了文件。
> 判定运行成功要看**日志里的业务行**，或下载 artifact 核对 `result.json` 内容，不要只看步骤颜色。
> （`gh run download` 在 Windows 上传 `/tmp/...` 会被解析成 `D:\tmp\...`，建议用工作区相对路径。）

## 本地自测

```bash
# 逻辑回归（mock 服务器，无需网络与真实 token）
python3 tests/test_cloud_flow.py
# 期望：先领后派 / 不重复派 / 上限保护 / 失败隔离 / 脱敏 / 落盘脱敏 全部通过

# 真接口只读演练（需本机已登录 WorkBuddy）
python3 scripts/export_token.py --print-env > /tmp/env.sh && source /tmp/env.sh
python3 scripts/buddy_station_cloud.py --dry-run --print-raw
```

## 退出码语义

| 退出码 | 含义 |
|---|---|
| 0 | 签到成功 or 今日已签到（**猫猫失败也算成功**，符合约定） |
| 1 | 签到失败 |
| 2 | 缺 `WORKBUDDY_TOKEN`，未执行 |

## 目录

```
buddy-station-actions/
├── .github/workflows/buddy-station.yml   # 定时任务定义（含仅本人可触发的守卫）
├── scripts/
│   ├── buddy_station_cloud.py            # 云端主流程（签到 + 猫猫）
│   ├── export_token.py                   # 本机导出 token → 仓库 Secret
│   ├── keep_token_fresh.py               # 令牌变化时自动同步 Secret（配合定时触发）
│   ├── trigger_dispatch.py               # 外部定时服务调用 dispatch 接口的封装
│   ├── setup_external_cron.md            # 外部定时服务配置指南（备选方案）
│   └── deploy_github.sh                  # 一键建仓库/推送/写 Secret/实跑
├── tests/test_cloud_flow.py              # 端到端逻辑回归
└── README.md
```

## 仓库公开后的加固清单

仓库为 Public 是**为了让免费账号的原生定时生效**（见《定时》）。公开前已逐项核对：

| 项 | 状态 |
|---|---|
| token / 授权码是否进过 git 历史 | ✅ 从未（`git log --all -p` 全量扫描 0 命中） |
| 源码是否含邮箱 / 邮件功能 | ✅ **已彻底移除**：无 smtplib、无收件箱、无 SMTP Secret，全仓 0 个真实邮箱 |
| 提交作者邮箱 | ✅ GitHub 匿名邮箱（`*@users.noreply.github.com`） |
| 旧历史是否残留个人信息 | ✅ 以孤儿提交重建，单一提交、旧引用已 gc 清除 |
| 是否可能被他人触发消耗积分 | ✅ `if: github.event_name == 'schedule' \|\| github.actor == '<owner>'` 守卫 |
| 运行产物是否含敏感数据 | ✅ 落盘前统一脱敏（积分/记录 ID/域名/邮箱格式串/原始报文全部抹除） |

> **重点提醒**：公开仓库的 artifact **列表元数据匿名可见**（实测 `GET /actions/artifacts` 返回 200），
> 下载 zip 需认证（`GET /actions/artifacts/<id>/zip` 返回 401）。
> 但既然仓库公开，就不该假设 artifact 永远私有 —— 因此 `result.json` 在写盘前必须先过一遍
> `sanitize_for_output()`。这一步很容易被忽略，一旦漏掉，账号资产与个人标识就会随 artifact 外流，
> 抵消掉源码侧的全部清理。
