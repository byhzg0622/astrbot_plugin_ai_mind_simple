"""合并后的配置解析。

精简后配置只剩四块：

```
enabled            总开关
emotion.*          情绪：气质、动力学、关系、注入、安全
memory.*           记忆：采集、抽取、检索、向量、隐私
panel.*            面板：曲线采样与展示
privacy.*          隐私：拒绝名单、自助删除
commands.*         指令开关
advanced.*         共享的维护参数
```

两个子模块各自的 Settings 类保持原样不动 —— 它们已经被测试覆盖过，
这里只负责把配置切片喂给它们。

配图 / 分段 / 去 AI 味 / 护栏 / 表达示例 / 消息防抖 / 背景 都已经去掉，
对应的 Settings 类也一并删了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .emotion.engine import Settings as EmotionSettings
from .memory.config import Settings as MemorySettings
from .memory.config import cfg_get


def _f(value: Any, default: float, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if result != result:
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


def _str_list(value: Any) -> list[str]:
    """把配置里的列表/逗号串洗成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace(",", "\n").split("\n") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


@dataclass
class PanelSettings:
    """心智面板（WebUI 页面）的参数。"""

    #: 默认展示最近多少小时的曲线
    curve_hours: float = 72.0
    #: 每个会话最多保留多少个采样点（超出丢最旧的）
    curve_max_points: int = 3000
    #: 没有情绪事件时，也会按这个间隔打一个心跳点，让衰减过程画得出来
    heartbeat_seconds: float = 300.0
    #: 变化小于这个幅度就不采样，避免曲线被噪声填满
    sample_epsilon: float = 0.004
    #: 每个会话最多返回多少个事件点（防止图表卡死）
    max_points_returned: int = 1500

    @staticmethod
    def from_config(config: Mapping[str, Any] | None) -> "PanelSettings":
        return PanelSettings(
            curve_hours=_f(cfg_get(config, "panel.curve_hours", 72), 72.0, 1.0, 24 * 365.0),
            curve_max_points=_i(cfg_get(config, "panel.curve_max_points", 3000), 3000, 100, 200000),
            heartbeat_seconds=_f(cfg_get(config, "panel.heartbeat_seconds", 300), 300.0, 30.0, 86400.0),
            sample_epsilon=_f(cfg_get(config, "panel.sample_epsilon", 0.004), 0.004, 0.0, 0.2),
            max_points_returned=_i(cfg_get(config, "panel.max_points_returned", 1500), 1500, 50, 20000),
        )


@dataclass
class PrivacySettings:
    """隐私与合规：谁能被记住、谁能把数据删掉。"""

    #: 拒绝名单：这些人既不采集也不注入（支持 平台:uid 写法）
    denied_users: list[str] = field(default_factory=list)
    #: 允许本人通过指令删掉自己的全部数据
    allow_self_delete: bool = True
    #: 删自己的时候，要不要连"在同一个会话里学到的别的东西"一起删（默认不删）
    self_delete_group_memories: bool = False
    #: 同意门：会话里没同意过就不采集
    consent_required: bool = False
    #: 允许管理员导出某个人的档案
    allow_admin_export: bool = True

    @staticmethod
    def from_config(config: Any, overrides: Any = None) -> "PrivacySettings":
        raw = dict(cfg_get(config, "privacy", {}) or {})
        if overrides:
            raw.update({k: v for k, v in overrides.items() if v is not None})
        return PrivacySettings(
            denied_users=_str_list(raw.get("denied_users")),
            allow_self_delete=_b(raw.get("allow_self_delete"), True),
            self_delete_group_memories=_b(raw.get("self_delete_group_memories"), False),
            consent_required=_b(raw.get("consent_required"), False),
            allow_admin_export=_b(raw.get("allow_admin_export"), True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "denied_users": list(self.denied_users),
            "allow_self_delete": bool(self.allow_self_delete),
            "self_delete_group_memories": bool(self.self_delete_group_memories),
            "consent_required": bool(self.consent_required),
            "allow_admin_export": bool(self.allow_admin_export),
        }


@dataclass
class MindSettings:
    """整个插件的配置总成。"""

    enabled: bool = True
    commands_enabled: bool = True
    admin_only_mutations: bool = True
    debug_log: bool = False
    #: 省 token：把每轮都变的内容贴到请求最后，让前缀缓存能命中
    cache_friendly: bool = True
    maintenance_interval: float = 20.0

    emotion: EmotionSettings = None  # type: ignore[assignment]
    memory: MemorySettings = None    # type: ignore[assignment]
    panel: PanelSettings = None      # type: ignore[assignment]
    privacy: PrivacySettings = None  # type: ignore[assignment]

    @staticmethod
    def from_config(
        config: Mapping[str, Any] | None,
        privacy: Mapping[str, Any] | None = None,
    ) -> "MindSettings":
        emotion_raw = dict(cfg_get(config, "emotion", {}) or {})
        memory_raw = dict(cfg_get(config, "memory", {}) or {})
        # 子配置里没有 enabled 时默认开启，总开关在顶层
        emotion_raw.setdefault("enabled", True)
        memory_raw.setdefault("enabled", True)

        return MindSettings(
            enabled=_b(cfg_get(config, "enabled", True), True),
            commands_enabled=_b(cfg_get(config, "commands.enabled", True), True),
            admin_only_mutations=_b(cfg_get(config, "commands.admin_only_mutations", True), True),
            debug_log=_b(cfg_get(config, "advanced.debug_log", False), False),
            cache_friendly=_b(cfg_get(config, "advanced.cache_friendly", True), True),
            maintenance_interval=_f(
                cfg_get(config, "advanced.maintenance_interval", 20), 20.0, 5.0, 600.0
            ),
            emotion=EmotionSettings.from_config(emotion_raw),
            memory=MemorySettings.from_config(memory_raw),
            panel=PanelSettings.from_config(config),
            privacy=PrivacySettings.from_config(config, privacy),
        )


__all__ = [
    "MindSettings",
    "PanelSettings",
    "PrivacySettings",
    "cfg_get",
]
