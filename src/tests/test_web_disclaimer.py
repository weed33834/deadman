"""测试 deadman.web.app - Phase 9 端点（FastAPI TestClient 进程内）

覆盖点（6 个）：
  - test_get_disclaimer_full: GET /api/disclaimer 返回完整告知
  - test_get_disclaimer_scenario: GET /api/disclaimer?scenario=legal
  - test_get_hotlines: GET /api/hotlines 返回热线
  - test_get_institutions: GET /api/institutions 返回机构
  - test_get_institution_by_id: GET /api/institutions/<id>
  - test_response_includes_disclaimer: 所有响应含 disclaimer 字段

测试方式：TestClient 直接调真实端点；机构 store 通过 monkeypatch 指向 tmp_path。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deadman.institutions.store import InstitutionStore, make_institution

# =====================================================================
# 测试辅助：把 app 内 InstitutionStore 指向 tmp_path（隔离默认数据）
# =====================================================================


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from deadman.web.app import app

    orig_init = InstitutionStore.__init__

    def _isolated_init(self, auto_load_seed=False, data_dir=None):
        orig_init(self, auto_load_seed=False, data_dir=tmp_path)

    monkeypatch.setattr(InstitutionStore, "__init__", _isolated_init)
    return TestClient(app)


def _add_institution(store: InstitutionStore, name: str, type_: str, province: str, city: str):
    store.add(
        make_institution(
            name=name,
            type=type_,
            province=province,
            city=city,
            source="测试",
        )
    )


# =====================================================================
# 1. GET /api/disclaimer 完整告知
# =====================================================================


class TestGetDisclaimerFull:
    """测试 GET /api/disclaimer 返回完整告知"""

    def test_get_disclaimer_full(self, client) -> None:
        r = client.get("/api/disclaimer")
        assert r.status_code == 200
        payload = r.json()
        assert payload["kind"] == "full_opening"
        assert "deadman" in payload["text"]
        assert "不提供法律意见" in payload["text"]
        assert "disclaimer" in payload, "响应必须含 disclaimer 字段"


# =====================================================================
# 2. GET /api/disclaimer?scenario=legal
# =====================================================================


class TestGetDisclaimerScenario:
    """测试场景化提醒"""

    def test_get_disclaimer_scenario_legal(self, client) -> None:
        r = client.get("/api/disclaimer", params={"scenario": "legal"})
        assert r.status_code == 200
        payload = r.json()
        assert payload["kind"] == "scenario:legal"
        assert "不提供法律意见" in payload["text"]
        assert "disclaimer" in payload

    def test_get_disclaimer_scenario_agent(self, client) -> None:
        r = client.get("/api/disclaimer", params={"scenario": "agent"})
        assert r.status_code == 200
        assert "不代办" in r.json()["text"]

    def test_get_disclaimer_invalid_scenario_returns_400(self, client) -> None:
        r = client.get("/api/disclaimer", params={"scenario": "invalid"})
        assert r.status_code == 400
        assert "detail" in r.json() or "error" in r.json()

    def test_get_disclaimer_footer_format(self, client) -> None:
        r = client.get("/api/disclaimer", params={"format": "footer"})
        assert r.status_code == 200
        payload = r.json()
        assert payload["kind"] == "footer"
        assert len(payload["text"]) < 200


# =====================================================================
# 3. GET /api/hotlines
# =====================================================================


class TestGetHotlines:
    """测试 GET /api/hotlines"""

    def test_get_hotlines_default(self, client) -> None:
        # 不带参数返回全国热线
        r = client.get("/api/hotlines")
        assert r.status_code == 200
        payload = r.json()
        assert payload["count"] >= 6, "应至少返回 6 个全国职能热线"
        assert "disclaimer" in payload
        # 每条热线都应标 source
        for hl in payload["hotlines"]:
            assert hl.get("source"), "热线必须标 source"

    def test_get_hotlines_chongqing_funeral(self, client) -> None:
        r = client.get("/api/hotlines", params={"province": "重庆", "function": "殡葬服务"})
        assert r.status_code == 200
        payload = r.json()
        # 应包含重庆省级 96000
        provincial = [hl for hl in payload["hotlines"] if hl.get("scope") == "provincial"]
        assert len(provincial) >= 1
        assert provincial[0]["phone"] == "96000"
        assert "disclaimer" in payload


# =====================================================================
# 4. GET /api/institutions
# =====================================================================


class TestGetInstitutions:
    """测试 GET /api/institutions"""

    def test_get_institutions_by_province(self, client, tmp_path: Path) -> None:
        # 用隔离 store，先加 2 条
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        _add_institution(store, "Web 测试殡仪馆A", "funeral_home", "Web省", "Web市")
        _add_institution(store, "Web 测试殡仪馆B", "funeral_home", "其他省", "其他市")

        r = client.get("/api/institutions", params={"province": "Web省"})
        assert r.status_code == 200
        payload = r.json()
        assert payload["count"] == 1
        assert payload["institutions"][0]["name"] == "Web 测试殡仪馆A"
        assert "disclaimer" in payload

    def test_get_institutions_by_type(self, client, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        _add_institution(store, "殡仪馆X", "funeral_home", "北京", "北京")
        _add_institution(store, "公墓Y", "cemetery", "北京", "北京")

        r = client.get("/api/institutions", params={"type": "cemetery"})
        assert r.status_code == 200
        payload = r.json()
        assert payload["count"] == 1
        assert payload["institutions"][0]["type"] == "cemetery"


# =====================================================================
# 5. GET /api/institutions/<id>
# =====================================================================


class TestGetInstitutionById:
    """测试 GET /api/institutions/<id>"""

    def test_get_institution_by_id_found(self, client, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        inst = make_institution(
            name="详情测试殡仪馆",
            type="funeral_home",
            province="北京",
            city="北京",
            source="测试",
        )
        store.add(inst)

        r = client.get(f"/api/institutions/{inst.institution_id}")
        assert r.status_code == 200
        payload = r.json()
        assert payload["name"] == "详情测试殡仪馆"
        assert payload["institution_id"] == inst.institution_id
        assert "needs_verification_warning" in payload
        assert "disclaimer" in payload

    def test_get_institution_by_id_not_found(self, client, tmp_path: Path) -> None:
        InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        r = client.get("/api/institutions/nonexistent_id")
        assert r.status_code == 404
        assert "detail" in r.json() or "error" in r.json()


# =====================================================================
# 6. 所有响应含 disclaimer 字段
# =====================================================================


class TestResponseIncludesDisclaimer:
    """测试所有 Phase 9 响应都含 disclaimer 字段（transparency-framework）"""

    def test_disclaimer_response_includes_disclaimer(self, client) -> None:
        payload = client.get("/api/disclaimer").json()
        assert "disclaimer" in payload
        assert len(payload["disclaimer"]) > 0

    def test_hotlines_response_includes_disclaimer(self, client) -> None:
        payload = client.get("/api/hotlines").json()
        assert "disclaimer" in payload
        assert "核实" in payload["disclaimer"]

    def test_institutions_response_includes_disclaimer(self, client, tmp_path: Path) -> None:
        InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        payload = client.get("/api/institutions").json()
        assert "disclaimer" in payload

    def test_institution_by_id_response_includes_disclaimer(self, client, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        inst = make_institution(
            name="X",
            type="funeral_home",
            province="北京",
            city="北京",
            source="测试",
        )
        store.add(inst)
        payload = client.get(f"/api/institutions/{inst.institution_id}").json()
        assert "disclaimer" in payload

    def test_404_response_includes_disclaimer(self, client, tmp_path: Path) -> None:
        InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        r = client.get("/api/institutions/missing")
        assert r.status_code == 404
        # FastAPI HTTPException 会把业务 payload 包在 detail 字段里
        body = r.json().get("detail", r.json())
        assert "disclaimer" in body
