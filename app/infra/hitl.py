"""Human-in-the-Loop 高危操作检测与审批流 - Phase 4。

当识别到包含高危操作时，Graph 持久化状态并挂起 (Suspend)。
系统发送审批卡片至飞书/钉钉，Tech Lead 点击"允许提交"后唤醒图。

高危操作定义：
- 修改数据库迁移脚本 (alembic/migrations)
- 变更核心鉴权文件 (auth/security/rbac)
- 修改 CI/CD 配置 (.gitlab-ci.yml/Dockerfile)
- 变更基础设施配置 (terraform/k8s/helm)
- 修改支付/金融相关模块
"""

import logging
import re
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 高危文件路径模式
# ─────────────────────────────────────────────

_HIGH_RISK_PATTERNS: list[re.Pattern] = [
    # 数据库迁移
    re.compile(r"(alembic|migrations?|flyway|liquibase)/", re.IGNORECASE),
    re.compile(r"\b(migration|schema_change)", re.IGNORECASE),
    # 鉴权安全
    re.compile(r"(auth|security|rbac|permission|oauth|jwt|session)/", re.IGNORECASE),
    re.compile(r"\b(login|logout|password|token|credential)", re.IGNORECASE),
    # CI/CD
    re.compile(r"\.(gitlab-ci|github)\.yml$"),
    re.compile(r"Dockerfile"),
    re.compile(r"docker-compose"),
    # 基础设施
    re.compile(r"(terraform|k8s|kubernetes|helm|ansible)/", re.IGNORECASE),
    # 支付金融
    re.compile(r"(payment|billing|finance|transaction|refund)/", re.IGNORECASE),
    # 核心配置
    re.compile(r"(config|settings|env)\.(yaml|yml|json|toml|env)$"),
]


@dataclass
class HighRiskMatch:
    """高危操作匹配结果。"""

    file_path: str
    pattern_name: str
    severity: str  # "high" / "critical"


def detect_high_risk_operations(
    review_issues: list,
    search_replace_blocks: list,
) -> List[HighRiskMatch]:
    """检测审查结果中的高危操作。

    Args:
        review_issues: Reviewer 输出的问题列表
        search_replace_blocks: Fixer 输出的搜索/替换块

    Returns:
        匹配到的高危操作列表
    """
    matches: List[HighRiskMatch] = []

    # 从 review_issues 中提取文件路径
    for issue in review_issues:
        fp = issue.get("file_path", "") if isinstance(issue, dict) else getattr(issue, "file_path", "")
        severity = issue.get("severity", "") if isinstance(issue, dict) else getattr(issue, "severity", "")
        if fp and severity == "critical":
            risk = _match_risk_pattern(fp)
            if risk:
                matches.append(risk)

    # 从 search_replace_blocks 中提取文件路径
    for block in search_replace_blocks:
        fp = block.get("file_path", "")
        if fp:
            risk = _match_risk_pattern(fp)
            if risk:
                # 去重
                if not any(m.file_path == fp for m in matches):
                    matches.append(risk)

    if matches:
        logger.warning(
            "检测到 %d 个高危操作，需要人工审批: %s",
            len(matches),
            [m.file_path for m in matches],
        )

    return matches


def _match_risk_pattern(file_path: str) -> HighRiskMatch | None:
    """检查文件路径是否匹配高危模式。"""
    for pattern in _HIGH_RISK_PATTERNS:
        if pattern.search(file_path):
            return HighRiskMatch(
                file_path=file_path,
                pattern_name=pattern.pattern,
                severity="critical",
            )
    return None


# ─────────────────────────────────────────────
# 审批通知（飞书/钉钉 Webhook）
# ─────────────────────────────────────────────


async def send_approval_notification(
    pr_id: str,
    vcs_provider: str,
    high_risk_matches: List[HighRiskMatch],
    approval_url: str,
    webhook_url: str | None = None,
) -> bool:
    """发送高危文件审批通知到飞书/钉钉（submit 前）。"""
    import os
    import aiohttp

    url = webhook_url or os.getenv("APPROVAL_WEBHOOK_URL", "")
    if not url:
        logger.warning("未配置审批 Webhook URL (APPROVAL_WEBHOOK_URL)，跳过通知")
        return False

    risk_files = "\n".join(f"- `{m.file_path}`" for m in high_risk_matches)

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "⚠️ AutoReviewer 高危操作审批"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**{vcs_provider.upper()} !{pr_id}** 包含高危操作，需要审批：\n\n"
                            f"{risk_files}\n\n"
                            f"请确认是否允许提交。"
                        ),
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✅ 允许提交"},
                            "type": "primary",
                            "url": approval_url,
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                            "type": "danger",
                            "url": approval_url + "?action=reject",
                        },
                    ],
                },
            ],
        },
    }

    return await _send_card(url, card, pr_id)


async def send_fix_approval_notification(
    pr_id: str,
    vcs_provider: str,
    review_issues: list,
    approval_url: str,
    webhook_url: str | None = None,
) -> bool:
    """发送修复审批通知到飞书/钉钉（fixer 前）。

    Reviewer 完成审查后，将问题清单发送给 Tech Lead 审批，
    确认后 Fixer 才开始执行修复。
    """
    import os
    import aiohttp

    url = webhook_url or os.getenv("APPROVAL_WEBHOOK_URL", "")
    if not url:
        logger.warning("未配置审批 Webhook URL (APPROVAL_WEBHOOK_URL)，跳过通知")
        return False

    sev_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}

    issue_lines = []
    for i, issue in enumerate(review_issues, 1):
        sev = issue.get("severity", "info")
        fp = issue.get("file_path", "")
        ln = issue.get("line_number", "")
        desc = issue.get("description", "")
        icon = sev_icon.get(sev, "•")
        issue_lines.append(f"{i}. {icon} **[{sev.upper()}]** `{fp}:{ln}` — {desc}")

    issues_text = "\n".join(issue_lines) if issue_lines else "（无问题）"
    total = len(review_issues)
    critical_count = sum(1 for i in review_issues if i.get("severity") == "critical")
    warning_count = sum(1 for i in review_issues if i.get("severity") == "warning")

    summary = f"共 {total} 个问题"
    if critical_count:
        summary += f"，其中 🔴 critical {critical_count} 个"
    if warning_count:
        summary += f"，🟡 warning {warning_count} 个"

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🔧 AutoReviewer 修复审批"},
                "template": "orange",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**{vcs_provider.upper()} !{pr_id}** 审查完成，{summary}：\n\n"
                            f"{issues_text}\n\n"
                            f"请确认是否允许自动修复这些问题。"
                        ),
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✅ 允许修复"},
                            "type": "primary",
                            "url": approval_url,
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "❌ 跳过修复"},
                            "type": "danger",
                            "url": approval_url + "?action=reject",
                        },
                    ],
                },
            ],
        },
    }

    return await _send_card(url, card, pr_id)


async def _send_card(url: str, card: dict, pr_id: str) -> bool:
    """发送卡片消息到飞书/钉钉 Webhook。"""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=card) as resp:
                if resp.status == 200:
                    logger.info("审批通知已发送: pr=%s", pr_id)
                    return True
                else:
                    text = await resp.text()
                    logger.error("审批通知发送失败: status=%d, body=%s", resp.status, text)
                    return False
    except Exception as e:
        logger.error("审批通知发送异常: %s", e)
        return False
