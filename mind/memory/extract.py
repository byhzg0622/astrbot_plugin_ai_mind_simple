"""记忆抽取：把聊天记录交给 LLM，换回结构化的长期记忆。

为什么是"异步攒批"而不是"每句都抽"
----------------------------------
每轮调用一次 LLM 会让 token 成本翻倍，而且大部分轮次根本没什么可记的。
所以这里攒够 N 轮、或者静默一段时间，才跑一次抽取，
并且明确要求模型"宁缺毋滥，没有值得记的就返回空数组"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .model import (
    KINDS,
    KIND_FACT,
    Memory,
    jaccard,
    normalize,
    now_ts,
    tokenize,
    truncate,
)
from .privacy import decide_scope, detect_sensitive

# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = """你是一个长期记忆整理器。你的唯一任务是从聊天记录里挑出**值得长期记住**的信息，并且只输出 JSON。你不需要回复聊天内容，不要寒暄，不要解释，不要输出 JSON 以外的任何字符。"""

EXTRACT_TEMPLATE = """下面是一段真实聊天记录（{user_label} = 正在和你聊天的人，{bot_label} = 你自己）。

请挑出**值得长期记住**的信息。

【判断标准】
值得记：关于{user_label}的稳定事实（名字、生日、工作、身体、家人）、喜好与厌恶、正在经历的事（考试、生病、出差、搬家）、他说过的计划与约定、你们之间的称呼与关系。
不值得记：寒暄、道谢、一次性的情绪、天气、你（{bot_label}）自己说过的话、纯粹的玩笑、以及任何你只是在复述他刚说的话的内容。

【输出格式】只输出 JSON：
{{"memories": [{{"content": "...", "keywords": ["..."], "kind": "fact", "pinned": true, "importance": 0.8, "subject": "..."}}]}}

字段说明：
- content：一句第三人称的陈述句，例如「他喜欢喝冰美式」「他下周三要期末考试」。不要写"用户说"。
- keywords：2~5 个词，将来他再提到相关话题时能靠这些词想起来。
- kind：fact（稳定事实）/ preference（喜好）/ event（经历过的事）/ promise（约定与计划）/ relation（关系与称呼）/ other
- pinned：true 表示这条几乎永远有用（名字、生日、称呼、长期爱好、重要约定）；一次性的经历填 false。
- importance：0~1，越重要越高。
- subject：这条记忆的主体，通常是"{user_label}"。

【硬性要求】
1. 宁缺毋滥。没有值得记的就输出 {{"memories": []}}。
2. 最多输出 {max_items} 条。
3. 不要记录任何证件号、手机号、具体住址、学校名、密码。如果聊天里出现了， 
请只记成「他提到过一些私人信息」这种模糊说法，或者干脆不记。
4. 不要重复下面"已经记住的内容"：
{existing}
{extra}

【聊天记录】
{transcript}
"""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json_object(raw: str) -> Any | None:
    """从模型输出里尽最大努力抠出一个 JSON 对象/数组。"""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()

    for candidate in (text,):
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (ValueError, TypeError):
                continue

    # 有些模型会一行一条 JSON
    lines = [line.strip().rstrip(",") for line in text.splitlines() if line.strip().startswith("{")]
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    if items:
        return {"memories": items}
    return None


@dataclass
class ExtractedItem:
    content: str
    keywords: tuple[str, ...] = ()
    kind: str = KIND_FACT
    pinned: bool = False
    importance: float = 0.5
    subject: str = ""


def _clean_keywords(raw: Any, limit: int = 6) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = re.split(r"[,，、;；|]", raw)
    if not isinstance(raw, (list, tuple)):
        return ()
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return tuple(result)


def parse_extraction(raw: str, *, max_items: int = 6) -> list[ExtractedItem]:
    """把模型输出解析成记忆条目；任何异常都退化成空列表。"""
    payload = extract_json_object(raw)
    if payload is None:
        return []
    if isinstance(payload, dict):
        items = payload.get("memories") or payload.get("items") or payload.get("data") or []
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []

    results: list[ExtractedItem] = []
    for raw_item in items:
        if isinstance(raw_item, str):
            raw_item = {"content": raw_item}
        if not isinstance(raw_item, dict):
            continue
        content = normalize(raw_item.get("content") or raw_item.get("text") or "")
        content = re.sub(r"^(他|她|对方|用户|你)\s*[:：]\s*", "", content).strip()
        content = content.strip("。. ")
        if len(content) < 3 or len(content) > 200:
            continue

        kind = str(raw_item.get("kind") or KIND_FACT).strip().lower()
        if kind not in KINDS:
            kind = KIND_FACT

        try:
            importance = float(raw_item.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        importance = max(0.0, min(1.0, importance))

        pinned_raw = raw_item.get("pinned", False)
        if isinstance(pinned_raw, str):
            pinned = pinned_raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            pinned = bool(pinned_raw)

        results.append(
            ExtractedItem(
                content=content,
                keywords=_clean_keywords(raw_item.get("keywords")),
                kind=kind,
                pinned=pinned,
                importance=importance,
                subject=str(raw_item.get("subject") or "").strip()[:20],
            )
        )
        if len(results) >= max_items:
            break
    return results


# ---------------------------------------------------------------------------
# 抽取器
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    """一轮对话。"""

    user: str
    assistant: str = ""
    at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"user": self.user, "assistant": self.assistant, "at": self.at}

    @staticmethod
    def from_dict(data: Any) -> "Turn | None":
        if not isinstance(data, dict):
            return None
        user = str(data.get("user") or "").strip()
        if not user:
            return None
        try:
            at = float(data.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        return Turn(user=user, assistant=str(data.get("assistant") or "").strip(), at=at)


class MemoryExtractor:
    """负责"问模型要记忆"和"把记忆安全地写进仓库"两件事。"""

    def __init__(self, settings: Any, logger: Any = None) -> None:
        self.settings = settings
        self.logger = logger
        self.last_error = ""

    # -- 提示词 -------------------------------------------------------------
    def build_prompt(self, turns: Sequence[Turn], existing: Sequence[str]) -> str:
        lines: list[str] = []
        for turn in turns:
            lines.append(f"{self.settings.user_label}：{turn.user}")
            if self.settings.include_assistant and turn.assistant:
                lines.append(f"{self.settings.bot_label}：{turn.assistant}")
        transcript = "\n".join(lines)
        existing_text = "\n".join(f"- {line}" for line in existing) if existing else "（暂无）"
        extra = self.settings.extra_instructions
        extra_block = f"\n5. 额外要求：{extra}" if extra else ""
        return EXTRACT_TEMPLATE.format(
            user_label=self.settings.user_label,
            bot_label=self.settings.bot_label,
            max_items=self.settings.max_items_per_batch,
            existing=existing_text,
            extra=extra_block,
            transcript=transcript,
        )

    # -- 调用模型 -----------------------------------------------------------
    async def request(self, provider: Any, prompt: str) -> str:
        """向 Provider 要一次抽取结果；失败返回空串。"""
        self.last_error = ""
        if provider is None:
            self.last_error = "没有可用的模型 Provider"
            return ""
        try:
            response = await provider.text_chat(prompt=prompt, system_prompt=EXTRACT_SYSTEM)
        except Exception as exc:  # noqa: BLE001 - 抽取失败绝不能影响聊天
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.logger is not None:
                self.logger.warning(f"[ai_memory] 记忆抽取请求失败：{self.last_error}")
            return ""
        text = getattr(response, "completion_text", "") or ""
        return text if isinstance(text, str) else ""

    # -- 入库 ---------------------------------------------------------------
    def integrate(
        self,
        store: Any,
        items: Sequence[ExtractedItem],
        *,
        session_id: str,
        sender_id: str,
        now: float | None = None,
    ) -> tuple[int, int, int]:
        """把抽取结果写进仓库，返回 (新增, 合并, 跳过)。

        - 敏感内容会被自动降级为"仅本人私聊可见"
        - 与已有记忆高度重合的会被合并，而不是新增一条
        """
        now = now_ts() if now is None else now
        added = merged = skipped = 0
        sender_id = str(sender_id or "")

        for item in items:
            content = item.content.strip()
            if not content:
                skipped += 1
                continue

            reasons = detect_sensitive(content) if self.settings.scan_sensitive else []
            sensitive = bool(reasons)
            scope = decide_scope(
                session_id=session_id,
                sensitive=sensitive,
                group_scope=self.settings.group_scope,
                private_scope=self.settings.private_scope,
            )
            # 通用记忆不挂人也不挂会话；其余一律记下「谁告诉我的」
            # 与「在哪个会话里说的」—— 面板的人员筛选、来源标签、
            # 图谱的会话过滤全靠这两个字段。
            owner_id = sender_id if scope != "global" else ""
            session_key = session_id if scope != "global" else ""

            # 同类内容合并：先看现有记忆里有没有同一件事
            existing = store.same_scope_snapshot(
                scope=scope,
                session_id=session_key,
                owner_id=owner_id,
                limit=500,
            )
            tokens = tokenize(content)
            best_id, best_score = 0, 0.0
            for memory_id, old_content in existing:
                if len(old_content) > len(content) * 3 or len(content) > len(old_content) * 3:
                    continue
                similarity = jaccard(tokens, tokenize(old_content))
                if similarity > best_score:
                    best_id, best_score = memory_id, similarity

            if best_id and best_score >= self.settings.dedup_threshold:
                store.merge(
                    best_id,
                    content=content,
                    keywords=item.keywords,
                    importance=item.importance,
                    now=now,
                )
                merged += 1
                continue

            memory = Memory(
                content=content,
                keywords=item.keywords,
                kind=item.kind,
                subject=item.subject,
                scope=scope,
                session_id=session_key,
                owner_id=owner_id,
                importance=item.importance,
                pinned=item.pinned,
                sensitive=sensitive,
                sensitive_reasons=tuple(reasons),
                source="auto",
                created_at=now,
                updated_at=now,
            )
            if store.add(memory):
                added += 1
            else:
                skipped += 1

        return added, merged, skipped


def format_existing_for_prompt(memories: Iterable[Memory], limit: int = 40) -> list[str]:
    """把已有记忆压成几行，喂给抽取提示词做去重参考。"""
    lines: list[str] = []
    for memory in memories:
        if not memory.content:
            continue
        lines.append(truncate(memory.content, 60))
        if len(lines) >= limit:
            break
    return lines

