<h1 align="center">MemoryGuard</h1>

<!-- mcp-name: io.github.MerakOsiris/memoryguard -->

<p align="center">
  <strong>面向编程 Agent 的受治理共享记忆。</strong><br />
  本地优先的 MCP 记忆层，提供自动整理、范围规则、证据链和可逆治理。
</p>

<p align="center">
  <a href="https://pypi.org/project/agent-memguard/"><img src="https://img.shields.io/pypi/v/agent-memguard.svg?label=PyPI" alt="PyPI 版本" /></a>
  <a href="https://github.com/irisxc4/memoryguard/actions/workflows/ci.yml"><img src="https://github.com/irisxc4/memoryguard/actions/workflows/ci.yml/badge.svg" alt="CI 状态" /></a>
  <a href="https://github.com/irisxc4/memoryguard/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10 或更高版本" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-2ea44f" alt="MIT 许可证" /></a>
  <a href="README.md">English</a>
</p>

> Agent 可以正常写入，MemoryGuard 负责分类、去重、冲突识别、隔离和版本治理。
> 每次变化保留证据，之后仍可修正、恢复或回滚。
>
> **无账号、无远端服务器、无远程遥测。可选的用量遥测仅保存在本地，且只记录有界的隐私保护汇总。**

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#升级">升级</a> ·
  <a href="#知识库">知识库</a> ·
  <a href="#系统架构">系统架构</a> ·
  <a href="#隐私与安全边界">隐私边界</a>
</p>

<p align="center">
  <img src="docs/assets/neuron-graph-live.gif" alt="MemoryGuard 动态神经图：本地治理投影中的记忆分类与信号流动" width="1120" />
</p>

<p align="center">
  <sub>神经图展示受治理投影；原始对话正文不会直接进入图谱或自动注入上下文。</sub>
</p>

## v0.7.15 更新

- **自动建图：** 可信 Hook/MCP bootstrap 在后台启动项目首次建图。默认与项目独立开关
  持久化保存；关闭自动建图保留已有图谱。宿主支持的文件写入事件触发增量刷新。
- **图谱浏览：** 文件总览与符号视图、按文件筛选、跨文件关系聚合、相邻关系突出显示、
  节点详情和缩放控制。
- **缓存与部署：** 普通图谱查询直接只读访问 SQLite，避免为每个进程复制整份数据库。
  校验、导入和安装临时文件位于所属项目或部署目录的缓存中，操作结束即清理。

详见 [v0.7.15 发布说明](docs/releases/v0.7.15.md) 和
[自动建图说明](docs/codegraph-automation.md)。升级后通过现有 provider 修复/安装流程
更新运行包，再重连 MCP 或重启缓存旧配置的宿主。自动更新依赖宿主事件；首次建图
最多支持 10,000 个代码文件，较大目录需选择更具体的项目。

### 早期 MCP 与治理改进

v0.7.12 改进 MCP 发现面、有界读取和本地用量计量，同时保留现有治理动作与
MCP name。当前发布命名空间为 `io.github.MerakOsiris/memoryguard`。

- **11 个默认工具和完整目录：** 新 MCP 客户端通过 `tools/list` 获取 11 个日常
  工具，包括能力发现和治理调用。`memoryguard_capabilities` 提供分页的 MCP
  操作与经审查的 headless GUI 操作元数据；高级 MCP 名称仍可按精确名称兼容调用。
- **有界读取响应：** 可重放的成功读取以完整 MCP envelope 的 24,000 UTF-8
  字节为上限；合格的超大读取才会返回短期进程内存的 `response_ref`，不截断原始数据；
  无法安全重放或超过快照上限的读取返回有界的缩小查询提示。私有引用每页都在当前可信
  session 和 binding 下复核；写入和
  `context_bootstrap` 保留完整 receipt/强制规则合约，不能请求响应分页。
- **缓存感知用量：** 分开记录宿主上报的 cache-read 和 cache-write input，并给出
  `complete`、`partial`、`unavailable` 覆盖状态；实测 0 保持为 0，缺失数据保持未知。
  确定性单位估算明确标为估算，不声称实测 token 降幅。
- **治理调用：** 变更类 broker 目标仍要求确认和非空 idempotency key，并由目标继续执行既有
  权限、作用域和 readiness 检查。

详见 [v0.7.12 发布说明](docs/releases/v0.7.12.md) 与
[更新日志](CHANGELOG.md)。

v0.7.14 仅修正 GitHub 仓库更名后的发布元数据，不改变运行功能，见
[v0.7.14 发布说明](docs/releases/v0.7.14.md)。

更早版本的细节请见[更新日志](CHANGELOG.md)和
[GitHub Releases](https://github.com/irisxc4/memoryguard/releases)。

### Token 证据与演示

仅在已授权的本地工作区运行基准：

```powershell
python scripts/benchmark_usage_telemetry.py --workspace . --window-days 7 --sync
```

请阅读[用量基准说明](docs/benchmarks/README.md)了解宿主上报、派生、估算和不支持状态，
并按[演示录制清单](docs/benchmarks/demo-script.md)制作脱敏演示。仓库中的神经图是合成插图，
不是实际产品录屏，也不能作为用量或节省证据。


## v0.6.0 重大 V2 重构

v0.6.0 不是单纯的存储升级，而是生产数据平面的整体重构：

- **权威 V2 分域：** Memory、Rules、Evidence、Content、Runtime、Projection、Assets、CodeGraph、Skills、System 分离为明确的 SQLite 域，并由治理边界统一约束。
- **显式切换：** `V1_ACTIVE → V2_BUILDING → V2_READY → V2_ACTIVE` 全程 fail-closed；进入 READY/ACTIVE 后不会静默回落旧存储，也不会双写。
- **无损迁移：** frozen-source 准备使用一致的 SQLite online backup，校验源/目标证据，复核 live-source drift，并保留 V1 数据与 migration-backups 以支持回滚。
- **Native 路由收口：** MCP、CLI、GUI、Hook 全部显式分类；233 个切换面中 138 个实现、95 个退休，neutral/blocker 均为 0。
- **治理智能链路：** Rule lifecycle、RuleMerge、抽取/富化、External MCP 导入、provider 控制面、会话历史、Knowledge Library 与 GUI 治理统一走 V2 evidence/decision 流程。
- **运维证据：** Reference Audit、分域 SQLite 健康检查、受保护维护、回滚证据和未绑定 Agent 的安全诊断共同参与 readiness 与运维。

## 为什么需要 MemoryGuard

持久化只解决“存下来”，没有解决“以后还能不能可靠复用”。

| 没有治理 | 使用 MemoryGuard |
|---|---|
| 笔记持续堆积，没有规范状态 | 写入会被分类、去重、覆盖或标记冲突 |
| 纠错直接覆盖旧值 | 证据和 supersede 链保留变化原因 |
| Token、凭证可能继续活跃 | 敏感内容进入隔离区，不参与活跃召回 |
| 每条写入都要人工审批 | Agent 正常工作，人只治理异常和结果 |
| 原始聊天自动混入未来上下文 | 对话历史独立保存，只能显式读取 |

## 系统架构

```mermaid
%%{init: {"theme":"base","themeVariables":{"background":"#071521","fontFamily":"Arial, sans-serif","fontSize":"14px","primaryTextColor":"#EEF4F8","lineColor":"#557287","edgeLabelBackground":"#071521","clusterBkg":"#0A1A29","clusterBorder":"#27445A"},"flowchart":{"htmlLabels":true,"curve":"basis","nodeSpacing":32,"rankSpacing":48,"padding":14}}}%%
flowchart TB
    Hosts["编程 AGENT<br/>Claude Code · Codex · Cursor · TRAE&nbsp;&nbsp;&nbsp;&nbsp;"]:::host
    Gateway["本地接入<br/>MCP stdio · 重定向规则 · 生命周期 Hook&nbsp;&nbsp;&nbsp;&nbsp;"]:::gateway

    subgraph Core["治理核心&nbsp;&nbsp;&nbsp;&nbsp;"]
        direction LR
        Identity["信任边界<br/>身份 · 范围&nbsp;&nbsp;&nbsp;&nbsp;"]:::core
        MemoryAPI["长期记忆<br/>受治理读写&nbsp;&nbsp;&nbsp;&nbsp;"]:::active
        Rules["规则<br/>分配 · 作用域&nbsp;&nbsp;&nbsp;&nbsp;"]:::rule
        HistoryAPI["对话证据<br/>搜索 · 时间线&nbsp;&nbsp;&nbsp;&nbsp;"]:::history
        Knowledge["知识库<br/>切片 · FTS · 图谱&nbsp;&nbsp;&nbsp;&nbsp;"]:::knowledge
        Identity --> MemoryAPI
        Identity --> Rules
        Identity --> HistoryAPI
        Identity --> Knowledge
    end

    Stores[("V2 分域存储<br/>Memory · Rules · Evidence · Content · Runtime · Projection&nbsp;&nbsp;&nbsp;&nbsp;")]:::store
    Bootstrap["有界上下文<br/>强制规则包 · 相关记忆 · 相关知识&nbsp;&nbsp;&nbsp;&nbsp;"]:::bootstrap
    Control["人工治理<br/>CLI · 桌面控制台&nbsp;&nbsp;&nbsp;&nbsp;"]:::surface

    Hosts --> Gateway --> Identity
    MemoryAPI --> Stores
    Rules --> Stores
    HistoryAPI --> Stores
    Knowledge --> Stores
    Stores --> Bootstrap
    Control --> Identity

    classDef host fill:#12243A,stroke:#38D5C8,color:#EEF4F8,stroke-width:1.4px;
    classDef gateway fill:#0D3338,stroke:#38D5C8,color:#EEF4F8,stroke-width:2.4px;
    classDef core fill:#12243A,stroke:#557287,color:#EEF4F8,stroke-width:1.4px;
    classDef active fill:#0D383A,stroke:#38D5C8,color:#EEF4F8,stroke-width:2px;
    classDef rule fill:#3B2C18,stroke:#F3B562,color:#EEF4F8,stroke-width:1.8px;
    classDef history fill:#102F45,stroke:#73C7F5,color:#EEF4F8,stroke-width:1.8px;
    classDef knowledge fill:#24351D,stroke:#9BD36A,color:#EEF4F8,stroke-width:1.8px;
    classDef store fill:#0B1624,stroke:#7F96A8,color:#EEF4F8,stroke-width:1.4px;
    classDef bootstrap fill:#EEF4F8,stroke:#38D5C8,color:#071521,stroke-width:2.4px;
    classDef surface fill:#EEF4F8,stroke:#38D5C8,color:#071521,stroke-width:2px;
    style Core fill:#081827,stroke:#27445A,stroke-width:1px,color:#EEF4F8
    linkStyle default stroke:#557287,stroke-width:1.4px;
```

## 快速开始

### MCP Registry 元数据

本包以 `io.github.MerakOsiris/memoryguard` 提供本地 stdio MCP 服务，注册表元数据见
[`server.json`](server.json)。上方 marker 随 PyPI 包 README 一并发布；发布由 GitHub
OIDC 推送至 PyPI 和官方 MCP Registry。请以公开页面实时核验当前包版本与
`io.github.MerakOsiris/memoryguard` 的 active/latest 状态。

### 1. 安装

```bash
python -m pip install agent-memguard
```

需要桌面治理台：

```bash
python -m pip install "agent-memguard[gui]"
```

### 2. 授权当前项目

```bash
memoryguard source add .
```

### 3. 连接 / 修复编程 Agent

全局 Provider 配置始终从用户级 canonical data home 的真实 binding 重建；重复执行是幂等的，也会清理被全局配置取代的 MemoryGuard 项目级覆盖。

```bash
# 单独修复
memoryguard provider repair claude
memoryguard provider repair codex
memoryguard provider repair cursor
memoryguard provider repair trae

# 一次修复全部已检测 Provider
memoryguard provider repair all
```

重启宿主后验证：

```bash
memoryguard doctor
memoryguard mcp-status
memoryguard hooks status --provider all
```

启动桌面治理台：

```bash
memoryguard gui
```

`memoryguard-gui .` 仍可用于桌面快捷方式。裸 `memoryguard gui` 始终打开固定用户级
控制目录（Windows 默认 `%LOCALAPPDATA%\MemoryGuard`），因此无论从项目目录还是
`C:\Windows\System32` 启动都不会静默切换数据库。`MEMORYGUARD_WORKSPACE` 是显式
运维覆盖；需要指定隔离工作区时仍可使用 `memoryguard gui <project-path>` 或
`memoryguard gui --workspace <project-path>`。
它不再记住上次项目，也不会从启动目录推断工作区或弹出文件夹选择器。
在 Windows 上，`memoryguard gui` 会把原生窗口独立到后台进程，关闭 PowerShell 不会关闭 GUI。

详细说明：[Claude Code](docs/install-claude-code.md) ·
[Codex](docs/install-codex.md) · [Cursor](docs/install-cursor.md)

### 稳定的 Codex / Router 绑定

Codex/Router 绑定的是本机稳定的 Codex 程序与控制安装。账号 Profile 只是
endpoint/alias，不会成为新的记忆所有者：切换 Profile 时会自动发现或修复该
Profile，并复用已验证的 Agent 绑定和当前 active group。请求身份仍然
fail-closed；这不会让不同机器或任意账号共享记录。

## 升级

当前版本通过 Python 包管理器升级：

```bash
python -m pip install --upgrade agent-memguard
memoryguard --version
memoryguard doctor
```

安装过 GUI extra 时：

```bash
python -m pip install --upgrade "agent-memguard[gui]"
```

目前**没有**包级自更新命令。包管理器是正式升级入口；下面的
`memoryguard upgrade` 是工作区迁移，不是包自更新。

### 升级现有 V1 用户数据

先升级包，再执行完整验证迁移。正常用户数据目录不需要 workspace、data-home、apply
或 confirm 参数：

```bash
python -m pip install --upgrade agent-memguard
memoryguard --version                    # 确认已安装版本
memoryguard upgrade
memoryguard doctor
```

该命令构建 V2、校验 frozen/live source evidence、迁移 Agent/Group control，并仅在
所有门禁通过后激活；随后只删除本次成功迁移的备份批次。重复执行 `V2_ACTIVE` 是幂等的。
零写入检查使用：

```bash
memoryguard upgrade --preview
```

隔离安装仍可使用显式 workspace/data-home 高级参数。任一门禁失败都会保持非 active
并保留证据；成功激活后不会继续保留冗余迁移备份。

### 更早的 pre-V2 工作区：显式切换到 V2

v0.6.0 随包安装 `memoryguard-v2` 运维命令：

```bash
# 只读查看 manifest
memoryguard-v2 status -w .

# 生成 frozen-source V2 shadow，并只停在 V2_READY
memoryguard-v2 prepare -w . --apply

# 仅当 prepare 输出 V2_READY / ready=true 后显式激活
memoryguard-v2 activate -w . --confirm V2_ACTIVE
```

prepare 会使用 SQLite online backup 捕获一致快照，保留 V1 与
`migration-backups`，并在 READY 前检查 live-source drift；activate 在真正修改
manifest 前还会再检查一次 drift。升级过程中不要删除 legacy V1 数据，也不要删除
迁移备份。

## 知识库

桌面治理台可以把你选择的文件夹或文件集合加入本地知识库。源文件保持原位，
MemoryGuard 把检索索引写入用户数据目录，不会给每个资料项目复制一套知识库数据库；
Knowledge metadata 不会变成第二个正文存储。

| 能力 | 当前行为 |
|---|---|
| 文件/文件夹入库 | 文件夹作为一本书，选中文件作为文档进入索引 |
| 结构化处理 | 解析文档、保留章节/小节上下文、生成可追溯切片 |
| 检索 | 全文检索、可选 Embedding、分层知识图谱 |
| 自然同步 | 重新整理变更文件；部分扫描或失败不会误删以前已索引内容 |
| 删除治理 | 移入知识库回收站、恢复，或明确永久清理恢复快照 |
| 记忆候选 | 先预览带来源证据的候选，确认后才写入长期记忆 |

从桌面治理台进入**知识库**。远程 Embedding 或模型索引必须显式授权；未授权时，
本地全文检索仍可使用，不会把资料正文发送给远程 Provider。

### CodeGraph 刷新

首次构建 CodeGraph 仍必须显式确认并执行全量构建。Scope 建图后，每次成功的受信任
文件写入都可以触发该 Scope 的增量刷新，但仍要通过严格的源路径和 active binding
校验。内容 hash 未变化时不产生新 revision；删除的文件会失活；下一轮上下文只注入
一次有界的 `affected` receipt。该路径不运行 daemon 或 watcher，也不会从 shell 或
自由文本猜测路径。

### 桌面治理台页面

GUI 有 8 个可见导航入口：7 个治理页，加上独立的 Token 用量与节省页：

1. 治理总览
2. 数据源与 Agent
3. 记忆核心
4. CodeGraph
5. 规则与习惯
6. 对话历史
7. 风险信号与治理控制台
8. Token 用量与节省（独立于七个治理页）

Agent 列表显示可读的程序/provider 名称，底层 ID 只在详情中展示；没有数据时明确
显示空态。

## 规则、历史与知识分层

| 数据面 | 用途 | 上下文行为 |
|---|---|---|
| **长期记忆与规则** | 偏好、流程、纠错、事实、项目和范围化强制规则 | 强制规则在作用域/排除/冲突与语义去重后使用独立字符/Token 预算。生效条数超过 20 只是健康告警，不是硬上限，存储也不按条数封顶。敏感、损坏、单条超限和总量溢出仍失败关闭且不会静默截断；普通记录按任务相关性召回 |
| **对话历史** | 带 owner 和共享组权限的本地原文证据 | 永不自动进入 bootstrap，只能经历史工具显式读取 |
| **知识库** | 用户选择的文档资料、切片和派生索引 | 仅返回有界相关片段；候选不会静默变成长时记忆 |
| **神经图** | 导航记忆、规则、项目、Agent、会话和知识投影 | 展示安全元数据和摘要，不展示原始聊天正文 |

历史读取采用“搜索结果 → 有界时间线 → 指定 Turn/会话”的渐进路径。历史萃取先
生成预览，再经明确接受进入正常治理写入。

## 可治理内容

| 信号 | 治理动作 |
|---|---|
| 重复或过期记忆 | 查看规范记录和覆盖链，必要时恢复旧版本 |
| 相互冲突的记忆 | 同时保留两侧，直到明确裁决 |
| Secret、Token 或凭证 | 隔离，不进入活跃共享记忆 |
| 自动整理错误 | 修正、合并、锁定、恢复或按证据回滚 |
| 多个编程 Agent | 绑定到同一共享组，同时保留来源身份和作用域 |
| 强制规则 | 分配给 Agent、项目、Provider、运行角色或共享组 |

治理台不是审批队列。Agent 不必等待；人可以在需要时根据证据治理结果。

## 支持的宿主

| 宿主 | 接入方式 | 当前边界 |
|---|---|---|
| Claude Code | 全局 MCP、重定向规则、用户级 Hook | 已验证接管路径 |
| Codex | 全局 MCP、重定向规则、用户级 Hook | 已验证接管路径 |
| Cursor | 全局 MCP、重定向规则、用户级 Hook | 已验证接管路径 |
| TRAE | MCP 与重定向规则 | 未验证可靠 Hook seam，按降级能力报告 |

MemoryGuard 会如实报告 redirected、observed、operational 或 unsupported，
不会在宿主没有可靠接入点时声称已关闭其原生记忆。

## 隐私与安全边界

- MemoryGuard 以本地 MCP stdio 服务运行。
- 除非你明确授权远程模型或 Embedding 操作，受治理数据不会离开本机。可选用量遥测只写入
  `.memoryguard/usage_telemetry.sqlite`，不会上传；其中宿主 token 事件与确定性 MCP 转换事件
  均为本地保存。Token 节省比例是基于 MemoryGuard 确定性单位的估算，不是 Provider 账单；
  不上报 token 的宿主在实际测量列中会明确标为不支持。
- 知识库数据库位于 `MEMORYGUARD_HOME` 或平台用户数据目录；被选中的源文件夹不会
  获得一套独立知识库数据库。
- V2 权威工作区状态按 Memory、Rules、Evidence、Content、Runtime、Projection、
  Assets、CodeGraph、Skills 与 System 分域保存在 `.memoryguard/`；History、Source、
  Binding、Group control 也全部是 V2-native surface。切换后 legacy V1 产物继续保留
  为本地回滚/审计证据，但不再是 V2 runtime 写入目标，且只有 `memoryguard.migration`
  可以读取。
- 来源扫描默认只读；变更路径带有校验、明确作用域、来源证据和可逆状态。
- 隔离记录不会进入活跃共享记忆。
- 原始对话历史永不自动注入 bootstrap。
- 共享组历史读取按当前活跃成员关系授权，且不授予删除其他 Agent 来源的权限。

## CLI

| 命令 | 说明 |
|---|---|
| `audit [path]` | 只读审计并生成报告 |
| `open [path]` | 打开最新交互报告 |
| `explain <finding_id>` | 解释发现项的证据和风险 |
| `source <action>` | 管理授权来源 |
| `scan` | 扫描授权来源并生成覆盖账本 |
| `doctor` | 诊断 V2 manifest、分域可用性和 native coverage |
| `mcp-status` | 查看 V2 MCP/后端健康状态；租户计数要求已绑定 Agent scope |
| `hooks <action>` | 安装、检查、暂停、修复或移除宿主 Hook |
| `provider <action>` | 检查或修复全局 Provider 集成 |
| `storage audit|report` | 执行只读 Reference Audit 与分域 SQLite 健康报告 |
| `storage sweep|compact` | 执行受门控的 V2 维护；物理变更要求 ACTIVE、lease、generation 与安全证明 |
| `groups <action>` | 检查受治理共享组状态 |
| `gui [path]` | 启动交互式治理台 |
| `desktop` | 启动可信桌面执行器 |

旧 V1 的 `plan`、`apply`、`verify`、`undo`、`import` 与 `gc` 命令名可能仍被解析，
但它们只是显式 retired compatibility surface，不是 V1 runtime path；在 `V2_ACTIVE`
下返回稳定 retired 结果，不会写回 legacy store。旧数据输入只接受
`memoryguard.migration` 的显式升级流程。

以 `memoryguard --help` 和 `memoryguard <command> --help` 为当前安装版本的命令真相源。

## MCP API

默认 MCP 发现面保持精简。新 MCP 客户端通过 `tools/list` 获得以下 11 个日常工具：

| 工具 | 用途 |
|---|---|
| `memoryguard_context_bootstrap` | 加载有界的强制规则和相关记忆上下文 |
| `memoryguard_memory_search` | 按查询词、生命周期状态和有界条数搜索受治理记忆。`kind` 不是 MCP 搜索过滤项；语义重复/冲突检查属于独立的高级治理能力。 |
| `memoryguard_memory_read` | 读取单条受治理记忆 |
| `memoryguard_memory_write` | 写入并整理受治理记忆 |
| `memoryguard_memory_update` | 更新已知记录的正文、类型、召回策略或优先级；不改变生命周期状态，也不能恢复已删除记录，恢复仅由 GUI 治理完成。 |
| `memoryguard_memory_delete` | 软删除受治理记忆 |
| `memoryguard_memory_status` | 查看共享记忆状态 |
| `memoryguard_audit` | 执行只读本地治理审计 |
| `memoryguard_explain` | 解释审计发现及其证据 |
| `memoryguard_capabilities` | 发现已注册 MCP 操作和经审查的 headless GUI 操作，支持分页和按需 JSON Schema |
| `memoryguard_invoke` | 调用已发现的 MCP 或经审查的 headless GUI 操作；变更目标要求确认和 idempotency key |

高级治理能力仍保留在 GUI 和 CLI：规则生命周期、绑定与共享组、来源扫描、
CodeGraph、知识库与历史复核、Provider 控制、外部 MCP 导入及维护操作。
已有高级 MCP 名称在已安装客户端按精确名称调用时仍保持兼容，但不会出现在默认
`tools/list` 中。这样减少发现和 schema 的上下文负担，不会删除这些治理能力。

`memoryguard_capabilities` 提供完整注册的 MCP 操作和 headless GUI 操作元数据，
支持精确名称、query、domain 过滤和 offset 分页；只有 `include_schema=true` 时才在
选定页返回 JSON Schema。`memoryguard_invoke` 调用已注册目标；变更目标仍要求
`confirmed=true` 和非空 `idempotency_key`，这些证明会传递给目标的权限、作用域和 readiness 检查。

### 有界读取响应

MemoryGuard 默认压缩 JSON 文本。可重放的成功读取以完整 MCP envelope 的 24,000
UTF-8 字节为上限。小响应保持原有形状；符合回取条件的超大成功读取返回带 `response_ref` 的有界
receipt，不会静默截断。通过已注册的 broker `memoryguard_response_read` 读取页面，
它不属于默认 `tools/list`，应通过 `memoryguard_invoke` 调用。

页面是带 `next_offset` 的 UTF-8 JSON 片段；`limit` 为 4–4096 字节，offset 必须位于
UTF-8 字符边界。单个 JSON 文本 payload 可以用顶层字段名或对象型 JSON Pointer（例如
`/data/memory_id`）选择业务字段；多 content 和非 JSON 结果拒绝字段选择，只能读取完整 envelope 页面。

私有引用只存在于 MCP 进程内，最长 5 分钟，最多 16 个快照，每个最多 512,000 字节；
它绑定准确的可信 session、principal、scope 和 active binding revision。每一页都会在当前
授权下重新执行原始读取并比较 digest。拒绝、结果改变、binding/session 改变或过期会返回
稳定错误，例如 `response_ref_access_denied`、`response_ref_expired` 或
`response_ref_result_changed`；旧缓存不能绕过当前读取。公共能力元数据继续使用现有 offset 分页。
写入动作和 `context_bootstrap` 保留完整 receipt/强制规则合约，不能请求响应分页，因此分页不会
再次执行 mutation。若超大读取无法安全建立引用，则返回 `delivery.status="unavailable"` 和
`action="narrow_query"`，不会承诺可取回完整结果。

底层兼容调用目录还覆盖：

- 长期记忆读、搜索、写、更新、删除和状态查询；
- 强制规则隔离预算与有界上下文 bootstrap（条数告警、字符/Token 失败关闭）；
- 规则创建、反馈、合并治理、撤销和作用域统计；
- Agent 绑定与共享组检查；
- 来源扫描、神经图投影、导入预览和构建规划；
- 文档提取预览、候选接受和知识检索；
- 对话历史搜索、时间线、显式读取、导出、删除和萃取预览；
- Provider 安装和宿主 Agent 信息补全。

精确工具列表以 MCP `tools/list` 为准。

## 项目链接

- [PyPI 包](https://pypi.org/project/agent-memguard/)
- [GitHub Releases](https://github.com/irisxc4/memoryguard/releases)
- [更新日志](CHANGELOG.md)
- [v0.7.12 发布说明](docs/releases/v0.7.12.md)
- [v0.7.11 发布说明](docs/releases/v0.7.11.md)
- [v0.7.9 发布记录](docs/releases/v0.7.9.md)
- [v0.7.8 发布记录](docs/releases/v0.7.8.md)
- [v0.7.7 发布记录](docs/releases/v0.7.7.md)
- [v0.7.6 发布记录](docs/releases/v0.7.6.md)
- [v0.7.5 发布记录](docs/releases/v0.7.5.md)
- [v0.7.4 发布记录](docs/releases/v0.7.4.md)
- [v0.7.3 发布记录](docs/releases/v0.7.3.md)
- [v0.7.2 发布记录](docs/releases/v0.7.2.md)
- [v0.7.1 发布记录](docs/releases/v0.7.1.md)
- [v0.7.0 发布门禁](docs/releases/v0.7.0.md)
- [长期记忆连续性与无损控体积 Spec](docs/memory-continuity-storage-spec-v1.md)
- [隐私政策](PRIVACY.md)
- [使用条款](TERMS.md)
- [贡献指南](CONTRIBUTING.md)
- [贡献者许可协议](CLA.md)
- [Issue](https://github.com/irisxc4/memoryguard/issues)

## 路线图

- **发布历史：** v0.7.9 汇总 canonical 治理、本地 Token 证据、可读的多 Agent 治理，并通过 GitHub、PyPI 与官方 MCP Registry 公开分发。v0.7.8 记录前一阶段的治理、遥测与 Codex 运行时工作；v0.7.7 让已验证且唯一绑定的控制目录可以安全执行普通 Shell 下的
  provider repair，并让已安装版 Codex MCP/Hook 修复统一使用当前解释器，同时保留 Agent 与
  共享组身份；v0.7.6 统一 Codex Hook/MCP 的不可变 runtime snapshot，缩短 Hook 状态锁窗口，
  并让 bootstrap 成功/失败状态保持一致。更早版本的冲突复核、canonical 治理、共享历史、写入→
  读回与 Codex 生命周期细节见对应发布记录；v0.7.1 的 V2-only 迁移与桌面生命周期内容保留为
  历史发布背景。
- **验收边界：** Graphify 证据是专项 `3 / 3` 加上前文所述真实全仓
  export/projection；不表示 upstream Graphify 的全仓测试套件通过。
- **发布后下一步：** 扩展 CodeGraph/Skills ingestion、维护报告和迁移可观测性。
  长期记录不会仅仅因为存在时间长而被淘汰。
- **以后：** 只在需求被验证后扩展团队和企业能力。

详细设计见[长期记忆连续性与无损控体积 Spec](docs/memory-continuity-storage-spec-v1.md)。
其中 Content Plane、Delta/Checkpoint 等核心存储机制已进入 V2；Spec 中更远期的
扩展仍以对应实现与验收状态为准，不把设计稿当已交付功能。

## 贡献

欢迎提交 Issue 和 PR。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；提交 PR 即表示
同意 [CLA](CLA.md)。

## 许可证

[MIT](LICENSE)
