"""AI 心智内核：情绪 + 长期记忆 + 记忆图谱。

两个子包各自独立、各自被测试覆盖：

- `emotion/` —— PAD 三维情绪模型、衰减动力学、情绪触发规则、危机拦截
  （没有人格、没有表达基调：说话方式一律由机器人自己的提示词决定）
- `memory/`  —— 长期记忆抽取、检索、隐私闸门

本层补充合并后才有的东西：

- `config`   —— 把一份配置切成两块喂给它们
- `identity` —— 「平台:uid」写法解析（拒绝名单用）
- `samples`  —— 情绪曲线的采样点存储 + 词表命中记录
- `panel`    —— WebUI 面板要用的数据组装
- `graph`    —— 记忆图谱（节点 / 边 / 簇）

注入给模型的三块提示词**只有代码默认值、没有覆盖层**：
`emotion.rules` / `emotion.crisis` 在 `emotion/prompt.py`，
`memory.rules` 在 `memory/prompt.py`（旧的 `prompts.json` 已废弃并在启动时删除）。
"""

from .config import (
    MindSettings,
    PanelSettings,
    PrivacySettings,
    cfg_get,
)
from .identity import identity_matches, parse_identity
from .emotion.engine import Settings  # 情绪设置（合并后仍叫 Settings，便于复用）
from .panel import (
    build_curve,
    describe_session,
    emotion_payload,
    is_group,
    memory_payload,
    memory_to_dict,
    session_entries,
)
from . import graph  # noqa: F401
from .samples import (
    LexiconHit,
    LexiconStore,
    Sample,
    SampleStore,
    should_sample,
)

# 情绪内核
from .emotion import (  # noqa: F401
    CRISIS_BLOCK,
    PROTOTYPES,
    get_preset,
    EMOTION_TAG_RE,
    PAD,
    PRESETS,
    RULES_BLOCK as EMOTION_RULES_BLOCK,
    RULES_MARKER as EMOTION_RULES_MARKER,
    EmotionEngine,
    SessionMood,
    Snapshot,
    classify,
    detect_crisis,
    heuristic_self_emotion,
    level_of_intensity,
    mood_word_pad,
    parse_custom_rule,
    resolve_mood_word,
    resolve_prototype,
    session_intensity,
)
from .emotion.display import history_panel, mood_panel  # noqa: F401
from .emotion.prompt import build_injection as build_emotion_injection  # noqa: F401

# 记忆内核
from .memory import (  # noqa: F401
    ExtractedItem,
    KINDS,
    KIND_LABELS,
    Memory,
    MemoryExtractor,
    MemoryStore,
    RetrievalResult,
    Retriever,
    RULES_BLOCK as MEMORY_RULES_BLOCK,
    RULES_MARKER as MEMORY_RULES_MARKER,
    Turn,
    allows,
    decide_scope,
    derive_keywords,
    detect_sensitive,
    format_existing_for_prompt,
    parse_extraction,
    render_memory_block,
)
from .identity import identity_matches, parse_identity  # noqa: F401
from .memory.display import (  # noqa: F401
    clear_panel,
    forget_panel,
    help_panel as memory_help_panel,
    main_panel as memory_main_panel,
    remember_panel,
    search_panel,
)
from .memory.privacy import is_group_session  # noqa: F401

__all__ = [
    "EMOTION_RULES_BLOCK",
    "PROTOTYPES",
    "get_preset",
    "EMOTION_RULES_MARKER",
    "MEMORY_RULES_BLOCK",
    "MEMORY_RULES_MARKER",
    "PrivacySettings",
    "KINDS",
    "KIND_LABELS",
    "MindSettings",
    "Settings",
    "cfg_get",
    "PanelSettings",
    "Sample",
    "SampleStore",
    "build_curve",
    "build_emotion_injection",
    "describe_session",
    "emotion_payload",
    "level_of_intensity",
    "MOOD_WORDS",
    "mood_word_pad",
    "resolve_mood_word",
    "memory_payload",
    "memory_to_dict",
    "session_entries",
    "session_intensity",
    "should_sample",
    "LexiconHit",
    "LexiconStore",
    "derive_keywords",
    "identity_matches",
    "parse_identity",
    "graph",
]
