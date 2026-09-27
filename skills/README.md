# Shared Open WebUI skills

Open WebUI does **not** load this folder automatically. Files here are the git copy so a push/clone does not lose them. Import into the running app separately.

| Path | What it is |
| --- | --- |
| `frontend-slides/` | Upstream [zarazhangrui/frontend-slides](https://github.com/zarazhangrui/frontend-slides) (MIT), without `.git`, Claude `plugins/`, and `.claude-plugin/` |
| `frontend-slides.open-webui.json` | Open WebUI Workspace Skills import (markdown + inlined CSS/templates, public `user:*` read) |

## How Open WebUI uses this

- **Workspace → Skills → Import JSON** → `frontend-slides.open-webui.json` (one shared skill, not per-user copies).
- **Workspace → Knowledge** → upload `frontend-slides/` so `kb_exec` / `view_file` can read templates/scripts. Scripts are readable, not executed.

On `chat.ailib.io.vn` this was already imported (skill + Knowledge collection `frontend-slides`, shared with every logged-in user).

## Do not commit

- `/home/anhtri/open-webui-deploy/.env`
- One-shot JWT upload helpers
- `frontend-slides-kb-staging`
