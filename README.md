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
# Clone and enter repo
git clone https://github.com/joeyxue0808/AutoReviewer-MAS.git
cd AutoReviewer-MAS

# Install dependencies
pip install -r requirements.txt

# [Recommended] Install as editable package for use from any directory
pip install -e .

# Copy and edit config
cp config/settings.yaml.example config/settings.yaml
# Edit settings.yaml with your API keys (MIMO_API_KEY, etc.)
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

> 💡 After running `pip install -e .`, you can use `python -m app.cli.main` from **any project directory**.

---

**Mode 1: Local Auto-Fix Review (recommended for daily use)**

Automatically reviews changes and iteratively fixes issues in a convergence loop (max 5 rounds):

```bash
# Review working tree changes (staged + unstaged)
python -m app.cli.main local

# Review against another branch
python -m app.cli.main local --branch main

# Review a specific commit
python -m app.cli.main local --commit abc1234

# Review a commit range
python -m app.cli.main local --range abc1234..def5678

# Full codebase scan
python -m app.cli.main local --full

# Set max review rounds
python -m app.cli.main local --max-rounds 3

# Verbose logging
python -m app.cli.main local --verbose
```

The auto-fix loop will:
1. Review diff → display issues → filter out deleted-file issues
2. Auto-fix (with configurable countdown if `auto_approve` is enabled)
3. Re-run review → check convergence → stop when no new issues appear
4. Generate final report with remaining issue breakdown

---

**Mode 2: Multi-Round Interactive Review (V4.0)**

Interactive session with real-time user intervention via LangGraph `interrupt()`:

```bash
python -m app.cli.main multiround
python -m app.cli.main multiround --max-rounds 10
python -m app.cli.main multiround --auto-approve
python -m app.cli.main multiround --branch feature/auth
python -m app.cli.main multiround --full
```

Supported interactive commands during review:

| Command | Action |
|---------|--------|
| `y` / `yes` / `是` | Approve and apply fixes |
| `n` / `no` / `否` | Reject fixes |
| `stop` / `停止` | Stop execution |
| `skip` / `跳过` | Skip current round |
| `忽略性能问题` | Ignore specific category |
| `只关注安全问题` | Focus on specific category |
| `检查 auth.py` | Focus on specific file |
| `最多修2次` | Set max rounds |

---

**Mode 3: API Server (webhook mode)**

For GitLab/GitHub webhook integration:

```bash
python main.py
# POST /api/v1/webhook/gitlab
# POST /api/v1/webhook/github
```

**Worker process** (consumes Redis queue):
```bash
python -m app.infra.worker
```

### Testing

```bash
pytest
pytest --cov=app --cov-report=term-missing
```

## Features

### V4.0 (Current)

- **Multi-Round Interactive Review**: Real-time user intervention via LangGraph `interrupt()` mechanism
- **Intelligent Instruction Parser**: Natural language command parsing ("忽略性能问题", "只关注安全问题")
- **Map-Reduce Aggregation**: `reduce_reviewer_node` deduplicates concurrent reviewer results
- **Convergence Detection**: Auto-stops after issues plateau or increase
- **User Checkpoint Node**: Issue summaries and user approval/rejection
- **Two-Tier Caching**:
  - **Memory LRU cache**: Per-process in-memory file content cache (microsecond)
  - **Persistent disk cache**: Per-project `.autoreviewer/cache.json`, survives restarts, stores file contents (with mtime validation), Repo-Maps, and known issues for cross-session dedup
- **Convergence-Aware Final Report**: Distinguishes "converged" vs "max rounds reached" vs "unfixable issues"
- **Deleted-File Filtering**: Automatically skips issues on deleted files before sending to Fixer
- **Auto-Approve Buffer**: 3-second countdown before applying fixes
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
│   │   ├── main.py                  # CLI entry point (Typer)
│   │   └── interactive.py           # Interactive session manager
│   ├── core/
│   │   ├── config.py                # Settings loader
│   │   ├── diff_analyzer.py         # Diff chunking & language detection
│   │   ├── llm_factory.py           # LLM factory with retry
│   │   ├── cache_utils.py           # Prefix caching
│   │   ├── file_cache.py            # In-memory LRU file cache + persistent backend binding
│   │   ├── persistent_cache.py      # Per-project JSON cache (.autoreviewer/cache.json)
│   │   ├── repo_mapper.py           # Repo-Map generation (tree-sitter AST / os.walk)
│   │   └── language_matrix.py       # 9-language config matrix
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
