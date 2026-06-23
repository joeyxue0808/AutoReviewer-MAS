"""AutoReviewer CLI 终端极客模式 - Phase 4 全模态接入。

本地伴随开发工具：
    auto-review --local

读取本地 Git 暂存区作为输入，绕过 Webhook，
通过 asyncio 直接调用后端 Graph，
利用 rich 库在终端流式打印 Agent 思考过程。

安装后使用：
    pip install -e .
    auto-review --local
    auto-review --local --branch feature/auth
"""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 GBK 编码，强制 UTF-8 以支持 emoji 和中文
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import typer
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

app = typer.Typer(
    name="auto-review",
    help="🤖 AutoReviewer-MAS CLI - 本地伴随代码审查工具",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool = False) -> None:
    """配置 Rich 日志。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )
    # 抑制 httpx/httpcore 的 HTTP 请求日志（每次 LLM 调用都刷屏）
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _get_git_diff(mode: str = "working") -> str:
    """获取 Git Diff。

    Args:
        mode: diff 模式
            - "working": 工作区全部变更（暂存+未暂存），默认
            - "staged": 仅暂存区变更
            - "branch:<name>": 当前分支与指定分支的差异
            - "commit:<sha>": 指定 commit 的变更
            - "range:<sha1>..<sha2>": commit 范围的变更

    Returns:
        diff 文本
    """
    if mode == "staged":
        cmd = ["git", "diff", "--cached"]
    elif mode == "working":
        # 已暂存 + 未暂存的全部变更
        # 先尝试 git diff HEAD，失败则回退到 git diff（新仓库无 commit 时）
        cmd = ["git", "diff", "HEAD"]
    elif mode.startswith("branch:"):
        branch = mode.split(":", 1)[1]
        cmd = ["git", "diff", f"{branch}...HEAD"]
    elif mode.startswith("commit:"):
        sha = mode.split(":", 1)[1]
        cmd = ["git", "diff", f"{sha}~1", sha]
    elif mode.startswith("range:"):
        range_str = mode.split(":", 1)[1]
        cmd = ["git", "diff", range_str]
    else:
        cmd = ["git", "diff"]

    try:
        result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=True)
        return result.stdout
    except subprocess.CalledProcessError:
        if mode == "working":
            # 无 commit 的新仓库：git diff HEAD 失败，合并 staged + unstaged
            return _run_git(["git", "diff", "--cached"]) + "\n" + _run_git(["git", "diff"])
        console.print("[red]Git diff 失败[/red]")
        raise typer.Exit(1)


def _run_git(cmd: list[str]) -> str:
    """执行 git 命令，返回 stdout。"""
    try:
        result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=True)
        return result.stdout
    except subprocess.CalledProcessError:
        return ""


def _get_full_scan_diff(repo_root: str) -> str:
    """生成全量扫描的合成 diff。

    将仓库中所有源文件视为新增文件（diff against /dev/null），
    使 DiffAnalyzer 能像处理正常 diff 一样处理全量扫描。
    """
    from pathlib import Path

    _SOURCE_SUFFIXES = {
        ".go", ".py", ".cpp", ".cc", ".h", ".hpp", ".java",
        ".vue", ".js", ".cjs", ".mjs", ".ts", ".tsx", ".dart", ".cs",
    }
    _SKIP_DIRS = {
        "node_modules", "vendor", "__pycache__", ".git", "dist", "build",
        ".venv", "venv", "env", ".tox", ".mypy_cache", ".lancedb",
        "tests", "test",
    }

    parts = []
    root = Path(repo_root)

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in _SOURCE_SUFFIXES:
            continue
        # 跳过指定目录
        rel = path.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        rel_str = str(rel).replace("\\", "/")
        lines = content.splitlines()
        # 必须用 a/xxx b/xxx 格式，否则 DiffAnalyzer 的正则无法匹配
        diff_header = (
            f"diff --git a/{rel_str} b/{rel_str}\n"
            f"new file mode 100644\n"
            f"--- /dev/null\n"
            f"+++ b/{rel_str}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n"
        )
        diff_body = "\n".join(f"+{line}" for line in lines)
        parts.append(diff_header + diff_body + "\n")

    return "\n".join(parts)


def _get_git_branch() -> str:
    """获取当前 Git 分支名。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, encoding="utf-8", errors="replace", check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _get_repo_root() -> str:
    """获取 Git 仓库根目录。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, encoding="utf-8", errors="replace", check=True,
        )
        return result.stdout.strip()
    except Exception:
        return os.getcwd()


@app.command()
def local(
    staged: bool = typer.Option(False, "--staged/--all", help="仅审查暂存区 (--staged) 或全部变更 (--all，默认)"),
    branch: str = typer.Option(None, "--branch", "-b", help="与指定分支对比差异"),
    commit: str = typer.Option(None, "--commit", "-c", help="审查指定 commit 的变更 (SHA)"),
    commit_range: str = typer.Option(None, "--range", "-r", help="审查 commit 范围 (如 abc123..def456)"),
    full: bool = typer.Option(False, "--full", "-f", help="全量扫描（审查整个代码库，非增量）"),
    max_rounds: int = typer.Option(5, "--max-rounds", "-m", help="最大审查轮次（收敛检测自动停止，默认 5）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示详细日志"),
):
    """🔍 审查本地代码变更。

    支持多种审查模式（自动多轮修复循环）：
    \b
    默认        审查工作区全部变更（暂存+未暂存）
    --staged    仅审查暂存区
    --branch    与指定分支对比
    --commit    审查某个 commit 的变更
    --range     审查 commit 范围的变更
    --full      全量扫描整个代码库
    -m/--max    最大审查轮次（默认 5）
    """
    _setup_logging(verbose)

    # 显示 Banner
    console.print(Panel.fit(
        "[bold cyan]🤖 AutoReviewer-MAS[/bold cyan]\n"
        "[dim]本地伴随代码审查 · 收敛自动修复[/dim]",
        border_style="cyan",
    ))

    # 获取 Git 信息
    repo_root = _get_repo_root()
    current_branch = branch or _get_git_branch()

    # 确定 diff 模式
    if full:
        diff_text = _get_full_scan_diff(repo_root)
    elif commit:
        diff_text = _get_git_diff(mode=f"commit:{commit}")
        current_branch = f"commit-{commit[:8]}"
    elif commit_range:
        diff_text = _get_git_diff(mode=f"range:{commit_range}")
        current_branch = f"range-{commit_range[:16]}"
    elif branch:
        diff_text = _get_git_diff(mode=f"branch:{branch}")
    elif staged:
        diff_text = _get_git_diff(mode="staged")
    else:
        diff_text = _get_git_diff(mode="working")

    if not diff_text.strip():
        console.print("[yellow]⚠️  没有检测到代码变更[/yellow]")
        if staged:
            console.print("[dim]提示: 默认模式已审查全部变更，--staged 仅审查暂存区[/dim]")
        elif not full:
            console.print("[dim]提示: 使用 --full 进行全量代码扫描[/dim]")
        raise typer.Exit(0)

    # 显示变更概览
    _display_diff_summary(diff_text)

    # 执行审查
    console.print()
    asyncio.run(_run_review(diff_text, current_branch, repo_root, max_rounds=max_rounds))


def _display_diff_summary(diff_text: str) -> None:
    """显示 Diff 概览表格。"""
    import re

    files = re.findall(r"^diff --git a/(.+?) b/(.+?)$", diff_text, re.MULTILINE)
    additions = len(re.findall(r"^\+[^+]", diff_text, re.MULTILINE))
    deletions = len(re.findall(r"^-[^-]", diff_text, re.MULTILINE))

    table = Table(title="📋 变更概览", show_lines=True)
    table.add_column("文件", style="cyan")
    table.add_column("状态", style="green")

    for _, new_path in files[:20]:  # 最多显示 20 个文件
        table.add_row(new_path, "modified")

    if len(files) > 20:
        table.add_row(f"... 还有 {len(files) - 20} 个文件", "")

    console.print(table)
    console.print(f"[green]+{additions}[/green] / [red]-{deletions}[/red] 行")


async def _run_review(diff_text: str, branch: str, repo_root: str, max_rounds: int = 3) -> None:
    """执行审查流水线 — 多轮自动循环模式。

    流程：
    1. Reviewer 审查 → 展示问题摘要
    2. 用户选择: 修复全部 / 跳过
    3. 如果用户选择修复：
       a. Fixer 修复 → 写入磁盘
       b. 重新获取最新 diff → 再次审查
       c. 如果还有 critical 问题，继续修复
       d. 循环直到没有 critical 问题或达到最大轮次
    """
    from app.agents.workflow import build_review_only_graph, build_fix_only_graph
    from app.core.diff_analyzer import DiffAnalyzer
    from app.schemas.state import ReviewState

    analyzer = DiffAnalyzer()
    review_graph = build_review_only_graph().compile()
    fix_graph = build_fix_only_graph().compile()

    current_round = 0
    all_round_issues = []
    total_fixes_applied = 0
    current_diff = diff_text
    prev_issue_count = -1  # -1 表示首轮
    hard_limit = 10  # 防止无限循环的硬上限

    while current_round < hard_limit:
        current_round += 1
        round_label = f"第 {current_round} 轮" if current_round == 1 else f"第 {current_round} 轮 (上轮 {prev_issue_count} 个问题)"
        console.print(f"\n[bold cyan]═══ {round_label} ═══[/bold cyan]")

        # ── 准备阶段 ──
        detected = analyzer.detect_languages(current_diff)
        chunk_list = analyzer.chunk_diff(current_diff)

        if not detected:
            console.print("[yellow]⚠️  无法识别编程语言，使用默认审查[/yellow]")
            detected = ["unknown"]

        if current_round == 1:
            console.print(f"[cyan]🔍 检测到语言: {', '.join(detected)}[/cyan]")
            console.print(f"[cyan]📦 切分为 {len(chunk_list)} 个 Chunk (防上下文爆炸)[/cyan]")

        diff_chunks = {c.chunk_id: c.content for c in chunk_list}

        from app.core.repo_mapper import generate_repo_map
        repo_context = generate_repo_map(repo_root)
        if current_round == 1:
            console.print(f"[cyan]🗺️  Repo-Map 已生成 ({len(repo_context)} chars)[/cyan]")

        initial_state: ReviewState = {
            "vcs_provider": "cli",
            "pr_id": f"local-{branch}",
            "trigger_type": "cli",
            "repo_id": repo_root,
            "repo_context": repo_context,
            "diff_chunks": diff_chunks,
            "detected_languages": detected,
            "review_issues": [],
            "search_replace_blocks": [],
            "test_logs": "",
            "is_test_passed": False,
            "retry_count": 0,
            "error_count": 0,
            "error_type": "",
            "last_node": "",
        }

        # ── 审查 ──
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"🤖 第 {current_round} 轮审查中...", total=None)

            try:
                review_state = await review_graph.ainvoke(initial_state)
            except Exception as e:
                progress.update(task, description=f"[red]❌ 审查失败: {e}[/red]")
                console.print_exception()
                raise typer.Exit(1)

            progress.update(task, description="[green]✅ 审查完成[/green]")

        issues = review_state.get("review_issues", [])

        # ── 无问题 ──
        if not issues:
            console.print(Panel(
                "[bold green]✅ 未发现问题 — 代码审查通过[/bold green]",
                border_style="green",
                padding=(0, 2),
            ))
            break

        all_round_issues.extend(issues)

        # ── 逐条展示问题 ──
        severity_counts = {"critical": 0, "warning": 0, "info": 0}
        for issue in issues:
            sev = issue.get("severity", "info")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        badges = []
        if severity_counts["critical"]:
            badges.append(f"[bold red]🔴 Critical: {severity_counts['critical']}[/bold red]")
        if severity_counts["warning"]:
            badges.append(f"[yellow]🟡 Warning: {severity_counts['warning']}[/yellow]")
        if severity_counts["info"]:
            badges.append(f"[blue]🔵 Info: {severity_counts['info']}[/blue]")

        console.print()
        console.print(Panel(
            f"[bold]🔍 发现 {len(issues)} 个问题 — {' | '.join(badges)}[/bold]",
            border_style="blue",
        ))

        # 紧凑表格展示
        sev_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
        table = Table(show_lines=False, show_header=True, header_style="bold")
        table.add_column("#", style="dim", width=4)
        table.add_column("级别", width=8)
        table.add_column("位置", style="cyan", min_width=30)
        table.add_column("描述", ratio=1)

        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "info")
            fp = issue.get("file_path", "")
            ln = issue.get("line_number", "")
            desc = issue.get("description", "")
            loc = f"{fp}:{ln}" if ln else fp
            # 截断过长描述
            if len(desc) > 100:
                desc = desc[:97] + "..."
            table.add_row(str(i), f"{sev_icon.get(sev, '•')} {sev}", loc, desc)

        console.print(table)
        console.print()

        # ── 收敛检测 ──
        current_issue_count = len(issues)
        has_fixable = severity_counts["critical"] > 0 or severity_counts["warning"] > 0

        if not has_fixable:
            console.print("[green]✅ 仅有 info 级别提示，无需修复[/green]")
            break

        # 非首轮：检查是否收敛
        if prev_issue_count >= 0:
            if current_issue_count >= prev_issue_count:
                console.print(f"[yellow]📊 问题数未减少（{prev_issue_count} → {current_issue_count}），已收敛，停止修复[/yellow]")
                break
            else:
                console.print(f"[green]📊 问题数减少（{prev_issue_count} → {current_issue_count}），继续修复[/green]")

        prev_issue_count = current_issue_count

        # ── 用户决策（仅首轮询问）──
        if current_round == 1:
            fix_target = "critical + warning" if severity_counts["critical"] == 0 else "critical"
            console.print(f"[bold]选择操作（目标: {fix_target} 问题）:[/bold]")
            console.print("  [cyan]y[/cyan] — 自动修复")
            console.print("  [cyan]n[/cyan] — 跳过，不修复")
            console.print()

            choice = console.input("[bold green]是否修复？(y/N) > [/bold green]").strip().lower()
            if choice != "y":
                console.print("[yellow]⏭️  已跳过修复，审查结束[/yellow]")
                break
        else:
            console.print(f"[yellow]🔄 继续自动修复...[/yellow]")

        # ── 修复 ──
        fix_state = dict(review_state)
        fix_state["search_replace_blocks"] = []
        fix_state["test_logs"] = ""
        fix_state["is_test_passed"] = False
        fix_state["retry_count"] = 0
        fix_state["error_count"] = 0
        fix_state["error_type"] = ""
        fix_state["last_node"] = ""

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"🔧 第 {current_round} 轮修复中...", total=None)

            try:
                final_state = await fix_graph.ainvoke(fix_state)
            except Exception as e:
                progress.update(task, description=f"[red]❌ 修复失败: {e}[/red]")
                console.print_exception()
                raise typer.Exit(1)

            progress.update(task, description="[green]✅ 修复完成[/green]")

        # ── 将修复写入磁盘（自动模式，跳过确认） ──
        blocks = final_state.get("search_replace_blocks", [])
        if blocks:
            written = _apply_blocks_to_files(blocks, repo_root, auto_write=True)
            total_fixes_applied += written or 0
            if written:
                # 重新获取最新 diff
                new_diff = _get_git_diff("working")
                if new_diff.strip():
                    current_diff = new_diff
                    console.print(f"[cyan]🔄 已重新获取最新 diff，准备下一轮审查[/cyan]")
                else:
                    console.print("[green]✅ 所有变更已提交，没有新的 diff[/green]")
                    break
            else:
                console.print("[yellow]⚠️  没有文件被修改，停止循环[/yellow]")
                break
        else:
            console.print("[yellow]⚠️  没有生成修复块，停止循环[/yellow]")
            break

    # ── 最终报告 ──
    console.print(f"\n[bold cyan]═══ 审查完成（共 {current_round} 轮）═══[/bold cyan]")
    if total_fixes_applied:
        console.print(f"[green]📝 共写入 {total_fixes_applied} 个文件的修改[/green]")

    if all_round_issues:
        severity_counts = {"critical": 0, "warning": 0, "info": 0}
        for issue in all_round_issues:
            sev = issue.get("severity", "info")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        badges = []
        if severity_counts["critical"]:
            badges.append(f"[bold red]🔴 Critical: {severity_counts['critical']}[/bold red]")
        if severity_counts["warning"]:
            badges.append(f"[yellow]🟡 Warning: {severity_counts['warning']}[/yellow]")
        if severity_counts["info"]:
            badges.append(f"[blue]🔵 Info: {severity_counts['info']}[/blue]")

        console.print(f"[bold]📊 累计发现 {len(all_round_issues)} 个问题 — {' | '.join(badges)}[/bold]")

        if severity_counts["critical"] == 0 and severity_counts["warning"] == 0:
            if current_round > 1:
                console.print(Panel(
                    "[bold green]🎉 所有问题已修复[/bold green]",
                    border_style="green",
                    padding=(0, 2),
                ))
            else:
                console.print(Panel(
                    "[bold green]✅ 仅有 info 提示，无需修复[/bold green]",
                    border_style="green",
                    padding=(0, 2),
                ))
        elif severity_counts["critical"] == 0:
            console.print(Panel(
                f"[bold yellow]📋 仍有 {severity_counts['warning']} 个 warning 问题（无 critical）[/bold yellow]",
                border_style="yellow",
                padding=(0, 2),
            ))
        else:
            console.print(Panel(
                f"[bold red]⚠️  还有 {severity_counts['critical']} 个 critical 问题未修复（已达最大轮次）[/bold red]",
                border_style="red",
                padding=(0, 2),
            ))
    else:
        console.print(Panel(
            "[bold green]✅ 未发现问题 — 代码审查通过[/bold green]",
            border_style="green",
            padding=(0, 2),
        ))


def _prompt_issue_selection(issues: list) -> list:
    """展示审查问题清单，让用户选择要修复的项。

    Returns:
        用户选中的 issue 子列表
    """
    sev_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
    sev_style = {"critical": "bold red", "warning": "yellow", "info": "cyan"}

    # 展示问题清单
    console.print()
    console.print(Panel(
        f"[bold]🔍 发现 {len(issues)} 个问题 — 请选择要修复的项目[/bold]",
        border_style="blue",
    ))
    console.print()

    for i, issue in enumerate(issues, 1):
        sev = issue.get("severity", "info")
        fp = issue.get("file_path", "")
        ln = issue.get("line_number", "")
        desc = issue.get("description", "")
        sug = issue.get("suggestion", "")

        loc = f"{fp}:{ln}" if ln else fp
        content = (
            f"[{sev_style.get(sev, 'white')}]{sev_icon.get(sev, '•')} [{sev.upper()}][/] "
            f"[cyan]{loc}[/cyan]\n\n{desc}"
        )
        if sug:
            content += f"\n[green]💡 建议:[/green] {sug}"

        console.print(Panel(
            content,
            title=f"[bold]#{i}[/bold]",
            border_style=sev_style.get(sev, "white"),
            padding=(0, 1),
        ))

    # 交互提示
    console.print()
    console.print("[bold]选择操作:[/bold]")
    console.print("  [cyan]a[/cyan] — 修复全部问题")
    console.print("  [cyan]1,3,5[/cyan] — 修复指定编号（逗号分隔）")
    console.print("  [cyan]n[/cyan] — 跳过，不修复")
    console.print()

    choice = console.input("[bold green]请输入选择 > [/bold green]").strip().lower()

    if choice == "n" or choice == "":
        return []
    elif choice == "a":
        return issues
    else:
        try:
            indices = [int(x.strip()) for x in choice.split(",")]
            selected = []
            for idx in indices:
                if 1 <= idx <= len(issues):
                    selected.append(issues[idx - 1])
                else:
                    console.print(f"[yellow]⚠️  忽略无效编号: {idx}[/yellow]")
            return selected
        except ValueError:
            console.print("[yellow]⚠️  输入格式无效，跳过修复[/yellow]")
            return []


def _apply_blocks_to_files(blocks: list, repo_root: str, auto_write: bool = False) -> int:
    """将 Search/Replace Blocks 应用到磁盘上的源文件。

    流程：读取源文件 → 展示变更预览 → 用户确认 → PatchApplier 写入

    Args:
        blocks: Search/Replace 块列表
        repo_root: 仓库根目录
        auto_write: 是否自动写入（跳过用户确认，用于多轮修复的后续轮次）

    Returns:
        写入的文件数量
    """
    from pathlib import Path
    from app.utils.patch_applier import PatchApplier

    if not blocks:
        return 0

    # 收集需要读取的文件
    files_to_read: set[str] = set()
    for block in blocks:
        fp = block.get("file_path", "")
        if fp:
            files_to_read.add(fp)

    if not files_to_read:
        return 0

    # 读取源文件
    source_files: dict[str, str] = {}
    for fp in files_to_read:
        full_path = Path(repo_root) / fp
        if full_path.exists():
            try:
                source_files[fp] = full_path.read_text(encoding="utf-8")
            except Exception as e:
                console.print(f"[yellow]⚠️  无法读取 {fp}: {e}[/yellow]")
        else:
            console.print(f"[yellow]⚠️  文件不存在: {fp}[/yellow]")

    if not source_files:
        console.print("[yellow]⚠️  无法读取任何源文件，跳过写入[/yellow]")
        return 0

    # 展示变更预览
    console.print()
    console.print(Panel(
        f"[bold]📝 即将修改 {len(source_files)} 个文件（共 {len(blocks)} 个修改块）[/bold]",
        border_style="cyan",
    ))

    for i, block in enumerate(blocks, 1):
        fp = block.get("file_path", "")
        search = block.get("search_block", block.get("search", ""))
        replace = block.get("replace_block", block.get("replace", ""))

        diff_lines = [f"--- a/{fp}", f"+++ b/{fp}", ""]
        for line in search.splitlines():
            diff_lines.append(f"[red]- {line}[/red]")
        diff_lines.append("")
        for line in replace.splitlines():
            diff_lines.append(f"[green]+ {line}[/green]")

        console.print(Panel(
            "\n".join(diff_lines),
            title=f"[cyan]Block #{i}[/cyan] — [dim]{fp}[/dim]",
            border_style="bright_black",
            padding=(0, 1),
        ))

    # 用户确认（auto_write 模式下跳过确认）
    if not auto_write:
        console.print()
        confirm = console.input("[bold green]确认写入以上修改？(y/N) > [/bold green]").strip().lower()
        if confirm != "y":
            console.print("[yellow]⏭️  已跳过文件写入[/yellow]")
            return 0

    # 应用 patches
    applier = PatchApplier()
    try:
        modified = applier.apply(source_files, blocks)
    except Exception as e:
        console.print(f"[red]❌ Patch 应用失败: {e}[/red]")
        return

    # 写回磁盘
    written = 0
    for fp, content in modified.items():
        if content != source_files.get(fp):
            full_path = Path(repo_root) / fp
            try:
                full_path.write_text(content, encoding="utf-8")
                written += 1
                console.print(f"  [green]✓[/green] {fp}")
            except Exception as e:
                console.print(f"  [red]✗[/red] {fp}: {e}")

    console.print()
    console.print(Panel(
        f"[bold green]✅ 已写入 {written} 个文件的修改[/bold green]",
        border_style="green",
        padding=(0, 2),
    ))
    
    return written


def _display_results(state: dict) -> None:
    """在终端显示审查结果（完整版，不截断关键内容）。"""
    issues = state.get("review_issues", [])
    blocks = state.get("search_replace_blocks", [])
    test_logs = state.get("test_logs", "")
    is_passed = state.get("is_test_passed", False)
    retry_count = state.get("retry_count", 0)

    console.print()

    # ── 审查问题 ──
    if issues:
        severity_counts = {"critical": 0, "warning": 0, "info": 0}
        for issue in issues:
            sev = issue.get("severity", "info")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        badges = []
        if severity_counts["critical"]:
            badges.append(f"[bold red]🔴 critical {severity_counts['critical']}[/bold red]")
        if severity_counts["warning"]:
            badges.append(f"[yellow]🟡 warning {severity_counts['warning']}[/yellow]")
        if severity_counts["info"]:
            badges.append(f"[blue]🔵 info {severity_counts['info']}[/blue]")

        header = f"🔍 审查报告 — {len(issues)} 个问题  {' │ '.join(badges)}"
        if retry_count > 0:
            header += f"  [dim](重试 {retry_count} 次)[/dim]"

        # 每个 issue 用独立 Panel 展示，避免表格截断
        console.print(Panel(header, style="bold", border_style="blue"))
        console.print()

        sev_style = {"critical": "bold red", "warning": "yellow", "info": "cyan"}
        sev_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}

        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "info")
            fp = issue.get("file_path", "")
            ln = issue.get("line_number", "")
            desc = issue.get("description", "")
            sug = issue.get("suggestion", "")

            # 构建单个 issue 的内容
            loc = f"{fp}:{ln}" if ln else fp
            content = f"[{sev_style.get(sev, 'white')}]{sev_icon.get(sev, '•')} [{sev.upper()}][/] [cyan]{loc}[/cyan]\n\n"
            content += f"{desc}\n"
            if sug:
                content += f"\n[green]💡 建议:[/green] {sug}"

            console.print(Panel(
                content,
                title=f"[dim]#{i}[/dim]",
                border_style=sev_style.get(sev, "white"),
                padding=(0, 1),
            ))
    else:
        console.print(Panel(
            "[bold green]✅ 未发现问题 — 代码审查通过[/bold green]",
            border_style="green",
            padding=(0, 2),
        ))

    # ── Search/Replace Blocks ──
    if blocks:
        console.print()
        console.print(Panel(
            f"[bold cyan]📝 生成 {len(blocks)} 个修复块[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        ))
        console.print()

        for i, block in enumerate(blocks, 1):
            fp = block.get("file_path", "")
            search = block.get("search_block", block.get("search", ""))
            replace = block.get("replace_block", block.get("replace", ""))

            # 用完整 diff 格式展示，不截断
            diff_lines = []
            diff_lines.append(f"--- a/{fp}")
            diff_lines.append(f"+++ b/{fp}")
            diff_lines.append("")
            for line in search.splitlines():
                diff_lines.append(f"- {line}")
            diff_lines.append("")
            for line in replace.splitlines():
                diff_lines.append(f"+ {line}")

            diff_text = "\n".join(diff_lines)

            console.print(Panel(
                Syntax(diff_text, "diff", theme="monokai", line_numbers=False, word_wrap=True),
                title=f"[cyan]Block #{i}[/cyan] — [dim]{fp}[/dim]",
                border_style="bright_black",
                padding=(0, 1),
            ))

    # ── 测试结果 ──
    if test_logs:
        status = "[bold green]✅ 通过[/bold green]" if is_passed else "[bold red]❌ 失败[/bold red]"
        console.print()
        console.print(Panel(
            Syntax(test_logs, "text", theme="monokai", word_wrap=True),
            title=f"🧪 测试结果: {status}",
            border_style="green" if is_passed else "red",
        ))

    # ── 最终状态 ──
    console.print()
    if is_passed:
        console.print(Panel(
            "[bold green]🎉 审查完成 — 测试通过，代码可合并[/bold green]",
            border_style="green",
            padding=(0, 2),
        ))
    elif retry_count >= 3:
        console.print(Panel(
            "[bold yellow]⚠️  审查完成 — 达到最大重试次数，降级提交已有结果[/bold yellow]",
            border_style="yellow",
            padding=(0, 2),
        ))
    else:
        console.print(Panel(
            "[bold red]❌ 审查完成 — 存在未解决问题[/bold red]",
            border_style="red",
            padding=(0, 2),
        ))


@app.command()
def multiround(
    staged: bool = typer.Option(False, "--staged/--all", help="仅审查暂存区 (--staged) 或全部变更 (--all，默认)"),
    branch: str = typer.Option(None, "--branch", "-b", help="与指定分支对比差异"),
    commit: str = typer.Option(None, "--commit", "-c", help="审查指定 commit 的变更 (SHA)"),
    range: str = typer.Option(None, "--range", "-r", help="审查 commit 范围 (如 abc123..def456)"),
    full: bool = typer.Option(False, "--full", "-f", help="全量扫描（审查整个代码库，非增量）"),
    max_rounds: int = typer.Option(3, "--max-rounds", "-m", help="最大审查轮次"),
    auto_approve: bool = typer.Option(False, "--auto-approve", "-a", help="自动批准修复（无需用户确认）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示详细日志"),
):
    """🔄 多轮交互式审查模式。

    支持自动多轮 review → fix → critic 循环，用户可以实时输入指令干预。
    \b
    交互指令：
      y/yes/是       批准修复
      n/no/否        拒绝修复
      stop/停止      停止执行
      skip/跳过      跳过当前轮次
      忽略xxx问题    忽略特定类别问题
      只关注xxx问题  只处理特定类别问题
    """
    _setup_logging(verbose)

    # 显示 Banner
    console.print(Panel.fit(
        "[bold cyan]🤖 AutoReviewer-MAS[/bold cyan]\n"
        "[dim]多轮交互式审查模式[/dim]\n"
        f"[dim]最大轮次: {max_rounds} | 自动批准: {'是' if auto_approve else '否'}[/dim]",
        border_style="cyan",
    ))

    # 获取 Git 信息
    repo_root = _get_repo_root()
    current_branch = branch or _get_git_branch()

    # 确定 diff 模式
    if full:
        diff_text = _get_full_scan_diff(repo_root)
    elif commit:
        diff_text = _get_git_diff(mode=f"commit:{commit}")
        current_branch = f"commit-{commit[:8]}"
    elif range:
        diff_text = _get_git_diff(mode=f"range:{range}")
        current_branch = f"range-{range[:16]}"
    elif branch:
        diff_text = _get_git_diff(mode=f"branch:{branch}")
    elif staged:
        diff_text = _get_git_diff(mode="staged")
    else:
        diff_text = _get_git_diff(mode="working")

    if not diff_text.strip():
        console.print("[yellow]⚠️  没有检测到代码变更[/yellow]")
        raise typer.Exit(0)

    # 显示变更概览
    _display_diff_summary(diff_text)

    # 执行多轮审查
    console.print()
    asyncio.run(_run_multiround_review(
        diff_text=diff_text,
        branch=current_branch,
        repo_root=repo_root,
        max_rounds=max_rounds,
        auto_approve=auto_approve,
    ))


async def _run_multiround_review(
    diff_text: str,
    branch: str,
    repo_root: str,
    max_rounds: int = 3,
    auto_approve: bool = False,
) -> None:
    """执行多轮交互式审查流水线。"""
    from app.agents.workflow_multiround import build_multiround_graph
    from app.core.diff_analyzer import DiffAnalyzer
    from app.cli.interactive import InteractiveSession, RealTimeDisplay
    from app.schemas.state import ReviewState

    # ── 准备阶段 ──
    analyzer = DiffAnalyzer()
    detected = analyzer.detect_languages(diff_text)
    chunk_list = analyzer.chunk_diff(diff_text)

    if not detected:
        console.print("[yellow]⚠️  无法识别编程语言，使用默认审查[/yellow]")
        detected = ["unknown"]

    console.print(f"[cyan]🔍 检测到语言: {', '.join(detected)}[/cyan]")
    console.print(f"[cyan]📦 切分为 {len(chunk_list)} 个 Chunk[/cyan]")

    diff_chunks = {c.chunk_id: c.content for c in chunk_list}

    from app.core.repo_mapper import generate_repo_map
    repo_context = generate_repo_map(repo_root)
    console.print(f"[cyan]🗺️  Repo-Map 已生成 ({len(repo_context)} chars)[/cyan]")

    # ── 启动交互式会话 ──
    session = InteractiveSession(
        auto_approve=auto_approve,
        input_timeout=30,
    )
    display = RealTimeDisplay()
    display.show_welcome()

    await session.start()

    # ── 构建初始状态 ──

    initial_state: ReviewState = {
        "vcs_provider": "cli",
        "pr_id": f"local-{branch}",
        "trigger_type": "cli",
        "repo_id": repo_root,
        "repo_context": repo_context,
        "diff_chunks": diff_chunks,
        "detected_languages": detected,
        "review_issues": [],
        "search_replace_blocks": [],
        "test_logs": "",
        "is_test_passed": False,
        "retry_count": 0,
        "error_count": 0,
        "error_type": "",
        "last_node": "",
        # 多轮审查字段
        "current_round": 0,
        "max_rounds": max_rounds,
        "round_issues": [],
        "user_input_queue": session.input_queue,
        "user_instructions": "",
        "user_decisions": {},
        "pending_user_approval": False,
        "user_approval_result": None,
        "fixed_issues": [],
        "remaining_issues": [],
        "round_reports": [],
    }

    # ── 构建并执行 Graph ──
    graph = build_multiround_graph()
    compiled_graph = graph.compile()

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("🤖 多轮审查中...", total=None)

            # 流式执行 Graph
            async for event in compiled_graph.astream(initial_state, {"recursion_limit": 50}):
                # 更新进度显示
                for node_name, node_output in event.items():
                    if node_name == "reviewer_node":
                        progress.update(task, description="🔍 Reviewer 审查中...")
                        issues = node_output.get("review_issues", [])
                        if issues:
                            display.show_issues_found(issues)
                    elif node_name == "reduce_reviewer_node":
                        progress.update(task, description="📊 合并审查结果...")
                    elif node_name == "fixer_node":
                        progress.update(task, description="🔧 Fixer 修复中...")
                    elif node_name == "critic_node":
                        progress.update(task, description="🔎 Critic 校验中...")
                    elif node_name == "user_checkpoint_node":
                        progress.update(task, description="👤 等待用户输入...")
                        if node_output.get("pending_user_approval"):
                            display.show_approval_prompt()
                            # 等待用户输入
                            user_text = await session.get_user_input(timeout=60)
                            if user_text:
                                display.show_user_input_received(user_text)
                                await session.put_user_input(user_text)
                    elif node_name == "decision_node":
                        progress.update(task, description="🧠 决策中...")
                    elif node_name == "submit_node":
                        progress.update(task, description="[green]✅ 生成报告[/green]")
                        # 显示最终报告
                        report = node_output.get("review_report", "")
                        stats = node_output.get("review_stats", {})
                        display.show_final_report(report, stats)

            progress.update(task, description="[green]✅ 多轮审查完成[/green]")

    except Exception as e:
        display.show_error(str(e))
        console.print_exception()
        raise typer.Exit(1)
    finally:
        await session.stop()

    # ── 将修复写入磁盘 ──
    # 注意：多轮模式下修复已在循环中逐步应用
    # 这里可以输出统计信息
    console.print("\n[dim]审查流程已完成[/dim]")


@app.command()
def version():
    """📌 显示版本信息。"""
    console.print("AutoReviewer-MAS CLI v0.5.0")


if __name__ == "__main__":
    app()
