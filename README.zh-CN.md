# SagaSmith Web

[English](README.md) · [官方网站](https://sagasmithai.github.io) ·
[平台总览](https://github.com/SagaSmithAI/.github/blob/main/profile/README.md) ·
[公开内容目录](https://github.com/SagaSmithAI/SagaSmith-dnd-content-library) ·
[全部仓库](https://github.com/orgs/SagaSmithAI/repositories) · [安全策略](SECURITY.md) ·
[运维手册](docs/operations.md)

SagaSmith Web 是 SagaSmith 的托管浏览器产品与控制平面。本仓库同时包含 PWA、FastAPI
API/BFF、campaign 协作、实时房间、Forge、Module Studio、Hosted Agent 编排和部署运维。

仓库源码公开可见，但仍受 [LICENSE](LICENSE) 中的专有条款约束。项目当前处于活跃 Alpha；
现有 component lock 是经过审查的兼容性锁，并不代表已经发布了正式版本。

历史实现标识保持兼容：Python 包名为 `sagasmith_service`，distribution/CLI 为
`sagasmith-service`，默认 Compose project 为 `sagasmith-service`。产品名统一使用
**SagaSmith Web**。

## 包含的产品能力

```text
SagaSmith Web
├── 浏览器前端与可安装 PWA
├── FastAPI API/BFF 与账户控制平面
├── Campaign 准入、协作、实时房间和 revision 投影
├── 持久 RoomTurnJob 与 Module Studio worker
├── Hosted Agent、身份、媒体和 workspace 生命周期
├── SagaSmith Forge 目录、审查、moderation 与安装
└── PostgreSQL、Redis、私有对象存储、代理、备份和可观测性
```

具体游戏状态始终由匹配的领域 MCP 管理。SagaSmith Web 只管理托管工作流和 audience-safe
投影，不会成为 D&D、CoC 或 Narrative 的第二份权威数据库。

### 产品功能面

- **账户与 campaign：** 注册、法律条款接受、session 生命周期、邀请、join approval、角色变更、
  权限撤销、plan/quota 记账和管理员审计。
- **实时房间：** 共享与 audience-filtered 聊天、私有角色卡、同步的
  Character/Play/Combat/Module panel、玩家 intent、实时 timeline 和战术 grid。
- **Forge：** Rule/Module Pack、角色 blueprint、Soul、Skill、asset 与 Hosted Identity 共用目录、
  version、provenance、license、discussion、favorite、report 和 moderation 基础能力；已发布 release
  不可变。
- **Module Studio：** brief/source、outline approval、持久 Agent 生成、MCP-owned evidence
  review/edit、显式 finalize、不可变 compile、import 和可选 activation。Pack 是内部交付物，不是
  面向用户的创作概念。
- **Hosted Identity：** DM/Keeper Identity 固定一个已发布 Soul release，并通过显式 campaign
  assignment 获得 quota payer、MCP role 和 campaign-isolated revisioned memory。

Agent review 只是证据，不具有发布权；管理员 moderation 是独立步骤，私有或商业 source 不能进入
公开目录。

## Local Kit 与 Hosted Web

SagaSmith 有两种部署形态，但共享同一套领域契约：

| 边界 | Local Agent Kit | Hosted Web |
|---|---|---|
| 入口 | SagaSmith Agent、其他 MCP Host 或 Bot | Browser/PWA 经 Web API/BFF |
| 领域传输 | stdio 或 localhost Streamable HTTP | D&D/CoC 网络 HTTP；Narrative 进程内 stdio |
| 身份 | 本地 Host 策略 | 服务端 session 加每请求签名 delegation |
| 存储 | SQLite 与本地文件 | PostgreSQL、Redis、私有对象存储和隔离的领域状态 |
| 云账户、quota、Forge | 不需要 | 由 SagaSmith Web 管理 |
| 规则权威 | 匹配的领域 MCP | 同一个匹配的领域 MCP |

开源 Local Agent Kit 不依赖本仓库。本地与托管路径使用相同领域 handler、schema、revision、
幂等、结构化结果和 authority 语义；只有传输、认证、存储和部署方式不同。

### 当前源码仓库

| 层 | 仓库 |
|---|---|
| Agent Host | [`SagaSmith-agent`](https://github.com/SagaSmithAI/SagaSmith-agent) |
| 中立运行时 | [`sagasmith-core`](https://github.com/SagaSmithAI/sagasmith-core) |
| D&D Domain / MCP / Skills / UI | [`sagasmith-dnd`](https://github.com/SagaSmithAI/sagasmith-dnd) |
| CoC Domain / MCP / Skills / UI | [`sagasmith-coc`](https://github.com/SagaSmithAI/sagasmith-coc) |
| Narrative Domain / MCP / Skills | [`sagasmith-narrative`](https://github.com/SagaSmithAI/sagasmith-narrative) |
| 已审查的公开内容目录 | [`SagaSmith-dnd-content-library`](https://github.com/SagaSmithAI/SagaSmith-dnd-content-library) |

以前的独立 MCP、Skills、UI 和通用 Module Generator 仓库均已归档；它们不是发布输入、兼容
fallback 或新工作的目标。

## 已审查的组件锁

托管构建由 [`component-versions.json`](component-versions.json) 完整固定，schema 为
`sagasmith.release-lock/v3`，lock 为 `2026.8.29-hosted-mcp-modern`：

| 强制组件 | 审查 revision |
|---|---|
| SagaSmith Agent | `056f295360bcfa56a9ade7c6c151e9aea447df41` |
| SagaSmith Core | `eef98fcfcaa96d08c069708b33ee7717ba1625c3` |
| D&D | `587f66e0673b686a7d47d1ee266d8404ef221741` |
| CoC | `515f6a7e3ba3c2a41fff7de2624ee19e4deb6190` |
| Narrative | `3f3694401dace148684f7fab9adda5b12679dfa0` |

该锁要求 MCP `2026-07-28`、`sagasmith.authoritative-mcp/v2`、
`sagasmith.auth-context/v2` 和 modern Hosted boundary。`compose.yaml`、`.env.example`、Agent
配置与 manifest 必须一起更新。workspace override 只用于协同开发；生产环境使用
`compose.yaml` 中固定的远程 revision。

## 权威与信任边界

SagaSmith Web 管理账户、session、plan、quota reservation/usage、邀请、Hosted process、
Forge、Module Studio 工作流、房间消息和云端投影。领域 MCP 独占 campaign membership、actor
authority、phase/combat/random、revision、幂等、原子结算和 Pack activation 的权威。

现代 Hosted 路径把可信身份与玩家内容从结构上分离：

- 浏览器只发送文本与幂等键，不能选择权威 principal。
- Web 从服务端 session 派生 `user:<uuid>`；已接受的 Hosted Identity assignment 可以提供
  acting `agent:<uuid>`，但模型不能选择任一身份。
- `sagasmith.auth-context/v2` 记录 caller/workload、requester、resource owner、acting
  Host/character、精确 target/audience、allowed operations、campaign、room turn、
  `base_revision` 与 expiry。
- Web 为每个目标 MCP 签发专用 delegation。浏览器或 provider token 不会透传，每次 MCP
  请求都会重新鉴权。
- 玩家文本与可信 context 是独立字段；拼接 prompt 不是安全边界。

完整所有权与数据流见[架构文档](docs/architecture.md)和[威胁模型](docs/threat-model.md)。

## MCP 2026-07-28 与有界工具选择

modern mode 是 Hosted 默认路径。它使用 `server/discover`、每请求 protocol/capability metadata
和每请求授权，不依赖 `initialize`、`Mcp-Session-Id` 或连接级 principal。跨调用状态使用显式
campaign/revision，或由服务器签发且具有 owner/TTL 的 opaque handle。

为了避免过长 tool list 降低模型命中率：

1. Agent 只连接与当前 campaign `system_id` 匹配的 MCP；
2. MCP 提供确定排序、authorization-scoped 的私有稳定目录；
3. Web 按 system/phase/role/task 选择 facade，并与持久 turn 一起保存最多 16 个排序去重的工具
   ID；
4. 每次调用时，领域 MCP 仍重新校验 role、phase、revision、幂等和 authority。

16 是 SagaSmith Host 策略，不是 MCP 协议限制。工具投影只改善选择质量和缓存行为，不能替代
服务端权限检查；modern mode 不再把 session-mutated exposure 或 `tools/list_changed` 当作权威。

legacy 仅用于 Web、Agent 和三个领域组件整组回滚到兼容锁。禁止混用 modern/legacy 组件，也
不能把 legacy prompt 字段作为身份边界。

### MCP Tasks

`RoomTurnJob` 是覆盖整个 LLM turn 的 Web Host job，不是 MCP Task。只有真正长耗时的领域工具
返回 Task claim 后，才使用协商的 `io.modelcontextprotocol/tasks` extension；目前对应 D&D
module-draft start 工作流。普通工具仍同步执行并受 `toolTimeout` 约束；只有接受 Task claim 后
才切换到 `taskTimeout` 和带新鲜授权的 poll/cancel/recovery。

## 持久房间回合

每个房间 action 会原子写入用户消息和 `RoomTurnJob`。状态机为：

```text
queued -> running -> succeeded
            |            terminal
            +-> waiting -> 重试发布/结算
            +-> failed
            +-> cancelled
```

worker 以 lease 领取任务、持续 heartbeat、分类错误、执行有界重试，并在启动及运行期间恢复
过期的 `running`/`waiting` 作业。浏览器、Web、Agent 和 MCP 的网络重试复用同一业务幂等键。
如果 Agent/MCP 已提交但 Web 发布失败，会复用已保存的标准 MCP `CallToolResult`，不会重复执行业务
操作或计费。

action 可以携带 `base_revision`。Web 在不持有房间或数据库锁的情况下预取 authoritative
phase/revision，再进行短暂 compare-and-set；stale revision 返回可恢复的 HTTP 409。只有最后的
有序消息/outbox 结算持有短暂 per-room lock，不同房间、读取和兼容动作仍可并行。

reservation TTL 必须大于 Agent completion timeout；默认分别为 1200 秒与 900 秒。作业 heartbeat
会续租；只要 active room/Module job 仍拥有 reservation，时间戳过期本身不能释放额度。Agent
完成后先按实际 usage 结算，再进入可重试的 Web 发布。

恢复与取消接口：

- `GET /api/campaigns/{campaign_id}/room/jobs/{job_id}`
- `POST /api/campaigns/{campaign_id}/room/jobs/{job_id}/cancel`

详细状态、迁移和回滚要求见[持久房间回合运维](docs/room-turn-jobs.md)。

## Hosted Agent 结果、媒体与 workspace

Hosted Worker 保留标准 MCP text、image、audio、resource 和 embedded-resource 内容。Web 原样保存
`CallToolResult`，再把通过校验的媒体内部投影为 `sagasmith.host-media/v1` envelope 和私有对象
ID，并执行 room audience、大小、checksum 和幂等 artifact key 检查。房间/群聊图片与战斗 grid
因此不需要私有的替代 MCP wire protocol。

每个 conversation 使用一个有界 worker 进程。Supervisor 限制 worker 数量与 spawn 并发、合并
同一 conversation 的并发启动，并在容量耗尽时返回 503，而不是无限创建进程。托管状态只位于
`/workspaces/hosted-v1`，每个目录具有 owner marker 和 opaque workspace ID。启动时会恢复 crash-left
marker；成功终态立即清理已登记状态；TTL/LRU 再限制目录数量和总字节。未知、格式错误、外部、
legacy、symlink 或 active 目录会保留给运维人员检查，不会被自动删除。

## 投影、缓存与实时交付

Web 不直接读取领域权威数据库。MCP receipt 驱动 revisioned、audience-safe Web projection 与
durable outbox。projection cache key 包含 authority revision；成功提交只使受影响 scope 失效，
failed、rolled-back 与 no-op 不失效。工具目录缓存只跟 authorization/catalog scope 变化，不会因
每次 combat write 全量刷新。

房间和 Module SSE 使用 Redis wake-up 加 PostgreSQL cursor replay；数据库 reconciliation 只是漏
事件兜底。结构化 activity/suggestion 字段和复合索引避免每次请求扫描完整 campaign history。

## 本地开发

需要 Python 3.12、`uv`、Git 和支持 Compose 的 Docker。使用 workspace build 时，请按
`compose.workspace.yaml` 中的目录名把上面的五个源码仓库放在本仓库的同级目录；该 override
还会挂载公开 content library。

准备配置：

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force secrets
Copy-Item config/agent-config.example.json secrets/agent-config.json
```

替换所有 `replace-*`，设置 `OPENAI_API_KEY`，并分别为 `SAGASMITH_SESSION_SECRET`、
`SAGASMITH_WORKER_SERVICE_TOKEN` 和 `SAGASMITH_AUTH_CONTEXT_SECRET` 使用至少 32 字节的随机值。
禁止提交 `.env` 与 `secrets/`。

启动同级 worktree 联调构建：

```powershell
docker compose -f compose.yaml -f compose.workspace.yaml config
docker compose -f compose.yaml -f compose.workspace.yaml up --build
```

Caddy 后的存活接口是 `http://127.0.0.1/api/health`，就绪接口是
`http://127.0.0.1/api/ready`。

快速本地检查：

```powershell
uv sync --frozen --all-extras
uv run ruff check .
uv run pytest
```

async hot-path harness 默认使用一次性 SQLite：

```powershell
uv run python scripts/benchmark_async_hotpaths.py --concurrency 4 --iterations 5
```

使用显式 gated 的一次性 PostgreSQL 模式前请阅读[异步数据库 hot-path 手册](docs/async-database-hotpaths.md)，
不得使用真实生产 campaign 数据进行 benchmark。

## 容器验收

hermetic acceptance stack 使用本地 deterministic OpenAI-compatible provider，同时运行真实固定的
Agent 与领域组件：

```powershell
docker compose -p sagasmith-service-e2e -f compose.yaml -f compose.e2e.yaml up -d --build --wait --wait-timeout 300
uv run python scripts/container_e2e.py
uv run python scripts/container_fault_e2e.py
docker compose -p sagasmith-service-e2e -f compose.yaml -f compose.e2e.yaml down --volumes --remove-orphans
```

它覆盖 D&D、CoC、Narrative discovery/call，requester 与 acting Host 分离、有界工具 facade、quota
与幂等、Module Studio 和 Pack activation、Redis/MCP/Agent/worker 重启恢复、workspace 清理、权限
撤销与审计 receipt。完整 CI/发布证据见[验收矩阵](docs/test-matrix.md)。

## 生产部署、升级与回滚

生产环境只使用 `compose.yaml`，Docker 会构建 component lock 中精确固定的远程 revision。按上文
复制配置、提供真实 secret 与模型凭据，然后验证候选版本：

```powershell
uv run python scripts/audit_components.py --fetch --strict
uv run python scripts/audit_components.py --scope build --strict --json
docker compose config
docker compose up -d --build
docker compose ps
```

对外服务前必须设置真实 `SAGASMITH_SITE_ADDRESS`、`SAGASMITH_PUBLIC_ORIGIN`、secure cookie、
私有对象存储凭据和 HTTPS。只有 Caddy 的 80/443 端口应公开；PostgreSQL、Redis、MinIO、MCP、
Agent、worker metrics 与 `/metrics` 均应留在私有网络。

升级前应备份 PostgreSQL、对象存储、D&D/CoC state 和 Agent workspace，验证 manifest，拉取并
审计新 component lock，并在开放新 worker 前运行 `alembic upgrade head`。首次引入
`room_turn_jobs` 时需要先排空旧 replica，因为旧版本不能理解新的持久状态。

回滚时先停止接收新 action，等待 active job 进入终态，保留数据库和对象备份，再把 Web、Agent
和所有领域作为一个兼容锁整体回滚。需要保留 job/media 时不要 downgrade room-job migration，
也不要只把单个组件切换成 legacy。完整备份、restore drill、release 与 rollback 流程见
[运维与恢复](docs/operations.md)。

## 可观测性

- `/api/health` 提供 liveness；`/api/ready` 检查 PostgreSQL、Redis/rate limiting、私有对象存储、
  Agent 和所有领域 runtime。
- `/metrics` 暴露低基数 service、MCP 阶段、projection、durable job、quota、database、outbox 和
  realtime 指标；user、campaign、room、job 与 tool args 不能成为 metric label。
- `traceparent`、`tracestate` 和 `baggage` 会贯穿 Web、Agent 与 MCP，并以有界字段随 durable job
  保存。
- `module-worker:9101/metrics` 与 `agent:8910/metrics` 只存在于私有网络。

可选 observability profile 提供 Prometheus、Grafana、Loki、Tempo、OTLP Collector 与 Alloy：

```powershell
docker compose -f compose.yaml -f compose.observability.yaml --profile observability up -d
```

修改 loopback binding 前必须设置 `GRAFANA_ADMIN_PASSWORD` 并保护 dashboard/ingestion endpoint。
指标、告警、retention 与运行限制见[运维手册的可观测性章节](docs/operations.md#health-and-observability)。

## 安全要点

- 禁止提交 `.env`、`secrets/`、provider credential、私有 Pack、商业原文、campaign export、备份
  内容或 worker state。
- 浏览器身份、prompt、Soul 和模型输出都不能授予 MCP authority。
- 公开 release 拒绝私有/商业 source 与 executable rule；发布后不可变，管理员 moderation 与
  Agent review 是两个独立步骤。
- backup script 生成的备份可迁移但不自带加密；应转存到加密的异地存储，部署 secret 另行托管。
- 安全问题必须按 [SECURITY.md](SECURITY.md) 私下报告；未经授权不得测试他人的 campaign 或生产
  部署。

## 文档导航

- [架构与 authority](docs/architecture.md)
- [持久房间回合](docs/room-turn-jobs.md)
- [部署、备份、恢复与回滚](docs/operations.md)
- [威胁模型](docs/threat-model.md)
- [验收矩阵](docs/test-matrix.md)
- [Forge 与社区](docs/community.md)
- [前端模块](docs/frontend-modules.md)
- [异步数据库 hot path](docs/async-database-hotpaths.md)
- [仓库改名记录](docs/repository-rename-checklist.md)
