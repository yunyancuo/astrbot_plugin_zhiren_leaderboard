"""智人排行榜 — 纯逻辑层（不依赖 astrbot，便于本地单测）"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

# 东八区（QQ 场景固定使用，无夏令时）
TZ_SH = timezone(timedelta(hours=8))

# 加/扣分关键词：加一分 / 扣一分 / 减一分 / 加1分 …
SCORE_KEYWORD_RE = re.compile(r"(?:加|扣|减)\s*[一1]\s*分")

# 周键格式：2026-W37
_WEEK_KEY_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def parse_score_command(text: str) -> dict | None:
    """从纯文本中解析「@某成员 加/扣一分 理由」的指令部分。

    返回:
        {"delta": 1/-1, "reason": str, "no_reason": bool}  命中关键词
        None                                               未命中
    """
    if not text:
        return None
    m = SCORE_KEYWORD_RE.search(text)
    if not m:
        return None
    delta = 1 if m.group(0).startswith("加") else -1
    reason = text[m.end():].strip(" \t\r\n，,。.：:、！!？?~·…")
    return {"delta": delta, "reason": reason, "no_reason": len(reason) == 0}


def parse_query_command(text: str) -> tuple | None:
    """解析 @bot 后的查询指令。

    返回:
        ("weekly", None)              本周排行
        ("total", None)               总分排行
        ("detail", "me"|名字片段)     个人得分/详情
        ("help", None)                帮助
        None                          未命中（不响应，避免干扰 LLM 对话）
    """
    t = (text or "").strip()
    if not t:
        return None
    if re.search(r"帮助|指令|命令|怎么用|help", t, re.I):
        return ("help", None)
    m = re.search(r"AI裁判\s*(状态|开启|打开|开|关闭|关掉|关)?", t, re.I)
    if m:
        w = m.group(1)
        if not w or w == "状态":
            return ("ai_status", None)
        return ("ai_toggle", "关" not in w)
    if re.search(r"确认重置", t):
        return ("reset_confirm", None)
    if re.search(r"重置(?:分数|积分|排行|排行榜|数据)?", t):
        return ("reset", None)
    m = re.search(r"(?:评价|裁决|审判)\s*(.*)", t)
    if m:
        return ("ai_judge", m.group(1).strip())
    if re.search(r"本周|这周|周榜|周排行", t):
        return ("weekly", None)
    if re.search(r"总分|总榜|总排行|累计", t) or t in ("排行榜", "排行", "榜"):
        return ("total", None)
    m = re.search(r"^(?:查询|详情|查一查|查一下|查)\s*(.*)$", t)
    if m:
        rest = m.group(1).strip()
        if not rest:
            return ("detail", "me")
        if re.fullmatch(r"(我|自己)(的)?(总分|得分|分数|周分|情况|详情)?", rest):
            return ("detail", "me")
        return ("detail", rest)
    if re.fullmatch(r"我?的?(总分|得分|分数|周分|本周得分|总分榜|情况)", t):
        return ("detail", "me")
    return None


def week_key_of(dt: datetime | None = None) -> str:
    dt = dt.astimezone(TZ_SH) if dt else datetime.now(TZ_SH)
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def prev_week_key(dt: datetime | None = None) -> str:
    dt = dt.astimezone(TZ_SH) if dt else datetime.now(TZ_SH)
    return week_key_of(dt - timedelta(days=7))


def week_label(key: str) -> str:
    """'2026-W37' -> '09.07 ~ 09.13'（该 ISO 周的周一至周日）"""
    m = _WEEK_KEY_RE.match(key)
    if not m:
        return key
    monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    sunday = monday + timedelta(days=6)
    return f"{monday.month:02d}.{monday.day:02d} ~ {sunday.month:02d}.{sunday.day:02d}"


def seconds_until_next_post(now: datetime | None = None,
                            weekday: int = 0, hour: int = 0, minute: int = 5) -> int:
    """距离下一次周榜播报（默认每周一 00:05，即上周刚刚结束）的秒数。"""
    now = (now or datetime.now(TZ_SH)).astimezone(TZ_SH)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (weekday - now.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return max(1, int((candidate - now).total_seconds()))


# ---------------- AI 裁判输出解析 ----------------

_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*?\]")
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*?\}")


def extract_json_array(text: str) -> list:
    """从 LLM 回复中提取第一个 JSON 数组（容忍 markdown 代码块、前后缀文本）。"""
    if not text:
        return []
    m = _JSON_ARRAY_RE.search(text)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def extract_json_object(text: str) -> dict:
    """从 LLM 回复中提取第一个 JSON 对象。失败返回 {}。"""
    if not text:
        return {}
    m = _JSON_OBJ_RE.search(text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def clamp_score(v, limit: int = 3) -> int:
    """AI 给的分数钳制到 [-limit, +limit] 的整数。"""
    try:
        v = int(round(float(v)))
    except Exception:
        return 0
    return max(-limit, min(limit, v))
