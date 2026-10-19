# -*- coding: utf-8 -*-
"""管理后台页面测试（D3-C：Jinja2 + Tabler 服务端渲染，TestClient 进程内）。

覆盖：
- 未登录 -> 303 跳 /login；登录后各业务页面 200
- 强制改密门：未改密只能进 /change-password（与 API 层同源）
- 群上下文档五页 + 404 边界（未知 tab / 未知群）
- 仪表盘口径：聚合数值正确；BackendError -> unknown（⛔ 不当 0 不当成功）
- 静态资产可达（tabler css/js/字体/logo）
- 旧静态 SPA 已移除（⛔ 防止两份页面并存）

⛔ 这是本地自测证据，不构成真实部署 / 平台验证（§7.2）。
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.admin import auth as admin_auth
from app.admin.server import create_admin_app
from app.period import TZ
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, enable_all, make_config

ADMIN_USER = "admin"
ADMIN_PASS = "initial-pass-1"
NEW_PASS = "formal-pass-2"


def _bootstrap(tmp_db, monkeypatch):
    monkeypatch.setenv("GOPS_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("GOPS_ADMIN_PASSWORD", ADMIN_PASS)
    admin_auth.bootstrap_admin(tmp_db)


def make_client(tmp_db, tmp_path, backend=None):
    return TestClient(create_admin_app(
        state=tmp_db, backend=backend or FakeBackend(),
        sender=RecordingSender(), config=make_config(tmp_path)))


def login(client, password=ADMIN_PASS):
    r = client.post("/api/admin/login",
                    json={"username": ADMIN_USER, "password": password})
    assert r.status_code == 200, r.text
    return r


def logged_in_client(tmp_db, tmp_path, monkeypatch, backend=None):
    """完成 登录 -> 强制改密 -> 重登 的会话（页面门已解锁）。"""
    _bootstrap(tmp_db, monkeypatch)
    client = make_client(tmp_db, tmp_path, backend)
    login(client)
    csrf = client.get("/api/admin/me").json()["csrf_token"]
    r = client.post("/api/admin/change-password",
                    json={"old_password": ADMIN_PASS,
                          "new_password": NEW_PASS},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    login(client, password=NEW_PASS)
    return client


# ---------- 页面门与会话 ----------

class TestPageGuards:
    def test_login_page_renders(self, tmp_db, tmp_path, monkeypatch):
        _bootstrap(tmp_db, monkeypatch)
        client = make_client(tmp_db, tmp_path)
        r = client.get("/login")
        assert r.status_code == 200
        assert "管理后台登录" in r.text
        assert "/static/css/tabler.min.css" in r.text
        # ⛔ 登录页不得内嵌任何 CSRF 令牌（无会话）
        assert 'name="csrf-token"' not in r.text

    @pytest.mark.parametrize("path", [
        "/", "/groups", "/robot-instances", "/audit", "/system", "/stats",
        "/groups/%s/config" % GROUP,
    ])
    def test_unauthenticated_redirects_to_login(self, tmp_db, tmp_path,
                                                monkeypatch, path):
        _bootstrap(tmp_db, monkeypatch)
        client = make_client(tmp_db, tmp_path)
        # starlette 1.7 TestClient 默认跟随重定向：断言跳转必须显式关闭
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"

    def test_must_change_gate(self, tmp_db, tmp_path, monkeypatch):
        """未改密：业务页一律 303 到 /change-password（⛔ 不放行）。"""
        _bootstrap(tmp_db, monkeypatch)
        client = make_client(tmp_db, tmp_path)
        login(client)  # must_change_password=True
        for path in ("/", "/groups", "/robot-instances"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 303, path
            assert r.headers["location"] == "/change-password", path
        r = client.get("/change-password")
        assert r.status_code == 200
        assert "修改初始密码" in r.text

    def test_change_password_page_redirects_after_done(self, tmp_db, tmp_path,
                                                       monkeypatch):
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        r = client.get("/change-password", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"

    def test_login_page_redirects_when_logged_in(self, tmp_db, tmp_path,
                                                 monkeypatch):
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        r = client.get("/login", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"


# ---------- 业务页面渲染 ----------

class TestPagesRender:
    def test_dashboard_page(self, tmp_db, tmp_path, monkeypatch):
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "张三", None, True)
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        r = client.get("/")
        assert r.status_code == 200
        assert "仪表盘" in r.text
        assert "最近提交记录" in r.text
        assert "今日打卡完成" in r.text
        # 分母 = 1 名应交人员；空库 -> 0 条记录
        assert "0 <span" in r.text or ">0 <" in r.text

    @pytest.mark.parametrize("tab,label", [
        ("config", "群配置与开关"),
        ("members", "应交人员"),
        ("whitelist", "导出白名单"),
        ("fields", "报数字段池"),
        ("backfill", "后台补录"),
    ])
    def test_group_tab_pages(self, tmp_db, tmp_path, monkeypatch, tab, label):
        enable_all(tmp_db)
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        r = client.get("/groups/%s/%s" % (GROUP, tab))
        assert r.status_code == 200
        assert label in r.text
        # 群上下文注入 sidebar「业务配置」下拉
        assert "navbar-business" in r.text

    def test_group_tab_unknown_404(self, tmp_db, tmp_path, monkeypatch):
        enable_all(tmp_db)
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        r = client.get("/groups/%s/nope" % GROUP)
        assert r.status_code == 404

    def test_group_unknown_404(self, tmp_db, tmp_path, monkeypatch):
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        r = client.get("/groups/no-such-group/config")
        assert r.status_code == 404

    def test_robot_instances_page(self, tmp_db, tmp_path, monkeypatch):
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        r = client.get("/robot-instances")
        assert r.status_code == 200
        assert "群实例绑定" in r.text
        assert "实例列表" in r.text
        assert "bind-modal" in r.text
        assert "暂不开放录入" in r.text

    def test_groups_page(self, tmp_db, tmp_path, monkeypatch):
        enable_all(tmp_db)
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        r = client.get("/groups")
        assert r.status_code == 200
        assert "群列表" in r.text
        assert "发现但未绑定" in r.text
        assert "new-group-modal" in r.text

    def test_audit_page(self, tmp_db, tmp_path, monkeypatch):
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        r = client.get("/audit")
        assert r.status_code == 200
        assert "审计日志" in r.text

    def test_system_page_no_secrets(self, tmp_db, tmp_path, monkeypatch):
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        r = client.get("/system")
        assert r.status_code == 200
        assert "本地数据版本" in r.text
        # v7：h5_upload（H5 图片上传凭证，§4.11.4 D4 定夺 2026-10-16）
        assert "v7" in r.text
        # ⛔ 系统页不得泄露任何凭据
        assert ADMIN_PASS not in r.text
        assert NEW_PASS not in r.text
        assert "GOPS_ADMIN_PASSWORD" not in r.text

    def test_stats_page(self, tmp_db, tmp_path, monkeypatch):
        enable_all(tmp_db)
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        r = client.get("/stats")
        assert r.status_code == 200
        assert "统计中心" in r.text
        # 多维统计：指标卡 + 维度表 + 提交明细（D3-C 用户指令第 5 项）
        assert "记录总数" in r.text
        assert "维度统计" in r.text
        assert "提交明细" in r.text
        # 导出入口：CSV / XLSX 下载链接指向导出端点
        assert "/api/admin/stats/export?group_id=" in r.text
        assert "format=csv" in r.text
        assert "format=xlsx" in r.text
        # 缺省选中唯一活跃群
        assert GROUP in r.text
        # 业务切换不 500
        for biz in ("checkin", "summary", "report"):
            r = client.get("/stats?biz=%s" % biz)
            assert r.status_code == 200
            assert "统计中心" in r.text


# ---------- 仪表盘口径 ----------

class TestDashboardApi:
    def test_dashboard_aggregation(self, tmp_db, tmp_path, monkeypatch):
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "张三", None, True)
        backend = FakeBackend()
        backend.results["checkin_query_records"] = {"records": [
            {"phone": PHONE, "name": "张三", "checkin_time":
             "2026-10-15T09:30:00+08:00", "entry_source": "user"}]}
        client = logged_in_client(tmp_db, tmp_path, monkeypatch, backend)
        r = client.get("/api/admin/dashboard")
        assert r.status_code == 200
        data = r.json()
        assert data["groups"] == {"total": 1, "active": 1, "disabled": 0}
        assert data["biz"]["checkin"] == {"done": 1, "total": 1,
                                          "unknown": False}
        assert data["biz"]["summary"] == {"done": 0, "total": 1,
                                          "unknown": False}
        assert data["biz"]["report"] == {"done": 0, "total": 1,
                                         "unknown": False}
        assert data["period_key"] == datetime.now(TZ).date().isoformat()
        assert len(data["recent"]) == 1
        assert data["recent"][0]["biz"] == "checkin"

    def test_dashboard_backend_error_is_unknown(self, tmp_db, tmp_path,
                                                monkeypatch):
        """MCP 查询失败 -> unknown=True；⛔ 不当 0 也不当成功。"""
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "张三", None, True)
        backend = FakeBackend()
        backend.raise_on.add("checkin_query_records")
        client = logged_in_client(tmp_db, tmp_path, monkeypatch, backend)
        r = client.get("/api/admin/dashboard")
        assert r.status_code == 200
        data = r.json()
        assert data["biz"]["checkin"]["unknown"] is True
        assert data["biz"]["checkin"]["done"] == 0
        assert data["biz"]["checkin"]["total"] == 1  # 分母仍如实计
        assert data["biz"]["summary"]["unknown"] is False
        # 页面同样可渲染（unknown 走「数据暂不可用」分支）
        page = client.get("/")
        assert page.status_code == 200
        assert "数据暂不可用" in page.text

    def test_dashboard_requires_session(self, tmp_db, tmp_path, monkeypatch):
        _bootstrap(tmp_db, monkeypatch)
        client = make_client(tmp_db, tmp_path)
        r = client.get("/api/admin/dashboard")
        assert r.status_code == 401

    def test_dashboard_group_mode(self, tmp_db, tmp_path, monkeypatch):
        """按群模式（D3-C 用户指令第 2 项）：mode=group / biz 带 enabled /
        pending_people 只按当日有记录的去重手机号计；全量模式历史契约不变。"""
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "张三", None, True)
        backend = FakeBackend()
        backend.results["checkin_query_records"] = {"records": [
            {"phone": PHONE, "name": "张三", "checkin_time":
             "2026-10-15T09:30:00+08:00", "entry_source": "user"}]}
        client = logged_in_client(tmp_db, tmp_path, monkeypatch, backend)
        r = client.get("/api/admin/dashboard?group_id=%s" % GROUP)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["mode"] == "group"
        assert data["group"]["group_id"] == GROUP
        assert data["period_key"] == datetime.now(TZ).date().isoformat()
        assert data["biz"]["checkin"]["done"] == 1
        assert data["biz"]["checkin"]["total"] == 1
        assert data["biz"]["checkin"]["enabled"] is True
        assert data["biz"]["summary"]["enabled"] is True
        assert data["biz"]["report"]["enabled"] is True
        assert data["recovery"]["pending"] == 0
        assert data["reminders"]["pending"] == 0
        # 当日已有打卡记录 -> 待提醒 0
        assert data["pending_people"] == 0
        # 未知群 -> ok False（不伪造空成功）
        r = client.get("/api/admin/dashboard?group_id=no-such-group")
        assert r.status_code == 200
        assert r.json()["ok"] is False
        # 全量模式（历史契约）仍在：groups 汇总 + biz 三键形态
        r = client.get("/api/admin/dashboard")
        data = r.json()
        assert data["groups"] == {"total": 1, "active": 1, "disabled": 0}
        assert data["biz"]["checkin"] == {"done": 1, "total": 1,
                                          "unknown": False}


# ---------- 统计导出（D3-C 用户指令第 5 项：CSV / XLSX 下载） ----------

class TestStatsExport:
    def _client(self, tmp_db, tmp_path, monkeypatch, backend):
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "张三", None, True)
        return logged_in_client(tmp_db, tmp_path, monkeypatch, backend)

    def _records(self):
        return {"records": [
            {"phone": PHONE, "name": "张三", "session": "am",
             "checkin_time": "2026-10-15T09:30:00+08:00",
             "status": "NORMAL", "entry_source": "user",
             "image_name": "a.jpg"}]}

    def test_export_csv(self, tmp_db, tmp_path, monkeypatch):
        backend = FakeBackend()
        backend.results["checkin_query_records"] = self._records()
        client = self._client(tmp_db, tmp_path, monkeypatch, backend)
        r = client.get("/api/admin/stats/export",
                       params={"group_id": GROUP, "biz": "checkin",
                               "date": "2026-10-15", "format": "csv"})
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        body = r.content.decode("utf-8")
        # UTF-8 BOM：Excel 直接打开中文不乱码
        assert body.startswith("\ufeff")
        # 列口径与机器人侧导出一致（中文表头 + 映射值）
        assert "手机号" in body and "姓名" in body and "打卡时间" in body
        assert PHONE in body and "张三" in body
        assert "上午" in body and "按时" in body and "群员" in body
        # 中文文件名 + RFC 5987
        cd = r.headers["content-disposition"]
        assert "attachment" in cd
        assert "filename*=UTF-8''" in cd
        assert ".csv" in cd

    def test_export_xlsx(self, tmp_db, tmp_path, monkeypatch):
        backend = FakeBackend()
        backend.results["checkin_query_records"] = self._records()
        client = self._client(tmp_db, tmp_path, monkeypatch, backend)
        r = client.get("/api/admin/stats/export",
                       params={"group_id": GROUP, "biz": "checkin",
                               "date": "2026-10-15", "format": "xlsx"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(
            "application/vnd.openxmlformats")
        import io
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(r.content))
        sheet = workbook.active
        assert sheet.title == "打卡"
        header = [cell.value for cell in sheet[1]]
        assert header[:3] == ["手机号", "姓名", "时段"]
        rows = list(sheet.iter_rows(min_row=2, values_only=True))
        assert rows[0][0] == PHONE
        assert rows[0][1] == "张三"
        assert rows[0][2] == "上午"
        assert ".xlsx" in r.headers["content-disposition"]

    def test_export_summary_uses_group_period(self, tmp_db, tmp_path,
                                              monkeypatch):
        """总结按群周期类型：weekly 群 -> 周键（YYYY-Www）。"""
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, period_type="weekly")
        backend = FakeBackend()
        backend.results["summary_query_records"] = {"records": [
            {"phone": PHONE, "name": "张三", "period_type": "weekly",
             "content": "本周正常", "entry_source": "user",
             "updated_time": "2026-10-15T18:00:00+08:00"}]}
        client = logged_in_client(tmp_db, tmp_path, monkeypatch, backend)
        r = client.get("/api/admin/stats/export",
                       params={"group_id": GROUP, "biz": "summary",
                               "date": "2026-10-15", "format": "csv"})
        assert r.status_code == 200
        body = r.content.decode("utf-8")
        assert "本周正常" in body
        # 文件名带 ISO 周键（带连字符口径，period.iso_week_key）
        cd = r.headers["content-disposition"]
        from urllib.parse import unquote
        assert "2026-W42" in unquote(cd)

    def test_export_bad_biz_400(self, tmp_db, tmp_path, monkeypatch):
        client = self._client(tmp_db, tmp_path, monkeypatch, FakeBackend())
        r = client.get("/api/admin/stats/export",
                       params={"group_id": GROUP, "biz": "nope",
                               "date": "2026-10-15", "format": "csv"})
        assert r.status_code == 400
        # 通俗提示：不暴露技术名词
        assert "请选择要导出的业务" in r.text

    def test_export_bad_date_400(self, tmp_db, tmp_path, monkeypatch):
        client = self._client(tmp_db, tmp_path, monkeypatch, FakeBackend())
        r = client.get("/api/admin/stats/export",
                       params={"group_id": GROUP, "biz": "checkin",
                               "date": "not-a-date", "format": "csv"})
        assert r.status_code == 400
        assert "日期格式不正确" in r.text

    def test_export_unknown_group_404(self, tmp_db, tmp_path, monkeypatch):
        client = self._client(tmp_db, tmp_path, monkeypatch, FakeBackend())
        r = client.get("/api/admin/stats/export",
                       params={"group_id": "no-such-group",
                               "biz": "checkin", "date": "2026-10-15",
                               "format": "csv"})
        assert r.status_code == 404

    def test_export_bad_format_400(self, tmp_db, tmp_path, monkeypatch):
        client = self._client(tmp_db, tmp_path, monkeypatch, FakeBackend())
        r = client.get("/api/admin/stats/export",
                       params={"group_id": GROUP, "biz": "checkin",
                               "date": "2026-10-15", "format": "pdf"})
        assert r.status_code == 400

    def test_export_requires_session(self, tmp_db, tmp_path, monkeypatch):
        _bootstrap(tmp_db, monkeypatch)
        client = make_client(tmp_db, tmp_path)
        r = client.get("/api/admin/stats/export",
                       params={"group_id": GROUP, "biz": "checkin",
                               "date": "2026-10-15", "format": "csv"})
        assert r.status_code == 401


# ---------- 文案规范守卫（D3-C 用户指令第 7 项） ----------

class TestCopyGuard:
    """渲染后的可见文本：无 Emoji、无底层技术名词。

    扫描对象是 TestClient 实际渲染的页面（去 script/style 与标签后），
    即群运维真正看到的文案；⛔ 不看源码字符串，避免把代码标识符
    （csrf-token / needs-validation / JSON.stringify）误判为对外文案。
    """

    PAGES = ["/login", "/", "/groups", "/robot-instances", "/stats",
             "/audit", "/system", "/change-password",
             "/groups/%s/config" % GROUP,
             "/groups/%s/members" % GROUP,
             "/groups/%s/whitelist" % GROUP,
             "/groups/%s/fields" % GROUP,
             "/groups/%s/backfill" % GROUP]

    TECH_TERMS = re.compile(
        r"(sqlite|\bsql\b|json|payload|nullpointer|traceback|stack trace|"
        r"\bexception\b|\bcsrf\b|\bvalidation\b|internal server|"
        r"http\s*[45]\d\d|\bundefined\b|\bmcp\b|fail-closed|"
        r"\bwebhook\b|\bschema\b|\bnull\b)", re.IGNORECASE)

    @staticmethod
    def _visible_text(html):
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return text

    def _render_all(self, tmp_db, tmp_path, monkeypatch):
        enable_all(tmp_db)
        client = logged_in_client(tmp_db, tmp_path, monkeypatch)
        for path in self.PAGES:
            r = client.get(path)
            assert r.status_code == 200, path
            yield path, self._visible_text(r.text)

    def test_no_emoji_in_visible_text(self, tmp_db, tmp_path, monkeypatch):
        for path, text in self._render_all(tmp_db, tmp_path, monkeypatch):
            for ch in text:
                code = ord(ch)
                assert not (0x1F000 <= code <= 0x1FAFF
                            or 0x2600 <= code <= 0x27BF
                            or 0x2B00 <= code <= 0x2BFF
                            or code == 0xFE0F), (path, hex(code), ch)

    def test_no_tech_terms_in_visible_text(self, tmp_db, tmp_path,
                                           monkeypatch):
        for path, text in self._render_all(tmp_db, tmp_path, monkeypatch):
            hit = self.TECH_TERMS.search(text)
            assert hit is None, (path, hit.group(0) if hit else "")


# ---------- 静态资产 ----------

class TestStaticAssets:
    @pytest.mark.parametrize("path", [
        "/static/css/tabler.min.css",
        "/static/css/tabler-icons.min.css",
        "/static/css/admin.css",
        "/static/js/tabler.min.js",
        "/static/js/admin-core.js",
        "/static/js/admin-pages.js",
        "/static/fonts/tabler-icons.woff2",
        "/static/images/logo.svg",
    ])
    def test_asset_reachable(self, tmp_db, tmp_path, monkeypatch, path):
        client = make_client(tmp_db, tmp_path)
        r = client.get(path)
        assert r.status_code == 200

    def test_old_spa_removed(self, tmp_db, tmp_path, monkeypatch):
        """⛔ 旧静态 SPA 已被 Jinja2 模板取代，不得复活。"""
        client = make_client(tmp_db, tmp_path)
        for path in ("/static/admin.html", "/static/admin.js",
                     "/static/admin.css"):
            assert client.get(path).status_code == 404
