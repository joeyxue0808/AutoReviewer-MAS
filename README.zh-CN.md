<div align="center">

# 🤖 AutoReviewer-MAS

**多智能体协同代码审查系统**

*让 AI 成为你的 24 小时代码审查团队*

[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![LangGraph](https://img.shields.io/badge/LangGraph-Powered-green.svg)](https://langchain-ai.github.io/langgraph/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**中文文档** | **[English](README.md)**

---

[快速开始](#-快速开始) ·
[功能特性](#-功能特性) ·
[使用方式](#-使用方式) ·
[支持语言](#-支持语言) ·
[配置说明](#-配置说明) ·
[架构概览](#-架构概览)

</div>

---

## ✨ 它能做什么？

你提交了一个 PR/MR，AutoReviewer-MAS 会自动：

```
📥 接收 Webhook  →  🔍 分析 Diff  →  🧠 AI 审查代码  →  🔧 自动修复  →  🧪 沙盒测试  →  💬 写回评论
```

不只是发现问题 —— 它会**尝试修复**，**验证修复**，然后把结果**直接评论到你的 PR/MR 上**。

---

## 🎯 功能特性

### 🌐 双平台支持
同时支持 **GitLab** 和 **GitHub**，配置 Token 即可对接。

### 🧠 智能多 Agent 协同
| Agent | 角色 | 职责 |
|:---:|:---|:---|
| 🏗️ | **Reviewer** (高级架构师) | 预加载文件上下文，单次 LLM 调用完成深度审查 |
| 🔧 | **Fixer** (重构工程师) | 预加载变更文件，单次调用生成精确 Search/Replace 修复块 |
| 🧪 | **Tester** (测试工程师) | Docker 沙盒验证（CLI 模式自动跳过，节省时间） |
| 🛡️ | **Critic** (规则化审查) | 纯规则检查（括号匹配/空内容/重复修改），零 LLM 开销 |

### 🌍 9 大编程语言
Go · Python · C++ · Java · Vue · Node.js · TypeScript · Flutter · Unity(C#)

### 🛡️ 安全沙盒
- Docker 隔离执行，**命令白名单**拦截恶意代码
- 300 秒超时防护，512MB 内存限制
- 支持本地 Shell 模式（开发调试用）

### 🤝 人机协同 (HITL)
高危操作（数据库迁移、鉴权变更、CI 配置修改）自动暂停，发送飞书/钉钉通知，等待 Tech Lead 审批后继续。

### ⚡ 企业级高可用
- **Redis Stream** 消息队列削峰，Webhook 不再雪崩
- **Postgres Checkpointer** 状态持久化，Worker 崩溃后断点续传
- **pybreaker** 熔断器保护 LLM/VCS API

### 🔍 主动工具链 (MCP)
Reviewer/Fixer 不再被动分析 Diff，可以主动探索代码库：
- 读取文件完整上下文
- 全库搜索符号引用
- 探查目录结构

---

## 🚀 快速开始

### 1. 安装

```bash
git clone https://github.com/your-org/AutoReviewer-MAS.git
cd AutoReviewer-MAS
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
# 必填：LLM 网关
export MIMO_API_KEY="your-mimo-api-key"

# 按需配置平台 Token
export GITLAB_TOKEN="your-gitlab-token"
export GITHUB_TOKEN="your-github-token"

# 可选：飞书/钉钉审批通知
export APPROVAL_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
```

### 3. 启动服务

```bash
# 终端 1：启动 API 服务（接收 Webhook）
python main.py

# 终端 2：启动 Worker（执行审查任务）
python -m app.infra.worker
```

### 4. 配置 Webhook

在你的 GitLab/GitHub 项目中配置 Webhook：

| 平台 | Webhook URL | 触发事件 |
|:---|:---|:---|
| GitLab | `http://your-server:8000/api/v1/webhook/gitlab` | Merge Request Events |
| GitHub | `http://your-server:8000/api/v1/webhook/github` | Pull Requests |

现在，创建或更新 PR/MR 就会自动触发审查！

---

## 📖 使用方式

### 方式一：Webhook 自动审查（推荐）

创建 PR/MR 后，系统自动执行全流程：

```
Webhook 触发 → Diff 分析 → AI 审查 → 自动修复 → 沙盒测试 → 评论写回
```

审查结果会以 Markdown 表格直接评论到你的 PR/MR 上，包含：
- 🔴 Critical / 🟡 Warning / 🔵 Info 级别的问题
- 具体的修复建议
- 沙盒测试结果

### 方式二：CLI 本地审查

不想等 Webhook？直接在本地审查你的代码变更：

```bash
# 审查 Git 暂存区
python -m app.cli.main local

# 审查全部工作区变更
python -m app.cli.main local --all

# 指定分支名
python -m app.cli.main local --branch feature/auth

# 详细日志
python -m app.cli.main local -v
```

终端输出示例：

```
╭────────────────────────────────╮
│ 🤖 AutoReviewer-MAS            │
│ 本地伴随代码审查 - Phase 4 CLI │
╰────────────────────────────────╯
                          📋 变更概览
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ 文件           ┃ 状态     ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ src/auth.py    │ modified │
│ src/models.py  │ modified │
└────────────────┴──────────┘
+42 / -15 行
🔍 检测到语言: python
📦 切分为 1 个 Chunk
🗺️  Repo-Map 已生成

🔍 审查报告 — 2 个问题  🔴 critical 1 │ 🟡 warning 1

╭─ #1 ────────────────────────────────────────╮
│ 🔴 [CRITICAL] src/auth.py:42                │
│                                              │
│ Token 过期未刷新，会导致用户静默登出          │
│                                              │
│ 💡 增加 refresh_token 逻辑                   │
╰──────────────────────────────────────────────╯

╭─ #2 ────────────────────────────────────────╮
│ 🟡 [WARNING] src/models.py:15               │
│                                              │
│ 缺少类型注解，影响代码可读性                  │
│                                              │
│ 💡 添加 type hints                           │
╰──────────────────────────────────────────────╯

📝 生成 2 个修复块

╭─ Block #1 — src/auth.py ────────────────────╮
│ --- a/src/auth.py                            │
│ +++ b/src/auth.py                            │
│ - token = get_token()                        │
│ + token = get_token()                        │
│ + if token.is_expired():                     │
│ +     token = refresh_token(token)           │
╰──────────────────────────────────────────────╯

🎉 审查完成 — 测试通过，代码可合并
```

### 方式三：人工审批 (HITL)

当系统检测到高危操作时，会自动暂停并发送通知：

```
高危文件检测：
- alembic/versions/001_create_users.py (数据库迁移)
- src/auth/permissions.py (鉴权变更)
```

**审批 API：**

```bash
# 查看待审批列表
curl http://localhost:8000/api/v1/approval/pending

# 批准提交
curl -X POST http://localhost:8000/api/v1/approve/{thread_id}

# 拒绝提交
curl -X POST http://localhost:8000/api/v1/reject/{thread_id}
```

---

## 🌍 支持语言

| 技术栈 | 文件后缀 | 静态扫描 | 测试命令 | Docker 镜像 |
|:---|:---|:---|:---|:---|
| **Go** | `.go` | golangci-lint | `go test ./...` | golang:1.23-alpine |
| **Python** | `.py` | ruff | `pytest` | python:3.12-slim |
| **C++** | `.cpp .h .cc` | cpplint | `cmake && make && ctest` | gcc:latest |
| **Java** | `.java` | checkstyle | `mvn test` | maven:3.9-eclipse-temurin-21 |
| **Vue** | `.vue` | eslint | `vitest run` | node:20-alpine |
| **Node.js** | `.js .cjs .mjs` | eslint | `npm test` | node:20-alpine |
| **TypeScript** | `.ts .tsx` | tsc --noEmit | `npm test` | node:20-alpine |
| **Flutter** | `.dart` | dart analyze | `flutter test` | cirruslabs/flutter:stable |
| **Unity(C#)** | `.cs` | dotnet format | `dotnet test` | dotnet/sdk:8.0 |

多语言 MR 会自动按语言拆分，并发审查。

---

## ⚙️ 配置说明

所有配置集中在 `config/settings.yaml`：

```yaml
# LLM 配置
llm:
  roles:
    reviewer:
      base_url: "https://api.mimo.com/v1"
      model: "mimo-v2.5-pro"
      temperature: 0.3
    fixer:
      temperature: 0.2    # 更确定性的输出
    tester:
      temperature: 0.1    # 最确定性的输出

# 沙盒配置
sandbox:
  default_engine: "docker"  # 或 "shell"（开发调试用）
  timeout: 300

# 消息队列
queue:
  redis_url: "redis://localhost:6379/0"

# 持久化检查点
checkpointer:
  enabled: true
  postgres_url: "postgresql://postgres:postgres@localhost:5432/autoreviewer"

# 熔断器
circuit_breaker:
  fail_max: 5        # 60 秒内连续失败 5 次触发熔断
  reset_timeout: 30  # 熔断后 30 秒进入半开状态
```

---

## 🏗️ 架构概览

```
                    ┌──────────────────────────────────────────┐
                    │           接入与分发面                      │
                    │  GitLab/GitHub Webhook  →  Redis Stream   │
                    └──────────────┬───────────────────────────┘
                                   │
                    ┌──────────────▼───────────────────────────┐
                    │           状态与编排面                      │
                    │  Worker  →  LangGraph StateGraph          │
                    │           + Postgres Checkpointer          │
                    │                                            │
                    │  ┌──────┐   ┌────────┐   ┌───────┐       │
                    │  │Router├──►│Reviewer├──►│ Fixer │       │
                    │  └──┬───┘   └────────┘   └───┬───┘       │
                    │     │ 并发         ↑         │            │
                    │     ▼             │     ┌───▼───┐        │
                    │  ┌──────┐         │     │Critic │        │
                    │  │Synth.│         │     └───┬───┘        │
                    │  └──────┘         │         │            │
                    │                   │     ┌───▼───┐        │
                    │                   │     │Tester │        │
                    │                   │     └───┬───┘        │
                    │                   │         │            │
                    │                   └─ (重试 ◄─┘)          │
                    │                       │                   │
                    │                   ┌───▼───┐              │
                    │                   │Submit │              │
                    │                   └───────┘              │
                    └──────────────────────────────────────────┘
                                   │
                    ┌──────────────▼───────────────────────────┐
                    │           智能与执行面                      │
                    │  LLM Gateway (Mimo/vLLM)                  │
                    │  MCP Tools (文件读取/符号搜索/目录探查)      │
                    │  Docker Sandbox (9 语言矩阵)               │
                    └──────────────────────────────────────────┘
```

---

## ❓ FAQ

**Q: 支持私有部署吗？**

A: 完全支持。LLM 网关、Redis、Postgres 均可内网部署。也支持接入 vLLM 等本地模型服务。

**Q: 沙盒安全吗？**

A: Docker 沙盒启用了命令白名单（只允许标准测试命令）、300 秒超时、512MB 内存限制、网络访问控制。不会执行任意 Shell。

**Q: 审查延迟多久？**

A: Webhook 立即返回 `{"status": "processing"}`，审查在后台异步执行。CLI 模式下，7 文件以内的变更通常 30 秒内完成（预加载上下文 + 单次 LLM 调用，无需 ReAct 工具循环）。Webhook 模式取决于 MR 大小和 LLM 响应速度。

**Q: 支持 Windows 吗？**

A: 完全支持。已处理 Windows 特有的 asyncio 事件循环（SelectorEventLoop）、GBK 控制台编码、信号处理等兼容性问题。

**Q: 可以只审查特定语言吗？**

A: 系统会自动检测 Diff 中涉及的语言，只对相关语言执行审查和测试。多语言 MR 按语言拆分并发处理。

**Q: Worker 崩溃了怎么办？**

A: 启用 Postgres Checkpointer 后，Graph 状态会自动持久化。新 Worker 启动后可从上次检查点恢复，无需重新消耗 Token。

**Q: 如何自定义审查规则？**

A: 修改 `app/agents/prompts/` 目录下的 System Prompt 文件。你也可以在 `config/settings.yaml` 中调整 LLM temperature 来控制审查严格程度。

---

<div align="center">

**Made with ❤️ by AutoReviewer-MAS Team**

</div>
