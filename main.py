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
import re
import sqlite3
import time
from datetime import datetime

from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star, register

from .zhiren_logic import (
    TZ_SH,
    clamp_score,
    extract_json_array,
    extract_json_object,
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
RANK_DISPLAY_N = 25           # 周榜/总榜显示人数（v1.4.0 起 15 → 25）
_DEDUPE_WINDOW = 10  # 秒：相同操作去重窗口

# ---------------- AI 裁判配置 ----------------
AI_TICK_INTERVAL = 2           # 监听循环节拍(秒)，发现新发言立即送审
AI_CONTEXT_N = 25              # 判决携带的上下文条数(短 prompt = 快响应)
AI_BUFFER_MAX = 60             # 单群上下文缓冲上限(条)
AI_MAX_JUDGMENTS = 8           # 每轮最多判罚条数
AI_SCORE_LIMIT = 1             # 单条判罚最大 ±1 分（扣分一律 1 分，禁止一次扣 2 分以上）
AI_JUDGE_HISTORY = 60          # 单人裁决上下文条数
AI_MIN_TEXT = 4                # 短于该长度的不进缓冲
SUPER_ADMIN = os.environ.get("ZHIREN_SUPER_ADMIN", "3468440670")  # 重置分数的超级管理员
# ---- 对抗学习（群友给机器人打分 → 自我迭代人格）----
BOT_EVOLVE_COOLDOWN = 600      # 自我迭代合成冷却(秒)
BOT_EVOLVE_MIN_NEW = 2         # 触发一轮迭代所需的新反馈条数
BOT_FEEDBACK_KEEP = 200        # 机器人评分反馈保留条数
POLICY_MARKER = "\n\n【自我迭代准则】"
BOT_POLICY_MAX_ITEMS = 15      # 准则条数上限（增量合并，超限不再新增）
BOT_POLICY_MAX_CHARS = 40      # 单条字数上限（要求通顺可读，禁止电报式缩写）
# 纯辱骂/无信息量的反馈理由：不进合成输入（池子里保留原始记录）
_NOISE_REASON_RE = re.compile(
    r"^(?:傻逼|滚|贱逼|草|nmsl|脑瘫|智障|没脑子|不是人|太迟钝|也中|垃圾|废物|"
    r"神经病|有病|若智|nt|逆天|离谱|无语)[了吧!！。.~～?？的了个你是我它他]*$"
)
AI_WATCH_RECHECK = 8           # 观察中的候选每隔 N 秒复议一次(有新发言时)
AI_WATCH_MAX = 300             # 观察上限(秒)，超时证据不足则放弃判罚
AI_SYSTEM_PROMPT = (
    "你是群聊积分系统『智人排行榜』的 AI 裁判，宗旨：公平、公正、公开，只依据发言文本判罚。"
    "你会看到带上下文的群聊记录。判罚必须综合两点证据："
    "①当事人自己的发言（同一人连续多条要整体看待，如连续刷脏话）；"
    "②其他群友的反应——被嘲笑、被集体声讨、被附和起哄都是重要判罚依据。\n"
    "记分标准（分数固定，不按情节加减）：\n"
    "1.【德行不足】判罚固定 -1：发言暴露人品问题，如放别人鸽子、爽约、甩锅、占便宜、"
    "背后嚼舌根、骗人等引起公愤的不良行为。\n"
    "2.【满嘴脏话】判罚固定 -1（高门槛，宁放过勿错判）：偶尔几句脏话、口头禅、玩梗式的脏字一律放过不判；"
    "只有连续刷脏话刷屏、大段辱骂、针对性人身攻击才判罚。\n"
    "3.【下头性暗示】判罚固定 -1（只判露骨，宁放过勿错判）：不那么露骨的玩梗黄腔、群友间玩笑一律放过不判；"
    "只有露骨下流的性骚发言、针对具体的人的持续骚扰才判罚。\n"
    "4.【智商掉线】判罚固定 -1：对极简单常识的迷惑发言，且明显引发群友集体嘲笑的（以群友回帖反应为准）。\n"
    "5.【其他逆天】判罚固定 -1：其余明显逆天、暴论、迷惑行为。\n"
    "硬性规则：\n"
    "· 每次判罚只能扣 1 分，绝对禁止一次扣 2 分及以上；同一事件无论多恶劣也只扣 1 分，情节轻重只体现在评语里。\n"
    "· 一条发言/一个事件只判一次，绝不重复扣分。\n"
    "· 作为判罚证据引用的群友回应（下文中的嘲笑、声讨、附和），即使言辞再激烈也不再对其单独扣分，"
    "除非该发言构成与本次事件完全无关的独立违规。\n"
    "· 大规模跟风辱骂多半是群友集体玩梗整活（人越多、越好笑越不是恶意），这种一律不扣分、一个都别判；"
    "只有少数人针对同一个具体受害者持续恶意辱骂才算【满嘴脏话】。\n"
    "· 给群友起昵称、起外号是群聊日常文化，本身绝不判罚——不算嚼舌根、不算下头、不算辱骂；"
    "起得巧妙、贴合人设、被群友接受并流传开来的，可按【金句神操作】或【幽默担当】给 +1。"
    "只有带人格侮辱性质且当事人明显反感的恶意绰号才可能构成判罚。\n"
    "· 机器人（群里的 bot）与群友一视同仁：它的逆天发言照常判罚，不要因为发言者是机器人就放过。\n"
    "· 加分同样每次只加 1 分，一条发言只加一次，绝不重复加分。\n"
    "加分标准（每项固定 +1，加分从宽：发言沾边就算命中，可给可不给时倾向给；纯灌水、硬凑的不给）：\n"
    "1.【金句神操作】+1：惊艳的金句、神回复、神级操作。\n"
    "2.【热心助人】+1：耐心解答群友问题、出手帮人解决实际困难（以问题被解决、被帮助者道谢为准）。\n"
    "3.【干货分享】+1：分享有价值的资源、教程、攻略、一手消息，群友明显受益（以感谢、追问、收藏反应为准）。\n"
    "4.【幽默担当】+1：真正好笑的段子或神回复，明显带活群里气氛（以群友大笑、复读、玩梗跟帖为准）。\n"
    "5.【救场调解】+1：争执升温时幽默化解矛盾、安抚双方，避免吵架升级。\n"
    "普通灌水、日常聊天一律 0 分不记；判罚拿不准一律放过（0 分），加分可给可不给时倾向给。"
    "评语不超过 15 字，要犀利、有梗、对事不对人。只输出 JSON。"
)


def _now() -> str:
    return datetime.now(TZ_SH).strftime("%Y-%m-%d %H:%M:%S")


@register(
    "astrbot_plugin_zhiren_leaderboard",
    "yunyancuo",
    "智人排行榜 — @成员加/扣一分记分、成员信息库、周榜自动播报、@bot 查询、内置 Web 排行榜界面",
    "1.7.1",
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
        self._msg_buffer: dict[str, list[dict]] = {}
        self._ai_warned: set[str] = set()
        self._seq: int = 0
        self._ai_judged: set[int] = set()
        self._ai_reviewed: set[int] = set()
        self._ai_watch: dict[str, list[dict]] = {}
        self._pending_reset: dict[str, float] = {}
        self._last_bot = None                # 最近一次消息携带的 OneBot 客户端(用于昵称治愈巡检)
        self._name_heal_ts: dict[tuple[str, str], float] = {}
        self._evolve_lock = asyncio.Lock()
        self._last_evolve_ts = 0.0
        self._self_id = ""                   # 机器人自身QQ号(不参与排行榜/判罚对象识别)
        _kv = self.db.execute("SELECT v FROM kv WHERE k='self_id'").fetchone()
        if _kv and _kv["v"]:
            self._self_id = _kv["v"]
        _pol = self.db.execute("SELECT updated_at FROM bot_policy WHERE id=1").fetchone()
        self._last_evolve_at = _pol["updated_at"] if _pol else ""

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
            CREATE TABLE IF NOT EXISTS ai_config(
                gid TEXT PRIMARY KEY, enabled INTEGER DEFAULT 0, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS bot_feedback(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gid TEXT, delta INTEGER, reason TEXT, operator_name TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS bot_policy(
                id INTEGER PRIMARY KEY CHECK (id=1),
                persona_id TEXT, base_prompt TEXT, policy_text TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS bot_policy_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                policy_text TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS kv(
                k TEXT PRIMARY KEY, v TEXT
            );
            """
        )
        self.db.commit()
        self._ai_enabled: set[str] = {
            r["gid"] for r in self.db.execute("SELECT gid FROM ai_config WHERE enabled=1")
        }

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

    async def _heal_member_name(self, gid: str, uid: str, bot=None) -> None:
        """昵称缺失/退化为QQ号时，通过 OneBot 拉群名片补全，并同步修补缓冲区与观察名单。"""
        bot = bot or self._last_bot
        if bot is None:
            return
        try:
            info = await bot.get_group_member_info(group_id=int(gid), user_id=int(uid))
        except Exception:
            return
        name = ((info or {}).get("card") or (info or {}).get("nickname") or "").strip()
        if not name or name == uid:
            return
        self._upsert_member(gid, uid, name)
        for m in self._msg_buffer.get(gid, []):
            if m["uid"] == uid:
                m["name"] = name
        for w in self._ai_watch.get(gid, []):
            if w["uid"] == uid:
                w["name"] = name
        logger.info(f"[智人排行榜] 已补全昵称: 群{gid} {uid} -> {name}")

    async def _name_heal_loop(self):
        """定期巡检：把 members 表里名字仍是QQ号/为空的成员治愈（QQ资料私密导致适配器退化）。"""
        while True:
            try:
                await asyncio.sleep(60)
                if self._last_bot is None:
                    continue
                rows = self.db.execute(
                    "SELECT gid, uid FROM members "
                    "WHERE name = uid OR TRIM(COALESCE(name,'')) = '' LIMIT 10"
                ).fetchall()
                now = time.time()
                for r in rows:
                    key = (r["gid"], r["uid"])
                    if now - self._name_heal_ts.get(key, 0) < 3600:
                        continue  # 一小时内试过就不再重试（真拿不到名片只能等）
                    self._name_heal_ts[key] = now
                    await self._heal_member_name(r["gid"], r["uid"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[智人排行榜] 昵称巡检异常: {e!r}")

    # ------------------------------------------------消息入口

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        gid = str(event.get_group_id() or "")
        if not gid:
            return

        self._remember_session(gid, event.unified_msg_origin)
        sender_uid = str(event.get_sender_id())
        sender_name = (event.get_sender_name() or "").strip()
        bot_ref = getattr(event, "bot", None)
        if bot_ref is not None:
            self._last_bot = bot_ref
        if sender_name and sender_name != sender_uid:
            self._upsert_member(gid, sender_uid, sender_name)
        else:
            # 昵称缺失或退化为QQ号：先记 QQ 号占位，异步拉群名片补全
            self._upsert_member(gid, sender_uid, sender_uid)
            asyncio.create_task(self._heal_member_name(gid, sender_uid, bot=bot_ref))
        self._remember_group(gid, event)

        comps = event.get_messages() or []
        self_id = str(event.get_self_id() or "")
        if self_id and self_id != self._self_id:
            self._self_id = self_id
            self.db.execute(
                "INSERT INTO kv(k,v) VALUES('self_id',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (self_id,)
            )
            self.db.commit()
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

        # 官方平台：群友互 @ 以 <@uid> 纯文本下发，组件层没有 At，从文本补解析
        if target_at is None and self_id:
            m = re.search(r"<@!?([0-9A-Za-z]{6,40})>", text)
            if m and m.group(1) not in (self_id, "all"):
                target_at = At(qq=m.group(1))

        # AI 裁判：非 @bot 的正常发言进入滚动上下文缓冲，持续监听
        if not bot_at and gid in self._ai_enabled:
            t_clean = text.strip()
            # 记分指令不进裁判缓冲：手动记分过的行为若再被 AI 判罚会双重计分
            if re.search(r"(加|扣|减)\s*[一两二0-9]+\s*分", t_clean):
                t_clean = ""
            if len(t_clean) >= AI_MIN_TEXT:
                t_clean = t_clean[:500]
                sender_uid = str(event.get_sender_id())
                buf = self._msg_buffer.setdefault(gid, [])
                if not buf or buf[-1]["uid"] != sender_uid or buf[-1]["text"] != t_clean:
                    self._seq += 1
                    buf.append({"seq": self._seq, "uid": sender_uid,
                                "name": sender_name or sender_uid, "text": t_clean,
                                "ts": time.time(),
                                "mid": str(getattr(event.message_obj, "message_id", "") or "")})
                    if len(buf) > AI_BUFFER_MAX:
                        for m in buf[: len(buf) - AI_BUFFER_MAX]:
                            self._ai_judged.discard(m["seq"])
                            self._ai_reviewed.discard(m["seq"])
                        del buf[: len(buf) - AI_BUFFER_MAX]

        if bot_at:
            parsed = parse_score_command(text)
            if parsed:
                # @bot 加/扣一分：允许给机器人记分；若同时 @ 了别人则给那个人记分
                score_target = target_at if target_at is not None else (
                    At(qq=int(self_id)) if self_id.isdigit() else At(qq=self_id)
                )
                async for r in self._handle_score(event, gid, text, score_target, parsed):
                    yield r
                return
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
            if not fetched:
                row = self.db.execute(
                    "SELECT name FROM members WHERE gid=? AND uid=?", (gid, target_uid)
                ).fetchone()
                fetched = (row[0] if row else None) or None
            target_name = fetched or f"QQ{target_uid}"
        self._upsert_member(gid, target_uid, target_name)

        delta = parsed["delta"]

        # 机器人不参与排行榜：打分仅作为对抗学习反馈，不进榜单
        if target_uid == str(event.get_self_id() or ""):
            self._record_bot_feedback(gid, delta, parsed["reason"], operator_name or "群友")
            logger.info(
                f"[智人排行榜] 群{gid} 机器人收到反馈 {delta:+d} "
                f"by {operator_name} 理由:{parsed['reason']}（不计入排行榜）"
            )
            yield event.chain_result(
                [
                    At(qq=int(target_uid) if target_uid.isdigit() else target_uid),
                    Plain(
                        f" 🧬 收到反馈：{'+' if delta > 0 else '-'}1\n"
                        f"理由：{parsed['reason']}\n"
                        f"（机器人不参与排行榜，此反馈已进入自我迭代）"
                    ),
                ]
            )
            return

        total_v = self._apply_delta(
            gid, operator_uid, operator_name, target_uid, target_name,
            delta, parsed["reason"],
        )

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

    # ------------------------------------------------分数入库（人工/AI 共用）

    def _apply_delta(self, gid: str, operator_uid: str, operator_name: str,
                     target_uid: str, target_name: str, delta: int, reason: str) -> int:
        """写入一条判罚记录并更新总分，返回该成员当前总分。"""
        self._upsert_member(gid, target_uid, target_name)
        self.db.execute(
            "INSERT INTO records(gid,week,operator_uid,operator_name,target_uid,target_name,delta,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (gid, week_key_of(), operator_uid, operator_name, target_uid, target_name,
             delta, reason, _now()),
        )
        self.db.execute(
            "INSERT INTO scores(gid,uid,total,plus,minus) VALUES(?,?,?,?,?) "
            "ON CONFLICT(gid,uid) DO UPDATE SET "
            "total=total+excluded.total, plus=plus+excluded.plus, minus=minus+excluded.minus",
            (gid, target_uid, delta, 1 if delta > 0 else 0, 1 if delta < 0 else 0),
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT total FROM scores WHERE gid=? AND uid=?", (gid, target_uid)
        ).fetchone()
        return row["total"] if row else delta

    # ------------------------------------------------对抗学习（自我迭代）

    def _record_bot_feedback(self, gid: str, delta: int, reason: str, operator_name: str) -> None:
        """任何来源（群友手动/AI裁判）对机器人的加减分都沉淀为迭代反馈。"""
        self.db.execute(
            "INSERT INTO bot_feedback(gid,delta,reason,operator_name,created_at) VALUES(?,?,?,?,?)",
            (gid, delta, reason, operator_name, _now()),
        )
        self.db.commit()
        asyncio.create_task(self._maybe_evolve(gid))

    async def _maybe_evolve(self, gid: str, force: bool = False) -> None:
        """冷却 + 新反馈达标才触发一轮迭代，避免每次打分都调 LLM；force=True 跳过检查。"""
        async with self._evolve_lock:
            try:
                if not force:
                    now = time.time()
                    if now - self._last_evolve_ts < BOT_EVOLVE_COOLDOWN:
                        return
                    row = self.db.execute(
                        "SELECT COUNT(*) AS c FROM bot_feedback WHERE created_at > ?",
                        (self._last_evolve_at or "",),
                    ).fetchone()
                    if row["c"] < BOT_EVOLVE_MIN_NEW:
                        return
                    self._last_evolve_ts = now
                await self._evolve_policy(gid)
                self._last_evolve_at = _now()
            except Exception as e:
                logger.warning(f"[智人排行榜] 自我迭代异常: {e!r}")

    async def _evolve_llm(self, gid: str, prompt: str, system_prompt: str) -> str:
        """合成准则的 LLM 调用：只用会话 Provider（中转站），失败重试一次。"""
        primary = self._provider_for(gid)
        if primary is None:
            return ""
        for attempt in range(2):
            try:
                resp = await primary.text_chat(prompt=prompt, system_prompt=system_prompt)
                text = (getattr(resp, "completion_text", "") or "").strip()
                if text:
                    if attempt:
                        logger.info("[智人排行榜] 自我迭代重试成功")
                    return text
            except Exception as e:
                logger.warning(f"[智人排行榜] 自我迭代调用失败(第{attempt + 1}次): {e!r}")
        return ""

    async def _evolve_policy(self, gid: str) -> None:
        """把群友对机器人的评分反馈综合成行为准则，注入人格 system_prompt。"""
        try:
            cfg = self.context.get_config()
            pid = ((cfg.get("agent_runner", {}).get("config", {})
                    .get("persona", {})) or {}).get("persona_id") or "default"
            pm = getattr(self.context, "persona_manager", None)
            if pm is None:
                return
            persona = await pm.get_persona(pid)
        except Exception as e:
            logger.debug(f"[智人排行榜] 读取人格失败，跳过自我迭代: {e!r}")
            return
        cur_prompt = persona.system_prompt or ""
        row = self.db.execute("SELECT * FROM bot_policy WHERE id=1").fetchone()
        base = (row["base_prompt"] if row and row["base_prompt"]
                else cur_prompt.split(POLICY_MARKER)[0].rstrip())
        prev = row["policy_text"] if row else ""
        fb = self.db.execute(
            "SELECT delta, reason FROM bot_feedback ORDER BY id DESC LIMIT 30"
        ).fetchall()
        if not fb:
            return        # 过滤纯辱骂/太短的噪声理由；相同(delta,理由)合并计数作为重复强调权重
        stats: dict[tuple[int, str], int] = {}
        for r in fb:
            reason = (r["reason"] or "").strip()
            if len(reason) < 4 or _NOISE_REASON_RE.fullmatch(reason):
                continue
            key = (int(r["delta"]), reason)
            stats[key] = stats.get(key, 0) + 1
        if not stats:
            return
        lines = []
        for (d, reason), n in sorted(stats.items(), key=lambda kv: -kv[1]):
            tag = f"（{n}人次，反复强调）" if n >= 2 else ""
            lines.append(f"{'+' if d > 0 else '-'}1｜{reason}{tag}")
        items = [ln[2:].strip() for ln in (prev or "").splitlines()
                 if ln.strip().startswith("· ")]
        cur = "\n".join(f"{i}. {it}" for i, it in enumerate(items, 1)) or "（暂无）"
        prompt = (
            "你是群聊机器人「酷狗」的行为准则维护器。群友通过加一分/扣一分给机器人打分："
            "+1 表示这种表现受欢迎，-1 表示这种表现让人不满。\n\n"
            "【现行行为准则】（带编号）\n" + cur + "\n\n"
            "【最近评分反馈】\n" + "\n".join(lines) + "\n\n"
            "请只输出增量修改，严格 JSON（不要 markdown 代码块）：\n"
            '{"add": ["新条目", ...], "replace": [[编号, "修订后条目"], ...], "delete": [编号, ...]}\n'
            "铁律：\n"
            "1. 未被反馈触及的编号不许出现在任何字段里——程序会原样保留它们；\n"
            "2. add=全新教训；replace=与现有条目冲突或需细化的反馈；delete=条目被反馈明确否定；\n"
            f"3. 每条不超过 {BOT_POLICY_MAX_CHARS} 字，通顺完整，严禁电报式缩写堆砌；\n"
            "4. 多人次反复强调的教训必须完整体现；矛盾的评价写成有分寸的拿捏，不一刀切；\n"
            "5. 若本轮反馈没有值得沉淀的教训，输出三个空字段。\n"
            "只输出 JSON。"
        )
        raw = await self._evolve_llm(gid, prompt, "你是行为准则维护器，只输出 JSON。")
        obj = extract_json_object(raw)
        if not obj:
            logger.warning("[智人排行榜] 自我迭代输出无法解析，本轮保持原准则")
            return
        dels: set[int] = set()
        for x in obj.get("delete") or []:
            try:
                dels.add(int(x))
            except (TypeError, ValueError):
                continue
        reps: dict[int, str] = {}
        for it in obj.get("replace") or []:
            if isinstance(it, (list, tuple)) and len(it) == 2:
                try:
                    reps[int(it[0])] = str(it[1]).strip()[:60]
                except (TypeError, ValueError):
                    continue
        adds = [str(a).strip()[:60] for a in (obj.get("add") or [])
                if 4 <= len(str(a).strip()) <= 60]
        new_items = []
        for i, old in enumerate(items, 1):
            if i in dels:
                continue
            new_items.append(reps.get(i, old))
        for a in adds:
            if len(new_items) >= BOT_POLICY_MAX_ITEMS:
                break
            new_items.append(a)
        if not new_items:
            return  # 安全兜底：不允许清空准则
        policy = "\n".join("· " + it for it in new_items[:BOT_POLICY_MAX_ITEMS])
        new_prompt = (base + POLICY_MARKER
                      + f"\n（由群友评分反馈自动迭代，最近更新 {_now()}）\n" + policy)
        try:
            await pm.update_persona(pid, system_prompt=new_prompt)
        except Exception as e:
            logger.warning(f"[智人排行榜] 更新人格失败: {e!r}")
            return
        if row and row["policy_text"]:
            self.db.execute(
                "INSERT INTO bot_policy_history(policy_text,updated_at) VALUES(?,?)",
                (row["policy_text"], row["updated_at"] or _now()),
            )
        self.db.execute(
            "INSERT INTO bot_policy(id,persona_id,base_prompt,policy_text,updated_at) VALUES(1,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET persona_id=excluded.persona_id, "
            "base_prompt=excluded.base_prompt, policy_text=excluded.policy_text, "
            "updated_at=excluded.updated_at",
            (pid, base, policy, _now()),
        )
        self.db.execute(
            "DELETE FROM bot_feedback WHERE id < "
            "(SELECT COALESCE(MAX(id),0) - ? FROM bot_feedback)",
            (BOT_FEEDBACK_KEEP,),
        )
        self.db.commit()
        logger.info(f"[智人排行榜] 自我迭代完成，人格「{pid}」准则已更新")
        srow = self.db.execute("SELECT umo FROM sessions WHERE gid=?", (gid,)).fetchone()
        if srow and srow["umo"]:
            try:
                await self.context.send_message(srow["umo"], MessageChain(chain=[Plain(
                    f"🧬 自我迭代完成：已吸收最近 {len(fb)} 条评分反馈并更新行为准则。"
                    "被夸的继续保持，被扣的我改。"
                )]))
            except Exception:
                pass

    # ------------------------------------------------AI 裁判

    def _provider_for(self, gid: str):
        row = self.db.execute("SELECT umo FROM sessions WHERE gid=?", (gid,)).fetchone()
        umo = row["umo"] if row else None
        try:
            return self.context.get_using_provider(umo)
        except Exception:
            return None

    @staticmethod
    def _transcript(batch: list[dict], judged: set[int] | None = None,
                    reviewed: set[int] | None = None,
                    target_uid: str | None = None) -> str:
        """渲染编号发言记录；已判的行标（已判），审过的标（已阅），目标成员标【目标】。"""
        judged = judged or set()
        reviewed = reviewed or set()
        lines = []
        for i, m in enumerate(batch):
            marks = ""
            if m.get("seq") in judged:
                marks += "（已判）"
            elif m.get("seq") in reviewed:
                marks += "（已阅）"
            tgt = "【目标】" if target_uid and m["uid"] == target_uid else ""
            lines.append(f"{i + 1}. {tgt}[{m['uid']}] {m['name']}：{m['text']}{marks}")
        return "\n".join(lines)

    def _judge_system_prompt(self) -> str:
        """裁判系统提示词 = 默认标准 + 迭代准则（群友评分校准的行为边界，优先级更高）。"""
        try:
            row = self.db.execute(
                "SELECT policy_text FROM bot_policy WHERE id=1"
            ).fetchone()
            policy = (row["policy_text"] if row else "").strip()
        except Exception:
            policy = ""
        if not policy:
            return AI_SYSTEM_PROMPT
        return (AI_SYSTEM_PROMPT
                + "\n\n【群友校准的行为边界（按评分反馈持续学习，优先级高于上述所有标准）】\n"
                + policy)

    async def _ai_score_messages(self, gid: str, context: list[dict]):
        """分诊：证据确凿 → 立即判罚；疑似逆天 → 进入观察等群友反应。
        返回 (immediate_verdicts, watch_candidates)。"""
        provider = self._provider_for(gid)
        if provider is None:
            if gid not in self._ai_warned:
                self._ai_warned.add(gid)
                logger.warning("[智人排行榜] 未配置 LLM Provider，AI 裁判不可用")
            return [], []
        prompt = (
            "以下是持续监听中的群聊最近记录（含上下文，格式：序号. [QQ号] 名字：内容）。\n"
            "标（已判）的发言已判罚过，绝对不要重复输出；"
            "标（已阅）的发言此前已审过，一律不要再输出。\n"
            "请依据判罚标准审查新发言，分两类输出：\n"
            '1. 证据确凿、直接判罚、无需观察的：连续脏话刷屏、大段辱骂、少数人针对同一受害者的跟风辱骂（如「傻逼XXX」）、露骨性骚发言（针对具体的人的下流骚扰）——输出：'
            '{"id": 序号, "delta": 判罚分数, "reason": "评语", "watch": false}。'
            "注意：大规模集体跟风辱骂是群友玩梗整活，全员放过、一个都不要输出；"
            "偶尔几句脏话、不露骨的玩梗黄腔、群友间玩笑也一律放过。"
            "delta 只能是 -1、0 或 +1：违规扣分一律 -1（禁止 -2/-3），命中加分项（金句/助人/干货/幽默/救场）一律 +1；"
            "判罚可判可不判时倾向放过，加分可加可不加时倾向 +1。"
            "已被引用为判罚证据的群友回应（下文）不要再判罚。\n"
            '2. 文本本身看不出的品行类疑似（如疑似放鸽子、甩锅、骗人，需要群友反应佐证）：'
            '{"id": 序号, "delta": 0, "reason": "疑似点(15字内)", "watch": true}，进入观察期\n'
            f"正常发言忽略。最多输出 {AI_MAX_JUDGMENTS} 条。只输出 JSON 数组，不要任何其他文字。\n\n"
            + self._transcript(context, self._ai_judged, self._ai_reviewed)
        )
        try:
            resp = await provider.text_chat(prompt=prompt, system_prompt=self._judge_system_prompt())
        except Exception as e:
            logger.warning(f"[智人排行榜] AI 裁判调用失败: {e!r}")
            return [], []
        raw = getattr(resp, "completion_text", "") or ""
        verdicts = []
        watch_new = []
        seen: set[str] = set()
        watched_uids = {w["uid"] for w in self._ai_watch.get(gid, [])}
        for item in extract_json_array(raw):
            if not isinstance(item, dict) or len(verdicts) + len(watch_new) >= AI_MAX_JUDGMENTS:
                break
            try:
                idx = int(item.get("id")) - 1
            except Exception:
                continue
            if not (0 <= idx < len(context)):
                continue
            m = context[idx]
            if m["seq"] in self._ai_judged or m["uid"] in seen:
                continue
            if m["uid"] == getattr(self, "_self_id", ""):
                continue  # 机器人不参与排行榜，不作为判罚对象（其发言仍可作上下文证据）
            reason = str(item.get("reason", "")).strip()[:60]
            if item.get("watch"):
                if m["uid"] in watched_uids or not reason:
                    continue
                seen.add(m["uid"])
                watch_new.append({"uid": m["uid"], "name": m["name"], "hint": reason,
                                  "since_ts": time.time(), "last_check_ts": 0.0})
                logger.info(f"[智人排行榜][AI] 群{gid} 进入观察: {m['name']}({m['uid']}) 疑点:{reason}")
                continue
            delta = clamp_score(item.get("delta"), AI_SCORE_LIMIT)
            if delta == 0 or not reason:
                continue
            seen.add(m["uid"])
            verdicts.append((m, delta, reason))
            self._ai_judged.add(m["seq"])
        for m in context:
            self._ai_reviewed.add(m["seq"])
        return verdicts, watch_new

    async def _ai_judge_member(self, gid: str, uid: str, name: str,
                               hint: str | None = None) -> list[tuple[dict, int, str]]:
        """对单个成员的近期言行（含完整上下文与群友反应）做裁决。返回空 = 证据仍不足。"""
        buf = self._msg_buffer.get(gid, [])
        if len(buf) < 2:
            return []
        provider = self._provider_for(gid)
        if provider is None:
            return []
        hint_line = f"此前初审判定疑似：{hint}。请重点验证群友反应是否支持该判定。\n" if hint else ""
        prompt = (
            f"以下是群聊上下文记录。请专注评估成员「{name}」(QQ:{uid}) 的近期言行"
            "（其发言已用【目标】标出），结合上下文与其他群友对其发言的反应（嘲笑、声讨、附和），给出最终裁决："
            f"delta 为 -{AI_SCORE_LIMIT}~+{AI_SCORE_LIMIT} 的整数（0=证据仍不足，继续观察；扣分一律 -1、加分一律 +1，禁止更重），"
            "总体口径：判罚从严从宽放过，加分从宽倾向给。"
            'reason 为不超过 15 字的评语。只输出 JSON：{"delta": 分数, "reason": "评语"}。\n'
            "注意：其他群友的回应只是判罚证据，不要对其回应发言另行扣分。\n\n"
            + hint_line
            + self._transcript(buf, target_uid=uid)
        )
        try:
            resp = await provider.text_chat(prompt=prompt, system_prompt=self._judge_system_prompt())
        except Exception as e:
            logger.warning(f"[智人排行榜] AI 裁判调用失败: {e!r}")
            return []
        obj = extract_json_object(getattr(resp, "completion_text", "") or "")
        if not obj:
            return []
        delta = clamp_score(obj.get("delta"), AI_SCORE_LIMIT)
        reason = str(obj.get("reason", "")).strip()[:60]
        if delta == 0 or not reason:
            return []
        target_last_mid = ""
        for mm in reversed(buf):
            if mm["uid"] == uid:
                target_last_mid = mm.get("mid", "")
                break
        anchor = dict(uid=uid, name=name, seq=-1, text="", mid=target_last_mid)
        return [(anchor, delta, reason)]

    def _mark_incident_judged(self, gid: str, uid: str,
                              since_ts: float | None = None) -> None:
        """终审落地后封账：目标本人缓冲区内的发言全部标记已判；
        观察期内其他群友的回应（被用作证据的下文）一并封账，防止同一事件被重复扣分。"""
        for m in self._msg_buffer.get(gid, []):
            if m["uid"] == uid or (since_ts is not None and m["ts"] >= since_ts):
                self._ai_judged.add(m["seq"])

    async def _announce(self, gid: str, verdicts: list[tuple[dict, int, str]], prefix: str):
        """逐条判决公示：用 QQ 原生引用指向被扣分的那句话。"""
        row = self.db.execute("SELECT umo FROM sessions WHERE gid=?", (gid,)).fetchone()
        if not row:
            return
        umo = row["umo"]
        for m, d, r in verdicts:
            total = self._apply_delta(gid, "ai", "AI裁判", m["uid"], m["name"], d, r)
            sign = "+" if d > 0 else ""
            logger.info(f"[智人排行榜][AI] 群{gid} {m['name']}({m['uid']}) {d:+d}分 评语:{r}")
            chain = []
            if m.get("mid"):
                chain.append(Reply(id=m["mid"]))
            chain.append(At(qq=int(m["uid"]) if m["uid"].isdigit() else m["uid"]))
            chain.append(Plain(
                f" {prefix}：{sign}{d} 分（现 {total} 分）\n评语：{r}"
            ))
            try:
                await self.context.send_message(umo, MessageChain(chain=chain))
            except Exception as e:
                logger.warning(f"[智人排行榜] AI 判决公示失败(群{gid}): {e!r}")

    async def _ai_flush_loop(self):
        """判断流程：持续监听 → 分诊（确凿立即判 / 疑似观察）→ 等群友反应 → LLM 认为可判即输出。"""
        while True:
            try:
                await asyncio.sleep(AI_TICK_INTERVAL)
                now = time.time()
                for gid in list(self._msg_buffer.keys()):
                    if gid not in self._ai_enabled:
                        self._msg_buffer.pop(gid, None)
                        self._ai_watch.pop(gid, None)
                        continue
                    buf = self._msg_buffer.get(gid) or []
                    watches = self._ai_watch.setdefault(gid, [])

                    # ① 观察中的候选：有新发言(群友反应)时复议，LLM 觉得能判就立即输出
                    for w in list(watches):
                        new_msgs = [m for m in buf if m["ts"] > w["last_check_ts"]]
                        if not new_msgs:
                            continue
                        if now - w["last_check_ts"] < AI_WATCH_RECHECK:
                            continue
                        w["last_check_ts"] = now
                        if now - w["since_ts"] > AI_WATCH_MAX:
                            watches.remove(w)
                            logger.info(f"[智人排行榜][AI] 群{gid} 对 {w['name']} 观察超时，证据不足放弃判罚")
                            continue
                        verdicts = await self._ai_judge_member(gid, w["uid"], w["name"], hint=w["hint"])
                        if verdicts:
                            watches.remove(w)
                            self._mark_incident_judged(gid, w["uid"], since_ts=w["since_ts"])
                            await self._announce(gid, verdicts, "🤖 AI 裁判判决（结合群友反应）")
                        # 返回空 = LLM 认为还需继续观察

                    # ② 分诊新发言：确凿立即判，疑似进入观察
                    has_new = any(
                        m["seq"] not in self._ai_judged and m["seq"] not in self._ai_reviewed
                        for m in buf
                    )
                    if not has_new:
                        continue
                    context = buf[-AI_CONTEXT_N:]
                    verdicts, watch_new = await self._ai_score_messages(gid, context)
                    watched_uids = {w["uid"] for w in watches}
                    for w in watch_new:
                        if w["uid"] not in watched_uids:
                            watches.append(w)
                    if verdicts:
                        await self._announce(gid, verdicts, "🤖 AI 裁判判决（证据确凿）")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[智人排行榜] AI 判决循环异常: {e!r}")
                await asyncio.sleep(5)


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

        if kind == "ai_toggle":
            if not event.is_admin():
                event.stop_event()
                yield event.plain_result("⛔ 只有管理员才能开关 AI 裁判")
                return
            enable = bool(arg)
            (self._ai_enabled.add if enable else self._ai_enabled.discard)(gid)
            self.db.execute(
                "INSERT INTO ai_config(gid,enabled,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(gid) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
                (gid, 1 if enable else 0, _now()),
            )
            self.db.commit()
            event.stop_event()
            yield event.plain_result(
                f"🤖 AI 裁判已{'开启' if enable else '关闭'}"
                + ("。现已持续监听群聊：依据德行/脏话/下头/智商/逆天标准，"
                   "结合上下文与群友反应，发现逆天证据即刻判罚并公示。"
                   "每次判罚固定扣 1 分，命中加分项固定加 1 分，同一事件只判一次，作为证据的群友回应不重复计分。"
                   if enable else "")
            )
            return

        if kind == "ai_status":
            event.stop_event()
            n = len(self._msg_buffer.get(gid, []))
            yield event.plain_result(
                f"🤖 AI 裁判：{'✅ 开启' if gid in self._ai_enabled else '❌ 关闭'}"
                f"（持续监听中，上下文 {n}/{AI_BUFFER_MAX} 条，发现逆天发言秒级判决）"
            )
            return

        if kind == "ai_judge":
            event.stop_event()
            if gid not in self._ai_enabled:
                yield event.plain_result("🤖 AI 裁判未开启（管理员发送：AI裁判 开）")
                return
            if target_at is not None:
                uid = str(target_at.qq)
                name = (getattr(target_at, "name", "") or "").strip() or f"QQ{uid}"
            elif arg in ("me", "我", "自己", "我的"):
                uid, name = str(event.get_sender_id()), event.get_sender_name()
            elif arg and not arg.isdigit():
                row = self.db.execute(
                    "SELECT uid,name FROM members WHERE gid=? AND name LIKE ? ORDER BY updated_at DESC LIMIT 1",
                    (gid, f"%{arg}%"),
                ).fetchone()
                if not row:
                    yield event.plain_result(f"😶 没有找到「{arg}」的发言记录")
                    return
                uid, name = row["uid"], row["name"]
            else:
                uid, name = "", ""
            if uid:
                verdicts = await self._ai_judge_member(gid, uid, name)
                if not verdicts:
                    yield event.plain_result(
                        f"🤖 对 {name} 的专项裁决：证据不足或发言记录太少，本轮不予判罚"
                    )
                    return
                m, d, r = verdicts[0]
                self._mark_incident_judged(gid, uid)
                total = self._apply_delta(gid, "ai", "AI裁判", m["uid"], m["name"], d, r)
                logger.info(f"[智人排行榜][AI] 群{gid} 专项裁决 {name}({uid}) {d:+d}分 评语:{r}")
                yield event.plain_result(
                    f"🤖 AI 裁判对 {name} 的专项裁决：{'+' if d > 0 else ''}{d} 分（现 {total} 分）\n评语：{r}"
                )
                return
            batch = self._msg_buffer.get(gid) or []
            unjudged = [m for m in batch if m["seq"] not in self._ai_judged
                             and m["seq"] not in self._ai_reviewed]
            if len(unjudged) < 2:
                yield event.plain_result("🤖 新发言不足，稍后再开庭")
                return
            verdicts, _watch_new = await self._ai_score_messages(gid, batch)
            if not verdicts:
                yield event.plain_result("🤖 本庭审议完毕：人人清白，无人被判罚")
                return
            lines = ["🤖 AI 裁判当庭宣判（公平 · 公正 · 公开）"]
            for m, d, r in verdicts:
                total = self._apply_delta(gid, "ai", "AI裁判", m["uid"], m["name"], d, r)
                lines.append(f"· {m['name']} {'+' if d > 0 else ''}{d} 分（现 {total} 分）｜ {r}")
            yield event.plain_result("\n".join(lines))
            return

        if kind == "reset":
            event.stop_event()
            if str(event.get_sender_id()) != SUPER_ADMIN:
                yield event.plain_result("⛔ 仅超级管理员可以重置分数")
                return
            row = self.db.execute("SELECT COUNT(*) AS c FROM records WHERE gid=?", (gid,)).fetchone()
            self._pending_reset[gid] = time.time() + 30
            yield event.plain_result(
                f"⚠️ 危险操作：将清空本群全部积分与判罚记录（共 {row['c']} 条）。\n"
                "确定请在 30 秒内发送：@我 确认重置"
            )
            return

        if kind == "reset_confirm":
            event.stop_event()
            if str(event.get_sender_id()) != SUPER_ADMIN:
                yield event.plain_result("⛔ 仅超级管理员可以重置分数")
                return
            if time.time() > self._pending_reset.get(gid, 0):
                yield event.plain_result("⏳ 重置确认已过期，请重新发送「重置分数」")
                return
            self._pending_reset.pop(gid, None)
            self.db.execute("DELETE FROM records WHERE gid=?", (gid,))
            self.db.execute("DELETE FROM scores WHERE gid=?", (gid,))
            self._ai_watch.pop(gid, None)
            self._msg_buffer.pop(gid, None)
            self.db.commit()
            yield event.plain_result("✅ 本群积分与判罚记录已全部重置，从零开始")
            return

        if kind == "evolve":
            event.stop_event()
            if arg == "status":
                row = self.db.execute("SELECT * FROM bot_policy WHERE id=1").fetchone()
                cnt = self.db.execute("SELECT COUNT(*) AS c FROM bot_feedback").fetchone()["c"]
                pos = self.db.execute(
                    "SELECT COUNT(*) AS c FROM bot_feedback WHERE delta>0"
                ).fetchone()["c"]
                lines = [
                    "🧬 自我迭代状态",
                    f"反馈池：{cnt} 条（+{pos} / -{cnt - pos}），保留最近 {BOT_FEEDBACK_KEEP} 条",
                    f"最近迭代：{row['updated_at'] if row else '尚未迭代过'}",
                ]
                if row and row["policy_text"]:
                    lines.append(f"当前行为准则：\n{row['policy_text']}")
                yield event.plain_result("\n".join(lines))
                return
            if not event.is_admin():
                yield event.plain_result("⛔ 只有管理员才能手动触发自我迭代")
                return
            cnt = self.db.execute("SELECT COUNT(*) AS c FROM bot_feedback").fetchone()["c"]
            if not cnt:
                yield event.plain_result("🧬 反馈池为空，先给机器人来几笔加减分（@机器人 加/扣一分 理由）")
                return
            yield event.plain_result("🧬 收到，开始综合评分反馈自我迭代（约半分钟）…")
            # 后台任务执行：生成器内 await 慢操作会被流水线取消，导致静默中断
            asyncio.create_task(self._maybe_evolve(gid, force=True))
            return

        if kind == "help":
            event.stop_event()
            yield event.plain_result(self._help_text())
            return

        if kind == "total":
            event.stop_event()
            yield event.plain_result(self._rank_text(gid, "total", reverse=(arg == "rev")))
            return

        if kind == "weekly":
            event.stop_event()
            yield event.plain_result(self._rank_text(gid, "weekly", reverse=(arg == "rev")))
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
        bot = getattr(self, "_self_id", "")
        sql = ("SELECT m.uid, m.name, s.total, s.plus, s.minus "
               "FROM scores s LEFT JOIN members m ON m.gid=s.gid AND m.uid=s.uid "
               "WHERE s.gid=?")
        args: list = [gid]
        if bot:  # 机器人不参与排行榜
            sql += " AND s.uid != ?"
            args.append(bot)
        sql += " ORDER BY s.total DESC, s.plus DESC LIMIT ?"
        args.append(limit)
        rows = self.db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def _weekly_rows(self, gid: str, wk: str, limit: int = 200) -> list[dict]:
        bot = getattr(self, "_self_id", "")
        sql = ("SELECT r.target_uid AS uid, COALESCE(m.name, MAX(r.target_name)) AS name, "
               "SUM(r.delta) AS delta, "
               "SUM(CASE WHEN r.delta>0 THEN 1 ELSE 0 END) AS plus, "
               "SUM(CASE WHEN r.delta<0 THEN 1 ELSE 0 END) AS minus "
               "FROM records r LEFT JOIN members m ON m.gid=r.gid AND m.uid=r.target_uid "
               "WHERE r.gid=? AND r.week=?")
        args: list = [gid, wk]
        if bot:
            sql += " AND r.target_uid != ?"
            args.append(bot)
        sql += " GROUP BY r.target_uid ORDER BY delta DESC, plus DESC LIMIT ?"
        args.append(limit)
        rows = self.db.execute(sql, args).fetchall()
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
        for i, r in enumerate(rows[:RANK_DISPLAY_N]):
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

    def _rank_text(self, gid: str, kind: str, reverse: bool = False) -> str:
        if kind == "weekly":
            wk = week_key_of()
            rows = self._weekly_rows(gid, wk)
            title = f"本周排行榜（{week_label(wk)}）"
        else:
            rows = self._total_rows(gid)
            title = "总分排行榜"
        if reverse:
            rows = list(reversed(rows))
            title += " · 倒序"
        body = self._fmt_rank(rows, "delta" if kind == "weekly" else "total", title)
        return body + "\n\n🌐 完整排行榜：https://yunyancuo.com/zhiren"

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
            "🏆 智人排行榜 使用说明\n"
            "🌐 完整排行榜：https://yunyancuo.com/zhiren\n\n"
            "记分（群里直接发）：\n"
            "· @某成员 加一分 理由\n"
            "· @某成员 扣一分 理由\n"
            "· @机器人 加/扣一分 理由 —— 给机器人反馈（不上榜，进自我迭代）\n"
            "· 迭代状态 —— 查看自我迭代反馈池与当前准则\n"
            "· 立即迭代 —— 管理员手动触发一轮自我迭代\n\n"
            "查询（@我 + 指令，前 25 名）：\n"
            "· 总分榜 / 总分 —— 总分排行榜\n"
            "· 周榜 / 本周排行 —— 本周排行榜\n"
            "· 榜单加「倒序」—— 从低到高看垫底\n"
            "· 查询 @某人（或 查询 名字）—— 个人详情\n"
            "· 我的得分 —— 查自己\n\n"
            "AI 裁判（管理员可开关）：\n"
            "· AI裁判 开 / 关 —— 开关自动判罚\n"
            "· AI裁判 状态 —— 查看状态\n"
            "· 重置分数 —— 清空本群全部积分与记录（仅超管）\n"
            "· 评价 @某人 —— 对其近期发言专项裁决\n"
            "· 评价 —— 立即开庭审议当前上下文\n"
            "开启后持续监听群聊（上下文 60 条，每分钟巡视），依据德行/脏话/下头/智商/逆天标准扣分、"
            "金句/助人/干货/幽默/救场加分，判罚并公示；加减分每次固定 ±1 分，同一事件只判一次，"
            "作为判罚证据的群友回应不重复计分\n\n"
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
            rows = self._weekly_rows(gid, pk, RANK_DISPLAY_N)
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
        self._tasks.append(asyncio.create_task(self._ai_flush_loop()))
        self._tasks.append(asyncio.create_task(self._name_heal_loop()))
        logger.info(
            f"[智人排行榜] 已加载，Web 界面: http://0.0.0.0:{WEB_PORT}，"
            f"AI 裁判已启用群数: {len(self._ai_enabled)}"
        )

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
