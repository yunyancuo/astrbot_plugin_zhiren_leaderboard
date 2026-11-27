"""核心业务、权限安全、数据库原子性与周榜播报集成测试。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import yaml
from conftest import (
    FakeAt,
    FakeEvent,
    FakeOneBot,
    FakePlain,
    collect_responses,
    response_text,
)


def test_metadata_yaml_valid():
    meta_path = Path(__file__).resolve().parent.parent / "metadata.yaml"
    data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    assert data["name"] == "astrbot_plugin_zhiren_leaderboard"
    assert data["version"]
    assert data["author"]


def test_scoring_flow_dedupe_isolation_and_bot_feedback(make_plugin):
    async def _run():
        plugin, _ctx = make_plugin(plugin_config={"public_url": "https://example.com/zhiren"})

        # 1. 禁止给自己记分
        e_self = FakeEvent(
            gid="1001",
            sender_id="101",
            sender_name="小明",
            components=[FakeAt(qq=101, name="小明"), FakePlain(" 加一分 自夸")],
        )
        r_self = await collect_responses(plugin, e_self)
        assert "不能给自己记分" in response_text(r_self[0])

        # 2. 必须附理由
        e_no_reason = FakeEvent(
            gid="1001",
            sender_id="101",
            sender_name="小明",
            components=[FakeAt(qq=102, name="小红"), FakePlain(" 扣一分")],
        )
        r_no = await collect_responses(plugin, e_no_reason)
        assert "请附上理由" in response_text(r_no[0])

        # 3. 正常加分与 10 秒去重
        e_ok = FakeEvent(
            gid="1001",
            sender_id="101",
            sender_name="小明",
            components=[FakeAt(qq=102, name="小红"), FakePlain(" 加一分 帮忙定位线上问题")],
        )
        r_ok1 = await collect_responses(plugin, e_ok)
        r_ok2 = await collect_responses(plugin, e_ok)
        assert "加 1 分" in response_text(r_ok1[0])
        assert "当前总分：1 分" in response_text(r_ok1[0])
        assert r_ok2 == []

        # 4. 群隔离：群 1002 记分不影响群 1001
        e_g2 = FakeEvent(
            gid="1002",
            sender_id="101",
            sender_name="小明",
            components=[FakeAt(qq=102, name="小红"), FakePlain(" 扣一分 开会迟到")],
        )
        await collect_responses(plugin, e_g2)
        assert plugin._total_rows("1001")[0]["total"] == 1
        assert plugin._total_rows("1002")[0]["total"] == -1

        # 5. 给机器人记分 -> 进反馈池，不进排行榜
        e_bot = FakeEvent(
            gid="1001",
            sender_id="101",
            sender_name="小明",
            self_id="99999",
            components=[FakeAt(qq=99999), FakePlain(" 扣一分 回复太啰嗦了")],
        )
        r_bot = await collect_responses(plugin, e_bot)
        assert "不参与排行榜" in response_text(r_bot[0])
        assert all(row["uid"] != "99999" for row in plugin._total_rows("1001"))
        fb_cnt = plugin.db.execute("SELECT COUNT(*) AS c FROM bot_feedback").fetchone()["c"]
        assert fb_cnt == 1

        # 6. 总分榜正序/倒序与外链配置展示
        plugin._apply_delta("1001", "101", "小明", "103", "老王", -1, "放鸽子")
        e_total = FakeEvent(
            gid="1001",
            sender_id="101",
            self_id="99999",
            components=[FakeAt(qq=99999), FakePlain(" 总分榜")],
        )
        r_total = await collect_responses(plugin, e_total)
        txt_total = response_text(r_total[0])
        assert txt_total.index("小红") < txt_total.index("老王")
        assert "https://example.com/zhiren" in txt_total

        e_rev = FakeEvent(
            gid="1001",
            sender_id="101",
            self_id="99999",
            components=[FakeAt(qq=99999), FakePlain(" 总分榜 倒序")],
        )
        r_rev = await collect_responses(plugin, e_rev)
        txt_rev = response_text(r_rev[0])
        assert "倒序" in txt_rev
        assert txt_rev.index("老王") < txt_rev.index("小红")

    asyncio.run(_run())


def test_qq_official_bot_openid_and_groups_count(make_plugin):
    """验证 QQ 官方机器人非数字 OpenID 完整记分、查询及群列表计数准确（非 0）。"""

    async def _run():
        bot = FakeOneBot()
        plugin, _ctx = make_plugin()
        gid = "GROUP_OPENID_A1B2C3D4"
        uid = "USER_OPENID_99887766"

        e = FakeEvent(
            gid=gid,
            sender_id="USER_OPENID_11223344",
            sender_name="官方群友",
            self_id="BOT_OPENID_000001",
            components=[FakePlain(f"<@{uid}> 加一分 分享好用的工具")],
            bot=bot,
        )
        res = await collect_responses(plugin, e)
        assert len(res) == 1
        assert f"<@{uid}>" in response_text(res[0])
        assert "加 1 分" in response_text(res[0])
        # 非数字 ID 不应调用 OneBot 整型群/成员接口
        assert bot.group_info_calls == []
        assert bot.member_info_calls == []

        # 修复验证：无 groups 表记录的群也必须统计真实记录数，而非 0
        groups = plugin._groups()
        assert groups == [{"gid": gid, "name": f"群 {gid}", "count": 1}]

    asyncio.run(_run())


def test_super_admin_authorization_and_reset_confirmation(make_plugin, monkeypatch):
    """验证无硬编码个人 QQ 号、分层超管鉴权、二次确认与 30 秒超时保护。"""

    async def _run():
        # 模式 A：未显式配置 ZHIREN_SUPER_ADMIN 时，回退至 AstrBot admins_id
        plugin, _ctx = make_plugin(ctx_config={"admins_id": ["astrbot", "888001"]})
        plugin._apply_delta("1001", "101", "小明", "102", "小红", 1, "表现好")

        # 历史上曾硬编码过的 QQ 号不应拥有后门权限
        e_legacy = FakeEvent(
            gid="1001",
            sender_id="3468440670",
            self_id="99999",
            components=[FakeAt(qq=99999), FakePlain(" 重置分数")],
        )
        r_legacy = await collect_responses(plugin, e_legacy)
        assert "仅超级管理员" in response_text(r_legacy[0])

        # AstrBot 配置的管理员允许发起重置
        e_reset = FakeEvent(
            gid="1001",
            sender_id="888001",
            self_id="99999",
            components=[FakeAt(qq=99999), FakePlain(" 重置分数")],
        )
        r_reset = await collect_responses(plugin, e_reset)
        assert "30 秒内发送" in response_text(r_reset[0])

        # 模拟确认超时
        plugin._pending_reset["1001"] = time.time() - 1
        e_confirm = FakeEvent(
            gid="1001",
            sender_id="888001",
            self_id="99999",
            components=[FakeAt(qq=99999), FakePlain(" 确认重置")],
        )
        r_expired = await collect_responses(plugin, e_confirm)
        assert "已过期" in response_text(r_expired[0])
        assert len(plugin._total_rows("1001")) == 1

        # 重新发起并确认重置
        await collect_responses(plugin, e_reset)
        old_epoch = plugin._group_epoch.get("1001", 0)
        r_done = await collect_responses(plugin, e_confirm)
        assert "已全部重置" in response_text(r_done[0])
        assert plugin._total_rows("1001") == []
        assert plugin._group_epoch["1001"] == old_epoch + 1

        # 模式 B：显式环境变量 ZHIREN_SUPER_ADMIN 优先（支持逗号分隔多超管）
        monkeypatch.setenv("ZHIREN_SUPER_ADMIN", "777001,777002")
        assert plugin._is_super_admin(FakeEvent(sender_id="777002")) is True
        assert plugin._is_super_admin(FakeEvent(sender_id="888001")) is False

    asyncio.run(_run())


def test_sqlite_wal_like_escape_and_missing_member_fallback(make_plugin):
    """验证 WAL 模式、LIKE 通配符转义与 members 缺失时 uid 回退。"""

    async def _run():
        plugin, _ctx = make_plugin()
        mode = plugin.db.execute("PRAGMA journal_mode;").fetchone()[0]
        assert str(mode).lower() == "wal"

        # 构造 scores 存在但 members 缺失的孤立记录
        with plugin._tx() as db:
            db.execute("INSERT INTO scores(gid,uid,total,plus,minus) VALUES('g1','555',3,3,0)")
        rows = plugin._total_rows("g1")
        assert rows[0]["uid"] == "555"
        assert rows[0]["name"] == "QQ555"

        # LIKE 通配符 % / _ 不能误匹配普通成员
        plugin._upsert_member("g1", "201", "普通成员")
        e_wild = FakeEvent(
            gid="g1",
            sender_id="202",
            self_id="99999",
            components=[FakeAt(qq=99999), FakePlain(" 查询 %")],
        )
        r_wild = await collect_responses(plugin, e_wild)
        assert r_wild == []

    asyncio.run(_run())


def test_weekly_broadcast_idempotency(make_plugin, plugin_modules):
    """验证周榜播报同一周只播报一次（幂等防重复发送）。"""
    logic, _ = plugin_modules

    async def _run():
        plugin, ctx = make_plugin()
        pk = logic.prev_week_key()
        plugin._remember_session("1001", "aiocqhttp:GroupMessage:1001")
        plugin._remember_session("1002", "aiocqhttp:GroupMessage:1002")
        with plugin._tx() as db:
            db.execute(
                "INSERT INTO records(gid,week,operator_uid,operator_name,target_uid,"
                "target_name,delta,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("1001", pk, "1", "甲", "2", "乙", 1, "优秀", "2026-09-01 12:00:00"),
            )

        sent1 = await plugin._post_weekly(week_key=pk)
        sent2 = await plugin._post_weekly(week_key=pk)
        assert sent1 == 1
        assert sent2 == 0
        assert len(ctx.sent_messages) == 1
        assert ctx.sent_messages[0][0] == "aiocqhttp:GroupMessage:1001"

    asyncio.run(_run())
