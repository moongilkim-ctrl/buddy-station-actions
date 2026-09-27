# 外部定时触发配置指南（备用方案）

> 背景：GitHub **免费个人账号的私有仓库不支持 `schedule` 触发器**（平台限制）。
> 若仓库为 Private + Free，原生 cron 恒不触发（实测 `event=schedule` 运行数 = 0）。
> 替代方案：外部 cron 服务按点调用 GitHub dispatch API，触发同一工作流，效果等价。
>
> **当前状态**：本仓库已转为 **Public**，原生定时已启用（每天 09:30 北京时间）。
> 本文档保留作为「日后改回私有仓库」时的备用方案，现阶段**无需配置**。

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
| Title | `Buddy 加油站日报` |
| URL | `https://api.github.com/repos/moongilkim-ctrl/buddy-station-actions/actions/workflows/buddy-station.yml/dispatches` |
| Schedule | 每天 `09:30`，时区选 **Asia/Shanghai**（北京时间） |
| Request method | `POST` |

3. **Advanced → Headers** 添加三条：

```
Authorization: Bearer <你的PAT>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
```

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
| 仓库转公开 | 代码对全网可见（虽无密钥，但用户选择保持私有） |
| GitHub Pro（$4/月） | 需付费 |
| 本机定时跑 | 要求电脑开机，失去「云端无需常开设备」的意义 |

---

## 五、注意事项

1. **PAT 到期**：细粒度 PAT 有有效期，过期后外部调用会返回 `401`，定时静默失效。
   建议与 token 同步检查（`WORKBUDDY_TOKEN` 有效期至 2026-11-20）。
2. **失败不会主动告警**：外部服务调用失败时不会通知你。建议在 cron-job.org 里
   开启 **"Notify on failure"**，否则又会出现「某天没跑」而不知原因。
3. **不要**把 PAT 写进仓库任何文件；它只存在于外部服务与本机环境变量中。
4. 改触发时间只需改外部服务的 schedule，工作流里的 `cron` 声明保留即可
   （原生定时与外部触发并存不会冲突，谁先到就谁触发，`concurrency` 会串行化）。

---

## 六、现状说明

> 本仓库已于 2026-09-27 转为 **Public**，GitHub 原生 `schedule` 已恢复（见 README《定时》）。
> 因此外部 cron 服务**不再是必需项**，本文档仅作为「日后改回私有仓库」时的备用方案保留。
> 当前工作流内声明 `cron: "30 1 * * *"`（UTC）= 每天 09:30 北京时间。
