# Superseded implementation plan

> This plan has been consolidated into the native Open WebUI documentation. It is no longer maintained as a separate implementation source.

Canonical documents:

- [`open-webui/OPEN_WEBUI_NATIVE_ARCHITECTURE_AND_STATUS.md`](./open-webui/OPEN_WEBUI_NATIVE_ARCHITECTURE_AND_STATUS.md) — implemented architecture and verified status.
- [`open-webui/OPEN_WEBUI_LOCAL_DATA_AND_PROTECTION.md`](./open-webui/OPEN_WEBUI_LOCAL_DATA_AND_PROTECTION.md) — authoritative local data paths, protected state and backup rules.
- [`open-webui/OPEN_WEBUI_RELEASE_GATES.md`](./open-webui/OPEN_WEBUI_RELEASE_GATES.md) — remaining work and production gates.
- [`open-webui/docs/WINDOWS_DEVELOPMENT.md`](./open-webui/docs/WINDOWS_DEVELOPMENT.md) — setup and run commands.

Current decision: native Open WebUI is the only execution pipeline. Do not restore embedded Hermes Harness code.