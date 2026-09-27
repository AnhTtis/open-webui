# Deploy Open WebUI (localai)

Tunnel Cloudflare **giữ tên `open-webui`**. Không đổi.

| | |
| --- | --- |
| Public URL | `https://chat.ailib.io.vn` |
| Host bind | `127.0.0.1:18473` → container `:8080` |
| SQL | SQLite in Docker volume `open-webui-data` (Postgres 16 + pgvector is a later target) |
| Vector | Chroma on-disk in the same volume |
| Image | build from this repo, branch `claude/native-memory-stability` |

Do not bind host 3000 or 8080 (8080 is reserved for a different plan). Do not publish `18473` on `0.0.0.0`.

## Secrets

This directory is the recipe. The live secret file is **not** in git:

- Copy `.env.example` → `deploy/.env` (gitignored), **or**
- Keep `/home/anhtri/open-webui-deploy/.env` (mode 600)

Never commit `WEBUI_SECRET_KEY`.

## Cloudflare dashboard

Zero Trust → Tunnels → **open-webui** → Public Hostname:

- Subdomain: `chat`
- Domain: `ailib.io.vn`
- Type: HTTP
- URL: `http://127.0.0.1:18473`

## Signup

First visitor at the URL becomes admin; the app then disables signup. Re-enable in Admin → Settings → General. Default role stays `pending`. Approve `pending` → `user`.

## Skills / Knowledge

Shared skill source lives in `../skills/` (git). Import JSON and Knowledge upload are operational steps against the running instance; they are not applied by compose.
