# AutoReviewer-MAS 多轮自动化审查技术方案

## 文档信息

| 项目 | 内容 |
|------|------|
| 版本 | v1.0 |
| 日期 | 2024年 |
| 状态 | 草案 |

---

## 一、需求背景

### 1.1 当前痛点

1. **线性流程限制**：当前系统执行 review → fix → critic 后即结束，无法实现多轮自动化优化
2. **用户决策负担过重**：用户需要手动选择修复哪些问题，而非简单的"是否修复"决策
3. **缺乏实时干预能力**：用户无法在 Agent 执行过程中随时输入意见或干预执行

### 1.2 目标愿景

实现类似 Claude Code 的多轮自优化审查流程，同时具备以下增强能力：
- 自动化多轮 review-fix-critic 循环，直至质量达标
- 用户仅需决策是否修复，系统自动处理问题优先级
- 支持用户实时输入干预，Agent 智能响应

---

## 二、开发方案

### 2.1 架构设计概览

#### 核心改动目标

1. **多轮循环机制**：实现 review → fix → critic → (retry/re-review) 的循环，直到满足终止条件
2. **自动化决策**：用户只需决定是否修复，而非选择具体问题
3. **实时用户干预**：支持用户在执行过程中随时输入，Agent 能立即响应

#### 新架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                    用户交互层 (CLI/WebSocket)                │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  实时输入队列  │  状态展示面板  │  控制指令解析      │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                            ▲
                            │ 用户输入
┌───────────────────────────┼─────────────────────────────────┐
│                           ▼                                 │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              LangGraph 状态图 (多轮循环)              │   │
│  │  ┌─────┐   ┌─────┐   ┌─────┐   ┌─────┐   ┌─────┐  │   │
│  │  │ 开始 │──▶│ 路由 │──▶│ 审查 │──▶│ 修复 │──▶│ 批评 │  │   │
│  │  └─────┘   └─────┘   └─────┘   └─────┘   └─────┘  │   │
│  │                │           │           │           │   │
│  │                └───────────┘           │           │   │
│  │                    重试循环            │           │   │
│  │                                        │           │   │
│  │                    ┌───────────────────┘           │   │
│  │                    ▼                               │   │
│  │              ┌──────────┐                          │   │
│  │              │ 检查点   │◀───── 用户输入处理       │   │
│  │              │ 决策逻辑 │                          │   │
│  │              └──────────┘                          │   │
│  │                    │                               │   │
│  │                    ▼                               │   │
│  │              ┌──────────┐                          │   │
│  │              │   结束   │                          │   │
│  │              └──────────┘                          │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 详细开发计划

#### 阶段一：状态模型扩展 (1-2天)

**1. 扩展 ReviewState 数据结构**

```python
# app/schemas/state.py - 新增字段
class ReviewState(TypedDict):
    # 现有字段保持不变...
    
    # 新增多轮控制字段
    current_round: int  # 当前轮次 (从0开始)
    max_rounds: int     # 最大轮次限制 (默认3)
    round_issues: Annotated[List[Dict[str, Any]], operator.add]  # 每轮发现的问题
    
    # 用户干预相关
    user_input_queue: Any  # 异步队列，用于接收用户输入
    user_instructions: str  # 当前有效的用户指令
    user_decisions: Dict[str, bool]  # 用户对问题的决策
    pending_user_approval: bool  # 是否在等待用户批准
    user_approval_result: Optional[bool]  # 用户批准结果
    
    # 多轮结果追踪
    fixed_issues: List[Dict[str, Any]]  # 已修复的问题
    remaining_issues: List[Dict[str, Any]]  # 剩余问题
    round_reports: List[Dict[str, Any]]  # 每轮的报告
```

**2. 新增用户输入事件模型**

```python
# app/schemas/user_input.py
from enum import Enum
from pydantic import BaseModel

class UserActionType(str, Enum):
    APPROVE = "approve"  # 批准当前修复
    REJECT = "reject"    # 拒绝当前修复
    INSTRUCTION = "instruction"  # 提供新指令
    STOP = "stop"        # 停止执行
    SKIP_ROUND = "skip_round"  # 跳过当前轮次

class UserInput(BaseModel):
    action: UserActionType
    content: Optional[str] = None  # 指令内容
    target_issues: Optional[List[str]] = None  # 针对特定问题的指令
    timestamp: float
```

#### 阶段二：工作流重构 (3-4天)

**1. 重构主工作流**

```python
# app/agents/workflow.py - 新增多轮循环逻辑
def build_multiround_graph() -> StateGraph:
    """构建支持多轮循环的工作流"""
    graph = StateGraph(ReviewState)
    
    # 添加节点
    graph.add_node("router_node", router_node)
    graph.add_node("reviewer_node", reviewer_node)
    graph.add_node("fixer_node", fixer_node)
    graph.add_node("critic_node", critic_node)
    graph.add_node("user_checkpoint_node", user_checkpoint_node)
    graph.add_node("decision_node", decision_node)
    graph.add_node("submit_node", submit_node)
    
    # 定义流转
    graph.add_conditional_edges("__start__", router_node)
    
    # 审查后：根据用户决策决定是否修复
    graph.add_conditional_edges(
        "reviewer_node",
        after_reviewer,
        {
            "fixer_node": "fixer_node",
            "user_checkpoint_node": "user_checkpoint_node",
            "__end__": END,
        },
    )
    
    # 用户检查点：等待用户输入
    graph.add_conditional_edges(
        "user_checkpoint_node",
        after_user_checkpoint,
        {
            "fixer_node": "fixer_node",
            "reviewer_node": "reviewer_node",
            "submit_node": "submit_node",
            "__end__": END,
        },
    )
    
    # 修复后：批评检查
    graph.add_edge("fixer_node", "critic_node")
    
    # 批评后：决策下一步
    graph.add_conditional_edges(
        "critic_node",
        after_critic,
        {
            "decision_node": "decision_node",
            "fixer_node": "fixer_node",
            "reviewer_node": "reviewer_node",
        },
    )
    
    # 决策节点：检查多轮条件
    graph.add_conditional_edges(
        "decision_node",
        make_decision,
        {
            "submit_node": "submit_node",
            "reviewer_node": "reviewer_node",
            "__end__": END,
        },
    )
    
    graph.add_edge("submit_node", END)
    
    return graph
```

**2. 实现关键节点**

```python
# app/agents/nodes/user_checkpoint.py
async def user_checkpoint_node(state: ReviewState) -> Dict[str, Any]:
    """用户检查点：等待并处理用户输入"""
    queue = state.get("user_input_queue")
    
    # 非阻塞检查用户输入
    user_input = None
    if queue and not queue.empty():
        user_input = await queue.get()
    
    if user_input:
        # 处理用户输入
        return _process_user_input(user_input, state)
    else:
        # 没有用户输入，继续自动流程
        return {"pending_user_approval": False}
```

**3. 实现智能决策逻辑**

```python
# app/agents/nodes/decision.py
def make_decision(state: ReviewState) -> str:
    """多轮决策逻辑"""
    current_round = state.get("current_round", 0)
    max_rounds = state.get("max_rounds", 3)
    remaining_issues = state.get("remaining_issues", [])
    user_instructions = state.get("user_instructions", "")
    
    # 终止条件检查
    if current_round >= max_rounds:
        return "submit_node"
    
    if not remaining_issues:
        return "submit_node"
    
    # 用户指令处理
    if "停止" in user_instructions or "stop" in user_instructions:
        return "submit_node"
    
    # 自动继续下一轮
    return "reviewer_node"
```

#### 阶段三：用户交互层 (2-3天)

**1. 实现实时输入监听**

```python
# app/cli/interactive.py
import asyncio
from typing import Optional

class InteractiveSession:
    def __init__(self):
        self.input_queue = asyncio.Queue()
        self.running = False
        self._input_task: Optional[asyncio.Task] = None
    
    async def start_input_listener(self):
        """启动异步输入监听"""
        self.running = True
        self._input_task = asyncio.create_task(self._listen_input())
    
    async def _listen_input(self):
        """监听用户输入"""
        import sys
        loop = asyncio.get_event_loop()
        
        while self.running:
            try:
                # 非阻塞读取用户输入
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if line:
                    line = line.strip()
                    if line:
                        await self.input_queue.put(line)
            except Exception as e:
                logging.error(f"输入监听错误: {e}")
                await asyncio.sleep(0.1)
```

**2. 重构 CLI 界面**

```python
# app/cli/main.py - 新增交互式审查
async def _run_interactive_review(diff_text: str, branch: str, repo_root: str):
    """交互式审查流程"""
    session = InteractiveSession()
    
    # 启动输入监听
    await session.start_input_listener()
    
    # 准备初始状态
    initial_state = {
        "user_input_queue": session.input_queue,
        "current_round": 0,
        "max_rounds": 3,
        "user_instructions": "",
    }
    
    # 构建并运行图
    graph = build_multiround_graph().compile()
    
    # 创建实时输出显示
    display = RealTimeDisplay()
    
    # 运行图，同时更新显示
    async for event in graph.astream(initial_state):
        display.update(event)
        
        # 检查是否需要用户输入
        if event.get("pending_user_approval"):
            display.show_approval_prompt()
            await _wait_for_approval(session.input_queue)
    
    await session.stop()
```

#### 阶段四：多轮优化与测试 (2-3天)

**1. 智能轮次管理**

- 实现基于问题严重性的动态轮次调整
- 添加收敛检测：如果连续两轮没有新问题，自动停止

**2. 用户指令解析**

```python
# app/utils/instruction_parser.py
class InstructionParser:
    def parse(self, instruction: str) -> Dict[str, Any]:
        """解析用户指令"""
        patterns = {
            "ignore": r"忽略(.*?)(问题|bug|错误)",
            "focus": r"只(修复|处理|关注)(.*?)(问题|bug|错误)",
            "max_rounds": r"(\d+)次(修复|重试|轮次)后停止",
        }
        # 解析逻辑...
```

### 2.3 配置与扩展

**1. 新增配置项**

```yaml
# config/settings.yaml.example
multiround:
  enabled: true
  max_rounds: 3  # 最大轮次
  auto_approve: false  # 是否自动批准修复
  convergence_threshold: 2  # 收敛阈值（连续无新问题的轮次）
  user_input_timeout: 30  # 用户输入超时（秒）
```

**2. 监控与日志增强**

- 添加每轮耗时统计
- 记录用户干预历史
- 生成多轮审查报告

---

## 三、验收方案

### 3.1 功能验收标准

#### 场景1：多轮自动修复

**测试用例**：
1. 准备一个包含3个不同严重性问题（critical, warning, info）的代码文件
2. 运行自动审查
3. 验证系统自动进行多轮修复

**验收标准**：
- [ ] 系统自动识别需要多轮修复
- [ ] 每轮修复后重新审查
- [ ] 达到最大轮次或无问题后自动停止
- [ ] 生成完整的多轮审查报告

#### 场景2：自动化决策

**测试用例**：
1. 运行审查，发现问题后
2. 系统显示问题摘要，仅询问"是否修复所有问题？(y/n)"
3. 用户选择 y 后，系统自动修复所有问题

**验收标准**：
- [ ] 用户无需选择具体问题
- [ ] 系统自动处理所有 critical 问题
- [ ] 提供清晰的问题摘要和修复计划
- [ ] 用户决策界面简洁明了

#### 场景3：实时用户干预

**测试用例**：
1. 启动审查，在第二轮修复过程中
2. 用户输入："忽略性能问题，只关注安全问题"
3. 系统立即响应，调整后续审查策略

**验收标准**：
- [ ] 用户输入能实时传递给 Agent
- [ ] Agent 在下个节点立即响应指令
- [ ] 系统状态实时更新
- [ ] 指令影响后续所有轮次

#### 场景4：复杂干预场景

**测试用例**：
1. 用户输入："第三次修复后停止，不管还有没有问题"
2. 系统按指令在第三轮后停止
3. 用户输入："重新检查文件A的安全性"
4. 系统针对文件A进行专项审查

**验收标准**：
- [ ] 复杂指令能正确解析和执行
- [ ] 指令优先级高于自动逻辑
- [ ] 提供指令执行反馈

### 3.2 性能验收标准

**响应时间**：
- 用户输入到系统响应：< 2秒
- 单轮审查-修复周期：< 60秒
- 多轮流程总时间：< 3分钟（3轮）

**资源使用**：
- 内存增量：< 100MB
- CPU 使用率：< 30%（空闲时）
- 网络请求：合理控制 API 调用频率

### 3.3 兼容性验收

**1. 向后兼容**
- [ ] 现有 CLI 命令正常工作
- [ ] 原有配置文件无需修改
- [ ] 现有测试用例全部通过

**2. 多平台支持**
- [ ] Windows/Linux/macOS 兼容
- [ ] 不同 Python 版本(3.9+)支持
- [ ] GitLab/GitHub webhook 正常工作

### 3.4 用户体验验收

**1. 交互流畅性**
- [ ] 输入响应即时反馈
- [ ] 进度显示清晰明了
- [ ] 错误提示友好明确

**2. 信息透明度**
- [ ] 每轮结果清晰展示
- [ ] 决策逻辑可解释
- [ ] 用户指令执行可追踪

---

## 四、与 Claude Code 对比优势分析

### 4.1 核心优势

| 优势维度 | AutoReviewer-MAS | Claude Code |
|----------|------------------|------------|
| **领域聚焦** | 专注于代码审查和修复，深度领域知识 | 通用编码助手，需明确任务上下文 |
| **多Agent协同** | Reviewer、Fixer、Critic 专业分工 | 单一模型处理所有任务 |
| **自动化程度** | 用户仅决策是否修复，系统自动处理 | 需要详细描述需求，手动选择 |
| **多轮优化** | 自动多轮循环直至质量达标 | 通常单次交互 |
| **实时干预** | 执行过程中随时输入干预 | 需要重新描述需求 |
| **企业集成** | 原生 GitLab/GitHub 集成 | 主要面向个人开发者 |
| **可扩展性** | 支持自定义规则和策略 | 配置相对固定 |
| **资源效率** | 针对性 API 调用，成本可控 | 可能产生不必要调用 |

### 4.2 适用场景对比

| 场景 | AutoReviewer-MAS | Claude Code |
|------|------------------|------------|
| **团队代码审查** | ✅ 原生支持，工作流集成 | ⚠️ 需要自定义流程 |
| **批量 PR 处理** | ✅ 自动化多轮处理 | ❌ 需要逐个处理 |
| **质量门禁** | ✅ 可配置的质量标准 | ⚠️ 需要手动检查 |
| **实时协作** | ✅ 支持实时干预 | ❌ 交互式但不实时 |
| **企业部署** | ✅ 私有部署，安全可控 | ⚠️ 依赖外部服务 |
| **学习成本** | ⚠️ 需要了解系统配置 | ✅ 即开即用 |

---

## 五、实施路线图

### 5.1 优先级排序

1. **P0 (核心功能)**：多轮循环机制、自动化决策
2. **P1 (关键体验)**：实时用户干预、智能指令解析
3. **P2 (增强功能)**：收敛检测、动态轮次调整
4. **P3 (优化完善)**：性能优化、监控增强

### 5.2 里程碑

| 里程碑 | 时间 | 交付内容 |
|--------|------|----------|
| 里程碑1 | 第1周 | 基础多轮循环工作正常 |
| 里程碑2 | 第2周 | 用户干预机制完成 |
| 里程碑3 | 第3周 | 全面测试与优化 |
| 里程碑4 | 第4周 | 生产就绪版本 |

### 5.3 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 多轮循环死循环 | 高 | 硬性最大轮次限制 + 收敛检测 |
| 用户输入阻塞 | 中 | 超时机制 + 非阻塞读取 |
| 状态管理复杂度 | 中 | 清晰的状态模型 + 充分测试 |
| 向后兼容性 | 低 | 保留原有接口，新增独立模块 |

---

## 六、附录

### 6.1 术语表

| 术语 | 说明 |
|------|------|
| 多轮循环 | review → fix → critic 的重复执行过程 |
| 收敛检测 | 判断是否需要继续循环的机制 |
| 用户检查点 | 等待并处理用户输入的节点 |
| 智能决策 | 基于规则自动决定下一步操作的逻辑 |

### 6.2 参考资料

- LangGraph 官方文档：StateGraph、Send API
- Claude Code 对话式编程模式
- 企业级代码审查最佳实践

---

## 七、变更记录

| 版本 | 日期 | 变更内容 | 作者 |
|------|------|----------|------|
| v1.0 | - | 初始版本 | - |
