# Historical Hermes/Open WebUI architecture

> **Superseded on 2026-09-11. This is not the current runtime architecture or an operator runbook.**

An earlier design proposed two execution lanes:

```text
ordinary chat: Open WebUI -> model provider
agent tasks:   Open WebUI -> Hermes -> model provider + MCP
```

In that design, Hermes would own the agent loop, approvals, retries, context compression, task/session state and artifact orchestration. Open WebUI would remain the user interface, authentication layer, file/RAG surface and artifact viewer.

That design was retired because embedding a second orchestration/persistence path inside Open WebUI duplicated system prompts, model/tool loops, SSE handling, context compaction and state ownership, and contributed to startup/chat stability risk.

The current decision is one native Open WebUI pipeline. Hermes is not an embedded runtime dependency. The separate `hermes-agent` repository remains read-only reference material only.

Current canonical documents:

- `open-webui/OPEN_WEBUI_NATIVE_ARCHITECTURE_AND_STATUS.md`
- `open-webui/OPEN_WEBUI_LOCAL_DATA_AND_PROTECTION.md`
- `open-webui/OPEN_WEBUI_RELEASE_GATES.md`
- `open-webui/docs/WINDOWS_DEVELOPMENT.md`

Do not use historical Hermes gateway, workspace, state-database, `execution_mode=hermes_harness`, session-mapping or dual-memory instructions for the current Open WebUI checkout.