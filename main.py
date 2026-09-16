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
_DEDUPE_WINDOW = 10  # 秒：相同操作去重窗口

# ---------------- AI 裁判配置 ----------------
AI_TICK_INTERVAL = 2           # 监听循环节拍(秒)，发现新发言立即送审
AI_CONTEXT_N = 25              # 判决携带的上下文条数(短 prompt = 快响应)
AI_BUFFER_MAX = 60             # 单群上下文缓冲上限(条)
AI_MAX_JUDGMENTS = 5           # 每轮最多判罚条数
AI_SCORE_LIMIT = 3             # 单条判罚最大 ±3 分
AI_JUDGE_HISTORY = 60          # 单人裁决上下文条数
AI_MIN_TEXT = 4                # 短于该长度的不进缓冲
AI_WATCH_RECHECK = 8           # 观察中的候选每隔 N 秒复议一次(有新发言时)
AI_WATCH_MAX = 600             # 观察上限(秒)，超时证据不足则放弃判罚
AI_SYSTEM_PROMPT = (
    "你是群聊积分系统『智人排行榜』的 AI 裁判，宗旨：公平、公正、公开，只依据发言文本判罚。"
    "你会看到带上下文的群聊记录。判罚必须综合两点证据："
    "①当事人自己的发言（同一人连续多条要整体看待，如连续刷脏话）；"
    "②其他群友的反应——被嘲笑、被集体声讨、被附和起哄都是重要判罚依据。\n"
    "扣分标准：\n"
    "1.【德行不足】-1~-3：发言暴露人品问题，如放别人鸽子、爽约、甩锅、占便宜、"
    "背后嚼舌根、骗人等引起公愤的不良行为；情节越严重、群友越愤怒扣得越多，引发集体声讨可直接 -3。\n"
    "2.【满嘴脏话】-2~-3（高门槛）：偶尔带脏字不算；同一人连续多条刷脏话、或大段辱骂、"
    "攻击性极强才扣分，刷得越凶扣得越重。\n"
    "3.【下头性暗示】-1~-3：令人不适的黄腔、性暗示、油腻骚扰发言，越下头扣越多。\n"
    "4.【智商掉线】-1~-2：对极简单常识的迷惑发言，且明显引发群友集体嘲笑的（以群友回帖反应为准）。\n"
    "5.【其他逆天】-1~-3：其余明显逆天、暴论、迷惑行为，自由裁量。\n"
    "另有：极少数惊艳的金句/神操作可 +1（难得才给，宁缺毋滥）；普通灌水、日常聊天一律 0 分不记。"
    "拿不准的一律 0 分。评语不超过 15 字，要犀利、有梗、对事不对人。只输出 JSON。"
)


def _now() -> str:
    return datetime.now(TZ_SH).strftime("%Y-%m-%d %H:%M:%S")


@register(
    "astrbot_plugin_zhiren_leaderboard",
    "yunyancuo",
    "智人排行榜 — @成员加/扣一分记分、成员信息库、周榜自动播报、@bot 查询、内置 Web 排行榜界面",
    "1.1.2",
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

        # AI 裁判：非 @bot 的正常发言进入滚动上下文缓冲，持续监听
        if not bot_at and gid in self._ai_enabled:
            t_clean = text.strip()
            if len(t_clean) >= AI_MIN_TEXT:
                t_clean = t_clean[:500]
                sender_uid = str(event.get_sender_id())
                buf = self._msg_buffer.setdefault(gid, [])
                if not buf or buf[-1]["uid"] != sender_uid or buf[-1]["text"] != t_clean:
                    self._seq += 1
                    buf.append({"seq": self._seq, "uid": sender_uid,
                                "name": event.get_sender_name(), "text": t_clean,
                                "ts": time.time()})
                    if len(buf) > AI_BUFFER_MAX:
                        for m in buf[: len(buf) - AI_BUFFER_MAX]:
                            self._ai_judged.discard(m["seq"])
                            self._ai_reviewed.discard(m["seq"])
                        del buf[: len(buf) - AI_BUFFER_MAX]

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
            "标（已阅）的发言此前审过，除非群友反应明显升级也不要再输出。\n"
            "请依据判罚标准审查新发言，分两类输出：\n"
            '1. 证据确凿、可直接判罚的（如连续脏话刷屏、大段辱骂）：'
            '{"id": 序号, "delta": 判罚分数, "reason": "评语", "watch": false}\n'
            '2. 疑似违规但需要群友反应佐证的（如疑似放鸽子、疑似引发嘲笑、疑似性暗示）：'
            '{"id": 序号, "delta": 0, "reason": "疑似点(15字内)", "watch": true}，该发言将进入观察期\n'
            f"正常发言忽略。最多输出 {AI_MAX_JUDGMENTS} 条。只输出 JSON 数组，不要任何其他文字。\n\n"
            + self._transcript(context, self._ai_judged, self._ai_reviewed)
        )
        try:
            resp = await provider.text_chat(prompt=prompt, system_prompt=AI_SYSTEM_PROMPT)
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
            f"delta 为 -{AI_SCORE_LIMIT}~+{AI_SCORE_LIMIT} 的整数（0=证据仍不足，继续观察），"
            'reason 为不超过 15 字的评语。只输出 JSON：{"delta": 分数, "reason": "评语"}。\n\n'
            + hint_line
            + self._transcript(buf, target_uid=uid)
        )
        try:
            resp = await provider.text_chat(prompt=prompt, system_prompt=AI_SYSTEM_PROMPT)
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
        anchor = dict(uid=uid, name=name, seq=-1, text="")
        return [(anchor, delta, reason)]

    async def _announce(self, gid: str, verdicts: list[tuple[dict, int, str]], prefix: str):
        applied = []
        for m, d, r in verdicts:
            total = self._apply_delta(gid, "ai", "AI裁判", m["uid"], m["name"], d, r)
            applied.append(f"· {m['name']} {'+' if d > 0 else ''}{d} 分（现 {total} 分）｜ {r}")
            logger.info(f"[智人排行榜][AI] 群{gid} {m['name']}({m['uid']}) {d:+d}分 评语:{r}")
        row = self.db.execute("SELECT umo FROM sessions WHERE gid=?", (gid,)).fetchone()
        if not row:
            return
        text = prefix + "\n" + "\n".join(applied)
        try:
            await self.context.send_message(row["umo"], MessageChain(chain=[Plain(text)]))
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
            "AI 裁判（管理员可开关）：\n"
            "· AI裁判 开 / 关 —— 开关自动判罚\n"
            "· AI裁判 状态 —— 查看状态\n"
            "· 评价 @某人 —— 对其近期发言专项裁决\n"
            "· 评价 —— 立即开庭审议当前上下文\n"
            "开启后持续监听群聊（上下文 60 条，每分钟巡视），依据德行/脏话/下头/智商/逆天标准，"
            "结合上下文与群友反应判罚 ±3 分并公示\n\n"
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
        self._tasks.append(asyncio.create_task(self._ai_flush_loop()))
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
