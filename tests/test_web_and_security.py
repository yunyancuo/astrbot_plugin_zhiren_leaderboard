"""内置 Web API、安全响应头、生命周期容错与前端 HTML 安全审计测试。"""

from __future__ import annotations

import asyncio
import re
import socket
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer


def test_web_api_endpoints_headers_and_validation(make_plugin):
    """端到端测试所有 HTTP 接口、安全响应头、输入校验与 CORS 配置。"""

    async def _run():
        plugin, _ctx = make_plugin(plugin_config={"cors_origin": "https://board.example.com"})
        plugin._apply_delta("1001", "201", "操作员", "301", "<script>alert(1)</script>", 1, "测试<b>理由</b>")

        app = plugin._build_web_app()
        async with TestServer(app) as server, TestClient(server) as client:
            # 1. 首页 HTML 与安全头
            resp = await client.get("/")
            assert resp.status == 200
            assert "text/html" in resp.headers["Content-Type"]
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
            assert resp.headers["Referrer-Policy"] == "no-referrer"
            assert "Content-Security-Policy" in resp.headers
            assert resp.headers["Access-Control-Allow-Origin"] == "https://board.example.com"
            assert resp.headers["Vary"] == "Origin"

            # 2. CORS OPTIONS 预检
            opt = await client.options("/api/data")
            assert opt.status == 204
            assert "GET" in opt.headers["Access-Control-Allow-Methods"]

            # 3. 健康检查
            health = await client.get("/api/health")
            assert health.status == 200
            h_json = await health.json()
            assert h_json["status"] == "ok"

            # 4. 群列表
            groups_resp = await client.get("/api/groups")
            assert groups_resp.status == 200
            assert groups_resp.headers["Cache-Control"] == "no-store"
            g_json = await groups_resp.json()
            assert g_json["groups"] == [{"gid": "1001", "name": "群 1001", "count": 1}]

            # 5. 榜单数据
            data_resp = await client.get("/api/data?gid=1001")
            assert data_resp.status == 200
            d_json = await data_resp.json()
            assert d_json["gid"] == "1001"
            assert len(d_json["total"]) == 1
            assert d_json["total"][0]["uid"] == "301"

            # 6. 个人详情
            mem_resp = await client.get("/api/member?gid=1001&uid=301")
            assert mem_resp.status == 200
            m_json = await mem_resp.json()
            assert m_json["total"] == 1
            assert len(m_json["records"]) == 1

            # 7. 不存在的成员安全返回空对象
            miss_resp = await client.get("/api/member?gid=1001&uid=999")
            assert miss_resp.status == 200
            assert await miss_resp.json() == {}

            # 8. 非法超长或含控制字符的参数返回 400
            bad_gid = await client.get("/api/data?gid=" + ("x" * 80))
            assert bad_gid.status == 400
            bad_uid = await client.get("/api/member?gid=1001&uid=123%0a456")
            assert bad_uid.status == 400

    asyncio.run(_run())


def test_initialize_port_conflict_does_not_break_background_tasks(make_plugin):
    """验证 Web 端口被占用时平滑降级，后台任务照常启动且 terminate 可安全清理。"""

    async def _run():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        busy_port = sock.getsockname()[1]
        try:
            plugin, _ctx = make_plugin(
                plugin_config={"web_host": "127.0.0.1", "web_port": busy_port}
            )
            await plugin.initialize()
            assert plugin._runner is None
            assert len(plugin._tasks) == 3
            await plugin.terminate()
            assert plugin._closed is True
            assert plugin._tasks == []
        finally:
            sock.close()

    asyncio.run(_run())


def test_frontend_html_xss_and_robustness_audit():
    """静态安全审计 `web/index.html`：杜绝 innerHTML XSS、验证竞态控制与无障碍特性。"""
    html_path = Path(__file__).resolve().parent.parent / "web" / "index.html"
    html = html_path.read_text(encoding="utf-8")

    # 1. 严禁任何 innerHTML / outerHTML / insertAdjacentHTML / document.write 用法
    assert not re.search(r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b", html)

    # 2. 必须使用安全 DOM API 与文本赋值
    assert "document.createElement" in html
    assert "textContent" in html
    assert "replaceChildren" in html

    # 3. 必须具备竞态取消、安全首字处理、键盘 Esc 关闭与子路径保留
    assert "AbortController" in html
    assert "Array.from(" in html
    assert "Escape" in html
    assert "url.pathname + url.search" in html
    assert 'role="dialog"' in html
    assert "visibilitychange" in html
