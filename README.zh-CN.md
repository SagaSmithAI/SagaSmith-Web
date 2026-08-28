# SagaSmith Web

[English README](README.md) · [项目总览](https://github.com/SagaSmithAI/.github/blob/main/profile/README.md)

SagaSmith Web 是托管的浏览器产品，包含 FastAPI API/BFF、多人房间、Hosted Agent、Forge、
Module Studio 与部署运维。Python 包名仍为 `sagasmith_service`。D&D、CoC 与 Narrative 的
campaign、actor、phase、combat、random、revision 和幂等状态始终由相应领域 MCP 管理；Web
只保存托管工作流、收据、可重建的按 revision 投影与缓存。

## 可靠房间回合

每个 action 会先原子写入用户消息和持久 `RoomTurnJob`。worker 使用 lease、heartbeat、重试
预算和启动恢复推进 `queued/running/waiting/succeeded/failed/cancelled` 状态。浏览器网络重试、
Web 重启和 worker 崩溃都复用同一业务幂等键；Agent 已完成而 Web 尚未发布时，会复用已保存的
标准 MCP `CallToolResult`，不会再次发起业务操作或重复计费。

`RoomTurnJob` 是 Web Host 的完整 LLM 回合作业，不等同于 MCP Tasks。MCP Tasks 只用于单个
经过能力协商的长耗时领域工具。Agent/MCP 等待期间不持有房间或数据库锁；只有最终消息和 outbox
结算使用短暂 per-room 串行。客户端可提交 `base_revision`，stale revision 会返回可恢复的 409。

配额 reservation TTL 必须长于 Agent completion timeout，并随 worker heartbeat 续租。过期时间
本身不会从余额中移除仍被活动作业拥有的 reservation，从而避免超卖窗口。Agent 结果会先结算，
再执行可重试的 Web 发布。

## 身份与媒体边界

Web 持久保存 `sagasmith.auth-context/v2` 所需的 caller/workload/requester/resource owner、acting
host/character、audience、allowed operations、room turn、base revision 与 expiry，但不保存或透传
浏览器 token。玩家文本与可信 authority context 使用独立结构。Hosted Agent 返回标准 MCP
text/image/audio/resource/embedded-resource 内容；Web 随后内部投影为受房间 audience 保护的
`HostMediaEnvelope` 与私有对象 ID。

## 本地开发与上线

```powershell
Copy-Item .env.example .env
docker compose -f compose.yaml -f compose.workspace.yaml up --build
uv sync --all-extras
uv run pytest
uv run ruff check .
```

生产环境只使用 [`component-versions.json`](component-versions.json) 中经审查的固定提交，不以归档
仓库作为 release input 或 fallback。升级、监控、排空与回滚步骤见
[`docs/room-turn-jobs.md`](docs/room-turn-jobs.md) 和 [`docs/operations.md`](docs/operations.md)。
