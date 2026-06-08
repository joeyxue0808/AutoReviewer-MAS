# AutoReviewer-MAS V3.0: 轻量级架构演进与能力补全技术蓝图

## 1. 演进愿景与核心目标
在 V2.0 解决代码幻觉与沙盒安全的基础上，V3.0 致力于将 AutoReviewer-MAS 打造成**“开箱即用、低成本、高智能”**的轻量级审查中台。系统将彻底移除对重型中间件（Redis、PostgreSQL）的依赖，通过内嵌数据库实现持久化与向量检索；同时引入“主动纠错与猜测授权”机制，赋予 Agent 在容错场景下的自主探索能力。

---

## 2. 核心架构“瘦身”与降本方案 (Lightweight Infrastructure)

本章节改造必须完全兼容现有的 LangGraph 状态机拓扑与 FastAPI 异步入口。

### 2.1 零外部依赖的持久化与队列
*   **状态机 Checkpointer 降级**：废弃 `langgraph-checkpoint-postgres`。全面采用 `langgraph-checkpoint-sqlite`。在项目根目录生成 `state.db`，实现 Graph 状态的本地文件级持久化，足以支撑中小团队的并发 MR 审查。
*   **内置异步任务队列**：移除 Redis Stream 依赖。在 `app/infra/worker.py` 中，使用 Python 原生的 `asyncio.Queue` 结合 FastAPI 的 `BackgroundTasks`。Webhook 接收 payload 后即刻压入内存队列，后台 Worker 协程循环消费，实现“零组件部署”的流量削峰。

### 2.2 渐进式沙盒 (Opt-in Sandbox)
*   **自适应降级执行**：重构 `app/sandbox/factory.py`。系统启动时检测本地环境（是否有 Docker 进程/权限）。若无 Docker 依赖，系统将自动降级为“静态审查模式（Static Mode）”——仅流转 `reviewer_node` 和 `fixer_node`，跳过 `tester_node`，使项目可以被任意开发者 `git clone` 后在本地笔记本秒级启动。

### 2.3 LLM Prompt 缓存加速 (Prefix Caching)
*   **API 成本压榨**：在 `app/core/llm_factory.py` 中，针对所有支持 Prefix Caching 的模型（如 Claude 3.5 Sonnet, vLLM 部署模型），在 System Prompt（包含公司规范、超大 Repo-Map）的组装处，添加 `{"type": "ephemeral"}` 缓存标记。只为 Diff 增量 token 付费，实现 50% 成本下降与 3 倍速度提升。

---

## 3. 语义级全局检索补全 (Embedded Vector RAG)

为解决大模型“仅看 Diff 导致全局逻辑断层”的劣势，V3.0 将在不增加运维负担的前提下引入本地化向量检索。

### 3.1 Serverless 向量引擎 (LanceDB)
*   **技术选型**：引入 `lancedb`。它无需独立部署容器，直接将向量索引以文件形式存储在本地 `.lancedb/` 目录下。
*   **增量索引构建 (Indexer)**：新增 `app/rag/indexer.py`。当指定分支（如 `main`）发生 Merge 时，后台任务通过 `tree-sitter` 解析全量代码库及 Markdown 文档，切分为函数级 Chunk，调用轻量级 Embedding 模型（如 `BGE-m3`）存入 LanceDB。

### 3.2 语义搜索 MCP 工具绑定
*   **Agent 能力扩充**：在 `app/tools/` 目录下新增 `semantic_code_search(query: str, top_k: int = 3)` 工具。
*   **兼容逻辑**：`reviewer_node` 和 `fixer_node` 通过 `bind_tools()` 获取此能力。当发现 Diff 中调用了不熟悉的内部函数，或涉及特定的业务词汇（如“支付防重”），Agent 可自主调用该工具，将 LanceDB 返回的相关源码上下文注入到当前的 reasoning loop（推理循环）中。

---

## 4. 主动探索与猜测授权机制 (Autonomous Exploration & HITL)

彻底改变传统 Agent“遇到错误就崩溃”的刻板行为，实现基于人机对话（CLI / Webhook Comment）的柔性容错流转。

### 4.1 异常捕获与意图推测 (ErrorRecoveryNode)
*   **节点定义**：在 LangGraph 中新增 `error_recovery_node`。当 `tester_node`（沙盒执行错误，如找不到构建脚本）或 `fixer_node`（工具调用失败）抛出异常时，Graph 路由至此节点。
*   **LLM 猜测逻辑**：传入异常堆栈与当前上下文，要求大模型输出包含推测选项的 JSON。
    *   *数据结构更新 (`app/schemas/state.py`)*：在 `ReviewState` 中新增 `hitl_options: List[Dict[str, str]]` 字段。
    *   *输出示例*：
```json
        [
          {"id": "1", "action": "run_tool", "command": "npm run test", "desc": "尝试使用 npm 替代 yarn 执行单测"},
          {"id": "2", "action": "skip_test", "command": "none", "desc": "跳过沙盒测试，直接提交当前重构代码"}
        ]
        ```

### 4.2 全端人机交互中断 (Interrupt & Resume)
*   **LangGraph 悬挂**：设置 `interrupt_before=["error_recovery_node"]`。
*   **多渠道授权响应**：
    *   **CLI 模式**：通过 `rich` 库在终端渲染交互式菜单，开发者按下对应数字键后，继续 Graph 流程。
    *   **VCS 平台模式 (GitLab/GitHub)**：调用 VCS API 在当前 MR 下发表一条包含选项的评论。系统 Webhook 监听开发者的评论回复（如 `@bot 选 1`）。解析后，调用 Graph 的 `update_state(thread_id, {"selected_option": "1"})` 释放挂起状态，Agent 根据人类授权的意图继续自主探索。

---

## 5. 核心流转架构兼容确认 (Architecture Compatibility Checklist)

以上 V3.0 的新增特性将与你原有的设计形成完美闭环：

1.  **VCS 多态网关不变**：GitLab, GitHub 和 CLI 模式依旧共享同一个 Graph，仅在输入和人类交互的输出通道（Comment vs Terminal）上做差异化处理。
2.  **9 大语言矩阵 (Language Matrix) 增强**：如果 `language_matrix` 中预设的 `test_command` 在特定项目中失效，将无缝触发 `ErrorRecoveryNode`，由 Agent 读取项目目录（如探测到 `pom.xml` 或 `Makefile`）后向人类提供新的编译命令选项。
3.  **SearchReplaceBlock 核心不变**：Fixer 输出代码依然严格遵从“搜索/替换块”结构，确保大模型幻觉消除策略在轻量化版本中依旧生效。

## 6. 实施路径建议
供后续 Claude Code 辅助生成代码时的 Phase 拆解：
*   **Sprint 1**: 移除 Redis/Postgres，完成 SQLite 与 BackgroundTasks 改造。
*   **Sprint 2**: 接入 LanceDB，编写增量索引脚本与语义查询工具。
*   **Sprint 3**: 实现 `ErrorRecoveryNode` 与跨平台的互动式授权流（HITL）。

---

## 7. 实施状态 (Implementation Status)

> 本章节记录 V3.0 蓝图的实际开发进展，供团队参考。

### Sprint 1: 安全加固 + 测试基础设施 ✅

| 任务 | 状态 | 说明 |
|------|------|------|
| 删除死代码 `app/gitlab/` | ✅ 完成 | 已被 `app/vcs/gitlab_client.py` 完全取代 |
| ShellSandbox 路径穿越修复 | ✅ 完成 | `and` → `or` 逻辑修复，兼容 `/` 和 `\` 分隔符 |
| 测试基础设施搭建 | ✅ 完成 | `tests/` 目录、`conftest.py`、`pytest.ini` |
| Critic 单元测试 | ✅ 完成 | 8 个测试用例覆盖所有规则 |
| DiffAnalyzer 单元测试 | ✅ 完成 | 语言检测、Chunk 拆分、token 估算 |
| PatchApplier 单元测试 | ✅ 完成 | apply/try_apply、错误诊断 |
| LanguageMatrix 单元测试 | ✅ 完成 | 9 语言后缀映射和配置查询 |

### Sprint 2: RAG 语义检索 ✅

| 任务 | 状态 | 说明 |
|------|------|------|
| `app/rag/indexer.py` | ✅ 完成 | LanceDB 索引器，支持 AST 分块 + 行窗口降级 |
| `app/tools/semantic_code_search.py` | ✅ 完成 | LangChain @tool，语义搜索代码库 |
| 远程嵌入 API 支持 | ✅ 完成 | OpenAI-compatible embedding API |
| 本地嵌入降级 | ✅ 完成 | sentence-transformers 可选 |
| 随机向量降级 | ✅ 完成 | 开发测试用，无嵌入模型时可用 |

### Sprint 3: 错误恢复 + 前缀缓存 ✅

| 任务 | 状态 | 说明 |
|------|------|------|
| `app/agents/nodes/error_recovery.py` | ✅ 完成 | 指数退避 + jitter，最大 30 秒等待 |
| `ReviewState` 新增 `error_type`/`last_node` | ✅ 完成 | 错误类型和出错节点追踪 |
| Reviewer/Fixer 错误类型标注 | ✅ 完成 | 429/timeout/connection 自动分类 |
| Workflow 集成 error_recovery_node | ✅ 完成 | `_after_reviewer` 路由到 error_recovery |
| Error recovery 单元测试 | ✅ 完成 | 错误次数上限和恢复逻辑 |
| 前缀缓存 | ⏳ 待定 | 依赖 LLM Provider 支持，需实际 API 验证 |
| Reviewer map-reduce 模式 | ⏳ 待定 | 已有 Send API 动态扇出，reduce 阶段待实现 |

### Sprint 4: 零依赖模式 ✅

| 任务 | 状态 | 说明 |
|------|------|------|
| `app/infra/local_queue.py` | ✅ 完成 | asyncio.Queue 进程内队列 |
| `app/infra/sqlite_checkpointer.py` | ✅ 完成 | SQLite + MemorySaver 降级 |
| `app/sandbox/null_engine.py` | ✅ 完成 | 空沙箱，始终返回成功 |
| `app/sandbox/factory.py` | ✅ 完成 | Docker → Shell → Null 自适应降级 |
| `app/infra/queue.py` 工厂函数 | ✅ 完成 | `create_queue()` 根据配置选择队列 |
| `app/infra/checkpointer.py` 降级 | ✅ 完成 | Postgres → SQLite → MemorySaver |
| Worker 支持 local_mode | ✅ 完成 | 使用 `create_queue()` 替代硬编码 |
| `settings.yaml.example` 更新 | ✅ 完成 | 新增 `local_mode` 配置段 |
| Sandbox Factory 单元测试 | ✅ 完成 | NullSandbox 测试 |

### Sprint 5: 可观测性 + 文档 ⏳

| 任务 | 状态 | 说明 |
|------|------|------|
| `/health` 存活探针 | ✅ 完成 | 进程存活检查 |
| `/ready` 就绪探针 | ✅ 完成 | 队列连通检查 |
| 版本号更新 | ✅ 完成 | 0.4.0 → 0.5.0 |
| README.md 更新 | ✅ 完成 | V3.0 功能说明 |
| README.zh-CN.md 更新 | ✅ 完成 | 中文文档同步 |
| CLAUDE.md 更新 | ✅ 完成 | 架构图、设计决策、测试指南 |
| Blueprint 状态更新 | ✅ 完成 | 本章节 |
| Langfuse 全链路追踪增强 | ⏳ 待定 | 需要实际 Langfuse 环境验证 |
| structlog 结构化日志 | ⏳ 待定 | 可选依赖，不影响核心功能 |
| docs/ 目录文档 | ⏳ 待定 | 部署/配置/开发/架构文档 |

### 已实现但蓝图未规划的改动

| 改动 | 说明 |
|------|------|
| `requirements.txt` 新增测试依赖 | pytest, pytest-asyncio, pytest-cov, httpx |
| `requirements.txt` 新增 RAG 依赖 | lancedb |
| `config/settings.yaml.example` 新增 RAG 配置 | embedding_model, embedding_api_base, db_path |
| 集成测试 `test_workflow_graph.py` | Graph 拓扑、条件路由、报告格式化 |