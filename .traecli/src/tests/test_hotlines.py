"""测试 deadman.hotlines.lookup - 官方热线查询

覆盖点（4 个）：
  - test_lookup_national_funeral: 全国殡葬服务热线（96000）
  - test_lookup_provincial_chongqing: 重庆 96000 省级热线
  - test_lookup_unknown_province_returns_empty: 未知省份省级返回空
  - test_list_functions: 列出所有职能

依据 compliance-framework.md：不编造电话号码，所有热线必须标 source。
"""

from __future__ import annotations

from deadman.hotlines.lookup import HotlineLookup


# =====================================================================
# 1. 全国殡葬服务热线
# =====================================================================


class TestLookupNational:
    """测试全国热线查询"""

    def test_lookup_national_funeral(self) -> None:
        # 不指定 province，返回全国热线
        results = HotlineLookup().lookup(function="殡葬服务")
        # 应至少返回 1 条全国级殡葬服务热线
        national_results = [r for r in results if r["scope"] == "national"]
        assert len(national_results) >= 1
        funeral = national_results[0]
        assert funeral["phone"] == "96000", "全国殡葬服务热线应为 96000"
        assert funeral["source"], "必须标 source"
        assert funeral["confidence"] > 0.5, "官方源 confidence 应 > 0.5"

    def test_get_national_funeral(self) -> None:
        result = HotlineLookup().get_national("殡葬服务")
        assert result is not None
        assert result["phone"] == "96000"
        assert result["scope"] == "national"
        assert result["source"]

    def test_get_national_unknown_function_returns_none(self) -> None:
        result = HotlineLookup().get_national("不存在的职能")
        assert result is None


# =====================================================================
# 2. 省级热线查询
# =====================================================================


class TestLookupProvincial:
    """测试省级热线查询"""

    def test_lookup_provincial_chongqing(self) -> None:
        # 指定 province=重庆，function=殡葬服务
        results = HotlineLookup().lookup(province="重庆", function="殡葬服务")
        # 应包含重庆省级热线 96000
        provincial_results = [r for r in results if r.get("scope") == "provincial"]
        assert len(provincial_results) >= 1
        chongqing = provincial_results[0]
        assert chongqing["phone"] == "96000", "重庆殡葬服务热线应为 96000"
        assert chongqing["province"] == "重庆"
        assert chongqing["source"], "必须标 source"

    def test_lookup_provincial_shanghai(self) -> None:
        results = HotlineLookup().lookup(province="上海", function="殡葬服务")
        provincial = [r for r in results if r.get("scope") == "provincial"]
        assert len(provincial) >= 1
        assert provincial[0]["phone"] == "021-962200"

    def test_get_provincial_chongqing(self) -> None:
        results = HotlineLookup().get_provincial("重庆", "殡葬服务")
        assert len(results) >= 1
        assert results[0]["phone"] == "96000"

    def test_get_provincial_all_functions(self) -> None:
        # 不指定 function 返回该省全部热线
        results = HotlineLookup().get_provincial("重庆")
        assert len(results) >= 1


# =====================================================================
# 3. 未知省份
# =====================================================================


class TestLookupUnknownProvince:
    """测试未知省份的处理"""

    def test_lookup_unknown_province_returns_empty_provincial(self) -> None:
        # 未知省份应只返回全国级，省级部分为空
        results = HotlineLookup().lookup(province="不存在的省份")
        provincial = [r for r in results if r.get("scope") == "provincial"]
        assert provincial == [], "未知省份不应返回省级热线"

    def test_get_provincial_unknown_returns_empty(self) -> None:
        results = HotlineLookup().get_provincial("不存在的省份")
        assert results == []


# =====================================================================
# 4. 职能列表
# =====================================================================


class TestListFunctions:
    """测试 list_functions()"""

    def test_list_functions(self) -> None:
        functions = HotlineLookup().list_functions()
        # 应包含 6 个职能
        assert "殡葬服务" in functions
        assert "政策咨询" in functions
        assert "法律援助" in functions
        assert "心理援助" in functions
        assert "消费者投诉" in functions
        assert "社保咨询" in functions
        assert len(functions) >= 6

    def test_list_provinces(self) -> None:
        provinces = HotlineLookup().list_provinces()
        # 应包含已收录的省份
        assert "北京" in provinces
        assert "上海" in provinces
        assert "重庆" in provinces
        assert "山东" in provinces
        assert "安徽铜陵" in provinces


# =====================================================================
# 5. 所有热线必须标 source（compliance 红线）
# =====================================================================


class TestSourceCompliance:
    """测试所有热线都标 source（compliance-framework 反红线）"""

    def test_all_national_hotlines_have_source(self) -> None:
        lookup = HotlineLookup()
        for func, entry in lookup._db.get("national", {}).items():
            assert entry.get("source"), f"全国职能 {func} 缺 source"

    def test_all_provincial_hotlines_have_source(self) -> None:
        lookup = HotlineLookup()
        for province, funcs in lookup._db.get("provincial", {}).items():
            for func, entry in funcs.items():
                assert entry.get("source"), f"{province}/{func} 缺 source"
