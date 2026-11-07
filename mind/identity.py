"""身份判定：把「平台:用户 ID」这种写法认成人。

这个模块是从原来的 `mind/guard.py` 里拆出来的最小子集。

护栏（拦截注入、摘工具、清理回复）和「认人 / 专属用户」都已经去掉了，
但**「平台:uid」这种写法的解析要留着** —— 记忆的拒绝名单用它：

- `123456`           任何平台上的这个 ID
- `aiocqhttp:123456` 只认这个平台类型
- `<实例ID>:123456`  只认这个平台实例
- `aiocqhttp:*`      这个平台上所有人

只比裸 uid 的话，Telegram 的 12345 和 QQ 的 12345 会被当成同一个人。
"""

from __future__ import annotations

from typing import Any, Iterable


def parse_identity(text: Any) -> tuple[str, str]:
    """把一条身份配置拆成 (平台限定, 用户 ID)。

    - 123456              -> ("", "123456")          任何平台
    - aiocqhttp:123456    -> ("aiocqhttp", "123456") 只认这个平台类型
    - <实例ID>:123456     -> ("<实例ID>", "123456")  只认这个平台实例
    - aiocqhttp:*         -> ("aiocqhttp", "*")      这个平台上所有人
    """
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    if ":" not in raw:
        return "", raw
    head, _, tail = raw.rpartition(":")
    head, tail = head.strip(), tail.strip()
    if not head or not tail:
        return "", raw
    return head.lower(), tail


def identity_matches(
    entry: Any, uid: str, platform: str = "", platform_id: str = ""
) -> bool:
    """这条配置认不认这个人。用户 ID 原样比较（区分大小写）。"""
    uid = str(uid or "")
    if not uid:
        return False
    scope, target = parse_identity(entry)
    if not scope:
        return target == uid
    names = {str(platform or "").lower(), str(platform_id or "").lower()}
    names.discard("")
    if scope not in names:
        return False
    return target == "*" or target == uid


__all__ = ["identity_matches", "parse_identity"]
