"""隐私闸门：敏感信息识别 + 记忆作用域判定。

为什么这个模块单独拎出来
------------------------
长期记忆插件最大的风险不是"记不住"，而是**记太牢、说错地方**。
一个真人化的人设往往有自己的隐私红线（比如"绝不透露真名、生日、学校、地址
给非专属的人或在群聊里提及"）。如果记忆无差别注入，插件会亲手把这条红线捅穿。

所以这里的规则是**默认拒绝**：
- 命中敏感特征（证件号、手机号、住址、学校、真名…）→ 永久限定为"只在私聊里、只对本人"。
- 群里学到的记忆 → 默认只在该群里可用，不跨群。
- 私聊学到的记忆 → 默认不在群里注入（可配置放开，但敏感的永远不放）。
"""

from __future__ import annotations

import re

from .model import SCOPE_SESSION, SCOPE_USER, is_group_session

# ---------------------------------------------------------------------------
# 敏感特征
# ---------------------------------------------------------------------------

#: 结构性敏感信息：(正则, 说明)
SENSITIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\d{17}[\dXx]", "疑似身份证号"),
    (r"(?<!\d)\d{15}(?!\d)", "疑似身份证号"),
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "疑似手机号"),
    (r"(?<!\d)\d{16,19}(?!\d)", "疑似银行卡号"),
    (r"(?<!\d)\d{6,}(?!\d)", "疑似长串数字（证件/学号/密码）"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "疑似邮箱"),
    (r"(?<!\d)\d{3,4}-\d{7,8}(?!\d)", "疑似座机号"),
)

#: 关键词型敏感信息
SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "真名",
    "全名",
    "身份证",
    "证件号",
    "住址",
    "家庭住址",
    "我家在",
    "我家住",
    "学校",
    "班级",
    "学号",
    "班主任",
    "密码",
    "银行卡",
    "手机号",
    "电话号码",
    "微信号",
    "qq号",
    "工牌",
    "社保",
)

#: 地址特征：必须同时命中"行政区划/道路"与"门牌"才算
_ADDRESS_AREA = re.compile(r"(省|市|区|县|镇|乡|村|路|街|道|巷|小区|花园|公寓|大厦|号楼|单元|门牌|室)")
_ADDRESS_NUMBER = re.compile(r"\d+\s*(号|栋|幢|单元|室|楼)")

#: 学校特征
_SCHOOL = re.compile(r"(小学|初中|高中|中学|大学|学院|附中|职高|技校|幼儿园)")

#: 直接的性/财务等高度私密话题（仅作弱提示，不单独判敏感）
_SOFT_PRIVATE = re.compile(r"(工资|月薪|存款|欠款|贷款|病历|抑郁症|确诊)")

_PATTERN_RES = tuple((re.compile(pattern, re.IGNORECASE), label) for pattern, label in SENSITIVE_PATTERNS)


def detect_sensitive(text: str) -> list[str]:
    """返回命中的敏感原因列表；为空表示没检测到。"""
    if not text:
        return []
    reasons: list[str] = []
    for regex, label in _PATTERN_RES:
        if regex.search(text):
            reasons.append(label)

    lowered = text.lower()
    for keyword in SENSITIVE_KEYWORDS:
        if keyword in lowered:
            reasons.append(f"提到「{keyword}」")
            break

    if _ADDRESS_AREA.search(text) and _ADDRESS_NUMBER.search(text):
        reasons.append("疑似具体住址")
    elif _ADDRESS_AREA.search(text) and re.search(r"(住在|家住|搬到|老家)", text):
        reasons.append("疑似住址")

    if _SCHOOL.search(text):
        reasons.append("提到学校")

    if _SOFT_PRIVATE.search(text):
        reasons.append("私密话题")

    # 去重保序
    return list(dict.fromkeys(reasons))


def is_sensitive(text: str) -> bool:
    return bool(detect_sensitive(text))


# ---------------------------------------------------------------------------
# 作用域判定
# ---------------------------------------------------------------------------


def decide_scope(
    *,
    session_id: str,
    sensitive: bool,
    group_scope: str = SCOPE_SESSION,
    private_scope: str = SCOPE_USER,
    allow_global: bool = False,
) -> str:
    """决定一条新记忆的作用域。

    - 敏感的：一律 `user` —— 只对本人可见，且永远不会进群聊。
    - 群里学到的：默认 `session` —— 只在这个群里可用，不跨群传播。
    - 私聊学到的：默认 `user` —— 只在该用户出现时可用。
    """
    if sensitive:
        return SCOPE_USER
    if is_group_session(session_id):
        return group_scope if group_scope in {"session", "user", "global"} else SCOPE_SESSION
    return private_scope if private_scope in {"session", "user", "global"} else SCOPE_USER


def allows(
    memory,
    *,
    session_id: str,
    sender_id: str,
    group: bool | None = None,
    allow_private_in_group: bool = False,
) -> bool:
    """这条记忆能不能出现在当前这个会话里。"""
    if not getattr(memory, "content", ""):
        return False

    group = is_group_session(session_id) if group is None else group
    sender_id = str(sender_id or "")
    owner = str(getattr(memory, "owner_id", "") or "")

    # 敏感信息：只有"本人 + 私聊"这一种组合能看见
    if getattr(memory, "sensitive", False):
        return (not group) and bool(owner) and owner == sender_id

    scope = getattr(memory, "scope", SCOPE_SESSION)
    if scope == "global":
        return True
    if scope == SCOPE_SESSION:
        return str(getattr(memory, "session_id", "") or "") == session_id
    if scope == SCOPE_USER:
        if not owner or owner != sender_id:
            return False
        if group and not allow_private_in_group:
            return False
        return True
    return False


def describe_blocked(memory) -> str:
    """给面板用的一句话说明。"""
    if getattr(memory, "sensitive", False):
        return "敏感信息 · 仅私聊"
    scope = getattr(memory, "scope", "")
    if scope == "global":
        return "通用"
    if scope == SCOPE_USER:
        return "仅该用户私聊"
    return "仅本会话"

