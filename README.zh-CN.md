# AutoReviewer-MAS

<p align="center">
  <strong>🤖 多智能体自动代码审查系统</strong><br>
  <em>LangGraph 编排 · 9 语言沙盒 · GitLab & GitHub · 多轮交互式审查</em>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#架构设计">架构设计</a> ·
  <a href="#功能特性">功能特性</a> ·
  <a href="#配置说明">配置说明</a> ·
  <a href="#开发指南">开发指南</a>
</p>

---

## 项目概述

AutoReviewer-MAS 是一个基于多智能体架构的自动代码审查系统。使用 LangGraph 编排 Reviewer、Fixer、Critic、Tester 四个专用 Agent，通过反馈循环迭代提升代码质量。

**核心能力：**
- 🔍 **自动审查**：深度分析代码变更，针对 9 种语言的专业审查
- 🔧 **自动修复**：生成精确的 Search/Replace Block（替代脆弱的 unified diff）
- 🧪 **沙盒测试**：Docker 隔离容器执行，支持 9 种语言的测试和 lint
- 🔄 **迭代改进**：审查 → 修复 → 测试 → 重试循环（最多 3 轮）
- 🗣️ **多轮交互式审查**：支持用户实时输入指令干预审查流程
- 🛡️ **人工审批**：高风险操作通过飞书/钉钉通知 Tech Lead 审批
- 📊 **可观测性**：可选 Langfuse 集成，追踪 token 消耗和 LLM 调用链

## 支持语言

| 语言 | Lint 工具 | 测试命令 | Docker 镜像 |
|------|----------|----------|-------------|
| Go | golangci-lint | `go test ./...` | golang:1.23-alpine |
| Python | ruff | `pytest` | python:3.12-slim |
| C++ | cpplint | `cmake && make && ctest` | gcc:latest |
| Java | checkstyle | `mvn test` | maven:3.9-eclipse-temurin-21 |
| Vue | eslint | `vitest run` | node:20-alpine |
| JavaScript | eslint | `npm test` | node:20-alpine |
| TypeScript | tsc | `npm test` | node:20-alpine |
| Flutter | dart analyze | `flutter test` | ghcr.io/cirruslabs/flutter:stable |
| C# | dotnet format | `dotnet test` | mcr.microsoft.com/dotnet/sdk:8.0 |

## 架构设计

```
Webhook → Redis Stream → Worker → LangGraph StateGraph → VCS 评论
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
              DiffAnalyzer     Agent 节点            Sandbox
              (按语言+token      (预加载+             (Docker + 白名单)
               上限切片)         单次调用)             CLI 模式: 跳过
                                reviewer → fixer
                                → critic → tester
```

**Graph 拓扑（V4.0 多轮交互模式）：**
```
__start__ → router_node → Send(reviewer_node) × N
                                  ↓
                         reduce_reviewer_node (合并结果)
                                  ↓
                    ┌─ error_recovery_node (错误恢复)
                    ├─ fixer_node (有 critical 问题)
                    └─ user_checkpoint_node (无 critical 问题)
                                  ↓ (interrupt 暂停)
                         fixer_node → critic_node
                                  ↓
                    ┌─ fixer_node (critic 拒绝)
                    └─ decision_node (critic 通过)
                                  ↓
                    ┌─ reviewer_node (继续下一轮)
                    └─ submit_node (终止条件)
                                  ↓
                               END
```

**关键设计决策：**
- **预加载优于 ReAct**：Reviewer/Fixer 预加载文件上下文到 prompt，单次 LLM 调用完成
- **Search/Replace Block**：LLM 生成精确的搜索/替换对，比 diff 格式更可靠
- **Critic 是规则引擎**：括号匹配、空内容、重复编辑 — 零 LLM 成本，即时执行
- **CLI 跳过沙盒**：Docker 冷启动慢；CLI 模式自动跳过沙盒测试
- **retry_count 由 workflow 层管理**：Critic/Fixer 不递增 retry_count
- **多轮 interrupt 机制**：用户检查点使用 LangGraph `interrupt()` 暂停图执行，等待用户输入
- **智能指令解析**：`instruction_parser.py` 解析自然语言指令（"忽略性能问题"、"只关注安全问题"）
- **Map-Reduce 聚合**：`reduce_reviewer_node` 对并发 reviewer 结果去重、按严重级别排序
- **前缀缓存**：System Prompt 标记 `cache_control`，为支持的 LLM 提供商节省 50% token 成本

## 快速开始

### 环境要求

- Python 3.10+
- Redis（可选，webhook 模式需要）
- Docker（可选，沙盒测试需要）

### 安装

```bash
git clone https://github.com/joeyxue0808/AutoReviewer-MAS.git
cd AutoReviewer-MAS
pip install -r requirements.txt

# 复制并编辑配置
cp config/settings.yaml.example config/settings.yaml
# 编辑 settings.yaml 填入 API Key 等配置
```

### 环境变量

运行前必须设置以下环境变量。推荐写入 shell 配置文件（`~/.bashrc`、`~/.zshrc` 或 PowerShell `$PROFILE`）使其永久生效：

```bash
# Linux/macOS — 写入 ~/.bashrc 或 ~/.zshrc
export MIMO_API_KEY=你的API密钥
export GITHUB_TOKEN=你的GitHub Token
export GITLAB_TOKEN=你的GitLab Token

# Windows PowerShell — 写入 $PROFILE
$env:MIMO_API_KEY = "你的API密钥"
$env:GITHUB_TOKEN = "你的GitHub Token"
```

> 💡 **提示**：如果在其他项目目录下运行 CLI，环境变量需要在**当前 shell 会话**中可用，
> 不是写在 AutoReviewer-MAS 的 `.env` 文件里。

| 变量 | 必需 | 说明 |
|------|------|------|
| `MIMO_API_KEY` | 是 | LLM 网关 API Key |
| `GITLAB_TOKEN` | GitLab 模式 | GitLab API Token |
| `GITHUB_TOKEN` | GitHub 模式 | GitHub API Token |
| `GITHUB_WEBHOOK_SECRET` | 推荐 | GitHub Webhook HMAC 签名密钥 |
| `GITLAB_WEBHOOK_SECRET` | 推荐 | GitLab Webhook Token |
| `APPROVAL_WEBHOOK_URL` | 可选 | 飞书/钉钉审批通知 Webhook（自动识别渠道类型） |
| `WECOM_WEBHOOK_URL` | 可选 | 企业微信审批通知 Webhook |
| `LANGFUSE_SECRET_KEY` | 可选 | Langfuse 监控密钥 |

> 💡 `APPROVAL_WEBHOOK_URL` 和 `WECOM_WEBHOOK_URL` 可同时配置，通知会发送到所有已配置的渠道。

### 使用方式

**CLI 本地审查（在任意项目目录下审查代码）：**

> ⚠️ **重要**：CLI 需要能 `import app` 模块，因此必须让 Python 能找到 AutoReviewer-MAS 的路径。
> 有两种方式：

**方式一 — 以可编辑模式安装（推荐，一次配置永久生效）：**
```bash
# 在 AutoReviewer-MAS 目录下安装一次：
cd /path/to/AutoReviewer-MAS
pip install -e .

# 然后在任意项目目录下直接使用：
cd /path/to/your-project

# 审查工作区全部变更（暂存+未暂存，默认模式）
python -m app.cli.main local

# 仅审查暂存区
python -m app.cli.main local --staged

# 与指定分支对比
python -m app.cli.main local --branch feature/auth

# 审查某个 commit 的变更
python -m app.cli.main local --commit abc1234

# 审查 commit 范围的变更（多提交）
python -m app.cli.main local --range abc1234..def5678

# 全量扫描整个代码库
python -m app.cli.main local --full
```

**方式二 — 设置 PYTHONPATH 环境变量：**
```bash
# 每次使用前设置 PYTHONPATH 指向 AutoReviewer-MAS 目录：
export PYTHONPATH=/path/to/AutoReviewer-MAS

# 然后在任意项目目录下：
cd /path/to/your-project
python -m app.cli.main local
```

> 💡 Windows PowerShell 用户使用：`$env:PYTHONPATH = "D:\path\to\AutoReviewer-MAS"`

**CLI 多轮交互式审查（V4.0 新功能）：**

```bash
# 多轮交互式审查，支持实时输入指令
python -m app.cli.main multiround

# 指定最大审查轮次
python -m app.cli.main multiround --max-rounds 5

# 自动批准模式（无需用户确认）
python -m app.cli.main multiround --auto-approve

# 与指定分支对比的多轮审查
python -m app.cli.main multiround --branch feature/auth

# 全量扫描的多轮审查
python -m app.cli.main multiround --full
```

多轮审查过程中，支持以下实时交互指令：

| 指令 | 动作 |
|------|------|
| `y` / `yes` / `是` | 批准并应用修复 |
| `n` / `no` / `否` | 拒绝修复 |
| `stop` / `停止` | 停止执行 |
| `skip` / `跳过` | 跳过当前轮次 |
| `忽略性能问题` | 忽略特定类别问题 |
| `只关注安全问题` | 只关注特定类别问题 |
| `检查 auth.py` | 针对特定文件 |
| `最多修2次` | 设置最大审查轮次 |
| `3次修复后停止` | 到达轮次后停止 |

**API 服务（Webhook 模式）：**
```bash
python main.py
# Webhook 端点:
#   POST /api/v1/webhook/gitlab
#   POST /api/v1/webhook/github
```

**Worker 进程：**
```bash
python -m app.infra.worker
```

### 运行测试

```bash
pip install pytest pytest-asyncio pytest-cov
pytest
pytest --cov=app --cov-report=term-missing
```

## 功能特性

### V4.0（当前版本）

- **多轮交互式审查**：基于 LangGraph `interrupt()` 机制的实时用户干预
- **智能指令解析**：自然语言指令解析（"忽略性能问题"、"只关注安全问题"、"3次修复后停止"）
- **Map-Reduce 聚合**：`reduce_reviewer_node` 对并发 reviewer 结果去重、按严重级别排序
- **收敛检测**：连续无新问题的轮次自动停止
- **用户检查点**：展示问题摘要，等待用户批准/拒绝
- **前缀缓存**：System Prompt 标记 `cache_control`，支持 Claude 3.5/vLLM 等 Provider

### V3.0

- **零依赖模式**：通过 `local_mode` 配置，无需 Redis/Postgres/Docker 即可本地运行
- **RAG 语义检索**：基于 LanceDB 的代码向量索引，提升大仓库的上下文召回质量
- **错误恢复节点**：429/网络错误自动指数退避重试
- **自适应沙箱**：Docker → Shell → Null 自动降级
- **SQLite Checkpointer**：零依赖的状态持久化替代 Postgres
- **健康检查端点**：`/health` 存活探针 + `/ready` 就绪探针
- **修复写入磁盘**：Fixer 生成的修改经用户确认后直接写入源文件
- **测试套件**：完善的单元测试和集成测试

### V2.0

- **多 VCS 平台**：GitLab 和 GitHub 统一抽象层
- **Search/Replace Block**：精确代码修改，替代脆弱的 unified diff
- **规则化 Critic**：零 LLM 成本的快速质量检查
- **9 语言沙盒**：语言专属 Docker 镜像 + 命令白名单
- **熔断器**：pybreaker 集成，保护 LLM 和 VCS API
- **Redis Stream**：Webhook 解耦 + 消费者组负载均衡
- **HITL 审批**：高风险操作检测 + 飞书/钉钉通知

## 配置说明

完整配置参考：[config/settings.yaml.example](config/settings.yaml.example)

### 多轮审查配置（V4.0）

```yaml
multiround:
  enabled: true            # 启用多轮审查
  max_rounds: 3           # 最大审查轮次
  auto_approve: false      # 自动批准（无需用户确认）
  convergence_threshold: 2 # 连续无新问题的轮次阈值，达到后自动停止
  user_input_timeout: 30   # 用户输入超时时间（秒）
```

### 零依赖模式

```yaml
local_mode:
  enabled: true
  queue: "memory"         # asyncio.Queue 替代 Redis
  checkpointer: "sqlite"  # SQLite 替代 Postgres
  sandbox: "auto"         # Docker → Shell → Null 自动降级
```

### RAG 配置

```yaml
rag:
  enabled: true
  embedding_model: "text-embedding-3-small"
  embedding_api_base: "https://your-llm-gateway.com/v1"
  db_path: ".lancedb"
  top_k: 10
```

### 前缀缓存

```yaml
llm:
  roles:
    reviewer:
      prefix_caching: true   # 为支持的 Provider 启用前缀缓存
    fixer:
      prefix_caching: true
```

## 开发指南

```bash
# 安装开发依赖
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-cov

# 运行测试
pytest

# 带覆盖率
pytest --cov=app --cov-report=term-missing

# 运行指定测试模块
pytest tests/unit/test_critic.py -v

# 运行多轮审查测试
pytest tests/unit/test_multiround.py -v

# 运行前缀缓存测试
pytest tests/unit/test_prefix_caching.py -v
```

### 项目结构

```
AutoReviewer-MAS/
├── app/
│   ├── agents/
│   │   ├── nodes/
│   │   │   ├── reviewer.py          # Reviewer 审查节点
│   │   │   ├── fixer.py             # Fixer 修复节点
│   │   │   ├── critic.py            # Critic 规则检查节点
│   │   │   ├── reduce_reviewer.py   # Map-Reduce 聚合节点
│   │   │   ├── decision.py          # 多轮决策逻辑
│   │   │   ├── user_checkpoint.py   # 用户交互检查点
│   │   │   └── error_recovery.py    # 错误恢复（指数退避）
│   │   ├── prompts/                 # Agent System Prompts
│   │   ├── workflow.py              # 单轮审查 Graph
│   │   └── workflow_multiround.py   # 多轮交互式审查 Graph
│   ├── cli/
│   │   ├── main.py                  # CLI 入口（Typer）
│   │   └── interactive.py           # 交互式会话管理
│   ├── core/
│   │   ├── config.py                # 配置加载
│   │   ├── diff_analyzer.py         # Diff 切片与语言检测
│   │   ├── llm_factory.py           # LLM 实例工厂（含重试）
│   │   ├── cache_utils.py           # 前缀缓存工具
│   │   └── language_matrix.py       # 9 语言配置矩阵
│   ├── schemas/
│   │   ├── state.py                 # ReviewState TypedDict
│   │   ├── llm_out.py               # LLM 结构化输出模型
│   │   └── user_input.py            # 用户输入事件模型
│   └── utils/
│       ├── instruction_parser.py    # 自然语言指令解析器
│       └── patch_applier.py         # Search/Replace 应用器
├── config/
│   └── settings.yaml.example        # 配置模板
├── tests/
│   └── unit/
│       ├── test_multiround.py       # 多轮审查测试（38 个用例）
│       └── test_prefix_caching.py   # 前缀缓存测试（10 个用例）
├── CLAUDE.md
└── README.md
```

## 许可证

MIT
