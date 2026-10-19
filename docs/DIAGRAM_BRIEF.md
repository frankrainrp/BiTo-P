# FamilyOS — Illustration brief

Status: **planned; artwork deferred**. Deliver two separate images, with English labels and a Chinese caption/translation attachment.

## 1. Technical implementation map

Audience: software engineers and technical interviewers. The reader should understand components, data flow, model boundaries and tradeoffs in 30 seconds.

Show the public API edition:

1. Demo script / external client → HTTP API → session and member scope.
2. Domain services: tasks, calendar, habits and privacy/visibility rules → SQLite with 24 migrations.
3. Assistant context → optional configured local Ollama or OpenAI-compatible provider → allow-listed proposal → explicit user confirmation → domain service. No model receives raw database or shell authority.
4. Optional background lane: scheduled jobs / source monitors → domain operations and execution records. Optional redacted activity observations enter through the API; raw screenshot payloads are rejected.

Use approximately 10–12 grouped nodes. Mark optional integrations with dashed borders, and installed core paths with solid arrows. No MCP server is evidenced in this public edition; do not add an MCP logo or tool box. Do not show the private production UI, private infrastructure addresses or Home Assistant execution as delivered. Model names are deployment configuration rather than bundled weights.

## 2. Plain-language capability map

Audience: recruiters and non-technical readers. The reader should understand what the software helps a person do in 10 seconds.

Show one household organizing **Shared tasks**, **Private plans**, **Events**, and **Habits**. Show that each member sees permitted information. The optional assistant path is **Ask → Review a suggestion → Confirm → Update the plan**. Add a short outcome: **A clearer view of everyday plans**. Make clear in a caption that the public demo is an API showcase, while avoiding database and framework terms in the main story.

## Later output files

- `technical-architecture.svg` and a readable PNG export.
- `product-overview.svg` and a readable PNG export.
- English source labels, with a Chinese text attachment.

中文：后续制作一张面向专业人士的技术实现图，以及一张面向非技术人员的能力理解图。只展示代码已实现的功能；可选集成、演示流程与未来计划必须清楚区分。本轮仅整理需求，不生成图片。
