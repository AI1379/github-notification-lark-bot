"""事件的中文文案、emoji 与飞书卡片配色。

放在独立模块里，避免 ``models`` 与 ``feishu.cards`` 相互 import。
"""

from __future__ import annotations

KIND_LABELS: dict[str, str] = {
    "push": "代码推送",
    "pull_request": "Pull Request",
    "pull_request_review": "PR 评审",
    "pull_request_review_comment": "PR 评审评论",
    "issues": "Issue",
    "issue_comment": "Issue 评论",
    "commit_comment": "提交评论",
    "release": "Release",
    "create": "分支/标签",
    "delete": "分支/标签",
    "fork": "Fork",
    "watch": "Star",
    "public": "仓库公开",
    "workflow_run": "Actions",
    "check_run": "检查",
    "status": "提交状态",
    "discussion": "Discussion",
    "discussion_comment": "Discussion 评论",
    "member": "成员变更",
    "gollum": "Wiki",
    "deployment": "部署",
    "unknown": "事件",
}

CONCLUSION_LABELS: dict[str, str] = {
    "success": "成功",
    "failure": "失败",
    "cancelled": "已取消",
    "timed_out": "超时",
    "skipped": "已跳过",
    "neutral": "中性",
    "action_required": "需人工处理",
    "stale": "已过期",
    "startup_failure": "启动失败",
}


def conclusion_label(value: str | None) -> str | None:
    if not value:
        return None
    return CONCLUSION_LABELS.get(value, value)


KIND_EMOJI: dict[str, str] = {
    "push": "📦",
    "pull_request": "🔀",
    "pull_request_review": "🔍",
    "pull_request_review_comment": "🔍",
    "issues": "🐛",
    "issue_comment": "💬",
    "commit_comment": "💬",
    "release": "🚀",
    "create": "🌱",
    "delete": "🗑️",
    "fork": "🍴",
    "watch": "⭐",
    "public": "🌍",
    "workflow_run": "⚙️",
    "check_run": "✅",
    "status": "🚦",
    "discussion": "🗣️",
    "discussion_comment": "🗣️",
    "member": "👥",
    "gollum": "📚",
    "deployment": "🚚",
    "unknown": "🔔",
}

# 飞书卡片 header template 取值：blue/wathet/turquoise/green/yellow/orange/red/
# carmine/violet/purple/indigo/grey
_TEMPLATE_BY_KIND: dict[str, str] = {
    "push": "blue",
    "pull_request": "turquoise",
    "pull_request_review": "turquoise",
    "pull_request_review_comment": "turquoise",
    "issues": "orange",
    "issue_comment": "yellow",
    "commit_comment": "yellow",
    "release": "violet",
    "create": "wathet",
    "delete": "grey",
    "fork": "purple",
    "watch": "yellow",
    "public": "indigo",
    "workflow_run": "indigo",
    "check_run": "indigo",
    "status": "grey",
    "discussion": "carmine",
    "discussion_comment": "carmine",
}

ACTION_LABELS: dict[str, str] = {
    "opened": "已开启",
    "closed": "已关闭",
    "reopened": "重新开启",
    "merged": "已合并",
    "synchronize": "有新提交",
    "ready_for_review": "可评审",
    "converted_to_draft": "转为草稿",
    "published": "已发布",
    "prereleased": "预发布",
    "released": "已发布",
    "created": "已创建",
    "edited": "已编辑",
    "deleted": "已删除",
    "labeled": "已加标签",
    "unlabeled": "已移除标签",
    "assigned": "已指派",
    "unassigned": "取消指派",
    "review_requested": "请求评审",
    "review_request_removed": "取消评审请求",
    "submitted": "已提交评审",
    "dismissed": "评审被驳回",
    "commented": "有新评论",
    "started": "已 Star",
    "completed": "已完成",
    "requested": "已触发",
    "in_progress": "进行中",
    "milestoned": "已加入里程碑",
    "locked": "已锁定",
    "unlocked": "已解锁",
    "pinned": "已置顶",
    "transferred": "已转移",
    "added": "已添加",
    "removed": "已移除",
}


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def kind_emoji(kind: str) -> str:
    return KIND_EMOJI.get(kind, "🔔")


def action_label(action: str | None, *, merged: bool = False, deleted: bool = False) -> str:
    if deleted:
        return "已删除"
    if merged:
        return "已合并"
    if not action:
        return "更新"
    return ACTION_LABELS.get(action, action)


def card_template(kind: str, action: str | None = None, *, merged: bool = False, success: bool | None = None) -> str:
    if merged:
        return "green"
    if kind == "workflow_run" and success is not None:
        return "green" if success else "red"
    if kind == "issues" and action == "closed":
        return "green"
    if kind == "pull_request" and action == "closed":
        return "grey"
    return _TEMPLATE_BY_KIND.get(kind, "grey")
