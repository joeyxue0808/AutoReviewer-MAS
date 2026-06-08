"""Human-in-the-Loop 高危操作检测与审批流 - Phase 4。

当识别到包含高危操作时，Graph 持久化状态并挂起 (Suspend)。
系统发送审批通知至飞书/钉钉/企业微信，Tech Lead 审批后唤醒图。

支持的通知渠道：
- 飞书 (Feishu): 交互式卡片消息（含按钮）
- 钉钉 (DingTalk): 交互式卡片消息（含按钮）
- 企业微信 (WeCom): Markdown 消息（含审批链接）

环境变量：
- APPROVAL_WEBHOOK_URL: 飞书/钉钉 Webhook URL
- WECOM_WEBHOOK_URL: 企业微信 Webhook URL
- 可同时配置，通知会发送到所有已配置的渠道
"""

import logging
import os
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
    """检测审查结果中的高危操作。"""
    matches: List[HighRiskMatch] = []

    for issue in review_issues:
        fp = issue.get("file_path", "") if isinstance(issue, dict) else getattr(issue, "file_path", "")
        severity = issue.get("severity", "") if isinstance(issue, dict) else getattr(issue, "severity", "")
        if fp and severity == "critical":
            risk = _match_risk_pattern(fp)
            if risk:
                matches.append(risk)

    for block in search_replace_blocks:
        fp = block.get("file_path", "")
        if fp:
            risk = _match_risk_pattern(fp)
            if risk:
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
# 通知渠道识别与分发
# ─────────────────────────────────────────────


def _detect_channel(url: str) -> str:
    """根据 URL 自动识别通知渠道。

    Returns:
        "feishu" | "dingtalk" | "wecom" | "unknown"
    """
    if "qyapi.weixin.qq.com" in url:
        return "wecom"
    elif "open.feishu.cn" in url or "open.larksuite.com" in url:
        return "feishu"
    elif "oapi.dingtalk.com" in url:
        return "dingtalk"
    else:
        # 尝试按飞书格式发送（兼容自建飞书）
        return "feishu"


def _get_all_webhook_urls() -> list[tuple[str, str]]:
    """获取所有已配置的 webhook URL。

    Returns:
        [(url, channel_name), ...]
    """
    urls = []

    # 飞书/钉钉
    main_url = os.getenv("APPROVAL_WEBHOOK_URL", "")
    if main_url:
        channel = _detect_channel(main_url)
        urls.append((main_url, channel))

    # 企业微信（独立环境变量）
    wecom_url = os.getenv("WECOM_WEBHOOK_URL", "")
    if wecom_url and wecom_url != main_url:
        urls.append((wecom_url, "wecom"))

    return urls


async def _send_to_all_channels(
    pr_id: str,
    feishu_card: dict | None,
    wecom_markdown: str | None,
    webhook_url: str | None = None,
) -> bool:
    """向所有已配置渠道发送通知。

    Args:
        pr_id: PR ID（用于日志）
        feishu_card: 飞书/钉钉卡片消息体
        wecom_markdown: 企业微信 Markdown 内容
        webhook_url: 指定单个 URL（覆盖自动发现）

    Returns:
        至少一个渠道发送成功
    """
    import aiohttp

    if webhook_url:
        # 指定 URL 模式
        channel = _detect_channel(webhook_url)
        if channel == "wecom":
            body = {"msgtype": "markdown", "markdown": {"content": wecom_markdown or ""}}
        else:
            body = feishu_card or {}
        return await _post_webhook(webhook_url, body, pr_id)

    # 自动发现所有渠道
    urls = _get_all_webhook_urls()
    if not urls:
        logger.warning("未配置任何审批 Webhook URL，跳过通知")
        return False

    any_success = False
    async with aiohttp.ClientSession() as session:
        for url, channel in urls:
            if channel == "wecom":
                body = {"msgtype": "markdown", "markdown": {"content": wecom_markdown or ""}}
            else:
                body = feishu_card or {}

            try:
                async with session.post(url, json=body) as resp:
                    if resp.status == 200:
                        logger.info("审批通知已发送 [%s]: pr=%s", channel, pr_id)
                        any_success = True
                    else:
                        text = await resp.text()
                        logger.error("审批通知发送失败 [%s]: status=%d, body=%s", channel, resp.status, text)
            except Exception as e:
                logger.error("审批通知发送异常 [%s]: %s", channel, e)

    return any_success


async def _post_webhook(url: str, body: dict, pr_id: str) -> bool:
    """发送单个 webhook 请求。"""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body) as resp:
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


# ─────────────────────────────────────────────
# 高危操作审批通知（submit 前）
# ─────────────────────────────────────────────


async def send_approval_notification(
    pr_id: str,
    vcs_provider: str,
    high_risk_matches: List[HighRiskMatch],
    approval_url: str,
    webhook_url: str | None = None,
) -> bool:
    """发送高危文件审批通知（submit 前）。"""
    risk_files_feishu = "\n".join(f"- `{m.file_path}`" for m in high_risk_matches)
    risk_files_wecom = "\n".join(f"> `{m.file_path}`" for m in high_risk_matches)

    # 飞书/钉钉卡片
    feishu_card = {
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
                            f"{risk_files_feishu}\n\n"
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

    # 企业微信 Markdown
    reject_url = approval_url + "?action=reject"
    wecom_md = (
        f"## ⚠️ AutoReviewer 高危操作审批\n\n"
        f"**{vcs_provider.upper()} !{pr_id}** 包含高危操作，需要审批：\n\n"
        f"{risk_files_wecom}\n\n"
        f"[✅ 允许提交]({approval_url})\n\n"
        f"[❌ 拒绝]({reject_url})"
    )

    return await _send_to_all_channels(pr_id, feishu_card, wecom_md, webhook_url)


# ─────────────────────────────────────────────
# 修复审批通知（fixer 前）
# ─────────────────────────────────────────────


async def send_fix_approval_notification(
    pr_id: str,
    vcs_provider: str,
    review_issues: list,
    approval_url: str,
    webhook_url: str | None = None,
) -> bool:
    """发送修复审批通知（fixer 前）。"""
    sev_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}

    # 飞书/钉钉格式
    issue_lines_feishu = []
    # 企业微信格式（Markdown 兼容性不同）
    issue_lines_wecom = []

    for i, issue in enumerate(review_issues, 1):
        sev = issue.get("severity", "info")
        fp = issue.get("file_path", "")
        ln = issue.get("line_number", "")
        desc = issue.get("description", "")
        icon = sev_icon.get(sev, "•")
        issue_lines_feishu.append(f"{i}. {icon} **[{sev.upper()}]** `{fp}:{ln}` — {desc}")
        issue_lines_wecom.append(f"{i}. {icon} `[{sev.upper()}]` `{fp}:{ln}` - {desc}")

    issues_text_feishu = "\n".join(issue_lines_feishu) if issue_lines_feishu else "（无问题）"
    issues_text_wecom = "\n".join(issue_lines_wecom) if issue_lines_wecom else "（无问题）"

    total = len(review_issues)
    critical_count = sum(1 for i in review_issues if i.get("severity") == "critical")
    warning_count = sum(1 for i in review_issues if i.get("severity") == "warning")

    summary = f"共 {total} 个问题"
    if critical_count:
        summary += f"，其中 🔴 critical {critical_count} 个"
    if warning_count:
        summary += f"，🟡 warning {warning_count} 个"

    # 飞书/钉钉卡片
    feishu_card = {
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
                            f"{issues_text_feishu}\n\n"
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

    # 企业微信 Markdown
    reject_url = approval_url + "?action=reject"
    wecom_md = (
        f"## 🔧 AutoReviewer 修复审批\n\n"
        f"**{vcs_provider.upper()} !{pr_id}** 审查完成，{summary}：\n\n"
        f"{issues_text_wecom}\n\n"
        f"请确认是否允许自动修复。\n\n"
        f"[✅ 允许修复]({approval_url})\n\n"
        f"[❌ 跳过修复]({reject_url})"
    )

    return await _send_to_all_channels(pr_id, feishu_card, wecom_md, webhook_url)
