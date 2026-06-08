# AutoReviewer-MAS

<p align="center">
  <strong>🤖 Multi-Agent System for Automated Code Review</strong><br>
  <em>LangGraph Orchestration · 9-Language Sandbox · GitLab & GitHub</em>
</p>

<p align="center">
  <a href="#quickstart">Quick Start</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#features">Features</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#development">Development</a>
</p>

---

## Overview

AutoReviewer-MAS is an automated code review system powered by a Multi-Agent architecture. It uses LangGraph to orchestrate specialized AI agents — Reviewer, Fixer, Critic, and Tester — in a feedback loop that can iteratively improve code quality.

**Key capabilities:**
- 🔍 **Automated Review**: Deep analysis of code changes with language-specific expertise
- 🔧 **Auto-Fix**: Generates precise Search/Replace blocks (not fragile unified diffs)
- 🧪 **Sandboxed Testing**: Isolated Docker containers with 9-language support
- 🔄 **Iterative Improvement**: Review → Fix → Test → Retry loop (max 3 iterations)
- 🛡️ **HITL Approval**: High-risk operations require human approval via Feishu/DingTalk
- 📊 **Observability**: Optional Langfuse integration for token cost tracking

## Supported Languages

| Language | Lint | Test | Docker Image |
|----------|------|------|--------------|
| Go | golangci-lint | `go test ./...` | golang:1.23-alpine |
| Python | ruff | `pytest` | python:3.12-slim |
| C++ | cpplint | `cmake && make && ctest` | gcc:latest |
| Java | checkstyle | `mvn test` | maven:3.9-eclipse-temurin-21 |
| Vue | eslint | `vitest run` | node:20-alpine |
| JavaScript | eslint | `npm test` | node:20-alpine |
| TypeScript | tsc | `npm test` | node:20-alpine |
| Flutter | dart analyze | `flutter test` | ghcr.io/cirruslabs/flutter:stable |
| C# | dotnet format | `dotnet test` | mcr.microsoft.com/dotnet/sdk:8.0 |

## Architecture

```
Webhook → Redis Stream → Worker → LangGraph StateGraph → VCS Comment
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
              DiffAnalyzer     Agent Nodes          Sandbox
              (chunk by        (Pre-load +          (Docker + whitelist)
               language +       Single Call)         CLI mode: skipped
               token limit)     reviewer → fixer
                                → critic → tester
```

**Graph Topology (Send API dynamic fan-out):**
```
__start__ → router_node → Send(reviewer_node) → fixer_node → critic_node → tester_node → submit_node → END
                              ↑                        ↓              │
                              └── (retry < 3) ─────────┘              │
                              ↑                                       │
                              └── error_recovery_node ←───────────────┘
```

## Quick Start

### Prerequisites

- Python 3.10+
- Redis (optional, for webhook mode)
- Docker (optional, for sandboxed testing)

### Installation

```bash
git clone https://github.com/joeyxue0808/AutoReviewer-MAS.git
cd AutoReviewer-MAS
pip install -r requirements.txt

# Copy and edit config
cp config/settings.yaml.example config/settings.yaml
# Edit settings.yaml with your API keys
```

### Environment Variables

Set environment variables **in the project root directory** before running. You can use a `.env` file or export directly:

```bash
# Option 1: Export in shell (persists for current session)
export MIMO_API_KEY=your-api-key-here
export GITHUB_TOKEN=your-github-token

# Option 2: Create .env file in AutoReviewer-MAS project root
# (only works when running from AutoReviewer-MAS directory)
cat > /path/to/AutoReviewer-MAS/.env << 'EOF'
MIMO_API_KEY=your-api-key-here
GITHUB_TOKEN=your-github-token
EOF
```

> 💡 **Tip**: When reviewing other projects, export env vars in your shell profile
> (`~/.bashrc`, `~/.zshrc`, or PowerShell `$PROFILE`) so they are always available.

| Variable | Required | Description |
|----------|----------|-------------|
| `MIMO_API_KEY` | Yes | LLM gateway API key |
| `GITLAB_TOKEN` | For GitLab | GitLab API token |
| `GITHUB_TOKEN` | For GitHub | GitHub API token |
| `GITHUB_WEBHOOK_SECRET` | Recommended | GitHub webhook HMAC secret |
| `GITLAB_WEBHOOK_SECRET` | Recommended | GitLab webhook token |
| `APPROVAL_WEBHOOK_URL` | Optional | Feishu/DingTalk approval webhook (auto-detects channel) |
| `WECOM_WEBHOOK_URL` | Optional | WeCom (企业微信) approval webhook |
| `LANGFUSE_SECRET_KEY` | Optional | Langfuse monitoring |

> 💡 `APPROVAL_WEBHOOK_URL` and `WECOM_WEBHOOK_URL` can be set simultaneously —
> notifications will be sent to all configured channels.

### Usage

**CLI Local Review (review code in any project):**

> ⚠️ **Important**: The CLI must be able to import the `app` module from AutoReviewer-MAS.
> You have two options:

**Option A — Install as editable package (recommended):**
```bash
# In AutoReviewer-MAS directory, install once:
cd /path/to/AutoReviewer-MAS
pip install -e .

# Then in ANY project directory:
cd /path/to/your-project

# Review all working tree changes (staged + unstaged, default)
python -m app.cli.main local

# Review only staged changes
python -m app.cli.main local --staged

# Review diff against another branch
python -m app.cli.main local --branch feature/auth

# Review a specific commit
python -m app.cli.main local --commit abc1234

# Review a range of commits
python -m app.cli.main local --range abc1234..def5678

# Full codebase scan (all source files)
python -m app.cli.main local --full
```

**Option B — Set PYTHONPATH:**
```bash
# Set PYTHONPATH to AutoReviewer-MAS directory before running:
export PYTHONPATH=/path/to/AutoReviewer-MAS

# Then in any project directory:
cd /path/to/your-project
python -m app.cli.main local
```

> 💡 Environment variables (`MIMO_API_KEY`, etc.) must be set in the shell
> where you run the command, not necessarily in the project directory.

**API Server (webhook mode):**
```bash
python main.py
# Webhook endpoints:
#   POST /api/v1/webhook/gitlab
#   POST /api/v1/webhook/github
```

**Worker Process:**
```bash
python -m app.infra.worker
```

### Testing

```bash
pip install pytest pytest-asyncio pytest-cov
pytest
pytest --cov=app --cov-report=term-missing
```

## Features

### V3.0 (Current)

- **Zero-Dependency Mode**: Run locally without Redis/Postgres/Docker using `local_mode` config
- **RAG Context Retrieval**: LanceDB-based semantic code search for better context recall
- **Error Recovery Node**: Automatic retry with exponential backoff for 429/network errors
- **Adaptive Sandbox**: Auto-degrades Docker → Shell → Null when engines are unavailable
- **SQLite Checkpointer**: Zero-dependency state persistence alternative to Postgres
- **Health/Readiness Probes**: `/health` and `/ready` endpoints for deployment monitoring
- **Auto-Fix Write-Back**: Fixer patches are applied to source files on disk (with user confirmation)
- **Test Suite**: Comprehensive unit and integration tests with pytest

### V2.0

- **Multi-VCS Support**: GitLab and GitHub with unified VCS abstraction layer
- **Search/Replace Block**: Precise code modifications replacing fragile unified diffs
- **Rule-Based Critic**: Zero-LLM-cost quality checks (bracket matching, empty content)
- **9-Language Sandbox**: Language-specific Docker images with command whitelisting
- **Circuit Breaker**: pybreaker integration for LLM and VCS API resilience
- **Redis Streams**: Decoupled webhook ingestion with consumer group load balancing
- **HITL Approval**: High-risk operation detection with Feishu/DingTalk notifications

## Configuration

See [config/settings.yaml.example](config/settings.yaml.example) for the full configuration reference.

### Local Mode (Zero Dependencies)

```yaml
local_mode:
  enabled: true
  queue: "memory"
  checkpointer: "sqlite"
  sandbox: "auto"
```

When `local_mode.enabled = true`:
- Queue uses `asyncio.Queue` instead of Redis Streams
- Checkpointer uses SQLite/MemorySaver instead of Postgres
- Sandbox auto-degrades from Docker → Shell → Null

### RAG Configuration

```yaml
rag:
  enabled: true
  embedding_model: "text-embedding-3-small"
  embedding_api_base: "https://your-llm-gateway.com/v1"
  db_path: ".lancedb"
  top_k: 10
```

## Development

```bash
# Install dev dependencies
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-cov

# Run tests
pytest

# Run with coverage
pytest --cov=app --cov-report=term-missing

# Run specific test module
pytest tests/unit/test_critic.py -v
```

## License

MIT
