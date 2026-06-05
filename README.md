<div align="center">

# 🤖 AutoReviewer-MAS

**Multi-Agent Code Review System**

*Let AI be your 24/7 code review team*

[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![LangGraph](https://img.shields.io/badge/LangGraph-Powered-green.svg)](https://langchain-ai.github.io/langgraph/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**[中文文档](README.zh-CN.md)** | **English**

---

[Quick Start](#-quick-start) ·
[Features](#-features) ·
[Usage](#-usage) ·
[Languages](#-supported-languages) ·
[Configuration](#-configuration) ·
[Architecture](#-architecture)

</div>

---

## ✨ What Does It Do?

You submit a PR/MR, and AutoReviewer-MAS automatically:

```
📥 Webhook  →  🔍 Analyze Diff  →  🧠 AI Review  →  🔧 Auto-Fix  →  🧪 Sandbox Test  →  💬 Post Comment
```

It doesn't just find issues — it **tries to fix them**, **verifies the fix**, and **comments directly on your PR/MR**.

---

## 🎯 Features

### 🌐 Dual Platform Support
Supports both **GitLab** and **GitHub** — just configure the tokens.

### 🧠 Multi-Agent Collaboration
| Agent | Role | Responsibility |
|:---:|:---|:---|
| 🏗️ | **Reviewer** (Architect) | Deep code analysis with language-specific expertise |
| 🔧 | **Fixer** (Engineer) | Generates precise Search/Replace fix blocks |
| 🧪 | **Tester** (QA) | Validates fixes in Docker sandbox |
| 🛡️ | **Critic** (Adversary) | Fast pre-screening to reject bad fixes before sandbox |

### 🌍 9 Programming Languages
Go · Python · C++ · Java · Vue · Node.js · TypeScript · Flutter · Unity(C#)

### 🛡️ Secure Sandbox
- Docker isolation with **command whitelisting**
- 300s timeout, 512MB memory limit
- Local Shell mode for development

### 🤝 Human-in-the-Loop (HITL)
High-risk operations (DB migrations, auth changes, CI configs) automatically pause and notify via Feishu/DingTalk. Tech Lead approves via API before execution continues.

### ⚡ Enterprise-Grade HA
- **Redis Stream** queue for webhook decoupling
- **Postgres Checkpointer** for crash recovery (resume from last checkpoint)
- **pybreaker** circuit breaker for LLM/VCS API protection

### 🔍 MCP Tool Chain (Pure Python, No Vector DB)
Agents can actively explore the codebase (not just passively read diffs):
- **read_file_context**: Read file content at specific line ranges (Pydantic schema constrained)
- **ast_find_references**: Search symbol references across the entire repo (tree-sitter / regex fallback)
- **list_directory**: Explore directory structures with depth control

All tools are **read-only** Python functions — no vector database, no embedding model, no RAG retrieval.

---

## 🚀 Quick Start

### 1. Install

```bash
git clone https://github.com/your-org/AutoReviewer-MAS.git
cd AutoReviewer-MAS
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# Required: LLM Gateway
export MIMO_API_KEY="your-mimo-api-key"

# Platform tokens (as needed)
export GITLAB_TOKEN="your-gitlab-token"
export GITHUB_TOKEN="your-github-token"

# Optional: Feishu/DingTalk approval webhook
export APPROVAL_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
```

### 3. Start Services

```bash
# Terminal 1: API Server (receives webhooks)
python main.py

# Terminal 2: Worker (consumes queue, executes review)
python -m app.infra.worker
```

### 4. Configure Webhook

Set up webhook in your GitLab/GitHub project:

| Platform | Webhook URL | Trigger Events |
|:---|:---|:---|
| GitLab | `http://your-server:8000/api/v1/webhook/gitlab` | Merge Request Events |
| GitHub | `http://your-server:8000/api/v1/webhook/github` | Pull Requests |

Creating or updating a PR/MR now triggers automatic review!

---

## 📖 Usage

### Mode 1: Automatic Review via Webhook (Recommended)

After creating a PR/MR, the full pipeline runs automatically:

```
Webhook → Diff Analysis → AI Review → Auto-Fix → Sandbox Test → Comment
```

Results are posted as a Markdown table directly on your PR/MR, including:
- 🔴 Critical / 🟡 Warning / 🔵 Info issues
- Specific fix suggestions
- Sandbox test results

### Mode 2: CLI Local Review

Don't want to wait for webhook? Review locally:

```bash
# Review staged changes
python -m app.cli.main local

# Review all working changes
python -m app.cli.main local --all

# Specify branch name
python -m app.cli.main local --branch feature/auth

# Verbose logging
python -m app.cli.main local -v
```

### Mode 3: Human Approval (HITL)

When high-risk operations are detected, the system pauses and sends a notification:

```bash
# List pending approvals
curl http://localhost:8000/api/v1/approval/pending

# Approve
curl -X POST http://localhost:8000/api/v1/approve/{thread_id}

# Reject
curl -X POST http://localhost:8000/api/v1/reject/{thread_id}
```

---

## 🌍 Supported Languages

| Stack | Extensions | Linter | Test Command | Docker Image |
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

Multi-language MRs are automatically split and reviewed concurrently.

---

## ⚙️ Configuration

All configuration is in `config/settings.yaml`:

```yaml
# LLM
llm:
  roles:
    reviewer:
      base_url: "https://api.mimo.com/v1"
      model: "mimo-v2.5-pro"
      temperature: 0.3
    fixer:
      temperature: 0.2
    tester:
      temperature: 0.1

# Sandbox
sandbox:
  default_engine: "docker"
  timeout: 300

# Queue
queue:
  redis_url: "redis://localhost:6379/0"

# Checkpointer
checkpointer:
  enabled: true
  postgres_url: "postgresql://postgres:postgres@localhost:5432/autoreviewer"

# Circuit Breaker
circuit_breaker:
  fail_max: 5
  reset_timeout: 30
```

---

## 🏗️ Architecture

```
                    ┌──────────────────────────────────────────┐
                    │          Ingress & Dispatch               │
                    │  GitLab/GitHub Webhook → Redis Stream     │
                    └──────────────┬───────────────────────────┘
                                   │
                    ┌──────────────▼───────────────────────────┐
                    │          State & Orchestration             │
                    │  Worker → LangGraph StateGraph            │
                    │          + Postgres Checkpointer           │
                    │                                           │
                    │  ┌──────┐  ┌────────┐  ┌───────┐        │
                    │  │Router├─►│Reviewer├─►│ Fixer │        │
                    │  └──┬───┘  └────────┘  └───┬───┘        │
                    │     │ fan-out       ↑      │             │
                    │     ▼              │  ┌───▼───┐         │
                    │  ┌──────┐          │  │Critic │         │
                    │  │Synth.│          │  └───┬───┘         │
                    │  └──────┘          │      │             │
                    │                    │  ┌───▼───┐         │
                    │                    │  │Tester │         │
                    │                    │  └───┬───┘         │
                    │                    │      │             │
                    │                    └─ (retry ◄─┘)       │
                    │                        │                 │
                    │                    ┌───▼───┐            │
                    │                    │Submit │            │
                    │                    └───────┘            │
                    └──────────────────────────────────────────┘
                                   │
                    ┌──────────────▼───────────────────────────┐
                    │          Intelligence & Execution          │
                    │  LLM Gateway (Mimo/vLLM)                  │
                    │  MCP Tools (file read/symbol search/tree)  │
                    │  Docker Sandbox (9-language matrix)        │
                    └──────────────────────────────────────────┘
```

---

## ❓ FAQ

**Q: Supports private deployment?**
A: Fully. LLM gateway, Redis, Postgres can all be deployed on-premise. vLLM and other local model services are also supported.

**Q: Is the sandbox secure?**
A: Docker sandbox uses command whitelisting (only standard test commands allowed), 300s timeout, 512MB memory limit, and network controls. No arbitrary shell execution.

**Q: What's the review latency?**
A: Webhook returns `{"status": "processing"}` immediately. Review runs asynchronously, typically 1-3 minutes depending on MR size and LLM response time.

**Q: What if a Worker crashes?**
A: With Postgres Checkpointer enabled, graph state is auto-persisted. New Workers resume from the last checkpoint without re-consuming tokens.

**Q: Can I customize review rules?**
A: Edit System Prompt files in `app/agents/prompts/`. Adjust LLM temperature in `config/settings.yaml` to control strictness.

---

<div align="center">

**Made with ❤️ by AutoReviewer-MAS Team**

</div>
