# FamilyOS distribution guide

[Download v0.1.0](https://github.com/frankrainrp/FamilyOS-Showcase/releases/tag/v0.1.0) · [中文介绍](README.zh-CN.md)

| Package | Contents | Start | Address |
|---|---|---|---|
| `FamilyOS-API-Windows-x64.zip` | Public API, migrations, examples/tests, Python runtime and pinned dependencies | `Run-Demo.cmd` or `Start-Server.cmd` | `http://127.0.0.1:8080` |
| `FamilyOS-Source.zip` | Public API source, Dockerfile, Compose, examples, tests and optional activity-processing source | Python 3.12+ or Docker | `http://127.0.0.1:8080` |

Extract the Windows package into a writable folder. No global Python installation or administrator rights are needed. `Run-Demo.cmd` creates fictional members and tasks through a temporary HTTP server, prints four PASS results, then removes its temporary data. `Start-Server.cmd` runs the real API on loopback and keeps a database in `data/familyos.db`. Stop the console with Ctrl+C.

The public edition has **no production browser UI or separate end-user client installer**. The bundled HTTP demo is the review client. `agents/windows_activity/collector.py` is optional processing source, not a complete installed screenshot-capture service. For Linux/macOS, use the source package or Docker Compose.

Models and speech endpoints are optional and configured through environment variables. The basic demo requires no model, API key, household server or external account. Python 3.12.10, websocket-client 1.9.2, Pillow 12.2.0 and tzdata 2026.2 retain their distribution notices in the Windows package.

Keep the API on loopback: its username-only sign-in represents trusted local identity, not public internet authentication. No private production UI, household database, raw screenshot, model weight or deployment credential is included.

Two illustrations are deferred: [technical and non-technical diagram brief](DIAGRAM_BRIEF.md).

中文：Windows 便携包包含 Python 运行时。解压后用 `Run-Demo.cmd` 运行虚构数据演示，或用 `Start-Server.cmd` 启动本地 API。公开版没有生产网页客户端，不能当作含完整 UI 的整套软件。源码包支持 Python / Docker 部署；个人数据、模型、私人服务地址均不打包。
