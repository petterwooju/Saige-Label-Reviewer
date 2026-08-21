# Remote upload deployment (Cloudflare Named Tunnel)

This mode is separate from the local review workbench. It accepts only
self-contained `.visionproj` uploads and never exposes local path selection,
source overwrite, native file pickers, or the shared local session API.

## Fixed security boundary

- Origin bind: `127.0.0.1:8770` only.
- Public hostname: `saige-label-reviewer-beta.saigeai.com`.
- Cloudflare Access identity: exact email suffix `@saigeai.com`.
- The application requires both the authenticated email header and an Access
  assertion. The Tunnel route must additionally enable **Protect with Access**
  so `cloudflared` validates the assertion before proxying the request.
- Upload chunks: 8 MiB; maximum project: 2 GiB.
- Per-user concurrency: one uploading, validating, queued, or running task.
- GPU scheduling: one global analysis worker; `device=auto` prefers CUDA and
  falls back to CPU only when CUDA cannot complete.
- Remote storage reservation: 20 GiB, with 5 GiB free-space headroom.
- Each job uses an isolated directory and per-job analysis cache. Completed,
  failed, and abandoned upload data is deleted after 24 hours.
- Results use a per-job SQLite database and are paginated; other authenticated
  users receive `404` for a job they do not own.

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
7. Install `cloudflared` and the Saige remote origin as Windows services only
   after the configuration has passed the local and authenticated public tests.

Generated Tunnel credentials, Access tokens, and account identifiers are
machine secrets. Store them outside the repository and never commit them.

## Verification gates

Before enabling the public route:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_remote_server -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src tests run.py run_remote.py
node --check src/saige_reviewer/remote_static/remote.js
```

After enabling the route, verify all of the following:

- an address outside `@saigeai.com` cannot reach the application;
- a valid `@saigeai.com` user sees only their own task list;
- a multi-chunk `.visionproj` upload completes and reports the actual CUDA/CPU
  execution device in the result summary;
- direct requests to the loopback origin without explicit development mode are
  rejected;
- `/api/open`, `/api/overwrite`, and `/api/pick-path` return `404` on the remote
  service;
- the response is served through Cloudflare and the Tunnel stays healthy after
  a Windows restart.
