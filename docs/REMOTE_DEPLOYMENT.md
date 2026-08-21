# 远程完整工作台部署（Cloudflare Named Tunnel）

远程 v0.1.0 与本机版共用同一套完整工作台 UI。远程模式隐藏本机路径选择和覆盖源文件，改为共享项目、可续传上传、编辑租约、FIFO 分析队列和严格下载导出。支持 `.visionproj`、`.srproj`、Saige JSON 和按类别分组的文件夹。

## Fixed security boundary

- Origin bind: `127.0.0.1:8770` only.
- Public hostname: `saige-label-reviewer-beta.saigeai.com`.
- Cloudflare Access identity: exact email suffix `@saigeai.com`.
- The application requires both the authenticated email header and an Access
  assertion. The Tunnel route must additionally enable **Protect with Access**
  so `cloudflared` validates the assertion before proxying the request.
- 上传分块 8 MiB，每块和整文件均做 SHA-256；单项目上限 20 GiB。
- 所有 `@saigeai.com` 成员共享项目。项目默认只读，编辑租约 2 分钟、30 秒心跳；管理员可强制接管。
- 全局单 worker FIFO；`device=auto` 优先 CUDA，初始化不可用时才在开始前切换 CPU。
- 存储根固定为 `E:\remote\SaigeLabelReviewer`，最多管理 1 TiB，并保留至少 200 GiB 空闲。
- SQLite WAL 持久化项目、人工会话、租约、队列、导出和审计。原始上传保持只读。
- 7 天无明确活动进入回收区，24 小时后清理；后台轮询不刷新到期时间。

## Local verification

The development identity is deliberately opt-in and works only with a
loopback Host header:

```powershell
.\.venv\Scripts\python.exe .\run_remote.py `
  --dev-auth-email developer@saigeai.com `
  --expected-hostname saige-label-reviewer-beta.saigeai.com `
  --port 8770
```

Open `http://127.0.0.1:8770`. Do not use `--dev-auth-email` in the installed
service or Tunnel deployment.

## Cloudflare configuration order

1. Create the Cloudflare Access self-hosted application for the complete
   hostname before adding a Tunnel public route. Access is deny-by-default.
2. Add an Allow policy whose **Include** selector is **Emails ending in** and
   whose value is exactly `@saigeai.com`. Do not use `Everyone` or an OTP login
   method by itself as the Include rule.
3. If email OTP is the chosen identity provider, enable One-time PIN and keep
   the domain restriction above. A corporate IdP may be used instead.
4. Create a named Tunnel and route the hostname to `http://127.0.0.1:8770`.
5. On the published application route, enable **Protect with Access**. For a
   locally managed Tunnel, copy `deploy/cloudflare/config.yml.example`, fill in
   the Tunnel UUID, credentials path, Zero Trust team name, and Access AUD tag.
6. Let the Tunnel route create the proxied DNS CNAME to
   `<TUNNEL-UUID>.cfargotunnel.com`. Named Tunnel DNS must remain proxied; the
   earlier DNS-only requirement for GitHub Pages does not apply to this design.
7. 用 `deploy/configure-remote.ps1` 将真实 Access team domain、AUD 和管理员邮箱原子写入 `E:\remote\SaigeLabelReviewer\config\remote-config.json`；真实配置不进入 Git。
8. 本机与认证公网测试通过后，以管理员身份运行 `deploy/install-cloudflared-service.ps1`，再运行 `deploy/install-remote-origin-task.ps1` 注册源站自动任务。两者均配置失败自动重启。

Generated Tunnel credentials, Access tokens, and account identifiers are
machine secrets. Store them outside the repository and never commit them.

## Verification gates

Before enabling the public route:

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pip_audit --local --progress-spinner off
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src tests run.py run_remote.py deploy/migrate-legacy-remote.py
node --check src/saige_reviewer/static/plot-math.js
node --check src/saige_reviewer/static/upload-sha256.js
node --check src/saige_reviewer/static/app.js
```

After enabling the route, verify all of the following:

- an address outside `@saigeai.com` cannot reach the application;
- two valid members see the same shared projects, but only the lease holder can edit;
- all four formats can resume upload, review, analyze, compare images and download strict exports;
- the result summary reports actual CUDA/CPU, dtype and queue position;
- direct requests to the loopback origin without explicit development mode are rejected;
- `/api/open`, `/api/overwrite`, and `/api/pick-path` remain unavailable remotely;
- the response is served through Cloudflare and the Tunnel stays healthy after
  a Windows restart.

旧 v0.0.1 数据迁移脚本默认为 dry-run，不删除旧目录：

```powershell
.\.venv\Scripts\python.exe .\deploy\migrate-legacy-remote.py `
  --legacy-root 'E:\remote\SaigeLabelReviewer\legacy-v0.0.1-YYYYMMDD-HHMM' `
  --storage-root 'E:\remote\SaigeLabelReviewer'
```

确认后才加 `--apply`。状态检查使用 `deploy/remote-status.ps1`。切换失败时停止 v0.1.0，恢复 `v0.0.1` 程序与只读旧数据快照；永久删除旧配置或数据必须另行确认。
