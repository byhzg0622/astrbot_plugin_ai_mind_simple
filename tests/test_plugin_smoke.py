"""端到端冒烟测试：用"假的 astrbot 运行时"真实加载 main.py。

除了聊天侧的钩子，这一层还覆盖了合并后新增的**面板后端接口** ——
那些接口没有它们自己的单元测试，只能在这里跑通。

    python3 -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any

#: 一个保证写不进去的路径：Windows 用不存在的盘符，POSIX 用 /proc 下面的伪目录。
#: 原来直接写 /proc/... —— 在 Windows 上那不是只读，反而会被真的创建出来。
BAD_WRITE_DIR = (
    "Z:\\dsh-no-such-drive" if sys.platform.startswith("win")
    else "/proc/definitely/not/writable"
)

PLUGIN_DIR = Path(__file__).resolve().parent.parent
REGISTRY: dict[str, list[Any]] = {}

PRIVATE = "aiocqhttp:FriendMessage:1000000001"
GROUP = "aiocqhttp:GroupMessage:123456"
OWNER = "1000000001"
PLUGIN = "astrbot_plugin_ai_mind"


# ---------------------------------------------------------------------------
# 假的 AstrBot 运行时
# ---------------------------------------------------------------------------


def _decorator_factory(name: str):
    def factory(*_args: Any, **_kwargs: Any):
        def decorate(func):
            REGISTRY.setdefault(name, []).append(func)
            return func

        return decorate

    return factory


class Plain:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessageObj:
    def __init__(self, message_id: str) -> None:
        self.message_id = message_id


class FakeEvent:
    def __init__(self, text: str, umo: str = PRIVATE, uid: str = OWNER,
                 mid: str | None = None, admin: bool = False) -> None:
        self.message_str = text
        self.unified_msg_origin = umo
        self.message_obj = FakeMessageObj(mid or f"mid-{time.time_ns()}")
        self._uid = uid
        self._admin = admin
        self._result: Any = None
        self.stopped = False

    def get_sender_id(self) -> str:
        return self._uid

    def get_sender_name(self) -> str:
        return "他"

    def get_platform_name(self) -> str:
        return self.unified_msg_origin.split(":", 1)[0]

    def get_platform_id(self) -> str:
        return self.get_platform_name()

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str) -> tuple[str, str]:
        return ("plain", text)

    def set_result(self, result: Any) -> None:
        self._result = result

    def stop_event(self) -> None:
        self.stopped = True

    def is_stopped(self) -> bool:
        return self.stopped

    def chain_result(self, chain: Any) -> tuple[str, Any]:
        return ("chain", chain)

    def get_result(self) -> Any:
        return self._result


class Plain:
    def __init__(self, text: str = "") -> None:
        self.text = text


class Record:
    """模拟 astrbot.api.message_components.Record。

    真框架转出来的 Record 会把原文放在 .text 里，所以这里也带上。
    """

    def __init__(self, file: str = "", url: str = "", text: str = "") -> None:
        self.file = file
        self.url = url
        self.text = text


class Reply:
    def __init__(self, id: str = "1") -> None:
        self.id = id


class FakeTtsProvider:
    """框架内置 TTS 的桩：返回一个能认出来的假音频路径。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_audio(self, text: str) -> str:
        self.calls.append(text)
        return "tts://" + text


class FakeMessageChain:
    """模拟 astrbot.api.event.MessageChain：插件主动发送时要自己拼一条。"""

    def __init__(self, chain: list[Any] | None = None) -> None:
        self.chain = list(chain or [])


class FakeImage:
    def __init__(self, file: str = "") -> None:
        self.file = file

    @classmethod
    def fromFileSystem(cls, path: Any) -> "FakeImage":
        return cls(file=str(path))


class FakeUpload:
    """模拟 astrbot.api.web 的 PluginUploadFile。"""

    def __init__(self, filename: str = "a.png", content_type: str = "image/png",
                 payload: bytes = b"fake-png-bytes") -> None:
        self.filename = filename
        self.content_type = content_type
        self._payload = payload
        self.saved_to: str | None = None
        self.closed = False

    async def save(self, destination: Any, *, max_bytes: int | None = None) -> int:
        if max_bytes is not None and len(self._payload) > max_bytes:
            raise ValueError("文件太大")
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._payload)
        self.saved_to = str(path)
        return len(self._payload)

    async def close(self) -> None:
        self.closed = True


class LegacyFakeUpload(FakeUpload):
    """模拟 v4.28.1：save() 还没有 max_bytes 参数。"""

    async def save(self, destination: Any) -> int:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._payload)
        self.saved_to = str(path)
        return len(self._payload)


class FakeResult:
    def __init__(self, chain: list[Any]) -> None:
        self.chain = chain
        self.result_content_type = "llm"

    def is_llm_result(self) -> bool:
        return True


class FakeProviderRequest:
    def __init__(self) -> None:
        self.system_prompt = ""
        self.extra_user_content_parts: list[Any] = []
        #: 系统级受信任位（OpenAI 格式上下文），身份/情绪块现在放这里
        self.contexts: list[dict] = []
        self.func_tool: Any = None
        #: 防抖合并后会把拼好的话放这里
        self.prompt: str | None = None


class TextPart:
    def __init__(self, text: str = "", **_kwargs: Any) -> None:
        self.text = text
        self.temp = False

    def mark_as_temp(self) -> "TextPart":
        self.temp = True
        return self


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, message: Any, *_a: Any, **_k: Any) -> None:
        self.records.append((level, str(message)))

    def info(self, m: Any, *a: Any, **k: Any) -> None: self._log("info", m)
    def warning(self, m: Any, *a: Any, **k: Any) -> None: self._log("warning", m)
    def error(self, m: Any, *a: Any, **k: Any) -> None: self._log("error", m)
    def debug(self, m: Any, *a: Any, **k: Any) -> None: self._log("debug", m)


class FakeProvider:
    def __init__(self, response: str = "", responder: Any = None) -> None:
        self.response = response
        self.responder = responder
        self.prompts: list[str] = []

    async def text_chat(self, prompt: str | None = None, system_prompt: str | None = None, **_kw: Any):
        self.prompts.append(prompt or "")
        text = self.responder(prompt or "") if self.responder is not None else self.response
        return types.SimpleNamespace(completion_text=text)


class FakeWebRequest:
    """模拟 astrbot.api.web.request 代理背后的那个请求对象。"""

    def __init__(self, query: dict | None = None, body: dict | None = None,
                 files: dict | None = None) -> None:
        self._q = dict(query or {})
        self._b = dict(body or {})
        self._f = dict(files or {})

    async def body(self) -> bytes:
        return b""

    async def files(self) -> dict:
        return self._f

    @property
    def query(self) -> dict:
        return self._q

    async def json(self, default: Any = None) -> Any:
        return self._b


class RawBodyWebRequest(FakeWebRequest):
    """模拟 json() 不可用（签名不兼容）：只能自己啃原始 body。"""

    async def json(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("json() got an unexpected keyword argument")

    async def body(self) -> bytes:
        import json as _json

        return _json.dumps(self._b).encode("utf-8")


class FormBodyWebRequest(FakeWebRequest):
    """模拟请求体是表单编码而不是 JSON。"""

    async def json(self, *args: Any, **kwargs: Any) -> Any:
        raise ValueError("not json")

    async def body(self) -> bytes:
        from urllib.parse import urlencode

        return urlencode(self._b).encode("utf-8")


class FakeRequestProxy:
    current: FakeWebRequest | None = None

    def __getattr__(self, item: str) -> Any:
        if FakeRequestProxy.current is None:
            raise RuntimeError("no bound request")
        return getattr(FakeRequestProxy.current, item)


def _json_response(data: Any = None, *, status_code: int = 200, headers: Any = None) -> dict:
    return {"ok": True, "data": data, "status_code": status_code}


def _error_response(message: str, *, status_code: int = 400, data: Any = None, headers: Any = None) -> dict:
    return {"ok": False, "message": message, "status_code": status_code}


class FakeContext:
    def __init__(self, provider: Any = None, embedding: Any = None) -> None:
        self.provider = provider
        self.embedding = embedding
        self.web_apis: dict[str, tuple[Any, list[str], str]] = {}
        #: 插件通过 context.send_message 主动发出去的消息：(会话, 消息链)
        self.sent: list[tuple[str, Any]] = []
        #: 框架内置 TTS 的配置与 provider（测试里按需设置）
        self.astrbot_config: dict[str, Any] = {}
        self.tts_provider: Any = None

    async def get_using_provider_async(self, umo: str | None = None) -> Any:
        return self.provider

    def get_provider_by_id(self, provider_id: str | None = None) -> Any:
        return None

    def get_all_embedding_providers(self) -> list[Any]:
        return [self.embedding] if self.embedding else []

    def register_web_api(self, route: str, view_handler: Any, methods: list[str], desc: str) -> None:
        self.web_apis[route] = (view_handler, methods, desc)

    async def send_message(self, umo: str, chain: Any) -> None:
        self.sent.append((umo, chain))

    def get_config(self, umo: str | None = None) -> dict[str, Any]:
        return self.astrbot_config

    def get_using_tts_provider(self, umo: str | None = None) -> Any:
        return self.tts_provider


class FakeStar:
    def __init__(self, context: Any = None) -> None:
        self.context = context


class FakeStarTools:
    data_dir: Path = Path("./plugin_data")

    @classmethod
    def get_data_dir(cls, _name: str | None = None) -> Path:
        cls.data_dir.mkdir(parents=True, exist_ok=True)
        return cls.data_dir


def install_fake_astrbot(data_dir: Path) -> FakeLogger:
    log = FakeLogger()
    FakeStarTools.data_dir = data_dir
    FakeRequestProxy.current = None

    def module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    astrbot = module("astrbot")
    api = module("astrbot.api")
    astrbot.api = api
    api.logger = log
    api.AstrBotConfig = dict

    event_mod = module("astrbot.api.event")
    api.event = event_mod
    filter_mod = module("astrbot.api.event.filter")
    event_mod.filter = filter_mod
    for name in ("command", "command_group", "on_llm_request", "on_llm_response",
                 "on_decorating_result", "after_message_sent", "event_message_type",
                 "permission_type", "on_astrbot_loaded",
                 "on_using_llm_tool", "on_waiting_llm_request",
                 "llm_tool"):
        setattr(filter_mod, name, _decorator_factory(name))
    class FakeEventMessageType:
        ALL = "all"
        PRIVATE_MESSAGE = "private"
        GROUP_MESSAGE = "group"

    filter_mod.EventMessageType = FakeEventMessageType
    event_mod.AstrMessageEvent = FakeEvent
    event_mod.MessageChain = FakeMessageChain

    provider = module("astrbot.api.provider")
    api.provider = provider
    provider.ProviderRequest = FakeProviderRequest
    provider.LLMResponse = object

    web = module("astrbot.api.web")
    api.web = web
    web.json_response = _json_response
    web.error_response = _error_response
    web.request = FakeRequestProxy()

    star = module("astrbot.api.star")
    api.star = star
    star.Context = FakeContext
    star.Star = FakeStar
    star.StarTools = FakeStarTools
    star.register = lambda *_a, **_k: (lambda cls: cls)

    components = module("astrbot.api.message_components")
    api.message_components = components
    components.Plain = Plain
    components.Image = FakeImage
    components.Record = Record
    components.Reply = Reply

    core = module("astrbot.core")
    astrbot.core = core
    core_star = module("astrbot.core.star")
    core.star = core_star
    session_llm = module("astrbot.core.star.session_llm_manager")
    core_star.session_llm_manager = session_llm

    class FakeSessionServiceManager:
        @staticmethod
        async def should_process_tts_request(_event: Any) -> bool:
            return True

    session_llm.SessionServiceManager = FakeSessionServiceManager
    agent = module("astrbot.core.agent")
    core.agent = agent
    message = module("astrbot.core.agent.message")
    agent.message = message
    message.TextPart = TextPart

    return log


def load_plugin_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("ai_mind_main", PLUGIN_DIR / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ai_mind_main"] = module
    spec.loader.exec_module(module)
    return module


CONFIG: dict[str, Any] = {
    "enabled": True,
    "emotion": {
        "persona": {"preset": "tsundere", "bot_name": "小怡"},
        "idle": {"enabled": False},
    },
    "memory": {
        "capture": {"batch_turns": 1, "idle_flush_seconds": 1},
        "privacy": {"private_memory_in_group": False},
    },
    "panel": {"heartbeat_seconds": 1, "sample_epsilon": 0.0},
    "advanced": {"maintenance_interval": 3600},
}

EXTRACT_REPLY = (
    '{"memories":[{"content":"他喜欢喝冰美式","keywords":["冰美式","咖啡"],'
    '"kind":"preference","pinned":true,"importance":0.9}]}'
)


class PluginHarness:
    """公共夹具：临时数据目录 + 假 astrbot 运行时 + 几个常用小工具。

    故意**不**继承 TestCase —— 否则 unittest 会把它自己也当成一份测试收集，
    继承它的每个子类再各跑一遍，测试数会凭空翻几倍。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "plugin_data"
        REGISTRY.clear()
        self.log = install_fake_astrbot(self.data_dir)
        self.module = load_plugin_module()
        self.provider = FakeProvider(EXTRACT_REPLY)
        self.context = FakeContext(self.provider, None)
        self._seq = 0
        #: make_plugin 建出来的插件；tearDown 要把它们的库关掉
        self._plugins: list = []

    def tearDown(self) -> None:
        # 插件构造时就打开了 mind.db 的 SQLite 连接。原来这里只清临时目录，
        # 连接还活着 —— Windows 不允许删被占用的文件，于是**每个**用例都会多报
        # 一个 PermissionError，把真正的失败淹掉。这里先把库关掉再清理。
        for plugin in getattr(self, "_plugins", []):
            try:
                plugin.store.close()
            except Exception:  # noqa: BLE001 - 清理失败不算测试失败
                pass
        self._plugins = []
        try:
            self._tmp.cleanup()
        except Exception:  # Windows 上偶尔还被占用：环境问题，忽略
            pass

    def make_plugin(self, config: dict | None = None):
        # 深拷贝：浅拷贝下嵌套 dict 还是 CONFIG 的那几个，
        # 一旦有用例调了「恢复默认」，它就会把 CONFIG 里的值一起改掉，
        # 后面的用例（比如采样）拿到的配置就不是它以为的那份了。
        merged = copy.deepcopy(CONFIG)
        for key, value in (config or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        self._seq += 1
        FakeStarTools.data_dir = self.data_dir / f"p{self._seq}"
        self.context = FakeContext(self.provider, None)
        plugin = self.module.AIMindPlugin(self.context, merged)
        try:
            self._plugins.append(plugin)
        except Exception:  # noqa: BLE001
            pass
        return plugin

    def run_async(self, coro):
        return asyncio.run(coro)

    async def collect(self, async_gen) -> str:
        chunks = []
        async for item in async_gen:
            chunks.append(item[1])
        return "\n".join(chunks)

    async def send(self, plugin, text: str, *, umo: str = PRIVATE, uid: str = OWNER, reply: str = "嗯嗯"):
        event = FakeEvent(text, umo=umo, uid=uid)
        req = FakeProviderRequest()
        await plugin.on_llm_request(event, req)
        event.set_result(FakeResult([Plain(reply)]))
        await plugin.on_decorating_result(event)
        return req

    async def api(self, plugin, path: str, *, query: dict | None = None,
                  body: dict | None = None, **kwargs: Any):
        route = f"/{PLUGIN}/{path}"
        self.assertIn(route, self.context.web_apis, f"路由 {route} 没注册")
        handler, methods, _desc = self.context.web_apis[route]
        request_cls = kwargs.get("request_cls") or FakeWebRequest
        FakeRequestProxy.current = request_cls(query, body)
        result = handler()
        if asyncio.iscoroutine(result):
            result = await result
        return result

    @staticmethod
    def all_injected(req) -> str:
        """本轮注入给模型的全部内容：用户消息附加块 + 系统级受信任位。"""
        parts = [getattr(req, "system_prompt", "") or ""]
        parts.extend(
            getattr(p, "text", "")
            for p in (getattr(req, "extra_user_content_parts", None) or [])
        )
        for item in getattr(req, "contexts", []) or []:
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(content, str):
                parts.append(content)
        return "".join(parts)

    def blocks_of(self, req) -> str:
        return self.all_injected(req)

    @staticmethod
    def chain_text(chain) -> str:
        return "".join(
            getattr(c, "text", "") for c in getattr(chain, "chain", [])
            if type(c).__name__ == "Plain"
        )

    def final(self, event) -> str:
        return "".join(
            getattr(c, "text", "") for c in event.get_result().chain
            if type(c).__name__ == "Plain"
        )

    @staticmethod
    def _body(res: Any) -> Any:
        """把 json_response 的信封拆掉，拿里面的 data。

        假宿主把 json_response 实现成 ``{"ok":..., "data":...}``（真宿主也是
        这个形状），所以断言要看的是 ``res["data"]``。
        """
        if isinstance(res, dict) and "data" in res and "ok" in res:
            return res["data"]
        return res


class MergedPluginTest(PluginHarness, unittest.TestCase):
    """情绪 + 长期记忆 + 记忆图谱：注入、面板、指令、工具。"""

    async def _new_plugin(self, config: dict | None = None):
        """建插件并完成初始化。本身是协程：不能再套一层 asyncio.run。"""
        plugin = self.make_plugin(config)
        await plugin.initialize()
        return plugin

    def test_web_routes_registered(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            routes = self.context.web_apis
            for name in ("sessions", "emotion", "memory", "graph", "settings"):
                self.assertIn(f"/{PLUGIN}/{name}", routes, name)
            await plugin.terminate()

        self.run_async(scenario())

    def test_removed_feature_routes_are_gone(self) -> None:
        """配图 / 分段 / 防抖 / 去 AI 味 / 护栏 / 提示词的路由必须都没了。"""
        async def scenario() -> None:
            plugin = await self._new_plugin()
            routes = self.context.web_apis
            for name in ("images", "images/upload", "splitter", "debounce",
                         "humanize", "style", "prompts", "wizard", "wizard/mode",
                         "guard", "background", "memory/trash"):
                for prefix in (f"/{PLUGIN}/{name}", f"/dsh/{PLUGIN}/{name}"):
                    self.assertNotIn(prefix, routes, f"{name} 的路由不该还在")
            await plugin.terminate()

        self.run_async(scenario())

    def test_both_emotion_and_memory_are_injected(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.collect(plugin.cmd_memory(FakeEvent("记忆 记住 他喜欢喝冰美式")))

            req = await self.send(plugin, "宝宝你今天好棒")
            joined = self.blocks_of(req)
            self.assertIn("<emotion_state>", joined, "情绪块没注入")
            self.assertIn("<long_term_memory>", joined, "记忆块没注入")
            self.assertIn("冰美式", joined)
            self.assertIn("<emotion_system>", req.system_prompt)
            self.assertIn("<long_term_memory_rules>", req.system_prompt)
            await plugin.terminate()

        self.run_async(scenario())

    def test_group_still_hides_private_memory(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.collect(plugin.cmd_memory(FakeEvent("记忆 记住 他偷偷存了私房钱")))
            req = await self.send(plugin, "在吗", umo=GROUP, uid=OWNER)
            self.assertNotIn("私房钱", self.blocks_of(req))
            await plugin.terminate()

        self.run_async(scenario())

    def test_manual_memory_lands_in_the_selected_session(self) -> None:
        """面板上加一条记忆：要绑到当前选中的会话上，并自动生成关键词。"""
        async def scenario() -> None:
            plugin = await self._new_plugin()
            for key in (PRIVATE, GROUP):
                plugin.engine.resolve(key, time.time())

            res = self._body(await self.api(plugin, "memory/add", query={
                "content": "他喜欢喝冰美式", "session": GROUP, "kind": "preference",
            }))
            self.assertTrue(res.get("ok"), res)
            self.assertEqual(res.get("session_id"), GROUP, "没绑到选中的会话上")
            self.assertTrue(res.get("keywords"), "关键词应当自动生成")

            memory = plugin.store.get(int(res["id"]))
            self.assertIsNotNone(memory)
            self.assertEqual(memory.session_id, GROUP)
            self.assertTrue(memory.keywords)
            self.assertTrue(memory.pinned, "手动加的记忆默认一直保存")
            await plugin.terminate()

        self.run_async(scenario())

    def test_memory_graph_follows_the_selected_session(self) -> None:
        """图谱只看选中会话的记忆（外加没绑会话的通用记忆）。"""
        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.api(plugin, "memory/add", query={
                "content": "这个群里的暗号是蓝鲸", "session": GROUP,
            })
            await self.api(plugin, "memory/add", query={
                "content": "私聊里说过的事", "session": PRIVATE,
            })

            graph = self._body(await self.api(plugin, "graph", query={"session": GROUP}))
            labels = " ".join(
                node.get("label", "") for node in graph.get("nodes", [])
                if node.get("type") == "memory"
            )
            self.assertIn("蓝鲸", labels, "本群的记忆应当出现在图谱里")
            self.assertNotIn("私聊里说过的事", labels, "别的会话的记忆不该出现")
            self.assertEqual(graph.get("filters", {}).get("session"), GROUP)
            await plugin.terminate()

        self.run_async(scenario())

    def test_memory_add_accepts_relation_kind(self) -> None:
        """添加表单要支持「关系」类型 —— 搜索栏本来就有，添加里缺过。"""
        async def scenario() -> None:
            plugin = await self._new_plugin()
            res = self._body(await self.api(plugin, "memory/add", query={
                "content": "他管我叫宝宝", "session": PRIVATE, "kind": "relation",
            }))
            self.assertTrue(res.get("ok"), res)
            memory = plugin.store.get(int(res["id"]))
            self.assertEqual(memory.kind, "relation")

            listed = self._body(await self.api(plugin, "memory", query={"kind": "relation"}))
            self.assertEqual(len(listed.get("items", [])), 1)
            await plugin.terminate()

        self.run_async(scenario())

    def test_memory_add_rejects_unknown_kind(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            res = self._body(await self.api(plugin, "memory/add", query={
                "content": "随便写点什么", "session": PRIVATE, "kind": "不存在的类型",
            }))
            memory = plugin.store.get(int(res["id"]))
            self.assertEqual(memory.kind, "fact", "不认识的类型应当退回 fact")
            await plugin.terminate()

        self.run_async(scenario())

    def test_panel_memory_crud(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            added = self._body(await self.api(plugin, "memory/add", query={
                "content": "他养了一只叫团子的猫", "session": PRIVATE,
            }))
            mid = int(added["id"])

            listed = self._body(await self.api(plugin, "memory", query={"q": "团子"}))
            self.assertEqual(len(listed.get("items", [])), 1)

            updated = self._body(await self.api(plugin, "memory/update", body={
                "id": mid, "content": "他养了一只叫团子的橘猫",
            }))
            self.assertTrue(updated.get("ok"), updated)
            self.assertIn("橘猫", plugin.store.get(mid).content)

            deleted = self._body(await self.api(plugin, "memory/delete", body={"id": mid}))
            self.assertTrue(deleted.get("ok"), deleted)
            self.assertIsNone(plugin.store.get(mid))
            await plugin.terminate()

        self.run_async(scenario())

    def test_panel_memory_list_follows_session(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.api(plugin, "memory/add", query={
                "content": "群里的记忆", "session": GROUP,
            })
            await self.api(plugin, "memory/add", query={
                "content": "私聊的记忆", "session": PRIVATE,
            })

            group_view = self._body(await self.api(plugin, "memory", query={"session": GROUP}))
            contents = [item["content"] for item in group_view.get("items", [])]
            self.assertIn("群里的记忆", contents)
            self.assertNotIn("私聊的记忆", contents)

            all_view = self._body(await self.api(plugin, "memory"))
            self.assertEqual(len(all_view.get("items", [])), 2, "不传会话时看全部")
            await plugin.terminate()

        self.run_async(scenario())

    def test_panel_memory_add_marks_sensitive(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            res = self._body(await self.api(plugin, "memory/add", query={
                "content": "他的身份证是 110101199003071234", "session": PRIVATE,
            }))
            self.assertTrue(res.get("sensitive"))
            memory = plugin.store.get(int(res["id"]))
            self.assertEqual(memory.scope, "user", "敏感内容必须只留在本人私聊")
            await plugin.terminate()

        self.run_async(scenario())

    def test_recall_tool_works(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.collect(plugin.cmd_memory(FakeEvent("记忆 记住 他喜欢喝冰美式")))
            event = FakeEvent("我平时爱喝什么")
            text = await plugin.tool_recall(event, query="冰美式")
            self.assertIn("冰美式", text)
            await plugin.terminate()

        self.run_async(scenario())

    def test_memory_command_shows_and_forgets(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            remembered = await self.collect(
                plugin.cmd_memory(FakeEvent("记忆 记住 他喜欢喝冰美式"))
            )
            self.assertTrue(remembered)

            shown = await self.collect(plugin.cmd_memory(FakeEvent("记忆")))
            self.assertIn("冰美式", shown)
            await plugin.terminate()

        self.run_async(scenario())

    def test_garbage_never_raises(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            for call in (
                lambda: self.api(plugin, "memory", query={"limit": "abc", "offset": "-9"}),
                lambda: self.api(plugin, "graph", query={"limit": "x", "min_importance": "?"}),
                lambda: self.api(plugin, "memory/add", query={"content": ""}),
                lambda: self.api(plugin, "memory/update", body={"id": "x"}),
                lambda: self.api(plugin, "memory/delete", body={"id": "x"}),
                lambda: self.api(plugin, "emotion", query={"hours": "abc"}),
            ):
                await call()
            await plugin.terminate()

        self.run_async(scenario())

    def test_panel_sessions_and_emotion(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.send(plugin, "宝宝在吗")
            res = self._body(await self.api(plugin, "sessions"))
            self.assertTrue(res.get("sessions"), "会话列表不该是空的")
            key = res["sessions"][0]["key"]
            emotion = self._body(await self.api(plugin, "emotion", query={"session": key}))
            self.assertIn("current", emotion)
            await plugin.terminate()

        self.run_async(scenario())

    def test_panel_set_emotion(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.send(plugin, "在吗")
            res = self._body(await self.api(plugin, "emotion/set", body={
                "session": PRIVATE, "p": 0.5, "a": -0.2, "d": 0.3,
            }))
            self.assertTrue(res.get("ok"), res)
            after = self._body(await self.api(plugin, "emotion", query={"session": PRIVATE}))
            current = after.get("current") or {}
            self.assertAlmostEqual(current.get("p", 0), 0.5, places=1)

            # 面板「一键改成」发的是情绪原型的锚点：设成「亲昵」的锚点
            # 必须分类回亲昵 + 它自己的 emoji（老版本的数值离感动更近，会串）
            from mind.emotion.model import PROTOTYPE_BY_KEY, PROTOTYPES

            affection = PROTOTYPE_BY_KEY["affection"]
            self._body(await self.api(plugin, "emotion/set", body={
                "session": PRIVATE,
                "p": affection.pad[0], "a": affection.pad[1], "d": affection.pad[2],
            }))
            now = self._body(await self.api(plugin, "emotion", query={"session": PRIVATE}))
            current = now.get("current") or {}
            self.assertEqual(current.get("label"), "亲昵", "亲昵的锚点分类错了")
            self.assertEqual(current.get("emoji"), affection.emoji, "emoji 跟情绪对不上")

            # 原型全表也要回给前端（按钮直接用它，23 种一个不少）
            protos = now.get("prototypes") or []
            self.assertEqual(len(protos), len(PROTOTYPES))
            self.assertEqual(
                [p.get("key") for p in protos], [p.key for p in PROTOTYPES]
            )
            await plugin.terminate()

        self.run_async(scenario())

    def test_panel_freeze_blocks_automatic_changes(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.send(plugin, "在吗")
            before = self._body(await self.api(
                plugin, "emotion", query={"session": PRIVATE}
            ))["current"]
            await self.api(plugin, "emotion/freeze", body={"session": PRIVATE, "frozen": True})
            await self.send(plugin, "你真好，谢谢你")
            after = self._body(await self.api(
                plugin, "emotion", query={"session": PRIVATE}
            ))["current"]
            self.assertAlmostEqual(before["p"], after["p"], places=3, msg="冻结后不该自动变")
            await plugin.terminate()

        self.run_async(scenario())

    def test_panel_freeze_stops_time_decay(self) -> None:
        """冻结后连随时间的衰减也要停：快进两小时情绪必须原地不动。"""

        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.send(plugin, "在吗")
            before = self._body(await self.api(
                plugin, "emotion", query={"session": PRIVATE}
            ))["current"]
            await self.api(plugin, "emotion/freeze", body={"session": PRIVATE, "frozen": True})

            # 快进两小时的后台维护
            plugin.engine.tick_all(time.time() + 7200)
            payload = self._body(await self.api(plugin, "emotion", query={"session": PRIVATE}))
            held = payload["current"]
            for dim in ("p", "a", "d"):
                self.assertAlmostEqual(
                    before[dim], held[dim], places=5,
                    msg=f"冻结后 {dim} 还在衰减（{before[dim]} → {held[dim]}）",
                )
            # 曲线在冻结点之后也该是平的（不按衰减往基线画）
            curve = payload.get("curve") or []
            if curve:
                tail = curve[-1]
                self.assertAlmostEqual(tail["p"], round(held["p"], 4), places=3,
                                       msg="冻结后曲线尾巴还在往下掉")

            # 解冻：恢复衰减
            await self.api(plugin, "emotion/freeze", body={"session": PRIVATE, "frozen": False})
            plugin.engine.tick_all(time.time() + 14400)
            after = self._body(await self.api(
                plugin, "emotion", query={"session": PRIVATE}
            ))["current"]
            self.assertGreater(abs(after["p"] - held["p"]), 0.001, "解冻后没有恢复衰减")
            await plugin.terminate()

        self.run_async(scenario())

    def test_panel_intensity_is_a_first_class_parameter(self) -> None:
        """强度是主动产物：能单独调、会衰减、冻结跟着停，面板与提示词都读它。"""

        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.send(plugin, "在吗")
            set_ = self._body(await self.api(plugin, "emotion/intensity", body={
                "session": PRIVATE, "intensity": 0.75,
            }))
            self.assertTrue(set_.get("ok"), set_)
            self.assertAlmostEqual(set_["intensity"], 0.75, places=3)
            self.assertEqual(set_.get("level"), 4)

            payload = self._body(
                await self.api(plugin, "emotion", query={"session": PRIVATE})
            )
            current = payload.get("current") or {}
            self.assertAlmostEqual(current.get("intensity", 0), 0.75, places=3)
            self.assertEqual(current.get("level"), 4)
            self.assertTrue(payload.get("intensity_curve"), "情绪页必须带回强度曲线")
            self.assertEqual(
                (payload.get("params") or {}).get("intensity_decay"), "exponential",
                "衰减策略要从情绪配置里带出来给面板",
            )

            # 会话下拉框显示的情绪强度，读的必须是同一个强度模型
            sessions = self._body(await self.api(plugin, "sessions"))["sessions"]
            mine = [s for s in sessions if s.get("key") == PRIVATE]
            self.assertTrue(mine, "会话列表里没有当前会话")
            self.assertAlmostEqual(mine[0].get("emotion_intensity", 0), 0.75, places=3)
            self.assertEqual(mine[0].get("emotion_level"), 4)

            # 冻结：强度跟着一起停住（快进两小时一丁点都不许掉）
            await self.api(
                plugin, "emotion/freeze", body={"session": PRIVATE, "frozen": True}
            )
            plugin.engine.tick_all(time.time() + 7200)
            held = self._body(
                await self.api(plugin, "emotion", query={"session": PRIVATE})
            )
            self.assertAlmostEqual(
                (held.get("current") or {}).get("intensity", 0), 0.75, places=3,
                msg="冻结后强度还在衰减",
            )

            # 提示词里必须注入情绪强度
            from mind.emotion.prompt import render_state_block

            session = plugin.engine.resolve(PRIVATE, time.time())
            text = render_state_block(plugin.engine.snapshot(session, None, time.time()))
            self.assertIn("情绪强度", text)
            self.assertIn("%", text)
            await plugin.terminate()

        self.run_async(scenario())

    def test_mood_command_reads_and_sets_the_new_intensity(self) -> None:
        """/情绪 输出与 /情绪 强度 都要走新的强度模型。"""

        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.send(plugin, "在吗")
            set_out = "".join(
                await self.collect(plugin.cmd_mood(FakeEvent("情绪 强度 0.8", admin=True)))
            )
            self.assertIn("强度已调到 80%", set_out)

            panel = "".join(
                await self.collect(plugin.cmd_mood(FakeEvent("情绪", admin=True)))
            )
            self.assertIn("80%", panel, "主面板要显示新的强度")

            denied = "".join(
                await self.collect(plugin.cmd_mood(FakeEvent("情绪 强度 0.1")))
            )
            self.assertIn("管理员", denied, "强度调节应当是管理员的事")
            await plugin.terminate()

        self.run_async(scenario())

    def test_panel_curve_point_crud(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.send(plugin, "在吗")
            inserted = self._body(await self.api(plugin, "emotion/point", body={
                "session": PRIVATE, "action": "insert", "t": time.time(),
                "p": 0.4, "a": 0.1, "d": 0.2,
            }))
            self.assertTrue(inserted.get("ok"), inserted)
            removed = self._body(await self.api(plugin, "emotion/point", body={
                "session": PRIVATE, "action": "delete", "t": time.time(),
            }))
            self.assertTrue(removed.get("ok"), removed)
            await plugin.terminate()

        self.run_async(scenario())

    def test_settings_lists_the_kept_groups(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            res = self._body(await self.api(plugin, "settings"))
            keys = {group["key"] for group in res.get("groups", [])}
            self.assertIn("emotion", keys)
            self.assertIn("memory", keys)
            for gone in ("images", "splitter", "humanize", "guard", "debounce"):
                self.assertNotIn(gone, keys, f"{gone} 的设置不该还在")
            await plugin.terminate()

        self.run_async(scenario())

    def test_manage_reports_and_prunes(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin()
            await self.api(plugin, "memory/add", query={
                "content": "要被清理的记忆", "session": PRIVATE, "importance": 0.1,
            })
            stats = self._body(await self.api(plugin, "manage"))
            self.assertGreaterEqual(stats["stats"]["memory"]["total"], 1)

            pruned = self._body(await self.api(plugin, "manage/memory", body={
                "action": "prune", "max_importance": 0.2, "never_used": True,
            }))
            self.assertTrue(pruned.get("ok"), pruned)
            self.assertGreaterEqual(pruned.get("removed", 0), 1)
            await plugin.terminate()

        self.run_async(scenario())

    def test_disabled_plugin_still_serves_panel(self) -> None:
        async def scenario() -> None:
            plugin = await self._new_plugin({"enabled": False})
            res = self._body(await self.api(plugin, "sessions"))
            self.assertIn("sessions", res)
            await plugin.terminate()

        self.run_async(scenario())



class CurveMathTest(unittest.TestCase):
    def test_curve_decays_toward_baseline(self) -> None:
        from mind import PAD, Sample, build_curve

        baseline = PAD.of(0.0, 0.0, 0.0)
        samples = [Sample(t=1000.0, p=1.0, a=1.0, d=1.0)]
        curve = build_curve(samples, baseline=baseline, half_life=100.0,
                            since=1000.0, until=1300.0, points=4)
        self.assertAlmostEqual(curve[0]["p"], 1.0, places=3)      # t=1000
        self.assertAlmostEqual(curve[1]["p"], 0.5, places=2)      # 一个半衰期
        self.assertAlmostEqual(curve[2]["p"], 0.25, places=2)     # 两个半衰期
        self.assertLess(curve[3]["p"], 0.25)

    def test_curve_before_first_sample_uses_baseline(self) -> None:
        from mind import PAD, Sample, build_curve

        baseline = PAD.of(0.5, 0.0, 0.0)
        samples = [Sample(t=2000.0, p=-1.0, a=0.0, d=0.0)]
        curve = build_curve(samples, baseline=baseline, half_life=100.0,
                            since=1000.0, until=3000.0, points=3)
        self.assertAlmostEqual(curve[0]["p"], 0.5, places=3)
        self.assertAlmostEqual(curve[1]["p"], -1.0, places=3)

    def test_empty_samples_returns_nothing(self) -> None:
        """没有采样点时返回空 —— 不能伪造一条贴在基线上的假直线。"""
        from mind import PAD, build_curve

        self.assertEqual(build_curve([], baseline=PAD(), half_life=100.0,
                                     since=0.0, until=100.0), [])
        self.assertEqual(build_curve([], baseline=PAD(), half_life=100.0,
                                     since=0.0, until=100.0, points=50), [])

    def test_should_sample_respects_epsilon(self) -> None:
        from mind import PAD, should_sample

        base = PAD.of(0.0, 0.0, 0.0)
        self.assertTrue(should_sample(None, base, 0.01))
        self.assertFalse(should_sample(base, PAD.of(0.001, 0.0, 0.0), 0.01))
        self.assertTrue(should_sample(base, PAD.of(0.5, 0.0, 0.0), 0.01))
        self.assertTrue(should_sample(base, PAD.of(0.001, 0.0, 0.0), 0.01, force=True))








class ConfigWithSave(dict):
    """模拟 AstrBotConfig：面板改完能 save_config() 回写。"""

    def __init__(self, data: dict, config_path: str = "x_config.json") -> None:
        super().__init__(data)
        self.config_path = config_path
        self.saved = 0

    def save_config(self, replace_config: dict | None = None, *, indent: int = 2) -> None:
        if replace_config:
            self.update(replace_config)
        self.saved += 1


class PanelAdminTest(PluginHarness, unittest.TestCase):
    """「全部功能设置」和「管理」两个页签的后端。"""

    # -- 全部功能设置 -------------------------------------------------------
    def test_settings_lists_every_leaf_of_the_schema(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            res = await self.api(plugin, "settings", query={})
            self.assertTrue(res["ok"])
            data = res["data"]
            paths: set[str] = set()

            def walk(fields) -> None:
                for node in fields:
                    if node.get("children"):
                        walk(node["children"])
                    else:
                        paths.add(node["path"])

            for group in data["groups"]:
                walk(group["fields"])
            self.assertEqual(paths, plugin._settings_leaf_paths(), "面板少列或多列了设置项")
            self.assertFalse(data["writable"], "普通 dict 不该被当成可回写")
            await plugin.terminate()

        self.run_async(scenario())

    def test_settings_save_writes_back_and_hot_reloads(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            fake = ConfigWithSave(plugin.config, "astrbot_plugin_ai_mind_config.json")
            plugin.config = fake

            res = await self.api(
                plugin, "settings/save",
                body={"values": {"emotion.appraisal.lexicon_gain": 0.4}},
            )
            self.assertTrue(res["ok"], res)
            self.assertEqual(fake.saved, 1, "没有调用 save_config")
            self.assertAlmostEqual(fake["emotion"]["appraisal"]["lexicon_gain"], 0.4, places=3)
            self.assertAlmostEqual(
                plugin.settings.emotion.lexicon_gain, 0.4, places=3, msg="热更新没生效"
            )
            self.assertAlmostEqual(
                plugin.engine.settings.lexicon_gain, 0.4, places=3, msg="引擎没热更新"
            )
            await plugin.terminate()

        self.run_async(scenario())

    def test_settings_save_rejects_unknown_keys(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.config = ConfigWithSave(plugin.config)
            res = await self.api(plugin, "settings/save", body={"values": {"没.这个.键": 1}})
            self.assertFalse(res["ok"])
            res = await self.api(plugin, "settings/save", body={})
            self.assertFalse(res["ok"])
            await plugin.terminate()

        self.run_async(scenario())

    def test_settings_reset_restores_schema_defaults(self) -> None:
        """面板「恢复默认」：全部叶子配置一次性还原成 _conf_schema.json 的默认值。"""
        from mind import cfg_get

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            fake = ConfigWithSave(plugin.config, "astrbot_plugin_ai_mind_config.json")
            plugin.config = fake

            # 先改成两个明显不是默认值的
            res = await self.api(plugin, "settings/save", body={"values": {
                "emotion.appraisal.lexicon_gain": 0.4,
                "advanced.debug_log": True,
            }})
            self.assertTrue(res["ok"], res)
            self.assertAlmostEqual(fake["emotion"]["appraisal"]["lexicon_gain"], 0.4, places=3)

            res = await self.api(plugin, "settings/reset", body={})
            self.assertTrue(res["ok"], res)
            data = res["data"]
            defaults = plugin._schema_defaults()
            self.assertTrue(data.get("reset"), "没回传恢复了哪些项")
            self.assertGreaterEqual(fake.saved, 2, "恢复默认没有落盘")

            for path, value in defaults.items():
                self.assertEqual(cfg_get(fake, path, None), value, f"{path} 没回到默认值")
            # 热更新也要跟着回到默认（引擎拿到的是同一份 Settings）
            self.assertAlmostEqual(
                plugin.settings.emotion.lexicon_gain,
                float(defaults["emotion.appraisal.lexicon_gain"]),
                places=6, msg="恢复默认后没有热更新",
            )
            self.assertFalse(plugin.settings.debug_log)
            await plugin.terminate()

        self.run_async(scenario())

    def test_settings_reset_without_write_support_explains(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            res = await self.api(plugin, "settings/reset", body={})
            self.assertFalse(res["ok"])
            self.assertIn("回写", res["message"])
            await plugin.terminate()

        self.run_async(scenario())

    def test_settings_save_without_write_support_explains(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            res = await self.api(
                plugin, "settings/save",
                body={"values": {"enabled": False}},
            )
            self.assertFalse(res["ok"])
            self.assertIn("回写", res["message"])
            await plugin.terminate()

        self.run_async(scenario())

    # -- 管理 ---------------------------------------------------------------
    def seed_memories(self, plugin, count: int = 3) -> None:
        from mind import Memory

        for index in range(count):
            plugin.store.add(Memory(
                content="这是一条测试记忆，编号 %d" % index,
                keywords=("测试",),
                session_id=PRIVATE,
                owner_id=OWNER,
                scope="session",
                importance=0.2 + index * 0.3,
            ))

    def test_manage_reports_stats(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed_memories(plugin, 3)
            res = await self.api(plugin, "manage", query={"session": PRIVATE})
            self.assertTrue(res["ok"])
            data = res["data"]
            self.assertEqual(data["stats"]["memory"]["total"], 3)
            self.assertEqual(data["session_memories"], 3)
            self.assertIn("kinds", data)
            self.assertIn("kind_labels", data)
            await plugin.terminate()

        self.run_async(scenario())

    def test_manage_clear_session_memory(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed_memories(plugin, 3)
            res = await self.api(
                plugin, "manage/memory", body={"action": "clear_session", "session": PRIVATE}
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["data"]["removed"], 3)
            self.assertEqual(plugin.store.stats()["total"], 0)
            res = await self.api(plugin, "manage/memory", body={"action": "clear_session"})
            self.assertFalse(res["ok"], "没给会话应当报错")
            await plugin.terminate()

        self.run_async(scenario())

    def test_manage_prune_by_condition(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed_memories(plugin, 3)   # 重要度 0.2 / 0.5 / 0.8
            res = await self.api(plugin, "manage/memory", body={
                "action": "prune", "max_importance": 0.4, "keep_pinned": True,
            })
            self.assertTrue(res["ok"])
            self.assertEqual(res["data"]["removed"], 1, "只该删掉重要度 0.2 那条")
            self.assertEqual(plugin.store.stats()["total"], 2)
            await plugin.terminate()

        self.run_async(scenario())

    def test_manage_prune_skips_pinned(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            from mind import Memory

            plugin.store.add(Memory(content="常驻的那条测试记忆", session_id=PRIVATE,
                                    owner_id=OWNER, importance=0.1, pinned=True))
            plugin.store.add(Memory(content="普通的那条测试记忆", session_id=PRIVATE,
                                    owner_id=OWNER, importance=0.1))
            res = await self.api(plugin, "manage/memory", body={
                "action": "prune", "max_importance": 0.5, "keep_pinned": True,
            })
            self.assertEqual(res["data"]["removed"], 1)
            remaining = plugin.store.list_memories(limit=10)
            self.assertEqual(len(remaining), 1)
            self.assertTrue(remaining[0].pinned)
            await plugin.terminate()

        self.run_async(scenario())

    def test_manage_data_operations(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin._frozen.add(PRIVATE)
            plugin.samples.append(PRIVATE, 1000.0, __import__("mind").PAD.of(0.5, 0.5, 0.5))

            res = await self.api(plugin, "manage/data", body={"action": "prune_samples", "keep": 50})
            self.assertTrue(res["ok"])
            res = await self.api(plugin, "manage/data", body={"action": "thaw_all"})
            self.assertTrue(res["ok"])
            self.assertEqual(plugin._frozen, set())
            res = await self.api(plugin, "manage/data", body={"action": "vacuum"})
            self.assertTrue(res["ok"])
            res = await self.api(plugin, "manage/data", body={"action": "胡说"})
            self.assertFalse(res["ok"])
            await plugin.terminate()

        self.run_async(scenario())






class GraphMultiSelectTest(PluginHarness, unittest.TestCase):
    """图谱页的多选管理走的是 memory/batch —— 这里把那条链路钉住。

    前端只负责把节点 id（形如 m:12）转成数字数组，真正干活的是这个接口；
    接口一改、前端就静默失效，所以在这里过一遍图谱会发的几种请求。
    """

    def seed(self, plugin, count: int = 3) -> None:
        from mind import Memory

        for index in range(count):
            plugin.store.add(Memory(
                content="第 %d 条记忆，写长一点好通过校验" % index,
                keywords=("测试", "词%d" % index),
                kind="fact" if index else "preference",
                session_id=PRIVATE,
                owner_id=OWNER,
                scope="session",
                importance=0.5,
            ))

    def ids_of(self, plugin) -> list[int]:
        memories = plugin.store.list_memories(limit=50)
        return [item.id for item in memories]

    def test_graph_nodes_carry_memory_ids(self) -> None:
        """图上的节点 id 必须能还原出记忆 id，否则多选删不动东西。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed(plugin, 3)
            res = await self.api(plugin, "graph", query={})
            nodes = [n for n in res["data"]["nodes"] if n["type"] == "memory"]
            parsed = sorted(int(n["id"].replace("m:", "")) for n in nodes)
            self.assertEqual(parsed, sorted(self.ids_of(plugin)))
            await plugin.terminate()

        self.run_async(scenario())

    def test_batch_pin_and_unpin(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed(plugin, 3)
            ids = self.ids_of(plugin)[:2]
            res = await self.api(plugin, "memory/batch", body={"action": "pin", "ids": ids})
            self.assertTrue(res["ok"])
            self.assertEqual(res["data"]["affected"], 2)
            pinned = [item for item in plugin.store.list_memories(limit=50) if item.pinned]
            self.assertEqual(len(pinned), 2)

            res = await self.api(plugin, "memory/batch", body={"action": "unpin", "ids": ids})
            self.assertEqual(res["data"]["affected"], 2)
            self.assertEqual(
                len([m for m in plugin.store.list_memories(limit=50) if m.pinned]), 0
            )
            await plugin.terminate()

        self.run_async(scenario())

    def test_batch_scope_and_delete(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed(plugin, 3)
            ids = self.ids_of(plugin)
            res = await self.api(
                plugin, "memory/batch", body={"action": "scope", "ids": ids[:1], "scope": "global"}
            )
            self.assertEqual(res["data"]["affected"], 1)
            changed = [m for m in plugin.store.list_memories(limit=50) if m.scope == "global"]
            self.assertEqual(len(changed), 1)
            self.assertEqual(changed[0].session_id, "", "换成全局作用域时必须把会话归属清掉")

            res = await self.api(plugin, "memory/batch", body={"action": "delete", "ids": ids})
            self.assertTrue(res["ok"])
            self.assertEqual(plugin.store.stats()["total"], 0)
            await plugin.terminate()

        self.run_async(scenario())

    def test_batch_rejects_junk(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed(plugin, 1)
            cases = [
                {"action": "delete", "ids": []},
                {"action": "delete", "ids": ["abc", "🙂"]},
                {"action": "胡说", "ids": self.ids_of(plugin)},
                {"action": "scope", "ids": self.ids_of(plugin), "scope": "胡说"},
            ]
            for body in cases:
                res = await self.api(plugin, "memory/batch", body=body)
                self.assertFalse(res["ok"], body)
            self.assertEqual(plugin.store.stats()["total"], 1, "非法请求不该删掉任何东西")
            await plugin.terminate()

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)












class PrefixCacheTest(PluginHarness, unittest.TestCase):
    """前缀缓存：两轮之间开头必须一模一样，否则 DeepSeek 一次都命中不了。

    以前情绪块、记忆块塞在 System Prompt 后面的受信任位，每轮都变 ——
    前缀一破，后面的整段历史全都落不进缓存，越聊越贵也越聊越慢。
    """

    @staticmethod
    def prefix_of(req) -> str:
        """前缀 = System Prompt + 受信任位（历史记录之前的那一段）。"""
        parts = [req.system_prompt or ""]
        for item in getattr(req, "contexts", []) or []:
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(content, str):
                parts.append(content)
        return "\n".join(parts)

    @staticmethod
    def tail_of(req) -> str:
        """贴在当前用户消息后面的那一坨。"""
        return "".join(
            getattr(p, "text", "") for p in getattr(req, "extra_user_content_parts", [])
        )

    def test_prefix_is_byte_identical_across_turns(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            first = await self.send(plugin, "你好")
            second = await self.send(plugin, "今天过得怎么样")
            self.assertEqual(
                self.prefix_of(first), self.prefix_of(second),
                "两轮的开头必须一模一样，否则前缀缓存一次都命中不了",
            )
            await plugin.terminate()

        self.run_async(scenario())

    def test_dynamic_blocks_land_in_tail(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            req = await self.send(plugin, "你好")
            tail = self.tail_of(req)
            self.assertIn("<emotion_state>", tail, "情绪块要贴到尾部")
            self.assertNotIn("<emotion_state>", self.prefix_of(req), "不能留在前缀里")
            await plugin.terminate()

        self.run_async(scenario())


    def test_turning_it_off_restores_old_position(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin({"advanced": {"cache_friendly": False}})
            await plugin.initialize()
            req = await self.send(plugin, "你好")
            self.assertIn("<emotion_state>", self.all_injected(req), "退回受信任位")
            self.assertEqual(self.tail_of(req), "", "旧行为下尾部是空的")
            await plugin.terminate()

        self.run_async(scenario())

    def test_tail_failure_falls_back_instead_of_losing_content(self) -> None:
        """附加到用户消息失败时，宁可少省点 token，也不能把情绪弄丢。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            req = await self.send(plugin, "你好")
            # 模拟新版本 AstrBot 改了字段名
            req.extra_user_content_parts = None
            await plugin.on_llm_request(FakeEvent("再来一句"), req)
            self.assertIn(
                "<emotion_state>", self.all_injected(req),
                "尾部贴不上去就得退回受信任位，绝不能静默丢掉",
            )
            await plugin.terminate()

        self.run_async(scenario())


class DeniedMemoryFilterTest(PluginHarness, unittest.TestCase):
    """拒绝名单必须在注入前真正生效。

    回归：RetrievalResult 是三个桶（pinned/recent/relevant）+ memories()，
    根本没有 .items。以前按 .items 用 → AttributeError 被 except 吞掉 →
    过滤等于不存在，被拒绝的人的记忆照样会端给别人，日志里只有一行 warning。
    """

    DENIED = "1000000009"

    def test_denied_users_memory_is_not_injected(self) -> None:
        async def scenario() -> None:
            from mind import Memory

            secret = "不该被看见的秘密暗号"
            # 对照组：没有拒绝名单时它会被正常注入 —— 先证明记忆确实检索得到
            open_plugin = self.make_plugin()
            await open_plugin.initialize()
            open_plugin.store.add(Memory(
                content=secret, session_id=PRIVATE, owner_id=self.DENIED, pinned=True))
            req = await self.send(open_plugin, "随便说点什么", umo=PRIVATE, uid=OWNER)
            self.assertIn(secret, self.all_injected(req),
                          "对照组：这条记忆本该被检索到")
            await open_plugin.terminate()

            # 实验组：拉进拒绝名单之后，一条都不能出现
            plugin = self.make_plugin({"privacy": {"denied_users": [self.DENIED]}})
            await plugin.initialize()
            plugin.store.add(Memory(
                content=secret, session_id=PRIVATE, owner_id=self.DENIED, pinned=True))
            req2 = await self.send(plugin, "随便说点什么", umo=PRIVATE, uid=OWNER)
            self.assertNotIn(secret, self.all_injected(req2), "拒绝名单没生效")
            noisy = [r for r in self.log.records if r[0] in ("error", "warning")]
            self.assertEqual(noisy, [], f"过滤时出错或打了警告：{noisy}")
            await plugin.terminate()

        self.run_async(scenario())

    def test_other_peoples_memories_still_pass(self) -> None:
        """拒绝名单只摘掉名单里的人，别人的记忆照常注入。"""

        async def scenario() -> None:
            from mind import Memory

            plugin = self.make_plugin({"privacy": {"denied_users": [self.DENIED]}})
            await plugin.initialize()
            plugin.store.add(Memory(
                content="可以看见的日常", session_id=PRIVATE, owner_id=OWNER, pinned=True))
            plugin.store.add(Memory(
                content="不该被看见的秘密暗号", session_id=PRIVATE,
                owner_id=self.DENIED, pinned=True))
            req = await self.send(plugin, "随便说点什么", umo=PRIVATE, uid=OWNER)
            seen = self.all_injected(req)
            self.assertIn("可以看见的日常", seen, "不该误伤别人的记忆")
            self.assertNotIn("不该被看见的秘密暗号", seen)
            await plugin.terminate()

        self.run_async(scenario())

    def test_filter_failure_fails_closed(self) -> None:
        """过滤炸了就必须一条都不注入 —— 拒绝名单是承诺，不能放行。"""

        async def scenario() -> None:
            from mind import Memory

            plugin = self.make_plugin({"privacy": {"denied_users": [self.DENIED]}})
            await plugin.initialize()
            plugin.store.add(Memory(
                content="不该被看见的秘密暗号", session_id=PRIVATE,
                owner_id=self.DENIED, pinned=True))
            plugin.store.add(Memory(
                content="可以看见的日常", session_id=PRIVATE, owner_id=OWNER, pinned=True))

            def boom(*_args, **_kwargs):
                raise RuntimeError("模拟过滤炸掉")

            plugin._filter_denied_memories = boom
            req = await self.send(plugin, "随便说点什么", umo=PRIVATE, uid=OWNER)
            seen = self.all_injected(req)
            self.assertNotIn("不该被看见的秘密暗号", seen, "过滤失败时必须 fail-closed")
            self.assertNotIn("可以看见的日常", seen, "宁可这一轮不回忆，也不能冒险")
            errors = [r for r in self.log.records if r[0] == "error"]
            self.assertTrue(errors, "失败了要留下 error 日志，别静默")
            await plugin.terminate()

        self.run_async(scenario())

    def test_recall_tool_works(self) -> None:
        """recall_long_term_memory 以前也按 .items 用，同样会报错。"""

        async def scenario() -> None:
            from mind import Memory

            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.store.add(Memory(
                content="他喜欢喝冰美式", session_id=PRIVATE, owner_id=OWNER))
            plugin.store.add(Memory(
                content="他下周要考试", session_id=PRIVATE, owner_id=OWNER))
            event = FakeEvent("你还记得我什么", umo=PRIVATE, uid=OWNER)
            # 用一个搜不到的词，逼它走检索那条路（就是当年报错的那条）
            text = await plugin.tool_recall(event, "zzz这个词不存在")
            self.assertNotIn("内部出错", text, "回忆工具内部炸了：" + text)
            self.assertIn("冰美式", text)
            await plugin.terminate()

        self.run_async(scenario())


class CurveDiagnosticsTest(PluginHarness, unittest.TestCase):
    """画不出曲线时，面板要能说清楚卡在哪一步。"""

    def test_payload_carries_the_counts(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "今天好累啊")
            data = (await self.api(plugin, "emotion", query={"session": PRIVATE, "hours": "72"}))["data"]
            for key in ("sample_count", "sample_total", "samples_broken", "curve_source"):
                self.assertIn(key, data, f"面板要用 {key} 来判断该显示哪句提示")
            self.assertGreater(data["sample_count"], 0, "聊过之后本会话该有采样点")
            self.assertFalse(data["samples_broken"])
            await plugin.terminate()

        self.run_async(scenario())

    def test_broken_store_is_reported_not_silent(self) -> None:
        """采样表坏掉时要报出来，而不是静默地什么都不写。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            # 模拟数据库真的用不了：来源和自带连接都指向一个建不出来的路径
            plugin.samples.conn = None
            plugin.samples._db_path = Path(BAD_WRITE_DIR) / "mind.db"
            await self.send(plugin, "喂")
            data = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            self.assertTrue(data["samples_broken"], "要如实报告采样不可用")
            warnings = [r for r in self.log.records if r[0] == "warning" and "曲线" in r[1]]
            self.assertTrue(warnings, f"要留下能查的日志：{self.log.records[-5:]}")
            await plugin.terminate()

        self.run_async(scenario())




class SamplingGateTest(PluginHarness, unittest.TestCase):
    """曲线采样的闸门：任何一步卡住，都要能在面板上说清楚。"""

    async def _turn(self, plugin, text: str, mid: str):
        event = FakeEvent(text, umo=PRIVATE, uid=OWNER, mid=mid)
        req = FakeProviderRequest()
        await plugin.on_llm_request(event, req)
        event.set_result(FakeResult([Plain("嗯")]))
        await plugin.on_decorating_result(event)
        return req

    def test_repeated_message_id_still_samples(self) -> None:
        """同一条消息被判成重复时，情绪可以不重复结算，曲线不能断。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            for i in range(3):
                await self._turn(plugin, f"第{i}句", "same-mid")
            data = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            self.assertGreaterEqual(data["diag"]["gated"], 2, "后两条该被判成重复")
            self.assertGreater(data["diag"]["writes"], 0, "被判重不该把采样一起掐掉")
            self.assertGreater(data["sample_count"], 0, "聊过就该有采样点")
            await plugin.terminate()

        self.run_async(scenario())

    def test_diag_explains_every_step(self) -> None:
        """面板要能一步一步看出采样卡在哪。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "喂喂喂")
            data = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            for key in ("turns", "prepares", "samples", "writes", "skipped", "gated",
                        "frozen", "emotion_enabled"):
                self.assertIn(key, data["diag"], f"面板要显示 {key}")
            self.assertGreaterEqual(data["diag"]["turns"], 1)
            self.assertGreaterEqual(data["diag"]["writes"], 1)
            check = data["db_check"]
            self.assertTrue(check["write"], f"数据库该是可写的：{check}")
            self.assertGreater(check["rows"], 0, "写完该有行")
            await plugin.terminate()

        self.run_async(scenario())

    def test_db_check_notices_a_dead_connection(self) -> None:
        """连接坏掉时，自检要给出原因而不是只报「失败」。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.samples.conn = None
            plugin.samples._db_path = Path(BAD_WRITE_DIR) / "mind.db"
            data = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            self.assertFalse(data["db_check"]["write"])
            self.assertTrue(data["db_check"]["error"], "要说清为什么写不进去")
            await plugin.terminate()

        self.run_async(scenario())


class StatusLineTest(unittest.TestCase):
    """顶栏不再常驻状态行：正常时安静，出问题必须看得见。"""

    @classmethod
    def setUpClass(cls) -> None:
        page = Path(__file__).resolve().parent.parent / "pages" / "mind" / "index.html"
        cls.html = page.read_text(encoding="utf-8")

    def test_no_always_on_ok_line(self) -> None:
        self.assertNotIn('setStatus("接口正常', self.html,
                         "正常状态不该往屏幕上写字")
        self.assertIn('id="api-status"', self.html)
        self.assertIn('display:none', self.html,
                      "状态行默认要藏着，否则会占一行空白")

    def test_errors_still_surface(self) -> None:
        for text in ("接口不通", "加载失败"):
            self.assertIn(text, self.html, f"出错时必须还能看见：{text}")


GRP = "aiocqhttp:GroupMessage:778899"


class ConnectionLifecycleTest(PluginHarness, unittest.TestCase):
    """宿主重载插件会先调 terminate()（我们把库关了），但老实例可能还在收消息。

    存下来的裸连接一旦被关，之后所有写入都会静默失败：曲线永远空的、
    记忆写不进去，面板上还看不出原因。"""

    def test_sampling_survives_a_closed_db(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "喂")
            plugin.store.close()          # 宿主重载插件时会这么干
            await self.send(plugin, "还在吗")
            data = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            self.assertTrue(data["db_check"]["write"], "库要能自己重新打开：%s" % data["db_check"])
            self.assertFalse(data["samples_error"], "不该再写进已关闭的库：%s" % data["samples_error"])
            closed = [r for r in self.log.records if r[0] == "warning" and "closed database" in r[1]]
            self.assertFalse(closed, "不该再报 closed database：%s" % closed)
            await plugin.terminate()

        self.run_async(scenario())

    def test_samples_keep_landing_after_reopen(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "喂")
            before = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]["sample_count"]
            plugin.store.close()
            await self.send(plugin, "喂喂喂")
            after = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]["sample_count"]
            self.assertGreater(after, before, "关过之后还得继续落点")
            await plugin.terminate()

        self.run_async(scenario())

    def test_session_list_survives_a_dead_memory_store(self) -> None:
        """记忆库不可用时，采样表里的会话还得能选到。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "喂", umo=GRP)
            plugin.engine.sessions.pop(GRP, None)        # 引擎状态里没有它了
            plugin.store.distinct_sessions = lambda: []  # 记忆库也帮不上忙
            data = (await self.api(plugin, "sessions"))["data"]
            keys = [s["key"] for s in data["sessions"]]
            self.assertIn(GRP, keys, "采样表里的会话也要能选到：%s" % keys)
            await plugin.terminate()

        self.run_async(scenario())

    def test_selfcheck_probe_is_not_a_session(self) -> None:
        """数据库自检写的探针不该被当成真会话说出去。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "喂")
            await self.api(plugin, "emotion", query={"session": PRIVATE})
            keys = [s["key"] for s in (await self.api(plugin, "sessions"))["data"]["sessions"]]
            self.assertNotIn("__selfcheck__", keys)
            self.assertFalse([k for k in keys if k.startswith("__")], keys)
            await plugin.terminate()

        self.run_async(scenario())


class SessionPickerTest(unittest.TestCase):
    """会话选择器：不能每次刷新都把用户的选择顶掉。"""

    @classmethod
    def setUpClass(cls) -> None:
        page = Path(__file__).resolve().parent.parent / "pages" / "mind" / "index.html"
        cls.html = page.read_text(encoding="utf-8")

    def test_options_are_rebuilt_only_when_changed(self) -> None:
        self.assertIn("sel.dataset.sig", self.html, "重建前要先比一比，别每次清空重填")
        self.assertIn('id="session-count"', self.html, "会话数量要显示出来")

    def test_stale_selection_falls_back(self) -> None:
        self.assertIn("var alive = state.sessions.some", self.html,
                      "选中的会话没了要退回第一个，不能让选择框空着")


class TidyTextTest(unittest.TestCase):
    """中文中间的空格要清掉，中英之间的空格要留着。"""

    def test_between_cjk(self) -> None:
        from mind.textfix import tidy_cjk_spaces as tidy
        self.assertEqual(tidy("切 干嘛"), "切干嘛")
        self.assertEqual(tidy("好 吧 好 吧"), "好吧好吧")
        self.assertEqual(tidy("切\u3000干嘛"), "切干嘛", "全角空格也要清")

    def test_before_cjk_punct(self) -> None:
        from mind.textfix import tidy_cjk_spaces as tidy
        self.assertEqual(tidy("行 。"), "行。")
        self.assertEqual(tidy("你说什么 ？"), "你说什么？")

    def test_latin_and_digits_keep_their_spaces(self) -> None:
        from mind.textfix import tidy_cjk_spaces as tidy
        self.assertEqual(tidy("AstrBot 很好用"), "AstrBot 很好用")
        self.assertEqual(tidy("第 3 章"), "第 3 章")
        self.assertEqual(tidy("hello world"), "hello world")


class ReplyTidyTest(PluginHarness, unittest.TestCase):
    """人设硬规则：她说话中文之间不留空格 —— 落地前统一清一遍。"""

    @staticmethod
    def _text(chain) -> str:
        out = []
        for comp in chain or []:
            text = getattr(comp, "text", None)
            if isinstance(text, str):
                out.append(text)
        return "".join(out)

    async def speak(self, plugin, reply: str):
        event = FakeEvent("在吗", umo=PRIVATE)
        await plugin.on_llm_request(event, FakeProviderRequest())
        event.set_result(FakeResult([Plain(reply)]))
        await plugin.on_decorating_result(event)
        return event

    def final(self, event) -> str:
        return self._text(getattr(event.get_result(), "chain", None))

    def test_last_bubble_has_no_space(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = await self.speak(plugin, "切 干嘛")
            self.assertEqual(self.final(event), "切干嘛")
            await plugin.terminate()

        self.run_async(scenario())

    def test_a_whole_reply_is_tidied(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = await self.speak(plugin, "哼 ，你 怎么才来 。")
            self.assertEqual(self.final(event), "哼，你怎么才来。")
            await plugin.terminate()

        self.run_async(scenario())

    def test_latin_spaces_survive_the_valve(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = await self.speak(plugin, "我在用 AstrBot 呢")
            self.assertEqual(self.final(event), "我在用 AstrBot 呢")
            await plugin.terminate()

        self.run_async(scenario())

class HistoryIsNotATailTest(PluginHarness, unittest.TestCase):
    """对话历史不是我们的留言板 —— 任何配置下都不许往里塞东西。"""

    def test_contexts_untouched_in_both_modes(self) -> None:
        async def scenario() -> None:
            history = [
                {"role": "user", "content": "在吗"},
                {"role": "assistant", "content": "干嘛"},
            ]
            for cfg in ({}, {"advanced": {"cache_friendly": False}}):
                plugin = self.make_plugin(cfg)
                await plugin.initialize()
                event = FakeEvent("我刚说什么了", umo=PRIVATE, uid=OWNER)
                req = FakeProviderRequest()
                req.contexts = [dict(item) for item in history]
                await plugin.on_llm_request(event, req)
                self.assertEqual(
                    req.contexts, history,
                    "cache_friendly=%s 时对话历史被注入了东西" % bool(cfg),
                )
                await plugin.terminate()

        self.run_async(scenario())


class DbFailureIsLoudTest(PluginHarness, unittest.TestCase):
    """数据库坏了必须大声报出来 —— 而且要报出文件和原因。

    这不是「曲线不好看」，是记忆、曲线、词表、配图、去 AI 味日志一起废。"""

    def test_check_carries_path_and_reason(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            # 让记忆库指向一个绝对建不出来的路径：真实的打开失败
            # 曲线存储现在是自足的：要让它真的打不开，直接掐掉它的连接与开库能力
            plugin.samples._conn = None
            plugin.samples.path = Path(BAD_WRITE_DIR) / "mind.db"
            plugin.samples._db_path = None
            plugin.samples._open = lambda: None          # type: ignore[assignment]
            plugin.samples.last_error = "OperationalError: unable to open database file"
            plugin.store.path = Path(BAD_WRITE_DIR) / "mind.db"
            plugin.store._conn = None
            data = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            check = data["db_check"]
            self.assertFalse(check["write"], "写不进去就得如实说")
            self.assertTrue(check["error"], "一定要给出原因，不能只说失败：%s" % check)
            self.assertIn("mind.db", check["path"], "要报出是哪个文件")
            self.assertFalse(check["exists"], "文件本来就不该存在")
            self.assertTrue(data["samples_broken"], "坏成这样必须整体标成不可用")
            await plugin.terminate()

        self.run_async(scenario())

    def test_panel_has_a_banner(self) -> None:
        page = Path(__file__).resolve().parent.parent / "pages" / "mind" / "index.html"
        html = page.read_text(encoding="utf-8")
        self.assertIn('id="db-alert"', html, "要有醒目横幅")
        self.assertIn("dbc.write === false", html, "横幅要真的挂在自检结果上")


class LazySchemaTest(PluginHarness, unittest.TestCase):
    """构造那一刻连不上库，表不能就这么永远不建。

    以前建表只在 __init__ 里跑一次：那时记忆库要是还没打开好（启动时被旧实例
    锁着、首次打开失败），emotion_samples 就永远不存在 —— 之后每次写采样都是
    「no such table」，曲线一路空白，错误还被吞掉，面板上什么都看不出来。"""

    def test_missing_table_is_rebuilt_on_next_use(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            # 把表删掉，再装作「从没建过」——正是用户遇到的现场
            plugin.samples.conn.execute("DROP TABLE IF EXISTS emotion_samples")
            plugin.samples.conn.commit()
            plugin.samples._schema_ready = False
            await self.send(plugin, "喂")
            data = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            self.assertTrue(data["db_check"]["table"], "表要能补建：%s" % data["db_check"])
            self.assertTrue(data["db_check"]["write"], "补建之后要写得进去：%s" % data["db_check"])
            self.assertGreater(data["sample_count"], 0, "补建之后曲线得有点")
            await plugin.terminate()

        self.run_async(scenario())

    def test_store_survives_a_dead_source(self) -> None:
        """来源彻底给不出连接时，仓库自己开一个照样干活。

        这条比「来源重开」更强：连记忆库都不配合了，曲线也不能是空的 ——
        数据库文件就在那儿，自己连上去就是了。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.samples.conn.execute("DROP TABLE IF EXISTS emotion_samples")
            plugin.samples.conn.commit()
            plugin.samples._schema_ready = False
            plugin.samples._source = None              # 旧钩子，留着不该有事
            await self.send(plugin, "喂")
            data = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            self.assertTrue(data["db_check"]["write"], "自己开连接也要写得进去：%s" % data["db_check"])
            self.assertGreater(data["sample_count"], 0, "曲线得有点")
            await plugin.terminate()

        self.run_async(scenario())


class DisabledPluginTest(PluginHarness, unittest.TestCase):
    """插件被停用时面板必须自己说出来 —— 否则查起来毫无头绪。

    AstrBot 里停用的插件照样加载、面板照样打得开，但钩子一次都不会被调用，
    日志里一个错都没有。"""

    def test_notice_when_disabled(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            before = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            self.assertEqual(before["plugin_disabled"], "", "正常情况下不该乱报")
            # 插件目录名就是 _disabled_notice 认的名字 —— 别写死：
            # 工作区里这份拷贝叫 astrbot_plugin_ai_mind(15)，写死就永远匹配不上。
            name = PLUGIN_DIR.name
            pref = Path(plugin.data_dir).parent.parent / "shared_preferences.json"
            pref.write_text(
                '{"inactivated_plugins": ["data.plugins.' + name + '.main"]}',
                encoding="utf-8",
            )
            after = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            self.assertIn("停用", after["plugin_disabled"])
            self.assertIn(name, after["plugin_disabled"])
            await plugin.terminate()

        self.run_async(scenario())


class ExternallyClosedTest(PluginHarness, unittest.TestCase):
    """连接被别人直接 close() 掉时，要重开一个接着写。

    日志里的原话是「曲线 SQL 失败：Cannot operate on a closed database.」——
    这说明关连接的不是 terminate()（那条路上我们会把 _conn 置空、
    下次访问自然重开）。真正的情况是：**缓存里那个连接对象被别处关了**，
    而 _conn 还指着它，于是每一次写入都写进一个已死的库。"""

    def test_closed_connection_is_reopened(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "喂")
            plugin.samples._conn.close()        # 有人绕过我们把曲线连接关了
            await self.send(plugin, "还在吗")
            data = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            self.assertTrue(data["db_check"]["write"], "库要能重开：%s" % data["db_check"])
            closed = [r for r in self.log.records if "closed database" in r[1]]
            self.assertFalse(closed, "不该再报 closed database：%s" % closed)
            await plugin.terminate()

        self.run_async(scenario())

    def test_samples_keep_coming_after_that(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "喂")
            before = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]["sample_count"]
            plugin.samples._conn.close()
            await self.send(plugin, "喂喂喂")
            after = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]["sample_count"]
            self.assertGreater(after, before, "重开之后曲线还得继续落点")
            await plugin.terminate()

        self.run_async(scenario())


class PendingTurnsPersistTest(PluginHarness, unittest.TestCase):
    """攒着还没抽取的对话必须落盘 —— 否则越常重启，自动记忆越不见效。

    原来这些对话只存在内存里：要攒够 batch_turns 轮、或者静默几分钟才抽一次，
    中途重启一次全没了。用户看到的就是「手动写入可以，自动写入从来不见效」。"""

    def test_pending_is_written_and_reloadable(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "我喜欢喝冰美式")
            self.assertTrue(plugin._buffers, "说了一句就该攒着")
            path = Path(plugin.data_dir) / "pending_turns.json"
            self.assertTrue(path.exists(), "攒着的对话必须落盘，不然一重启就没了")
            plugin._buffers.clear()
            plugin._load_pending()
            self.assertTrue(plugin._buffers, "重启后要能捡回来")
            await plugin.terminate()

        self.run_async(scenario())


class DebugModeTest(PluginHarness, unittest.TestCase):
    """隐藏调试开关：口令 + 指定 QQ，只影响那一个会话，面板上看不到。"""

    CODE = "369951"
    UID = "2020689845"

    async def _code(self, plugin, umo=PRIVATE, uid=None):
        event = FakeEvent(self.CODE, umo=umo, uid=uid or self.UID)
        await plugin.on_debug_code(event)
        return event

    def test_code_toggles_for_the_right_person(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.assertFalse(plugin.debug.is_active(PRIVATE))
            event = await self._code(plugin)
            self.assertTrue(plugin.debug.is_active(PRIVATE))
            self.assertTrue(event.is_stopped(), "口令不该发给模型")
            await self._code(plugin)
            self.assertFalse(plugin.debug.is_active(PRIVATE), "再发一次关闭")
            await plugin.terminate()

        self.run_async(scenario())

    def test_wrong_person_is_ignored(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = await self._code(plugin, uid="999")
            self.assertFalse(plugin.debug.is_active(PRIVATE))
            self.assertFalse(event.is_stopped(), "不该露痕迹")
            await plugin.terminate()

        self.run_async(scenario())

    def test_only_that_session_is_affected(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self._code(plugin)
            self.assertTrue(plugin.debug.is_active(PRIVATE))
            self.assertFalse(
                plugin.debug.is_active("aiocqhttp:GroupMessage:123456"),
                "只影响发口令的那一个会话",
            )
            await plugin.terminate()

        self.run_async(scenario())

    def test_debug_session_gets_no_injection(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self._code(plugin)
            req = FakeProviderRequest()
            req.contexts = [{"role": "user", "content": "上一句"}]
            await plugin.on_llm_request(
                FakeEvent("你现在什么心情", umo=PRIVATE, uid=self.UID), req
            )
            self.assertIn("调试模式", req.system_prompt or "", "要盖一条覆盖指令")
            self.assertEqual(req.extra_user_content_parts, [], "不该注入情绪 / 记忆")
            self.assertEqual(
                req.contexts, [{"role": "user", "content": "上一句"}],
                "对话历史一个字都不许动",
            )
            self.assertNotIn("<emotion_state>", req.system_prompt or "")
            await plugin.terminate()

        self.run_async(scenario())

    def test_debug_mode_is_not_exposed_in_the_panel(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self._code(plugin)
            data = (await self.api(plugin, "emotion", query={"session": PRIVATE}))["data"]
            leaked = [k for k in data if "debug" in str(k).lower()]
            self.assertEqual(leaked, [], "调试模式不该出现在面板数据里")
            await plugin.terminate()

        self.run_async(scenario())

    def test_last_resort_writes_when_everything_else_is_dead(self) -> None:
        """现有连接、来源、备用连接全废时，最后一招也要把数据写进去。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            before = plugin.samples.count()
            plugin.store._conn.close()          # 主连接死了
            plugin.samples._spare_conn = None
            plugin.samples._source = None       # 来源也不给了
            plugin.samples.append(PRIVATE, 1.0, __import__("mind.emotion.model", fromlist=["PAD"]).PAD(), "auto")
            self.assertGreater(plugin.samples.count(), before, "最后一招得写进去")
            await plugin.terminate()

        self.run_async(scenario())


class DebugCommandTest(PluginHarness, unittest.TestCase):
    """调试命令：只在开了调试模式的会话、只有指定 QQ 能用。"""

    CODE = "369951"
    UID = "2020689845"

    async def _turn(self, plugin, text, umo=PRIVATE, uid=None):
        event = FakeEvent(text, umo=umo, uid=uid or self.UID)
        await plugin.on_debug_code(event)
        return event

    def _sent(self, plugin) -> str:
        return " ".join(
            "".join(getattr(c, "text", "") for c in chain)
            for _umo, chain in plugin.context.sent
        )

    def test_help_lists_commands(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self._turn(plugin, self.CODE)
            event = await self._turn(plugin, "#帮助")
            self.assertTrue(event.is_stopped(), "命令不该发给模型")
            out = self._sent(plugin)
            for word in ("#状态", "#曲线", "#记忆", "#抽取", "#工具"):
                self.assertIn(word, out, "帮助里要列出 %s" % word)
            await plugin.terminate()

        self.run_async(scenario())

    def test_status_shows_the_whole_chain(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "喂")          # 先产生一点状态
            await self._turn(plugin, self.CODE)
            await self._turn(plugin, "#状态")
            out = self._sent(plugin)
            # 护栏一起去掉之后「工具闸门」不再出现在状态里，这里跟着更新
            for word in ("情绪 P/A/D", "曲线", "待抽取"):
                self.assertIn(word, out, "状态里要有 %s：%s" % (word, out))
            await plugin.terminate()

        self.run_async(scenario())

    def test_curve_reports_write_check(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self._turn(plugin, self.CODE)
            await self._turn(plugin, "#曲线")
            out = self._sent(plugin)
            self.assertIn("写入自检", out)
            self.assertIn("通过", out, "健康时自检该是通过：%s" % out)
            await plugin.terminate()

        self.run_async(scenario())

    def test_commands_need_debug_mode(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            event = await self._turn(plugin, "#状态")
            self.assertFalse(event.is_stopped(), "没开调试模式时不该认这个命令")
            self.assertEqual(self._sent(plugin), "", "更不该回消息")
            await plugin.terminate()

        self.run_async(scenario())

    def test_commands_need_the_right_person(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self._turn(plugin, self.CODE)
            event = await self._turn(plugin, "#状态", uid="999")
            self.assertFalse(event.is_stopped(), "别人发口令就装没看见")
            await plugin.terminate()

        self.run_async(scenario())

    def test_unknown_command_says_so(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self._turn(plugin, self.CODE)
            await self._turn(plugin, "#这是什么鬼")
            self.assertIn("不认识", self._sent(plugin))
            await plugin.terminate()

        self.run_async(scenario())



class MemoryFilterTest(PluginHarness, unittest.TestCase):
    """搜索栏的「人员 / 手动·自动」筛选、批量改作用域、图谱选人所需的数据。

    这批用例对应用户提的四件事，缺一件这里就红。
    """

    async def seed(self, plugin) -> None:
        from mind import Memory

        # 自动抽取（source=auto）：挂在群里，而且记下「谁告诉我的」
        plugin.store.add(Memory(
            content="他喜欢喝冰美式", keywords=("冰美式",), kind="preference",
            scope="session", session_id=GROUP, owner_id=OWNER, source="auto", pinned=True))
        # 手动加的（source=manual）：没有归属人
        plugin.store.add(Memory(
            content="宝宝生日是三月十二", keywords=("生日",), kind="relation",
            scope="session", session_id=GROUP, source="manual", pinned=True))
        # 另一个人说的
        plugin.store.add(Memory(
            content="555555 说下周出差", keywords=("出差",), kind="event",
            scope="session", session_id=GROUP, owner_id="555555", source="auto", pinned=True))


    def test_source_filter_manual_vs_auto(self) -> None:
        """手动加的和 AI 抽的要分得开。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.seed(plugin)

            auto = self._body(await self.api(
                plugin, "memory", query={"session": GROUP, "source": "auto"}))
            self.assertEqual(len(auto["items"]), 2)

            manual = self._body(await self.api(
                plugin, "memory", query={"session": GROUP, "source": "manual"}))
            self.assertEqual([m["content"] for m in manual["items"]],
                             ["宝宝生日是三月十二"])
            await plugin.terminate()

        self.run_async(scenario())

    def test_batch_set_global_then_session(self) -> None:
        """批量「设为通用 / 设为当前会话」都要真的改到字段上。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.seed(plugin)
            ids = [m["id"] for m in self._body(
                await self.api(plugin, "memory", query={"session": GROUP}))["items"]]
            self.assertEqual(len(ids), 3)

            out = self._body(await self.api(plugin, "memory/batch", body={
                "action": "scope", "scope": "global", "ids": ids}))
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["affected"], 3)
            for mid in ids:
                mem = plugin.store.get(mid)
                self.assertEqual(mem.scope, "global")
                self.assertEqual(mem.session_id, "", "通用记忆不该还绑着会话")

            out = self._body(await self.api(plugin, "memory/batch", body={
                "action": "scope", "scope": "session", "ids": ids, "session": GROUP}))
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["affected"], 3)
            for mid in ids:
                mem = plugin.store.get(mid)
                self.assertEqual(mem.scope, "session")
                self.assertEqual(mem.session_id, GROUP, "要挂回指定的那个会话")
                self.assertEqual(mem.owner_id, "")
            await plugin.terminate()

        self.run_async(scenario())

    def test_graph_carries_owner_for_the_person_selector(self) -> None:
        """图谱里的记忆节点要带 owner，否则「选人」没得选。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.seed(plugin)

            graph = self._body(await self.api(plugin, "graph", query={"session": GROUP}))
            people = {n["label"] for n in graph["nodes"] if n["type"] == "user"}
            self.assertEqual(people, {OWNER, "555555"})
            owners = {n["data"].get("owner") for n in graph["nodes"]
                      if n["type"] == "memory"}
            # 没挂人的那条（手加的）owner 为空，面板的「选人」下拉会跳过它
            self.assertEqual(owners - {""}, {OWNER, "555555"})
            await plugin.terminate()

        self.run_async(scenario())

    def test_manual_add_global_leaves_no_session(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            res = self._body(await self.api(plugin, "memory/add", query={
                "content": "所有会话都能看到的事实", "kind": "fact",
                "session": GROUP, "scope": "global"}))
            mem = plugin.store.get(int(res["id"]))
            self.assertEqual(mem.scope, "global")
            self.assertEqual(mem.session_id, "")
            await plugin.terminate()

        self.run_async(scenario())

    def test_sensitive_add_lands_on_the_person(self) -> None:
        """面板加的敏感记忆必须挂到人名下 —— 否则永远注入不了
        （allows() 对敏感记忆要求 owner 等于说话人）。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "在吗")
            res = self._body(await self.api(plugin, "memory/add", query={
                "content": "他的身份证是 110101199003071234",
                "kind": "fact", "session": PRIVATE}))
            mem = plugin.store.get(int(res["id"]))
            self.assertEqual(mem.scope, "user")
            self.assertEqual(mem.owner_id, OWNER, "敏感记忆没挂到人名下")
            self.assertEqual(mem.session_id, PRIVATE)
            await plugin.terminate()

        self.run_async(scenario())


class LegacyConfigCleanupTest(PluginHarness, unittest.TestCase):
    """已删功能留下的覆盖配置，启动时要清干净。

    否则旧版本的配置会一直躺在数据目录里，让人以为改它还有用。
    """

    LEGACY = (
        "splitter_overrides.json",
        "guard_overrides.json",
        "style_overrides.json",
        "debounce_overrides.json",
        "humanize_overrides.json",
        "relationship_overrides.json",
    )

    def test_orphan_overrides_removed_on_startup(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            for name in self.LEGACY:
                path = plugin.data_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"旧配置": true}', encoding="utf-8")
            self.assertTrue((plugin.data_dir / "splitter_overrides.json").is_file())

            await plugin.initialize()

            left = [n for n in self.LEGACY if (plugin.data_dir / n).is_file()]
            self.assertEqual(left, [], f"启动后还剩：{left}")
            # 记忆库原样留着（清理只碰覆盖配置，不碰数据）
            self.assertTrue((plugin.data_dir / "mind.db").is_file(), "记忆库不该被动")
            await plugin.terminate()

        self.run_async(scenario())

    def test_prompts_json_removed_and_defaults_injected(self) -> None:
        """提示词覆盖层已删：启动删掉 prompts.json，注入必须走代码默认值。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            path = plugin.data_dir / "prompts.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"emotion.rules": "我自己写的旧规则，别说出去"}),
                encoding="utf-8",
            )

            await plugin.initialize()

            self.assertFalse(path.is_file(), "启动后 prompts.json 应当被删掉")
            await self.collect(plugin.cmd_memory(FakeEvent("记忆 记住 他喜欢喝冰美式")))
            req = await self.send(plugin, "在吗")
            self.assertIn(
                "不规定你怎么说话", req.system_prompt, "情绪规则默认值没注入"
            )
            self.assertNotIn("别说出去", req.system_prompt, "废弃的覆盖还在生效")
            self.assertIn("<long_term_memory_rules>", req.system_prompt,
                          "记忆规则默认值没注入")
            await plugin.terminate()

        self.run_async(scenario())

    def test_legacy_tables_dropped_and_data_tables_kept(self) -> None:
        """多余表启动时自动 DROP；记忆与情绪曲线本体必须活下来。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            conn = plugin.store.connection
            self.assertIsNotNone(conn)
            for name in plugin.store.LEGACY_TABLES:
                conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO memories (content) VALUES ('别删我')")
            conn.commit()

            plugin._cleanup_legacy_files()

            names = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for name in plugin.store.LEGACY_TABLES:
                self.assertNotIn(name, names, f"{name} 应当在启动时被删掉")
            for keep in ("memories", "emotion_samples"):
                self.assertIn(keep, names, f"{keep} 是数据本体，绝不能被删")
            kept = [r[0] for r in conn.execute("SELECT content FROM memories").fetchall()]
            self.assertEqual(kept, ["别删我"], "删表把记忆也弄丢了")
            await plugin.terminate()

        self.run_async(scenario())

    def test_initialize_is_idempotent(self) -> None:
        """删过一次之后再启动也不该报错。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await plugin.terminate()
            await plugin.initialize()
            await plugin.terminate()

        self.run_async(scenario())


class DedupReferenceScopeTest(PluginHarness, unittest.TestCase):
    """抽取记忆时给模型看的「已有记忆」只能是本会话的（+ 通用）。

    老版本是 `list_memories(limit=40)`（全库最新 40 条）—— 别的群的记忆
    也会被当成"重复参考"塞进抽取提示词，白烧 token 还容易误合并。
    """

    def test_dedup_reference_only_covers_current_session(self) -> None:
        from mind import Memory, Turn

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()

            # 别的会话的记忆 —— 不许进去重参考
            plugin.store.add(Memory(
                content="另一个群的秘密：他下周要去相亲",
                keywords=("相亲",), session_id=GROUP, owner_id="666666",
                scope="session", importance=0.6,
            ))
            # 本会话的记忆 —— 要进
            plugin.store.add(Memory(
                content="本会话的：他喜欢冰美式",
                keywords=("冰美式",), session_id=PRIVATE, owner_id=OWNER,
                scope="session", importance=0.6,
            ))
            # 通用记忆 —— 每个会话都能看见，也进得来
            plugin.store.add(Memory(
                content="通用：他是学生",
                keywords=("学生",), scope="global", importance=0.6,
            ))
            self.provider.prompts.clear()

            await plugin._extract_session(PRIVATE, OWNER, [
                Turn(user="我今天有点累", assistant="早点休息"),
            ])

            sent = "\n".join(self.provider.prompts)
            self.assertTrue(sent, "抽取请求没发出去")
            self.assertIn("冰美式", sent, "本会话的记忆要进去重参考")
            self.assertIn("学生", sent, "通用记忆进得来（每个会话都看得见）")
            self.assertNotIn("相亲", sent, "别的群的记忆混进去了重参考 —— 白烧 token")
            await plugin.terminate()

        self.run_async(scenario())
