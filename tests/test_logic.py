"""纯逻辑层 `zhiren_logic.py` 全量单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


def test_parse_score_command_variants(plugin_modules):
    logic, _ = plugin_modules

    assert logic.parse_score_command("加一分 帮大家修好了线上 Bug") == {
        "delta": 1,
        "reason": "帮大家修好了线上 Bug",
        "no_reason": False,
    }
    assert logic.parse_score_command("扣 1 分，深夜炸鱼") == {
        "delta": -1,
        "reason": "深夜炸鱼",
        "no_reason": False,
    }
    assert logic.parse_score_command("减一分：放鸽子") == {
        "delta": -1,
        "reason": "放鸽子",
        "no_reason": False,
    }
    assert logic.parse_score_command("加一分！！！") == {
        "delta": 1,
        "reason": "",
        "no_reason": True,
    }
    assert logic.parse_score_command("今天天气不错") is None
    assert logic.parse_score_command("") is None


def test_parse_score_command_strips_qq_mention_and_truncates_reason(plugin_modules):
    logic, _ = plugin_modules

    res = logic.parse_score_command("<@ABCDEF123456> 扣一分  无故爽约\n还甩锅")
    assert res == {"delta": -1, "reason": "无故爽约 还甩锅", "no_reason": False}

    long_reason = "很长的理由" * 100
    res_long = logic.parse_score_command(f"加一分 {long_reason}")
    assert res_long is not None
    assert len(res_long["reason"]) == logic.MAX_REASON_LEN


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("今天吃什么", None),
        ("原神深渊什么时候重置", None),
        ("帮助", ("help", None)),
        ("HELP", ("help", None)),
        ("AI裁判", ("ai_status", None)),
        ("ai裁判 状态", ("ai_status", None)),
        ("AI裁判 开", ("ai_toggle", True)),
        ("AI裁判 关闭", ("ai_toggle", False)),
        ("迭代状态", ("evolve", "status")),
        ("自我迭代状态", ("evolve", "status")),
        ("查看迭代", ("evolve", "status")),
        ("立即迭代", ("evolve", "run")),
        ("手动迭代", ("evolve", "run")),
        ("重置", ("reset", None)),
        ("重置分数", ("reset", None)),
        ("确认重置", ("reset_confirm", None)),
        ("评价 小明", ("ai_judge", "小明")),
        ("评价", ("ai_judge", "")),
        ("周榜", ("weekly", None)),
        ("本周排行", ("weekly", None)),
        ("周榜 倒序", ("weekly", "rev")),
        ("倒序周榜", ("weekly", "rev")),
        ("总分", ("total", None)),
        ("总分榜", ("total", None)),
        ("排行榜", ("total", None)),
        ("总分榜 倒序", ("total", "rev")),
        ("倒序总榜", ("total", "rev")),
        ("垫底榜", ("total", "rev")),
        ("我的得分", ("detail", "me")),
        ("我的总分", ("detail", "me")),
        ("查我的总分", ("detail", "me")),
        ("查询 我的得分", ("detail", "me")),
        ("查询", ("detail", "me")),
        ("查询 张三", ("detail", "张三")),
        ("查询 <@ABCDEF123456>", ("detail", "me")),
    ],
)
def test_parse_query_command_matrix(plugin_modules, text: str, expected):
    logic, _ = plugin_modules
    assert logic.parse_query_command(text) == expected


def test_week_key_label_and_timezone_boundaries(plugin_modules):
    logic, _ = plugin_modules

    # UTC 2026-09-06 16:30 等于东八区 2026-09-07 00:30（周一，进入 W37）
    utc_dt = datetime(2026, 9, 6, 16, 30, tzinfo=timezone.utc)
    assert logic.week_key_of(utc_dt) == "2026-W37"
    assert logic.prev_week_key(utc_dt) == "2026-W36"
    assert logic.week_label("2026-W37") == "09.07 ~ 09.13"

    # naive datetime 固定按东八区解释，不受系统时区影响
    naive_dt = datetime(2026, 9, 7, 0, 30)
    assert logic.week_key_of(naive_dt) == "2026-W37"

    # 跨年 ISO 周边界与非法周键容错
    assert logic.week_key_of(datetime(2026, 1, 1, 12, 0, tzinfo=logic.TZ_SH)) == "2026-W01"
    assert logic.week_label("2026-W99") == "2026-W99"
    assert logic.week_label("not-a-week") == "not-a-week"


def test_seconds_until_next_post_edges(plugin_modules):
    logic, _ = plugin_modules

    # 周一 00:04:58.500 -> 向上取整应为 2 秒，防止提前醒来
    before = datetime(2026, 9, 7, 0, 4, 58, 500_000, tzinfo=logic.TZ_SH)
    assert logic.seconds_until_next_post(before) == 2

    # 刚好周一 00:05:00 -> 滚到下周一（整 7 天）
    exact = datetime(2026, 9, 7, 0, 5, 0, tzinfo=logic.TZ_SH)
    assert logic.seconds_until_next_post(exact) == 7 * 86400

    # 周日 23:55:00 -> 10 分钟后
    sunday = datetime(2026, 9, 6, 23, 55, 0, tzinfo=logic.TZ_SH)
    assert logic.seconds_until_next_post(sunday) == 600


def test_extract_json_handles_nested_structures_and_noise(plugin_modules):
    logic, _ = plugin_modules

    # 嵌套数组 + 字符串内含方括号 + 前置噪声方括号
    llm_arr = (
        "先说明[注意]：\n"
        "```json\n"
        '[{"id": 1, "delta": -1, "reason": "在[群里]刷屏", "tags": ["a", "b"]}]\n'
        "```\n完毕。"
    )
    parsed_arr = logic.extract_json_array(llm_arr)
    assert len(parsed_arr) == 1
    assert parsed_arr[0]["reason"] == "在[群里]刷屏"
    assert parsed_arr[0]["tags"] == ["a", "b"]

    # 嵌套对象 + 自我迭代 replace 二维数组
    llm_obj = (
        "说明 {仅供内部参考}\n"
        '{"add": ["不要在{群聊}抢话"], "replace": [[1, "语气更温和"]], "delete": [2]}'
    )
    parsed_obj = logic.extract_json_object(llm_obj)
    assert parsed_obj["add"] == ["不要在{群聊}抢话"]
    assert parsed_obj["replace"] == [[1, "语气更温和"]]
    assert parsed_obj["delete"] == [2]

    # 异常输入容错
    assert logic.extract_json_array("") == []
    assert logic.extract_json_array("没有 JSON") == []
    assert logic.extract_json_object("{bad json}") == {}


def test_clamp_score_and_helpers(plugin_modules):
    logic, _ = plugin_modules

    assert logic.clamp_score(5, 1) == 1
    assert logic.clamp_score(-9, 1) == -1
    assert logic.clamp_score("1.4", 3) == 1
    assert logic.clamp_score(True, 3) == 0
    assert logic.clamp_score(False, 3) == 0
    assert logic.clamp_score(float("nan"), 3) == 0
    assert logic.clamp_score(float("inf"), 3) == 0
    assert logic.clamp_score(None, 3) == 0

    assert logic.escape_like(r"100%_test\a") == r"100\%\_test\\a"
    assert logic.parse_port("6180") == 6180
    assert logic.parse_port("0", 6201) == 6201
    assert logic.parse_port("99999", 6201) == 6201
    assert logic.parse_port("abc", 6201) == 6201
    assert logic.parse_id_set("123, 456；789") == {"123", "456", "789"}
    assert logic.is_valid_api_id("123456") is True
    assert logic.is_valid_api_id("a" * 65) is False
    assert logic.is_valid_api_id("123\n456") is False
    assert logic.extract_qq_mention_id("<@99999> <@ABCDEF123456>", exclude={"99999"}) == (
        "ABCDEF123456"
    )
