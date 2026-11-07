"""把插件配置翻译成引擎能直接用的数值。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .model import (
    SCOPE_SESSION,
    SCOPE_USER,
    ScoreWeights,
)


def cfg_get(config: Mapping[str, Any] | None, path: str, default: Any = None) -> Any:
    """从（可能嵌套的）插件配置里安全地取一个值。"""
    current: Any = config
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return default
            current = current[part]
        else:
            return default
    return default if current is None else current


def _f(value: Any, default: float, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if result != result:  # NaN
        result = default
    return max(low, min(high, result))


def _i(value: Any, default: int, low: int, high: int) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = default
    return max(low, min(high, result))


def _b(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    if value is None:
        return default
    return bool(value)


def _s(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


@dataclass
class Settings:
    """记忆插件的全部可调参数。"""

    enabled: bool = True

    # -- 采集 ---------------------------------------------------------------
    batch_turns: int = 6            # 攒够几轮就抽取一次
    idle_flush_seconds: float = 180.0  # 或者静默这么久就抽取
    max_turns_kept: int = 40        # 单个会话最多缓存多少轮
    min_turn_chars: int = 4         # 太短的消息不值得进抽取队列

    # -- 抽取 ---------------------------------------------------------------
    provider_id: str = ""           # 留空 = 用当前会话的模型
    max_items_per_batch: int = 6
    include_assistant: bool = True  # 抽取时是否带上 AI 的回复（有上下文更准）
    extra_instructions: str = ""
    dedup_threshold: float = 0.6
    user_label: str = "对方"
    bot_label: str = "你"

    # -- 检索 ---------------------------------------------------------------
    pinned_limit: int = 12
    recent_limit: int = 5
    recent_max_age_days: float = 14.0
    relevant_limit: int = 4
    min_relevant_score: float = 0.22
    scan_limit: int = 1500
    recency_half_life_days: float = 30.0
    weight_lexical: float = 0.6
    #: 语义权重只有开了向量检索才生效；关着的时候会被强制归零，
    #: 所以这里默认给一个"打开就能用"的值，而不是 0。
    weight_semantic: float = 0.35
    weight_recency: float = 0.25
    weight_importance: float = 0.15
    max_inject_chars: int = 1400

    # -- 向量检索 -----------------------------------------------------------
    use_embedding: bool = False
    embedding_provider_id: str = ""

    # -- 隐私 ---------------------------------------------------------------
    scan_sensitive: bool = True
    private_memory_in_group: bool = False
    group_scope: str = SCOPE_SESSION
    private_scope: str = SCOPE_USER

    # -- 指令 ---------------------------------------------------------------
    # 「清空要不要管理员」由 commands.admin_only_mutations 统一决定
    # （配置页里那一个开关；以前这里还有个隐藏的 admin_only_clear，
    #   两个开关各管一半、关掉配置页那个也不生效 —— 已合并）

    # -- 高级 ---------------------------------------------------------------
    max_memories: int = 3000
    prune_importance_floor: float = 0.15

    weights: ScoreWeights = field(default_factory=ScoreWeights)

    @staticmethod
    def from_config(config: Mapping[str, Any] | None) -> "Settings":
        settings = Settings(
            enabled=_b(cfg_get(config, "enabled", True), True),
            batch_turns=_i(cfg_get(config, "capture.batch_turns", 6), 6, 1, 50),
            idle_flush_seconds=_f(cfg_get(config, "capture.idle_flush_seconds", 180), 180.0, 10.0, 7200.0),
            max_turns_kept=_i(cfg_get(config, "capture.max_turns_kept", 40), 40, 4, 400),
            min_turn_chars=_i(cfg_get(config, "capture.min_turn_chars", 4), 4, 0, 200),
            provider_id=_s(cfg_get(config, "extract.provider_id", "")),
            max_items_per_batch=_i(cfg_get(config, "extract.max_items_per_batch", 6), 6, 1, 30),
            include_assistant=_b(cfg_get(config, "extract.include_assistant", True), True),
            extra_instructions=_s(cfg_get(config, "extract.extra_instructions", "")),
            dedup_threshold=_f(cfg_get(config, "extract.dedup_threshold", 0.6), 0.6, 0.2, 0.98),
            user_label=_s(cfg_get(config, "extract.user_label", ""), "对方"),
            bot_label=_s(cfg_get(config, "extract.bot_label", ""), "你"),
            pinned_limit=_i(cfg_get(config, "retrieval.pinned_limit", 12), 12, 0, 60),
            recent_limit=_i(cfg_get(config, "retrieval.recent_limit", 5), 5, 0, 30),
            recent_max_age_days=_f(cfg_get(config, "retrieval.recent_max_age_days", 14), 14.0, 0.0, 3650.0),
            relevant_limit=_i(cfg_get(config, "retrieval.relevant_limit", 4), 4, 0, 30),
            min_relevant_score=_f(cfg_get(config, "retrieval.min_relevant_score", 0.22), 0.22, 0.0, 1.0),
            scan_limit=_i(cfg_get(config, "retrieval.scan_limit", 1500), 1500, 50, 20000),
            recency_half_life_days=_f(cfg_get(config, "retrieval.recency_half_life_days", 30), 30.0, 0.5, 3650.0),
            weight_lexical=_f(cfg_get(config, "retrieval.weight_lexical", 0.6), 0.6, 0.0, 1.0),
            weight_semantic=_f(cfg_get(config, "retrieval.weight_semantic", 0.35), 0.35, 0.0, 1.0),
            weight_recency=_f(cfg_get(config, "retrieval.weight_recency", 0.25), 0.25, 0.0, 1.0),
            weight_importance=_f(cfg_get(config, "retrieval.weight_importance", 0.15), 0.15, 0.0, 1.0),
            max_inject_chars=_i(cfg_get(config, "retrieval.max_inject_chars", 1400), 1400, 100, 8000),
            use_embedding=_b(cfg_get(config, "embedding.enabled", False), False),
            embedding_provider_id=_s(cfg_get(config, "embedding.provider_id", "")),
            scan_sensitive=_b(cfg_get(config, "privacy.scan_sensitive", True), True),
            private_memory_in_group=_b(cfg_get(config, "privacy.private_memory_in_group", False), False),
            group_scope=_s(cfg_get(config, "privacy.group_scope", SCOPE_SESSION), SCOPE_SESSION),
            private_scope=_s(cfg_get(config, "privacy.private_scope", SCOPE_USER), SCOPE_USER),
            max_memories=_i(cfg_get(config, "advanced.max_memories", 3000), 3000, 50, 200000),
            prune_importance_floor=_f(cfg_get(config, "advanced.prune_importance_floor", 0.15), 0.15, 0.0, 1.0),
        )
        settings.weights = ScoreWeights(
            settings.weight_lexical,
            settings.weight_semantic if settings.use_embedding else 0.0,
            settings.weight_recency,
            settings.weight_importance,
        ).renormalized()
        return settings

