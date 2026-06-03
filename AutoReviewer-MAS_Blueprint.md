# AutoReviewer-MAS 核心技术规格说明书 (Master Blueprint)

## 1. 核心目录与文件树定义 (严格遵循)
```text
AutoReviewer-MAS/
├── app/
│   ├── api/
│   │   └── webhook.py          # 暴露 /api/v1/webhook/gitlab 接口
│   ├── core/
│   │   ├── config.py           # Pydantic Settings 全局配置加载
│   │   └── llm_factory.py      # 返回 LangChain BaseChatModel 的工厂
│   ├── schemas/
│   │   ├── state.py            # LangGraph 的 TypedDict 状态定义
│   │   ├── gitlab.py           # GitLab Webhook Payload 模型
│   │   └── llm_out.py          # 约束大模型输出的 Pydantic V2 模型
│   ├── agents/
│   │   ├── workflow.py         # StateGraph 编译与流转逻辑
│   │   ├── nodes/              # 各个节点的具体实现
│   │   │   ├── reviewer.py
│   │   │   ├── fixer.py
│   │   │   └── tester.py
│   │   └── prompts/            # 存放 System Prompts
│   ├── sandbox/
│   │   ├── base.py             # BaseSandboxEngine 抽象类
│   │   ├── docker_engine.py    # 基于 docker-py 的实现
│   │   └── shell_engine.py     # 基于本地 asyncio shell 的实现
│   └── gitlab/
│       └── client.py           # GitLab API 异步封装 (获取Diff/发评论)
├── config/
│   └── settings.yaml           # LLM 路由与沙盒策略配置
├── main.py                     # FastAPI 启动入口
└── requirements.txt
```

## 2. 核心数据结构契约 (Data Contracts)
为了保证 LangGraph 各个节点的数据不畸变，必须严格按照以下模型定义：

### 2.1 LangGraph 状态机定义 (app/schemas/state.py)
Python
from typing import TypedDict, List, Optional, Dict
from pydantic import BaseModel

class ReviewIssue(BaseModel):
    file_path: str
    line_number: int
    severity: str  # enum: "info", "warning", "critical"
    description: str
    suggestion: str

class ReviewState(TypedDict):
    mr_id: int
    project_id: int
    source_branch: str
    target_branch: str
    diff_content: str               # 原始的 git diff 内容
    language: str                   # 识别出的主要语言 (go, python等)
    static_lint_logs: str           # 静态分析器输出的原始日志
    review_issues: List[ReviewIssue]# Reviewer Agent 发现的问题
    generated_patches: Dict[str, str] # Fixer 生成的代码补丁 (key: file_path, value: patch_content)
    test_logs: str                  # Tester 沙盒执行后的日志
    is_test_passed: bool            # 测试是否通过
    retry_count: int                # Fixer <-> Tester 的循环重试次数
### 2.2 大模型强约束输出定义 (app/schemas/llm_out.py)
利用 LangChain 的 .with_structured_output() 方法，强制 LLM 返回 JSON。

Python
from pydantic import BaseModel, Field
from typing import List

class ReviewerOutput(BaseModel):
    issues: List['ReviewIssue'] = Field(description="发现的代码缺陷列表")
    is_approved: bool = Field(description="如果没有发现 critical 级别问题，则为 True")

class FixerOutput(BaseModel):
    file_path: str = Field(description="被修复的文件路径")
    unified_diff: str = Field(description="严格标准的 Unified Diff 格式内容")
## 3. LangGraph 节点与流转逻辑 (Node Definitions)
Node 1: reviewer_node

Input: ReviewState (读取 diff_content 和 static_lint_logs)

LLM 角色: 高级架构师。根据语言特性（如 Go 查 goroutine 泄漏）审查代码。

Output: 写入 review_issues。如果 is_approved 为 True，直接流转到 End。

Node 2: fixer_node

Input: ReviewState (读取 review_issues 和上一轮失败的 test_logs)

LLM 角色: 重构工程师。必须输出严格的 Patch 文件内容。

Output: 写入 generated_patches，递增 retry_count += 1。

Node 3: tester_node

Input: ReviewState (读取 generated_patches)

逻辑: 调用 SandboxFactory，拉起隔离环境 -> 将 Patch 应用到源码 -> 执行对应语言的 test 命令。

Output: 写入 test_logs 和 is_test_passed。

Conditional Edge (条件路由):

在 tester_node 之后：如果 is_test_passed == True 或者 retry_count >= 3，则流转到 submit_node（向 GitLab 提交评论）。如果 is_test_passed == False，流转回 fixer_node。

## 4. 沙盒执行矩阵配置 (Sandbox Matrix)
在 app/sandbox/docker_engine.py 中，必须硬编码或通过配置文件实现以下语言映射表：

Go:

Image: golang:1.23-alpine

Test Command: go test ./... -v

Lint Command: golangci-lint run

Python:

Image: python:3.12-slim

Test Command: pytest --maxfail=1

Lint Command: flake8 .

## 5. 异常处理与降级策略 (Edge Cases & Fallbacks)
LLM 速率限制 (Rate Limit): 在 core/llm_factory.py 中封装的 LLM 客户端必须接入 tenacity 库，实现 @retry(wait=wait_exponential(multiplier=1, min=2, max=10))。

沙盒死循环防范 (Sandbox Timeout): 沙盒执行命令必须加上超时控制（如 asyncio.wait_for(process, timeout=300)），防止恶意代码或死锁导致沙盒假死。

GitLab 异步解耦: Webhook 接收到请求后，必须立即返回 {"status": "processing"}，实际的 Graph 流转放在 FastAPI 的 BackgroundTasks 中执行。


---

### 🚀 给 Claude Code 的究极提示词 (The Final Prompts)

有了这份极其详细的 Blueprint，接下来让 Claude Code 执行的任务将变得非常清晰。请分三次发给 Claude Code：

#### 第一步：基建与核心数据模型 (发给 Claude Code)
```text
# Context
请读取我刚刚放在根目录的 `AutoReviewer-MAS_Blueprint.md` 文件，彻底理解项目的数据结构和架构规范。

# Task 1: 核心基建搭建
1. 根据文档第 1 节，创建所有的目录结构和空白文件。
2. 根据文档第 2 节，在 `app/schemas/state.py` 和 `app/schemas/llm_out.py` 中精确实现 Pydantic 模型和 TypedDict。
3. 创建 `config/settings.yaml`，并使用 pydantic-settings 在 `app/core/config.py` 中实现配置加载。
4. 在 `app/core/llm_factory.py` 中实现一个函数 `get_llm(role: str)`，读取 yaml 配置并返回对应的 LangChain `ChatOpenAI` 实例（兼容 Mimo 网关和 vLLM），并使用 `tenacity` 库加上重试机制。

请严格遵照 Blueprint 中的契约进行代码生成。
第二步：沙盒引擎与图节点 (发给 Claude Code)
Plaintext
# Context
基于我们已建立的 schemas，现在开发执行层。

# Task 2: 沙盒与 Agent 节点
1. 参照 Blueprint 第 4 节，在 `app/sandbox/docker_engine.py` 中实现 `DockerSandbox` 类，需使用 `asyncio` 将 `docker-py` 的调用包装为非阻塞，并加入 300 秒超时控制（参考第 5 节）。
2. 在 `app/agents/nodes/reviewer.py` 中，编写一个异步函数 `reviewer_node(state: ReviewState) -> dict`。使用 `.with_structured_output(ReviewerOutput)` 强制大模型输出 JSON，并返回更新后的字典以更新 Graph 状态。
3. 同理，在 `app/agents/nodes/fixer.py` 实现 `fixer_node`（强制输出 FixerOutput）。
4. 在 `app/agents/nodes/tester.py` 中实现 `tester_node`，解析 Patch，调用 Sandbox，提取并更新 `test_logs`。
第三步：编排与 GitLab 闭环 (发给 Claude Code)
Plaintext
# Context
最后，我们需要将孤立的节点串联，并接入 Webhook。

# Task 3: Graph 编排与 API
1. 在 `app/agents/workflow.py` 中，初始化 `StateGraph(ReviewState)`。添加节点，并根据 Blueprint 第 3 节实现循环重试的 Conditional Edge。编译生成 `app_graph`。
2. 在 `app/gitlab/client.py` 中，实现 `GitLabClient` 类（包含拉取 Diff 和发表 MR 评论的方法，请 mock 具体的网络请求细节或使用 python-gitlab）。
3. 在 `app/api/webhook.py` 实现 FastAPI POST 接口，提取 GitLab webhook payload。使用 `BackgroundTasks` 异步执行 `app_graph.invoke()`，接口立即返回 HTTP 200。
4. 完善 `main.py` 启动逻辑。