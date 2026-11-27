"""AI 裁判两阶段分诊、观察超时清理、去重隔离与自我迭代测试。"""

from __future__ import annotations

import asyncio
import json
import time

from conftest import (
    FakeAt,
    FakeEvent,
    FakePersonaManager,
    FakePlain,
    FakeProvider,
    collect_responses,
    response_text,
)


def test_ai_buffer_excludes_score_commands_and_evicts_cleanly(make_plugin, plugin_modules):
    """验证手动记分指令不进 AI 缓冲，且缓冲满上限淘汰时同步清理已判/已阅集合。"""
    _, main_mod = plugin_modules

    async def _run():
        plugin, _ctx = make_plugin()
        plugin._ai_enabled.add("1001")

        # 记分指令不进缓冲
        e_score = FakeEvent(
            gid="1001",
            sender_id="101",
            sender_name="甲",
            components=[FakeAt(qq=102, name="乙"), FakePlain(" 扣一分 恶意抢人头")],
        )
        await collect_responses(plugin, e_score)
        assert plugin._msg_buffer.get("1001", []) == []

        # 超过 AI_BUFFER_MAX 条后，旧 seq 同步从 _ai_judged / _ai_reviewed 清理
        for i in range(main_mod.AI_BUFFER_MAX + 5):
            e_chat = FakeEvent(
                gid="1001",
                sender_id="101",
                sender_name="甲",
                components=[FakePlain(f"这是一条测试群聊消息编号 {i}")],
            )
            await collect_responses(plugin, e_chat)
            seq = plugin._msg_buffer["1001"][-1]["seq"]
            plugin._ai_judged.add(seq)
            plugin._ai_reviewed.add(seq)

        assert len(plugin._msg_buffer["1001"]) == main_mod.AI_BUFFER_MAX
        assert len(plugin._ai_judged) == main_mod.AI_BUFFER_MAX
        assert len(plugin._ai_reviewed) == main_mod.AI_BUFFER_MAX

        # 关闭 AI 裁判时同步释放全部状态，无残留泄漏
        plugin._clear_group_ai_state("1001")
        assert "1001" not in plugin._msg_buffer
        assert plugin._ai_judged == set()
        assert plugin._ai_reviewed == set()

    asyncio.run(_run())


def test_ai_triage_watch_timeout_and_reaction_verdict(make_plugin, plugin_modules):
    """验证分诊立判（±1 钳制）、观察项初始化防空转、冷场超时清理与群友反应终审封账。"""
    _, main_mod = plugin_modules

    async def _run():
        # 第 1 次 LLM：1 号确凿扣分（故意返回 -3 验证钳制到 -1），2 号进入观察，3 号是机器人被过滤
        triage_reply = json.dumps(
            [
                {"id": 1, "delta": -3, "reason": "恶意辱骂群友", "watch": False},
                {"id": 2, "delta": 0, "reason": "疑似放鸽子", "watch": True},
                {"id": 3, "delta": -1, "reason": "机器人发言", "watch": False},
            ],
            ensure_ascii=False,
        )
        # 第 2 次 LLM：观察期结合群友反应给出终审 -1
        watch_verdict = json.dumps({"delta": -1, "reason": "放鸽子惹众怒"}, ensure_ascii=False)
        provider = FakeProvider([triage_reply, watch_verdict])

        plugin, ctx = make_plugin(provider=provider)
        plugin._self_id = "99999"
        plugin._ai_enabled.add("1001")
        plugin._remember_session("1001", "aiocqhttp:GroupMessage:1001")

        now = time.time()
        plugin._msg_buffer["1001"] = [
            {"seq": 1, "uid": "201", "name": "张三", "text": "持续辱骂消息", "ts": now, "mid": "m1"},
            {"seq": 2, "uid": "202", "name": "李四", "text": "临时有事鸽了", "ts": now, "mid": "m2"},
            {"seq": 3, "uid": "99999", "name": "机器人", "text": "我是机器人", "ts": now, "mid": "m3"},
        ]

        await plugin._ai_tick_once()

        # 确凿判罚：张三应被扣 1 分（非 -3），机器人不被判入榜
        rows = {r["uid"]: r["total"] for r in plugin._total_rows("1001")}
        assert rows == {"201": -1}
        assert len(ctx.sent_messages) == 1

        # 李四进入观察，且 last_check_ts 等于 since_ts（非 0），不会立即空转复议
        watches = plugin._ai_watch["1001"]
        assert len(watches) == 1
        assert watches[0]["uid"] == "202"
        assert watches[0]["last_check_ts"] == watches[0]["since_ts"]

        # 立刻再 tick 一次，不应消耗 LLM
        calls_before = len(provider.calls)
        await plugin._ai_tick_once()
        assert len(provider.calls) == calls_before

        # 模拟冷却已过且出现群友声讨新发言 -> 触发终审并封账
        watches[0]["last_check_ts"] = time.time() - main_mod.AI_WATCH_RECHECK - 1
        watches[0]["since_ts"] = time.time() - 20
        plugin._msg_buffer["1001"].append(
            {
                "seq": 4,
                "uid": "203",
                "name": "王五",
                "text": "李四又放大家鸽子太离谱了",
                "ts": time.time(),
                "mid": "m4",
            }
        )
        await plugin._ai_tick_once()
        rows_after = {r["uid"]: r["total"] for r in plugin._total_rows("1001")}
        assert rows_after == {"201": -1, "202": -1}
        assert plugin._ai_watch["1001"] == []
        # 群友声讨作为证据（seq=4）应已被封账，防止重复扣分
        assert 4 in plugin._ai_judged

        # 回归测试：冷场群无新消息时，超时观察项仍必须被正确清理
        plugin._ai_watch["1001"].append(
            {
                "uid": "204",
                "name": "赵六",
                "hint": "疑似甩锅",
                "since_ts": time.time() - main_mod.AI_WATCH_MAX - 10,
                "last_check_ts": time.time(),
            }
        )
        await plugin._ai_tick_once()
        assert plugin._ai_watch["1001"] == []

    asyncio.run(_run())


def test_ai_judge_supports_numeric_uid_and_protects_bot(make_plugin):
    """验证专项裁决支持纯数字 QQ 号，且裁决机器人自身时走反馈而非入榜。"""

    async def _run():
        provider = FakeProvider(
            [
                json.dumps({"delta": 1, "reason": "热心帮群友排障"}, ensure_ascii=False),
                json.dumps({"delta": -1, "reason": "答非所问"}, ensure_ascii=False),
            ]
        )
        plugin, _ctx = make_plugin(provider=provider)
        plugin._self_id = "99999"
        plugin._ai_enabled.add("1001")
        plugin._upsert_member("1001", "301", "热心群友")
        plugin._upsert_member("1001", "99999", "小智Bot")
        now = time.time()
        plugin._msg_buffer["1001"] = [
            {"seq": 1, "uid": "301", "name": "热心群友", "text": "你把代理关了再试", "ts": now, "mid": "m1"},
            {"seq": 2, "uid": "302", "name": "提问者", "text": "谢谢大佬真的好了", "ts": now, "mid": "m2"},
            {"seq": 3, "uid": "99999", "name": "小智Bot", "text": "今天星期八", "ts": now, "mid": "m3"},
        ]

        # 1. 通过纯数字 QQ 号触发专项裁决
        e_num = FakeEvent(
            gid="1001",
            sender_id="302",
            self_id="99999",
            components=[FakeAt(qq=99999), FakePlain(" 评价 301")],
        )
        r_num = await collect_responses(plugin, e_num)
        assert "专项裁决：+1 分" in response_text(r_num[0])
        assert plugin._total_rows("1001")[0]["uid"] == "301"

        # 2. 按昵称评价机器人 -> 走反馈池，不进排行榜
        e_bot = FakeEvent(
            gid="1001",
            sender_id="302",
            self_id="99999",
            components=[FakeAt(qq=99999), FakePlain(" 评价 小智Bot")],
        )
        r_bot = await collect_responses(plugin, e_bot)
        assert "不参与排行榜" in response_text(r_bot[0])
        assert all(r["uid"] != "99999" for r in plugin._total_rows("1001"))

    asyncio.run(_run())


def test_self_evolve_incremental_and_failure_cursor_safety(make_plugin):
    """验证自我迭代过滤噪声、增量合并准则，且 LLM 失败时不误推进游标。"""

    async def _run():
        pm = FakePersonaManager("原始人设：冷静幽默。")
        # 第 1 次失败（解析失败），第 2 次成功输出增量 JSON
        bad_and_good = FakeProvider(
            [
                "不是合法 JSON",
                json.dumps(
                    {
                        "add": ["回复尽量简洁不刷屏", "群友互损时不要乱插嘴"],
                        "replace": [],
                        "delete": [],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        plugin, ctx = make_plugin(provider=bad_and_good, persona_manager=pm)
        plugin._remember_session("1001", "aiocqhttp:GroupMessage:1001")

        # 写入噪声理由（应过滤）和有效反馈
        with plugin._tx() as db:
            db.execute(
                "INSERT INTO bot_feedback(gid,delta,reason,operator_name,created_at) "
                "VALUES('1001',-1,'傻逼','甲','2026-09-07 10:00:00')"
            )
            db.execute(
                "INSERT INTO bot_feedback(gid,delta,reason,operator_name,created_at) "
                "VALUES('1001',-1,'回复太长刷屏了','甲','2026-09-07 10:01:00')"
            )
            db.execute(
                "INSERT INTO bot_feedback(gid,delta,reason,operator_name,created_at) "
                "VALUES('1001',-1,'不要在熟人互损时乱插嘴','乙','2026-09-07 10:02:00')"
            )

        # 第 1 次 LLM 解析失败 -> _last_evolve_at 必须保持原值，不丢弃这批反馈
        await plugin._maybe_evolve("1001", force=True)
        assert plugin._last_evolve_at == ""
        assert pm.update_calls == []

        # 第 2 次成功 -> 注入人格、落库 bot_policy 并通知群聊
        await plugin._maybe_evolve("1001", force=True)
        assert plugin._last_evolve_at != ""
        assert len(pm.update_calls) == 1
        assert "回复尽量简洁不刷屏" in pm.personas["default"].system_prompt
        assert "回复尽量简洁不刷屏" in plugin._judge_system_prompt()
        assert len(ctx.sent_messages) == 1

        # 查询「迭代状态」指令可正常返回
        e_status = FakeEvent(
            gid="1001",
            sender_id="101",
            self_id="99999",
            components=[FakeAt(qq=99999), FakePlain(" 迭代状态")],
        )
        r_status = await collect_responses(plugin, e_status)
        txt = response_text(r_status[0])
        assert "自我迭代状态" in txt
        assert "回复尽量简洁不刷屏" in txt

    asyncio.run(_run())


def test_ai_in_flight_verdict_discarded_after_reset(make_plugin):
    """验证 AI 裁判等待 LLM 返回期间若发生重置分数，在途判决不会回写污染清空后的榜单。"""

    async def _run():
        class _ResettingProvider(FakeProvider):
            def __init__(self, plugin_ref):
                super().__init__()
                self._plugin_ref = plugin_ref

            async def text_chat(self, prompt: str, system_prompt: str = ""):
                # 模拟 LLM 推理期间超级管理员重置了该群数据
                self._plugin_ref._group_epoch["1001"] = (
                    self._plugin_ref._group_epoch.get("1001", 0) + 1
                )
                with self._plugin_ref._tx() as db:
                    db.execute("DELETE FROM records WHERE gid='1001'")
                    db.execute("DELETE FROM scores WHERE gid='1001'")
                return type(
                    "Resp",
                    (),
                    {
                        "completion_text": json.dumps(
                            [{"id": 1, "delta": -1, "reason": "恶意辱骂", "watch": False}],
                            ensure_ascii=False,
                        )
                    },
                )()

        plugin, ctx = make_plugin()
        ctx.provider = _ResettingProvider(plugin)
        plugin._ai_enabled.add("1001")
        plugin._remember_session("1001", "aiocqhttp:GroupMessage:1001")
        plugin._msg_buffer["1001"] = [
            {"seq": 1, "uid": "201", "name": "张三", "text": "这是一句测试发言", "ts": time.time(), "mid": "m1"}
        ]

        await plugin._ai_tick_once()
        # 因为重置推进了 epoch，在途判决必须被丢弃，不能写入数据库或公示
        assert plugin._total_rows("1001") == []
        assert ctx.sent_messages == []

    asyncio.run(_run())
