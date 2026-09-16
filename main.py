"""智人排行榜 — AstrBot 插件

群聊记分系统：
  · @某成员 加一分 理由  → +1 分
  · @某成员 扣一分 理由  → -1 分
  · 成员昵称实时入库，周榜每周一自动播报
  · @bot 查询 总分榜 / 周榜 / 某人详情
  · 内置 Web 排行榜界面（默认端口 6201）
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime

from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register

from .zhiren_logic import (
    TZ_SH,
    parse_query_command,
    parse_score_command,
    prev_week_key,
    seconds_until_next_post,
    week_key_of,
    week_label,
)

PLUGIN_NAME = "astrbot_plugin_zhiren_leaderboard"
DATA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "plugin_data", PLUGIN_NAME)
)
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
WEB_PORT = int(os.environ.get("ZHIREN_WEB_PORT", "6201"))

MEDALS = ["🥇", "🥈", "🥉"]
_DEDUPE_WINDOW = 10  # 秒：相同操作去重窗口


def _now() -> str:
    return datetime.now(TZ_SH).strftime("%Y-%m-%d %H:%M:%S")


@register(
    "astrbot_plugin_zhiren_leaderboard",
    "yunyancuo",
    "智人排行榜 — @成员加/扣一分记分、成员信息库、周榜自动播报、@bot 查询、内置 Web 排行榜界面",
    "1.0.2",
)
class ZhirenLeaderboardPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        os.makedirs(DATA_DIR, exist_ok=True)
        self.db = sqlite3.connect(
            os.path.join(DATA_DIR, "zhiren.db"), check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self._init_db()
        self._name_cache: dict[tuple[str, str], str] = {}
        self._dedupe: dict[tuple, float] = {}
        self._group_fetch_ts: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._runner: web.AppRunner | None = None
        self._index_cache: bytes | None = None
        self._index_mtime: float = 0.0

    # ------------------------------------------------数据库

    def _init_db(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS members(
                gid TEXT NOT NULL,
                uid TEXT NOT NULL,
                name TEXT DEFAULT '',
                updated_at TEXT,
                PRIMARY KEY(gid, uid)
            );
            CREATE TABLE IF NOT EXISTS scores(
                gid TEXT NOT NULL,
                uid TEXT NOT NULL,
                total INTEGER DEFAULT 0,
                plus INTEGER DEFAULT 0,
                minus INTEGER DEFAULT 0,
                PRIMARY KEY(gid, uid)
            );
            CREATE TABLE IF NOT EXISTS records(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gid TEXT, week TEXT,
                operator_uid TEXT, operator_name TEXT,
                target_uid TEXT, target_name TEXT,
                delta INTEGER, reason TEXT, created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_records_gw ON records(gid, week);
            CREATE TABLE IF NOT EXISTS groups(
                gid TEXT PRIMARY KEY, name TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions(
                gid TEXT PRIMARY KEY, umo TEXT, updated_at TEXT
            );
            """
        )
        self.db.commit()

    def _upsert_member(self, gid: str, uid: str, name: str) -> None:
        name = (name or "").strip() or f"QQ{uid}"
        key = (str(gid), str(uid))
        if self._name_cache.get(key) == name:
            return
        self._name_cache[key] = name
        self.db.execute(
            "INSERT INTO members(gid,uid,name,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(gid,uid) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
            (key[0], key[1], name, _now()),
        )
        self.db.commit()

    def _remember_session(self, gid: str, umo: str) -> None:
        self.db.execute(
            "INSERT INTO sessions(gid,umo,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(gid) DO UPDATE SET umo=excluded.umo, updated_at=excluded.updated_at",
            (gid, umo, _now()),
        )
        self.db.commit()

    def _remember_group(self, gid: str, event: AstrMessageEvent) -> None:
        """通过 OneBot 拉取群名（每个群最多 10 分钟刷新一次）。"""
        now = time.time()
        if now - self._group_fetch_ts.get(gid, 0) < 600:
            return
        self._group_fetch_ts[gid] = now
        asyncio.create_task(self._fetch_group_name(gid, event))

    async def _fetch_group_name(self, gid: str, event: AstrMessageEvent) -> None:
        try:
            bot = getattr(event, "bot", None)
            name = None
            if bot is not None:
                info = await bot.get_group_info(group_id=int(gid))
                name = (info or {}).get("group_name")
            if name:
                self.db.execute(
                    "INSERT INTO groups(gid,name,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(gid) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                    (gid, name, _now()),
                )
                self.db.commit()
        except Exception as e:
            logger.debug(f"[智人排行榜] 获取群名失败: {e}")

    async def _fetch_member_name(self, event: AstrMessageEvent, gid: str, uid: str) -> str | None:
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                return None
            info = await bot.get_group_member_info(group_id=int(gid), user_id=int(uid))
            return (info or {}).get("card") or (info or {}).get("nickname")
        except Exception as e:
            logger.debug(f"[智人排行榜] 获取成员名片失败: {e}")
            return None

    # ------------------------------------------------消息入口

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        gid = str(event.get_group_id() or "")
        if not gid:
            return

        self._remember_session(gid, event.unified_msg_origin)
        self._upsert_member(gid, str(event.get_sender_id()), event.get_sender_name())
        self._remember_group(gid, event)

        comps = event.get_messages() or []
        self_id = str(event.get_self_id() or "")
        bot_at = False
        target_at: At | None = None
        for c in comps:
            if isinstance(c, At):
                qq = str(c.qq)
                if qq == "all":
                    continue
                if self_id and qq == self_id:
                    bot_at = True
                elif target_at is None:
                    target_at = c

        text = "".join(getattr(c, "text", "") for c in comps if isinstance(c, Plain))

        if bot_at:
            async for r in self._handle_query(event, gid, text, target_at):
                yield r
            return

        if target_at is not None:
            parsed = parse_score_command(text)
            if parsed:
                async for r in self._handle_score(event, gid, text, target_at, parsed):
                    yield r

    # ------------------------------------------------记分

    async def _handle_score(self, event: AstrMessageEvent, gid: str, text: str,
                            target_at: At, parsed: dict):
        operator_uid = str(event.get_sender_id())
        operator_name = event.get_sender_name()
        target_uid = str(target_at.qq)

        if target_uid == operator_uid:
            yield event.plain_result("😅 不能给自己记分哦")
            return

        if parsed["no_reason"]:
            yield event.plain_result(
                "📝 请附上理由，例如：@某成员 扣一分 深夜炸鱼\n（没有理由可不能随便记分）"
            )
            return

        dedupe_key = (gid, target_uid, operator_uid, parsed["delta"], parsed["reason"])
        now_ts = time.time()
        if now_ts - self._dedupe.get(dedupe_key, 0) < _DEDUPE_WINDOW:
            return
        self._dedupe[dedupe_key] = now_ts
        if len(self._dedupe) > 2000:
            cutoff = now_ts - _DEDUPE_WINDOW * 10
            self._dedupe = {k: v for k, v in self._dedupe.items() if v > cutoff}

        target_name = (getattr(target_at, "name", "") or "").strip()
        if not target_name:
            fetched = await self._fetch_member_name(event, gid, target_uid)
            target_name = fetched or f"QQ{target_uid}"
        self._upsert_member(gid, target_uid, target_name)

        delta = parsed["delta"]
        wk = week_key_of()
        cur = self.db.execute(
            "INSERT INTO records(gid,week,operator_uid,operator_name,target_uid,target_name,delta,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (gid, wk, operator_uid, operator_name, target_uid, target_name,
             delta, parsed["reason"], _now()),
        )
        self.db.execute(
            "INSERT INTO scores(gid,uid,total,plus,minus) VALUES(?,?,?,?,?) "
            "ON CONFLICT(gid,uid) DO UPDATE SET "
            "total=total+excluded.total, plus=plus+excluded.plus, minus=minus+excluded.minus",
            (gid, target_uid, delta, 1 if delta > 0 else 0, 1 if delta < 0 else 0),
        )
        self.db.commit()

        total = self.db.execute(
            "SELECT total FROM scores WHERE gid=? AND uid=?", (gid, target_uid)
        ).fetchone()
        total_v = total["total"] if total else delta
        arrow = "📈" if delta > 0 else "📉"
        word = "加" if delta > 0 else "扣"
        logger.info(
            f"[智人排行榜] 群{gid} {target_name}({target_uid}) {word}1分 "
            f"by {operator_name} 理由:{parsed['reason']} -> {total_v}分"
        )
        yield event.chain_result(
            [
                At(qq=int(target_uid) if target_uid.isdigit() else target_uid),
                Plain(
                    f" {arrow} {word} 1 分！\n"
                    f"理由：{parsed['reason']}\n"
                    f"当前总分：{total_v} 分（操作人：{operator_name}）"
                ),
            ]
        )

    # ------------------------------------------------查询

    async def _handle_query(self, event: AstrMessageEvent, gid: str, text: str,
                            target_at: At | None):
        q = parse_query_command(text)
        if q is None:
            if target_at is not None:
                q = ("detail", str(target_at.qq))
            else:
                return

        kind, arg = q
        comps = event.get_messages() or []
        if target_at is not None and kind == "detail":
            arg = str(target_at.qq)

        if kind == "help":
            event.stop_event()
            yield event.plain_result(self._help_text())
            return

        if kind == "total":
            event.stop_event()
            yield event.plain_result(self._rank_text(gid, "total"))
            return

        if kind == "weekly":
            event.stop_event()
            yield event.plain_result(self._rank_text(gid, "weekly"))
            return

        # detail
        if arg == "me":
            uid = str(event.get_sender_id())
        elif arg and arg.isdigit():
            uid = arg
        elif target_at is not None:
            uid = str(target_at.qq)
        else:
            row = self.db.execute(
                "SELECT uid FROM members WHERE gid=? AND name LIKE ? ORDER BY updated_at DESC LIMIT 1",
                (gid, f"%{arg}%"),
            ).fetchone()
            if not row:
                return
            uid = row["uid"]
        event.stop_event()
        detail = self._member_detail_text(gid, uid)
        yield event.plain_result(detail or f"😶 暂未找到该成员的记录（QQ:{uid}）")

    # ------------------------------------------------数据查询（回复文本与 Web 共用）

    def _total_rows(self, gid: str, limit: int = 200) -> list[dict]:
        rows = self.db.execute(
            "SELECT m.uid, m.name, s.total, s.plus, s.minus "
            "FROM scores s LEFT JOIN members m ON m.gid=s.gid AND m.uid=s.uid "
            "WHERE s.gid=? ORDER BY s.total DESC, s.plus DESC LIMIT ?",
            (gid, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def _weekly_rows(self, gid: str, wk: str, limit: int = 200) -> list[dict]:
        rows = self.db.execute(
            "SELECT r.target_uid AS uid, COALESCE(m.name, MAX(r.target_name)) AS name, "
            "SUM(r.delta) AS delta, "
            "SUM(CASE WHEN r.delta>0 THEN 1 ELSE 0 END) AS plus, "
            "SUM(CASE WHEN r.delta<0 THEN 1 ELSE 0 END) AS minus "
            "FROM records r LEFT JOIN members m ON m.gid=r.gid AND m.uid=r.target_uid "
            "WHERE r.gid=? AND r.week=? GROUP BY r.target_uid "
            "ORDER BY delta DESC, plus DESC LIMIT ?",
            (gid, wk, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def _groups(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT g.gid, COALESCE(g.name,'') AS name, "
            "(SELECT COUNT(*) FROM records r WHERE r.gid=g.gid) AS cnt "
            "FROM groups g UNION "
            "SELECT DISTINCT gid, '', 0 FROM records WHERE gid NOT IN (SELECT gid FROM groups) "
            "ORDER BY cnt DESC"
        ).fetchall()
        out = []
        for r in rows:
            name = r["name"] or f"群 {r['gid']}"
            out.append({"gid": r["gid"], "name": name, "count": r["cnt"]})
        return out

    def _member_detail(self, gid: str, uid: str) -> dict | None:
        m = self.db.execute(
            "SELECT uid,name,updated_at FROM members WHERE gid=? AND uid=?", (gid, uid)
        ).fetchone()
        s = self.db.execute(
            "SELECT total,plus,minus FROM scores WHERE gid=? AND uid=?", (gid, uid)
        ).fetchone()
        if not m and not s:
            return None
        wk = week_key_of()
        wd = self.db.execute(
            "SELECT COALESCE(SUM(delta),0) AS d FROM records WHERE gid=? AND week=? AND target_uid=?",
            (gid, wk, uid),
        ).fetchone()["d"]
        records = self.db.execute(
            "SELECT operator_name,target_name,delta,reason,created_at FROM records "
            "WHERE gid=? AND target_uid=? ORDER BY id DESC LIMIT 20",
            (gid, uid),
        ).fetchall()
        name = (m["name"] if m else None) or (f"QQ{uid}")
        return {
            "uid": uid,
            "name": name,
            "total": s["total"] if s else 0,
            "plus": s["plus"] if s else 0,
            "minus": s["minus"] if s else 0,
            "week_delta": wd,
            "updated_at": m["updated_at"] if m else None,
            "records": [dict(r) for r in records],
        }

    # ------------------------------------------------回复文本

    @staticmethod
    def _fmt_rank(rows: list[dict], score_key: str, title: str) -> str:
        if not rows:
            return f"🏆 {title}\n\n（暂无记录，快去 @成员 加/扣分吧）"
        lines = [f"🏆 {title}", ""]
        for i, r in enumerate(rows[:15]):
            medal = MEDALS[i] if i < 3 else f"{i + 1}."
            v = r.get(score_key, 0) or 0
            plus = r.get("plus", 0) or 0
            minus = r.get("minus", 0) or 0
            sign = "+" if v > 0 else ""
            lines.append(
                f"{medal} {r.get('name') or 'QQ' + str(r.get('uid'))}  "
                f"「{sign}{v}」分（+{plus} / -{minus}）"
            )
        return "\n".join(lines)

    def _rank_text(self, gid: str, kind: str) -> str:
        if kind == "weekly":
            wk = week_key_of()
            return self._fmt_rank(self._weekly_rows(gid, wk), "delta",
                                  f"本周排行榜（{week_label(wk)}）")
        return self._fmt_rank(self._total_rows(gid), "total", "总分排行榜")

    def _member_detail_text(self, gid: str, uid: str) -> str | None:
        d = self._member_detail(gid, uid)
        if not d:
            return None
        wk = week_key_of()
        lines = [
            f"👤 {d['name']} 的得分详情",
            "",
            f"总分：{d['total']} 分（+{d['plus']} / -{d['minus']}）",
            f"本周（{week_label(wk)}）：{'+' if d['week_delta'] >= 0 else ''}{d['week_delta']} 分",
            "",
            "最近记录：",
        ]
        if not d["records"]:
            lines.append("（暂无）")
        for r in d["records"]:
            sign = "+" if r["delta"] > 0 else ""
            lines.append(
                f"· {r['created_at'][5:16]} {sign}{r['delta']} ｜ {r['reason']} ｜ by {r['operator_name']}"
            )
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return (
            "🏆 智人排行榜 使用说明\n\n"
            "记分（群里直接发）：\n"
            "· @某成员 加一分 理由\n"
            "· @某成员 扣一分 理由\n\n"
            "查询（@我 + 指令）：\n"
            "· 总分榜 / 总分 —— 总分排行榜\n"
            "· 周榜 / 本周排行 —— 本周排行榜\n"
            "· 查询 @某人（或 查询 名字）—— 个人详情\n"
            "· 我的得分 —— 查自己\n\n"
            f"网页排行榜：http://<服务器IP>:{WEB_PORT}"
        )

    # ------------------------------------------------每周自动播报

    async def _weekly_loop(self):
        while True:
            try:
                delay = seconds_until_next_post()
                logger.info(f"[智人排行榜] 下次周榜播报约 {delay // 3600} 小时后")
                await asyncio.sleep(delay)
                await self._post_weekly()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[智人排行榜] 周榜任务异常: {e!r}")
                await asyncio.sleep(600)

    async def _post_weekly(self):
        pk = prev_week_key()
        title = f"上周排行榜（{week_label(pk)}）"
        sessions = self.db.execute("SELECT gid, umo FROM sessions").fetchall()
        for s in sessions:
            gid = s["gid"]
            rows = self._weekly_rows(gid, pk, 15)
            if not rows:
                continue
            text = self._fmt_rank(rows, "delta", title + "\n（每周一自动播报）")
            try:
                await self.context.send_message(s["umo"], MessageChain(chain=[Plain(text)]))
                logger.info(f"[智人排行榜] 已向群 {gid} 播报周榜")
            except Exception as e:
                logger.warning(f"[智人排行榜] 周榜播报失败(群{gid}): {e!r}")

    # ------------------------------------------------Web 界面

    async def _http_index(self, request: web.Request) -> web.Response:
        path = os.path.join(WEB_DIR, "index.html")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        if self._index_cache is None or mtime != self._index_mtime:
            with open(path, "rb") as f:
                self._index_cache = f.read()
            self._index_mtime = mtime
        return web.Response(body=self._index_cache, content_type="text/html", charset="utf-8")

    async def _http_groups(self, request: web.Request) -> web.Response:
        return web.json_response({"groups": self._groups()})

    async def _http_data(self, request: web.Request) -> web.Response:
        gid = request.query.get("gid", "")
        if not gid:
            g = self._groups()
            gid = g[0]["gid"] if g else ""
        wk = week_key_of()
        return web.json_response(
            {
                "gid": gid,
                "week_key": wk,
                "week_label": week_label(wk),
                "updated_at": _now(),
                "total": self._total_rows(gid),
                "weekly": self._weekly_rows(gid, wk),
            }
        )

    async def _http_member(self, request: web.Request) -> web.Response:
        gid = request.query.get("gid", "")
        uid = request.query.get("uid", "")
        d = self._member_detail(gid, uid) if gid and uid else None
        return web.json_response(d or {})

    # ------------------------------------------------生命周期

    async def initialize(self):
        app = web.Application()
        app.router.add_get("/", self._http_index)
        app.router.add_get("/api/groups", self._http_groups)
        app.router.add_get("/api/data", self._http_data)
        app.router.add_get("/api/member", self._http_member)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", WEB_PORT)
        await site.start()
        self._tasks.append(asyncio.create_task(self._weekly_loop()))
        logger.info(f"[智人排行榜] 已加载，Web 界面: http://0.0.0.0:{WEB_PORT}")

    async def terminate(self):
        for t in self._tasks:
            t.cancel()
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
        try:
            self.db.close()
        except Exception:
            pass
        logger.info("[智人排行榜] 已卸载")
