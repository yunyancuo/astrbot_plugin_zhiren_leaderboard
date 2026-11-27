"""智人排行榜 — 纯逻辑层（不依赖 AstrBot，便于离线单元测试与复用）。"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

# 东八区（QQ 场景固定使用，无夏令时）
TZ_SH = timezone(timedelta(hours=8))

# 加/扣分关键词：加一分 / 扣一分 / 减一分 / 加1分 …
SCORE_KEYWORD_RE = re.compile(r"(?:加|扣|减)\s*[一1]\s*分")

# 记分指令宽泛匹配（用于过滤进入 AI 裁判缓冲，防止手动记分与 AI 判罚双重计分）
SCORE_ACTION_FILTER_RE = re.compile(r"(?:加|扣|减)\s*[一两二0-9]+\s*分")

# QQ 官方平台纯文本艾特标签：<@uid> 或 <@!uid>
QQ_MENTION_RE = re.compile(r"<@!?([0-9A-Za-z_-]{6,64})>")

# 周键格式：2026-W37
_WEEK_KEY_RE = re.compile(r"^(\d{4})-W(\d{2})$")

# Markdown JSON 代码块围栏
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

# 倒序榜单关键词
_REVERSE_RE = re.compile(r"倒序|逆序|垫底|从低到高")

# 控制字符（排除常规空白以便规整）
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 单条记分理由最大长度，防止超长文本刷屏或撑爆界面
MAX_REASON_LEN = 200

# Web API 查询参数 ID 允许字符（排除空白与控制字符）
_VALID_API_ID_RE = re.compile(r"^[^\x00-\x1f\x7f\s]{1,64}$")


def sanitize_reason(reason: str, max_len: int = MAX_REASON_LEN) -> str:
    """清洗并截断记分理由：去除控制字符、折叠换行并限制最大长度。"""
    cleaned = _CTRL_CHARS_RE.sub("", reason or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def extract_qq_mention_id(text: str, exclude: set[str] | None = None) -> str | None:
    """从纯文本中提取首个有效的 QQ 官方平台 `<@uid>` 艾特目标 ID。"""
    if not text:
        return None
    excluded = exclude or set()
    for match in QQ_MENTION_RE.finditer(text):
        uid = match.group(1)
        if uid not in excluded and uid.lower() != "all":
            return uid
    return None


def strip_qq_mentions(text: str) -> str:
    """移除纯文本中的 `<@uid>` 占位符并规范首尾空白。"""
    if not text:
        return ""
    return QQ_MENTION_RE.sub(" ", text).strip()


def escape_like(value: str) -> str:
    """转义 SQL LIKE 查询中的通配符（`\\`、`%`、`_`），配合 `ESCAPE '\\'` 使用。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_port(raw: str | None, default: int = 6201) -> int:
    """安全解析服务端口号（1~65535），无效值时回退至默认端口。"""
    if not raw:
        return default
    try:
        port = int(raw.strip())
    except ValueError:
        return default
    return port if 1 <= port <= 65535 else default


def parse_id_set(raw: str | list[Any] | tuple[Any, ...] | set[Any] | None) -> set[str]:
    """解析管理员 ID 集合，支持逗号/分号/空白分隔字符串或序列类型。"""
    if not raw:
        return set()
    if isinstance(raw, str):
        parts = re.split(r"[,;，；\s]+", raw.strip())
        return {p.strip() for p in parts if p.strip()}
    if isinstance(raw, (list, tuple, set)):
        out: set[str] = set()
        for item in raw:
            val = str(item or "").strip()
            if val:
                out.add(val)
        return out
    val = str(raw).strip()
    return {val} if val else set()


def is_valid_api_id(value: str, max_len: int = 64) -> bool:
    """校验 Web API 传入的 `gid` 或 `uid` 是否合法（非空、长度受控、无空白或控制字符）。"""
    if not value or len(value) > max_len:
        return False
    return bool(_VALID_API_ID_RE.fullmatch(value))


def parse_score_command(text: str) -> dict[str, Any] | None:
    """从纯文本中解析「@某成员 加/扣一分 理由」的指令部分。

    返回:
        `{"delta": 1 | -1, "reason": str, "no_reason": bool}` 命中关键词；
        `None` 未命中。
    """
    if not text:
        return None
    cleaned_text = strip_qq_mentions(text)
    match = SCORE_KEYWORD_RE.search(cleaned_text)
    if not match:
        return None
    delta = 1 if match.group(0).startswith("加") else -1
    raw_reason = cleaned_text[match.end() :].strip(" \t\r\n，,。.：:、！!？?~·…")
    reason = sanitize_reason(raw_reason)
    return {"delta": delta, "reason": reason, "no_reason": len(reason) == 0}


def parse_query_command(text: str) -> tuple[str, Any] | None:
    """解析 `@bot` 后的查询与管理指令。

    返回:
        - `("help", None)`                  使用说明
        - `("ai_status", None)`             AI 裁判状态
        - `("ai_toggle", bool)`             AI 裁判开关
        - `("evolve", "status" | "run")`    自我迭代状态 / 手动触发迭代
        - `("reset_confirm", None)`         确认重置本群数据
        - `("reset", None)`                 发起重置本群数据
        - `("ai_judge", str)`               AI 裁判专项裁决 / 开庭
        - `("weekly", None | "rev")`        本周排行（支持倒序）
        - `("total", None | "rev")`         总分排行（支持倒序）
        - `("detail", "me" | str)`          个人得分/详情
        - `None`                            未命中（不响应，避免干扰正常 LLM 对话）
    """
    t = strip_qq_mentions(text or "")
    if not t:
        return None

    if re.search(r"帮助|指令|命令|怎么用|help", t, re.I):
        return ("help", None)

    ai_match = re.search(r"AI裁判\s*(状态|开启|打开|开|关闭|关掉|关)?", t, re.I)
    if ai_match:
        action = ai_match.group(1)
        if not action or action == "状态":
            return ("ai_status", None)
        return ("ai_toggle", "关" not in action)

    if re.search(r"(?:自我)?迭代状态|查看(?:自我)?迭代|行为准则", t):
        return ("evolve", "status")
    if re.search(r"(?:立即|手动|触发|开始)迭代|^自我迭代$", t):
        return ("evolve", "run")

    if re.search(r"确认重置", t):
        return ("reset_confirm", None)
    if re.fullmatch(r"重置", t) or re.search(r"重置(?:分数|积分|排行|排行榜|数据)", t):
        return ("reset", None)

    judge_match = re.search(r"(?:评价|裁决|审判)\s*(.*)", t)
    if judge_match:
        return ("ai_judge", judge_match.group(1).strip())

    # 显式自查优先于「总分/本周」榜单关键词，避免「我的总分」「查我的总分」被截胡
    if re.fullmatch(
        r"(?:(?:查询|详情|查一查|查一下|查)\s*)?(?:我|自己)的?(?:总分|得分|分数|周分|本周得分|情况|详情)",
        t,
    ):
        return ("detail", "me")

    is_rev = bool(_REVERSE_RE.search(t))
    if re.search(r"本周|这周|周榜|周排行", t):
        return ("weekly", "rev" if is_rev else None)
    if (
        re.search(r"总分|总榜|总排行|累计", t)
        or t in ("排行榜", "排行", "榜")
        or (is_rev and re.search(r"排行|榜", t))
    ):
        return ("total", "rev" if is_rev else None)

    query_match = re.search(r"^(?:查询|详情|查一查|查一下|查)\s*(.*)$", t)
    if query_match:
        rest = query_match.group(1).strip()
        if not rest:
            return ("detail", "me")
        if re.fullmatch(r"(?:我|自己)的?(?:总分|得分|分数|周分|本周得分|情况|详情)?", rest):
            return ("detail", "me")
        return ("detail", rest)

    if re.fullmatch(r"我?的?(?:得分|分数|周分|本周得分|情况)", t):
        return ("detail", "me")

    return None


def _to_shanghai(dt: datetime | None) -> datetime:
    """将时间对象统一转换为东八区 aware datetime（naive 时间按东八区解释）。"""
    if dt is None:
        return datetime.now(TZ_SH)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_SH)
    return dt.astimezone(TZ_SH)


def week_key_of(dt: datetime | None = None) -> str:
    """返回指定时间（默认当前东八区时间）对应的 ISO 周键，如 `'2026-W37'`。"""
    sh = _to_shanghai(dt)
    year, week, _ = sh.isocalendar()
    return f"{year}-W{week:02d}"


def prev_week_key(dt: datetime | None = None) -> str:
    """返回指定时间上一周对应的 ISO 周键。"""
    sh = _to_shanghai(dt)
    return week_key_of(sh - timedelta(days=7))


def week_label(key: str) -> str:
    """将 `'2026-W37'` 格式化为 `'09.07 ~ 09.13'`（该 ISO 周的周一至周日）；非法周键原样返回。"""
    match = _WEEK_KEY_RE.match(key or "")
    if not match:
        return key
    try:
        monday = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        return key
    sunday = monday + timedelta(days=6)
    return f"{monday.month:02d}.{monday.day:02d} ~ {sunday.month:02d}.{sunday.day:02d}"


def seconds_until_next_post(
    now: datetime | None = None,
    weekday: int = 0,
    hour: int = 0,
    minute: int = 5,
) -> int:
    """计算距离下一次周榜播报（默认每周一 00:05 东八区）的剩余秒数（至少 1 秒）。"""
    sh_now = _to_shanghai(now)
    candidate = sh_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (weekday - sh_now.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= sh_now:
        candidate += timedelta(days=7)
    return max(1, int(math.ceil((candidate - sh_now).total_seconds())))


# ---------------- AI 裁判输出解析 ----------------


def _extract_first_json(text: str, opener: str, expected_type: type) -> Any:
    """使用 `JSONDecoder.raw_decode` 安全提取首个合法的指定类型 JSON 结构。

    优先解析 Markdown 代码块内容，随后回退至全文扫描，正确支持嵌套结构与字符串内括号。
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    sources = [m.group(1) for m in _CODE_FENCE_RE.finditer(text)]
    sources.append(text)
    for src in sources:
        pos = 0
        while True:
            idx = src.find(opener, pos)
            if idx == -1:
                break
            try:
                obj, _ = decoder.raw_decode(src, idx)
            except json.JSONDecodeError:
                pos = idx + 1
                continue
            if isinstance(obj, expected_type):
                return obj
            pos = idx + 1
    return None


def extract_json_array(text: str) -> list[Any]:
    """从 LLM 回复中提取第一个合法的 JSON 数组（容忍 Markdown 代码块、嵌套对象与前后缀文本）。"""
    data = _extract_first_json(text, "[", list)
    return data if isinstance(data, list) else []


def extract_json_object(text: str) -> dict[str, Any]:
    """从 LLM 回复中提取第一个合法的 JSON 对象（容忍嵌套数组/对象）。失败返回 `{}`。"""
    data = _extract_first_json(text, "{", dict)
    return data if isinstance(data, dict) else {}


def clamp_score(v: Any, limit: int = 3) -> int:
    """将 AI 输出的分数钳制到 `[-limit, +limit]` 范围内的整数，非法输入/布尔值/非有限数返回 `0`。"""
    if isinstance(v, bool) or v is None:
        return 0
    bound = max(0, int(limit))
    try:
        num = float(v)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(num):
        return 0
    return max(-bound, min(bound, int(round(num))))
