"""测试公共脚手架：离线模拟 AstrBot 运行时并加载真实插件包。"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_NAME = "astrbot_plugin_zhiren_leaderboard"


@dataclass
class FakePlain:
    """模拟 AstrBot Plain 文本消息段。"""

    text: str


@dataclass
class FakeAt:
    """模拟 AstrBot At 艾特消息段。"""

    qq: int | str
    name: str = ""


@dataclass
class FakeReply:
    """模拟 AstrBot Reply 引用消息段。"""

    id: str


@dataclass
class FakeMessageChain:
    """模拟 AstrBot MessageChain。"""

    chain: list[Any] = field(default_factory=list)


@dataclass
class FakeMessageObj:
    """模拟底层消息对象（携带 message_id）。"""

    message_id: str = ""


@dataclass
class FakePersona:
    """模拟 AstrBot 人格对象。"""

    persona_id: str
    system_prompt: str


class FakePersonaManager:
    """模拟 AstrBot PersonaManager。"""

    def __init__(self, initial_prompt: str = "你是一个风趣的群聊助手。"):
        self.personas: dict[str, FakePersona] = {
            "default": FakePersona("default", initial_prompt),
        }
        self.update_calls: list[tuple[str, str]] = []
        self.fail_update = False

    async def get_persona(self, pid: str) -> FakePersona:
        return self.personas[pid]

    async def update_persona(self, pid: str, system_prompt: str) -> None:
        if self.fail_update:
            raise RuntimeError("模拟人格更新失败")
        self.personas[pid] = FakePersona(pid, system_prompt)
        self.update_calls.append((pid, system_prompt))


@dataclass
class FakeLLMResponse:
    """模拟 LLM Provider 返回对象。"""

    completion_text: str
    usage: Any = None


class FakeProvider:
    """模拟 AstrBot LLM Provider。"""

    def __init__(self, responses: list[str | Exception] | None = None):
        self.responses: list[str | Exception] = list(responses or [])
        self.calls: list[tuple[str, str]] = []

    async def text_chat(self, prompt: str, system_prompt: str = "") -> FakeLLMResponse:
        self.calls.append((prompt, system_prompt))
        if not self.responses:
            return FakeLLMResponse(completion_text="")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeLLMResponse(completion_text=nxt)


class FakeOneBot:
    """模拟 OneBot 客户端（群信息/成员名片查询）。"""

    def __init__(
        self,
        group_names: dict[int, str] | None = None,
        member_names: dict[tuple[int, int], str] | None = None,
    ):
        self.group_names = dict(group_names or {})
        self.member_names = dict(member_names or {})
        self.group_info_calls: list[int] = []
        self.member_info_calls: list[tuple[int, int]] = []

    async def get_group_info(self, group_id: int) -> dict[str, Any]:
        self.group_info_calls.append(group_id)
        return {"group_name": self.group_names.get(group_id, "")}

    async def get_group_member_info(self, group_id: int, user_id: int) -> dict[str, Any]:
        self.member_info_calls.append((group_id, user_id))
        name = self.member_names.get((group_id, user_id), "")
        return {"card": name, "nickname": name}


class FakeContext:
    """模拟 AstrBot Context。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        provider: FakeProvider | None = None,
        persona_manager: FakePersonaManager | None = None,
    ):
        self._config = dict(config or {})
        self.provider = provider
        self.persona_manager = persona_manager or FakePersonaManager()
        self.sent_messages: list[tuple[str, FakeMessageChain]] = []

    def get_config(self) -> dict[str, Any]:
        return self._config

    def get_using_provider(self, umo: str | None = None) -> FakeProvider | None:
        return self.provider

    async def send_message(self, umo: str, chain: FakeMessageChain) -> None:
        self.sent_messages.append((umo, chain))


class FakeEvent:
    """模拟 AstrMessageEvent。"""

    def __init__(
        self,
        *,
        gid: str = "10001",
        sender_id: str = "20001",
        sender_name: str = "张三",
        self_id: str = "99999",
        components: list[Any] | None = None,
        is_admin: bool = False,
        umo: str | None = None,
        message_id: str = "msg-1",
        bot: FakeOneBot | None = None,
    ):
        self._gid = gid
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._self_id = self_id
        self._components = list(components or [])
        self._is_admin = is_admin
        self.unified_msg_origin = umo or f"aiocqhttp:GroupMessage:{gid}"
        self.message_obj = FakeMessageObj(message_id=message_id)
        self.bot = bot
        self.stopped = False

    def get_group_id(self) -> str:
        return self._gid

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_self_id(self) -> str:
        return self._self_id

    def get_messages(self) -> list[Any]:
        return self._components

    def is_admin(self) -> bool:
        return self._is_admin

    def stop_event(self) -> None:
        self.stopped = True

    def plain_result(self, text: str) -> dict[str, Any]:
        return {"type": "plain", "text": text}

    def chain_result(self, chain: list[Any]) -> dict[str, Any]:
        return {"type": "chain", "chain": chain}


def _install_astrbot_stubs() -> None:
    """向 sys.modules 注入轻量 AstrBot stub，使插件模块可完全离线导入。"""
    if "astrbot.api.star" in sys.modules:
        return

    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = logging.getLogger("astrbot_test")

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = FakeEvent
    event_mod.MessageChain = FakeMessageChain

    class _FilterNamespace:
        class EventMessageType:
            GROUP_MESSAGE = "group_message"

        @staticmethod
        def event_message_type(_msg_type: Any):
            def _decorator(fn):
                return fn

            return _decorator

    event_mod.filter = _FilterNamespace

    comp_mod = types.ModuleType("astrbot.api.message_components")
    comp_mod.At = FakeAt
    comp_mod.Plain = FakePlain
    comp_mod.Reply = FakeReply

    star_mod = types.ModuleType("astrbot.api.star")

    class _StarBase:
        def __init__(self, context: Any):
            self.context = context

    def _register(*_args: Any, **_kwargs: Any):
        def _decorator(cls):
            return cls

        return _decorator

    star_mod.Context = FakeContext
    star_mod.Star = _StarBase
    star_mod.register = _register

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = comp_mod
    sys.modules["astrbot.api.star"] = star_mod


def _load_plugin_modules() -> tuple[Any, Any]:
    """按 AstrBot 插件包结构加载 `zhiren_logic` 与 `main` 模块。"""
    _install_astrbot_stubs()

    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(REPO_ROOT)]  # type: ignore[attr-defined]
        sys.modules[PKG_NAME] = pkg

    logic_name = f"{PKG_NAME}.zhiren_logic"
    if logic_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(logic_name, REPO_ROOT / "zhiren_logic.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[logic_name] = mod
        spec.loader.exec_module(mod)

    main_name = f"{PKG_NAME}.main"
    if main_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(main_name, REPO_ROOT / "main.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[main_name] = mod
        spec.loader.exec_module(mod)

    return sys.modules[logic_name], sys.modules[main_name]


async def collect_responses(plugin: Any, event: FakeEvent) -> list[dict[str, Any]]:
    """收集 `on_group_message` 异步生成器产生的全部响应，并让出事件循环跑完即时任务。"""
    outputs = []
    async for resp in plugin.on_group_message(event):
        outputs.append(resp)
    return outputs


def response_text(resp: dict[str, Any]) -> str:
    """将模拟响应统一提取为纯文本，便于断言。"""
    if resp["type"] == "plain":
        return str(resp["text"])
    parts = []
    for c in resp["chain"]:
        if isinstance(c, FakePlain):
            parts.append(c.text)
        elif isinstance(c, FakeAt):
            parts.append(f"@{c.qq}")
        elif isinstance(c, FakeReply):
            parts.append(f"[reply:{c.id}]")
    return "".join(parts)


@pytest.fixture(scope="session")
def plugin_modules():
    """返回 `(zhiren_logic_module, main_module)`。"""
    return _load_plugin_modules()


@pytest.fixture
def make_plugin(tmp_path: Path, plugin_modules, monkeypatch: pytest.MonkeyPatch):
    """创建隔离数据库的插件实例，并在测试结束时自动优雅关闭。"""
    _logic_mod, main_mod = plugin_modules
    created: list[Any] = []
    monkeypatch.delenv("ZHIREN_SUPER_ADMIN", raising=False)
    monkeypatch.delenv("ZHIREN_PUBLIC_URL", raising=False)
    monkeypatch.delenv("ZHIREN_CORS_ORIGIN", raising=False)
    monkeypatch.setattr(main_mod, "SUPER_ADMIN", "")

    def _factory(
        *,
        ctx_config: dict[str, Any] | None = None,
        plugin_config: dict[str, Any] | None = None,
        provider: FakeProvider | None = None,
        persona_manager: FakePersonaManager | None = None,
    ):
        ctx = FakeContext(
            config=ctx_config,
            provider=provider,
            persona_manager=persona_manager,
        )
        cfg = {"data_dir": str(tmp_path / f"data_{len(created)}")}
        if plugin_config:
            cfg.update(plugin_config)
        plugin = main_mod.ZhirenLeaderboardPlugin(ctx, cfg)
        created.append(plugin)
        return plugin, ctx

    yield _factory

    for p in created:
        if not p._closed:
            asyncio.run(p.terminate())
