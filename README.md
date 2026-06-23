# AutoReviewer-MAS

<p align="center">
  <strong>🤖 Multi-Agent System for Automated Code Review</strong><br>
  <em>LangGraph Orchestration · 9-Language Sandbox · GitLab & GitHub · Multi-Round Interactive Review</em>
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
- 🗣️ **Multi-Round Interactive Review**: Real-time user intervention with natural language instructions
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

**Graph Topology (V4.0 Multi-round - Interactive mode):**
```
__start__ → router_node → Send(reviewer_node) × N
                                  ↓
                         reduce_reviewer_node (merge results)
                                  ↓
                    ┌─ error_recovery_node (error recovery)
                    ├─ fixer_node (has critical issues)
                    └─ user_checkpoint_node (no critical issues)
                                  ↓ (interrupt pause)
                         fixer_node → critic_node
                                  ↓
                    ┌─ fixer_node (critic rejects)
                    └─ decision_node (critic passes)
                                  ↓
                    ┌─ reviewer_node (next round)
                    └─ submit_node (termination)
                                  ↓
                               END
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

| Variable | Required | Description |
|----------|----------|-------------|
| `MIMO_API_KEY` | Yes | LLM gateway API key |
| `GITLAB_TOKEN` | For GitLab | GitLab API token |
| `GITHUB_TOKEN` | For GitHub | GitHub API token |
| `GITHUB_WEBHOOK_SECRET` | Recommended | GitHub webhook HMAC secret |
| `GITLAB_WEBHOOK_SECRET` | Recommended | GitLab webhook token |
| `APPROVAL_WEBHOOK_URL` | Optional | Feishu/DingTalk approval webhook |
| `WECOM_WEBHOOK_URL` | Optional | WeCom approval webhook |
| `LANGFUSE_SECRET_KEY` | Optional | Langfuse monitoring |

### Usage

**CLI Local Review (single-round):**

```bash
# Review all working tree changes
python -m app.cli.main local

# Review only staged changes
python -m app.cli.main local --staged

# Review diff against another branch
python -m app.cli.main local --branch feature/auth

# Full codebase scan
python -m app.cli.main local --full
```

**CLI Multi-Round Interactive Review (V4.0):**

```bash
# Multi-round review with interactive user input
python -m app.cli.main multiround

# Set maximum review rounds
python -m app.cli.main multiround --max-rounds 5

# Auto-approve mode (no user confirmation needed)
python -m app.cli.main multiround --auto-approve

# Multi-round review against a branch
python -m app.cli.main multiround --branch feature/auth
```

During multi-round review, you can interact with the system in real-time:

| Command | Action |
|---------|--------|
| `y` / `yes` / `是` | Approve and apply fixes |
| `n` / `no` / `否` | Reject fixes |
| `stop` / `停止` | Stop execution |
| `skip` / `跳过` | Skip current round |
| `忽略性能问题` | Ignore issues in specific category |
| `只关注安全问题` | Focus on specific category only |
| `检查 auth.py` | Focus on specific file |
| `最多修2次` | Set max review rounds |

**API Server (webhook mode):**
```bash
python main.py
# Webhook endpoints:
#   POST /api/v1/webhook/gitlab
#   POST /api/v1/webhook/github
```

### Testing

```bash
pytest
pytest --cov=app --cov-report=term-missing
```

## Features

### V4.0 (Current)

- **Multi-Round Interactive Review**: Real-time user intervention via LangGraph `interrupt()` mechanism
- **Intelligent Instruction Parser**: Natural language command parsing
- **Map-Reduce Aggregation**: `reduce_reviewer_node` deduplicates concurrent reviewer results
- **Convergence Detection**: Auto-stops after consecutive zero-issue rounds
- **User Checkpoint Node**: Issue summaries and user approval/rejection
- **Prefix Caching**: System prompts with `cache_control` for supported LLM providers

### V3.0

- **Zero-Dependency Mode**: Run locally without Redis/Postgres/Docker
- **RAG Context Retrieval**: LanceDB-based semantic code search
- **Error Recovery Node**: Automatic retry with exponential backoff
- **Adaptive Sandbox**: Auto-degrades Docker → Shell → Null
- **Health/Readiness Probes**: `/health` and `/ready` endpoints
- **Auto-Fix Write-Back**: Fixer patches applied to source files

### V2.0

- **Multi-VCS Support**: GitLab and GitHub with unified abstraction
- **Search/Replace Block**: Precise code modifications
- **Rule-Based Critic**: Zero-LLM-cost quality checks
- **9-Language Sandbox**: Language-specific Docker images
- **Circuit Breaker**: pybreaker integration for API resilience
- **Redis Streams**: Decoupled webhook ingestion
- **HITL Approval**: High-risk operation detection

## Configuration

See [config/settings.yaml.example](config/settings.yaml.example) for the full configuration reference.

### Multi-Round Review (V4.0)

```yaml
multiround:
  enabled: true
  max_rounds: 3
  auto_approve: false
  convergence_threshold: 2
  user_input_timeout: 30
```

### Local Mode (Zero Dependencies)

```yaml
local_mode:
  enabled: true
  queue: "memory"
  checkpointer: "sqlite"
  sandbox: "auto"
```

### Prefix Caching

```yaml
llm:
  roles:
    reviewer:
      prefix_caching: true
    fixer:
      prefix_caching: true
```

## Development

```bash
# Install dev dependencies
pip install -r requirements.txt

# Run all tests
pytest

# Run multi-round review tests
pytest tests/unit/test_multiround.py -v

# Run prefix caching tests
pytest tests/unit/test_prefix_caching.py -v
```

### Project Structure

```
AutoReviewer-MAS/
├── app/
│   ├── agents/
│   │   ├── nodes/
│   │   │   ├── reviewer.py          # Reviewer agent
│   │   │   ├── fixer.py             # Fixer agent
│   │   │   ├── critic.py            # Rule-based critic
│   │   │   ├── reduce_reviewer.py   # Map-Reduce aggregation
│   │   │   ├── decision.py          # Multi-round decision logic
│   │   │   ├── user_checkpoint.py   # User interaction checkpoint
│   │   │   └── error_recovery.py    # Error recovery with backoff
│   │   ├── prompts/                 # LLM system prompts
│   │   ├── workflow.py              # Single-round graph
│   │   └── workflow_multiround.py   # Multi-round interactive graph
│   ├── cli/
│   │   ├── main.py                  # CLI entry point
│   │   └── interactive.py           # Interactive session manager
│   ├── core/
│   │   ├── config.py                # Settings loader
│   │   ├── diff_analyzer.py         # Diff chunking
│   │   ├── llm_factory.py           # LLM factory with retry
│   │   ├── cache_utils.py           # Prefix caching
│   │   └── language_matrix.py       # 9-language matrix
│   ├── schemas/
│   │   ├── state.py                 # ReviewState TypedDict
│   │   ├── llm_out.py               # LLM output models
│   │   └── user_input.py            # User input models
│   └── utils/
│       ├── instruction_parser.py    # NL instruction parser
│       └── patch_applier.py         # Search/Replace applier
├── config/
│   └── settings.yaml.example
├── tests/
│   └── unit/
│       ├── test_multiround.py       # Multi-round tests (38 cases)
│       └── test_prefix_caching.py   # Prefix caching tests (10 cases)
├── CLAUDE.md
└── README.md
```

## License

MIT
