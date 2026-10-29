# 外部定时触发配置指南

> 背景：外部定时服务按点调用 GitHub dispatch API，为本项目提供**准点**触发。
>
> ✅ **2026-10-06 复核修正**：此前「本账号原生 `schedule` 恒不投递」的结论**已被实测推翻**——
> 原生 `schedule` **一直在工作**，只是**延迟 2~3 小时**（8 天连续每天 1 条 success，
> 落地时间 09:16~10:22）。当时判定为「账号级投递失效」，是**在仓库刚建/刚改 cron 的
> 数小时窗口内去数运行数**所致（GitHub 的 schedule 注册需 15–60 分钟、执行延迟可达 2~3 小时）。
>
> **结论：两条链并存，互为补充** —— 原生 `schedule` 保「当天必签」，外部 cron 保「早上 7 点签」。
> 二者调同一工作流，**签到接口幂等**（已签到则 `skipped`、不发写请求），**不会重复扣次**。
>
> **✅ 当前方案：外部 cron 服务按点调用 GitHub dispatch API**，触发同一工作流，
> 效果等价且**准点**（实测 9 天全部 07:00:3x 命中）。

---

## 零、当前配置状态（2026-09-27 已落地 ✅）

| 项 | 值 |
|---|---|
| 服务 | cron-job.org |
| 任务 ID | `8522176` |
| 任务名 | `Buddy加油站每日签到` |
| 触发时间 | **每天 07:00（Asia/Shanghai）**，crontab `0 7 * * *` |
| 请求方法 | `POST` |
| 请求体 | `{"ref":"main"}` |
| 状态 | 已启用；`TEST RUN` 实测返回 **`204`**，GitHub 运行 `36306857072` → `success` |
| 下次执行 | 2026-09-28 07:00（北京时间） |
| 失败通知 | 已开（连续失败 1 次即通知） |

已配置的 4 条请求头（**`Content-Type` 必须是 `application/json`**）：

```
Authorization: Bearer <细粒度PAT>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

> ⚠️ **易踩的坑**：用 cron-job.org 的 `IMPORT FROM CURL` 导入时，它会自动补一条
> `Content-Type: application/x-www-form-urlencoded`，**必须手动改成 `application/json`**，
> 否则 GitHub 侧行为异常。导入后务必切到 ADVANCED 页逐条核对。
>
> 本机私钥（不在仓库内）：`~/.workbuddy/secrets/buddy-station-cron.pat`（`chmod 600`）。

---

## 一、先创建细粒度 PAT（一次授权）

用**细粒度令牌**（Fine-grained token）而非经典令牌，把权限压到最小：

1. 打开 <https://github.com/settings/personal-access-tokens/new>
2. 填写：
   - **Token name**：`buddy-station-cron`
   - **Expiration**：建议 90 天（到期需续，与 token 保鲜一并留意）
   - **Repository access** → `Only select repositories` → 只勾选 `buddy-station-actions`
3. **Permissions** → Repository permissions → 只改一项：
   - **Actions** → `Read and write`
   - 其余全部保持 `No access`
4. 生成并**立即复制**（离开页面后不再显示）。

> 该令牌的能力边界仅限「触发本仓库的 Actions 工作流」。
> 即便泄露，也无法读代码、改代码、动 Secret，影响面被限制在最小。

---

## 二、验证 PAT 可用（可选但推荐）

拿到 PAT 后先本机验证一次，确认权限正确再配到外部服务：

```bash
export GH_CRON_PAT="<你的PAT>"

# 触发一次
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer $GH_CRON_PAT" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/moongilkim-ctrl/buddy-station-actions/actions/workflows/buddy-station.yml/dispatches" \
  -d '{"ref":"main"}'
```

**期望返回 `204`**（成功，无正文）。随后确认运行已创建：

```bash
gh run list --repo moongilkim-ctrl/buddy-station-actions --limit 2
```

本机也可以用封装好的脚本（免 curl 细节）：

```bash
export GH_CRON_PAT="<你的PAT>"
python3 scripts/trigger_dispatch.py
```

---

## 三、配置外部 cron 服务

以 **cron-job.org** 为例（免费，支持自定义 Header，国内可访问）：

1. 注册并登录 <https://cron-job.org>
2. **Create cronjob**，填写：

| 字段 | 值 |
|---|---|
| Title | `Buddy加油站每日签到` |
| URL | `https://api.github.com/repos/moongilkim-ctrl/buddy-station-actions/actions/workflows/buddy-station.yml/dispatches` |
| Schedule | 每天 `07:00`，时区选 **Asia/Shanghai**（北京时间） |
| Request method | `POST` |

3. **Advanced → Headers** 添加四条：

```
Authorization: Bearer <你的PAT>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

> 💡 更快的做法：Advanced 页有 **`IMPORT FROM CURL`**，把第二节那条 curl 整行粘进去，
> URL / 方法 / 请求头 / body 会一次填好。但**导入后必须把自动补的
> `Content-Type: application/x-www-form-urlencoded` 改成 `application/json`**。

4. **Advanced → Request body** 填：

```json
{"ref":"main"}
```

5. 保存后点 **TEST RUN**，应看到 HTTP `204`。

> 其他等价服务：GitHub Actions 官方推荐的还有 EasyCron、Zapier 等，只要满足
> ①支持 POST ②支持自定义 Header ③支持指定时区/分钟 即可。

---

## 四、为什么不用别的方案

| 方案 | 未选原因 |
|---|---|
| 只依赖原生 schedule（不配外部 cron） | **可用但延迟 2~3 小时**（实测落地 09:16~10:22），赶不上「早上 7 点签到」的目标；故仅作兜底 |
| 转公开 / 升 Pro | 对「准点触发」毫无帮助 —— 实测原生 schedule 本来就是通的，改可见性/付费档不改变其延迟特性 |
| 本机定时跑 | 要求电脑开机，失去「云端无需常开设备」的意义 |

---

## 五、注意事项

1. **PAT 到期**：细粒度 PAT 有有效期，过期后外部调用会返回 `401`，定时静默失效。
   建议与 token 同步检查（`WORKBUDDY_TOKEN` 有效期至 2026-11-20）。
2. **失败不会主动告警**：外部服务调用失败时不会通知你。建议在 cron-job.org 里
   开启 **"Notify on failure"**，否则又会出现「某天没跑」而不知原因。
3. **不要**把 PAT 写进仓库任何文件；它只存在于外部服务与本机环境变量中。
4. 改触发时间只需改外部服务的 schedule，工作流里的 `cron` 声明保留即可
   （原生 schedule 本来就在跑，两者并存不会冲突，`concurrency` 会串行化，
   且签到接口本身幂等）。
5. **幂等性**：签到与派遣接口均幂等（今日已签到会跳过、派遣达上限会跳过），
   所以外部定时与原生 cron 同时触发**不会重复扣次**，可放心并存。

---

## 六、现状说明（2026-10-06 复核修正）

> - **原生 `schedule` 一直在工作**（本地实测 8 天连续每天 1 条 `success`），
>   只是**延迟 2~3 小时**：声明 `0 23 * * *`（北京 07:00），实际落地 **09:16~10:22**。
> - 此前「账号级 schedule 投递失效」（`event=schedule` 恒为 0）的判定**属误判** ——
>   根因是**在仓库刚建/刚改 cron 的数小时窗口内去数运行数**（注册需 15–60 分钟、执行延迟可达 2~3 小时）。
> - 因此**两条链并存、互为补充**：原生 `schedule` 保「当天必签」，外部 cron 保「早上 7 点签」。
>   外部 cron 实测 9 天全部 **07:00:3x** 命中，秒级准点。
> - 工作流内保留 `cron: "0 23 * * *"`（UTC）= 每天 07:00 北京时间。
> - 外部服务时间同步设为**每天 07:00（Asia/Shanghai）**。
>
> ⚠️ **可复用的判定原则**：判 `schedule` 是否真的失效，**观测窗口必须跨越多天**；
> 数小时无运行**不能**作为「永不触发」的证据。
