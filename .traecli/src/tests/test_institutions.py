"""测试 deadman.institutions.store - 殡葬机构存储

覆盖点（7 个）：
  - test_add_and_search: 添加后能搜到
  - test_search_by_province: 按省份过滤
  - test_search_by_type: 按类型过滤
  - test_search_by_keyword: 关键词搜索
  - test_low_confidence_flagged: confidence < 0.7 输出标记
  - test_import_from_seed: 从 seed.json 导入
  - test_seed_data_loaded: 启动时种子数据加载

依据 retrieval-guardrails.md：
- confidence < 0.5 不可信，输出必须提示"建议向官方核实"
- 每条数据必须有 source 字段
测试隔离：每个测试用 tmp_path 独立目录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deadman.institutions.store import (
    Institution,
    InstitutionStore,
    make_institution,
)


# =====================================================================
# 1. add + search 基本流程
# =====================================================================


class TestAddAndSearch:
    """测试 add + search 基本流程"""

    def test_add_and_search(self, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        inst = make_institution(
            name="测试殡仪馆",
            type="funeral_home",
            province="测试省",
            city="测试市",
            address="测试地址",
            phone="12345678",
            services=["遗体接运", "火化"],
            source="测试来源 2026",
            confidence=0.7,
        )
        store.add(inst)

        # 搜索应能找到
        results = store.search(province="测试省")
        assert len(results) == 1
        assert results[0].name == "测试殡仪馆"
        assert results[0].phone == "12345678"

        # get 也能找到
        got = store.get(inst.institution_id)
        assert got is not None
        assert got.name == "测试殡仪馆"

    def test_add_duplicate_merges_by_name_address(self, tmp_path: Path) -> None:
        # 同名同地址应去重合并，不重复添加
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        inst1 = make_institution(
            name="同地址殡仪馆",
            type="funeral_home",
            province="北京",
            city="北京",
            address="同地址",
            source="来源A 2026",
            confidence=0.7,
        )
        inst2 = make_institution(
            name="同地址殡仪馆",
            type="funeral_home",
            province="北京",
            city="北京",
            address="同地址",
            phone="99999999",
            source="来源B 2026",
            confidence=0.8,
        )
        store.add(inst1)
        store.add(inst2)
        assert store.count() == 1, "同名同地址应去重"
        # 合并后 confidence 取高者
        merged = store.get(inst1.institution_id)
        assert merged.confidence == 0.8
        # phone 应被新值覆盖
        assert merged.phone == "99999999"


# =====================================================================
# 2. 按省份过滤
# =====================================================================


class TestSearchByProvince:
    """测试按省份过滤"""

    def test_search_by_province(self, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        store.add(make_institution(
            name="A 殡仪馆", type="funeral_home",
            province="北京", city="北京", source="测试",
        ))
        store.add(make_institution(
            name="B 殡仪馆", type="funeral_home",
            province="上海", city="上海", source="测试",
        ))
        store.add(make_institution(
            name="C 殡仪馆", type="funeral_home",
            province="北京", city="北京", source="测试",
        ))

        beijing = store.search(province="北京")
        assert len(beijing) == 2
        assert all(i.province == "北京" for i in beijing)

        shanghai = store.search(province="上海")
        assert len(shanghai) == 1
        assert shanghai[0].name == "B 殡仪馆"


# =====================================================================
# 3. 按类型过滤
# =====================================================================


class TestSearchByType:
    """测试按机构类型过滤"""

    def test_search_by_type(self, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        store.add(make_institution(
            name="殡仪馆A", type="funeral_home",
            province="北京", city="北京", source="测试",
        ))
        store.add(make_institution(
            name="公墓B", type="cemetery",
            province="北京", city="北京", source="测试",
        ))
        store.add(make_institution(
            name="火化场C", type="crematorium",
            province="北京", city="北京", source="测试",
        ))

        funeral_homes = store.search(type="funeral_home")
        assert len(funeral_homes) == 1
        assert funeral_homes[0].type == "funeral_home"
        assert funeral_homes[0].name == "殡仪馆A"

        cemeteries = store.search(type="cemetery")
        assert len(cemeteries) == 1
        assert cemeteries[0].name == "公墓B"


# =====================================================================
# 4. 关键词搜索
# =====================================================================


class TestSearchByKeyword:
    """测试关键词模糊搜索"""

    def test_search_by_keyword_in_name(self, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        store.add(make_institution(
            name="八宝山殡仪馆", type="funeral_home",
            province="北京", city="北京", source="测试",
        ))
        store.add(make_institution(
            name="东郊殡仪馆", type="funeral_home",
            province="北京", city="北京", source="测试",
        ))

        results = store.search(keyword="八宝山")
        assert len(results) == 1
        assert "八宝山" in results[0].name

    def test_search_by_keyword_in_address(self, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        store.add(make_institution(
            name="某殡仪馆", type="funeral_home",
            province="北京", city="北京", address="石景山路9号",
            source="测试",
        ))
        results = store.search(keyword="石景山")
        assert len(results) == 1

    def test_search_by_keyword_in_services(self, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        store.add(make_institution(
            name="某殡仪馆", type="funeral_home",
            province="北京", city="北京",
            services=["遗体接运", "火化", "骨灰寄存"],
            source="测试",
        ))
        results = store.search(keyword="骨灰寄存")
        assert len(results) == 1

    def test_search_by_keyword_case_insensitive(self, tmp_path: Path) -> None:
        # 大小写不敏感（中文不区分，主要测试 ASCII）
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        store.add(make_institution(
            name="Test 殡仪馆", type="funeral_home",
            province="北京", city="北京", source="测试",
        ))
        results = store.search(keyword="test")
        assert len(results) == 1


# =====================================================================
# 5. 低可信度标记
# =====================================================================


class TestLowConfidenceFlagged:
    """测试低可信度机构输出时被标记（retrieval-guardrails）"""

    def test_low_confidence_flagged(self, tmp_path: Path) -> None:
        # confidence=0.5（低可信）应触发 needs_verification_warning
        inst = make_institution(
            name="低可信殡仪馆",
            type="funeral_home",
            province="北京",
            city="北京",
            source="单一非官方源 2026",
            confidence=0.5,
        )
        assert inst.confidence == 0.5
        assert inst.needs_verification_warning() is True

    def test_high_confidence_not_flagged(self, tmp_path: Path) -> None:
        inst = make_institution(
            name="中可信殡仪馆",
            type="funeral_home",
            province="北京",
            city="北京",
            source="民政厅 2026",
            confidence=0.7,
        )
        assert inst.needs_verification_warning() is False

    def test_no_source_forced_low_confidence(self, tmp_path: Path) -> None:
        # retrieval-guardrails: 缺失 source 强制降级到 <0.5
        inst = make_institution(
            name="无来源殡仪馆",
            type="funeral_home",
            province="北京",
            city="北京",
            source="",
            confidence=0.7,  # 即使设了 0.7，缺 source 应被降级
        )
        assert inst.confidence < 0.5, "缺 source 应被强制降级到 < 0.5"
        assert inst.needs_verification_warning() is True


# =====================================================================
# 6. 从 seed.json 导入
# =====================================================================


class TestImportFromSeed:
    """测试从种子文件导入"""

    def test_import_from_seed(self, tmp_path: Path) -> None:
        # 构造一份小型种子数据
        seed_data = {
            "institutions": [
                {
                    "institution_id": "test_import_1",
                    "name": "导入测试殡仪馆A",
                    "type": "funeral_home",
                    "province": "江苏",
                    "city": "南京",
                    "address": "南京某地",
                    "services": ["遗体接运", "火化"],
                    "source": "江苏省民政厅 2026",
                    "confidence": 0.7,
                },
                {
                    "institution_id": "test_import_2",
                    "name": "导入测试公墓B",
                    "type": "cemetery",
                    "province": "江苏",
                    "city": "南京",
                    "source": "江苏省民政厅 2026",
                    "confidence": 0.7,
                },
            ]
        }
        seed_file = tmp_path / "seed_test.json"
        seed_file.write_text(json.dumps(seed_data, ensure_ascii=False), encoding="utf-8")

        # 准备一个独立 data_dir，加载种子（先空）
        data_dir = tmp_path / "store"
        store = InstitutionStore(auto_load_seed=False, data_dir=data_dir)
        assert store.count() == 0

        added = store.import_from_official_source(
            "江苏省民政厅 2026", seed_data["institutions"]
        )
        assert added == 2
        assert store.count() == 2

        # 搜索应能找到
        results = store.search(province="江苏")
        assert len(results) == 2

    def test_import_dedup(self, tmp_path: Path) -> None:
        # 同名同地址重复导入应去重
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path / "store")
        records = [
            {
                "institution_id": "dup_1",
                "name": "去重测试殡仪馆",
                "type": "funeral_home",
                "province": "浙江",
                "city": "杭州",
                "address": "杭州某地",
                "source": "浙江省民政厅 2026",
                "confidence": 0.7,
            }
        ]
        added1 = store.import_from_official_source("浙江省民政厅 2026", records)
        added2 = store.import_from_official_source("浙江省民政厅 2026", records)
        assert added1 == 1
        assert added2 == 0, "重复导入不应新增"
        assert store.count() == 1


# =====================================================================
# 7. 启动时种子数据加载
# =====================================================================


class TestSeedDataLoaded:
    """测试首次启动时自动加载包内 seed.json"""

    def test_seed_data_loaded(self, tmp_path: Path) -> None:
        # 用独立 data_dir，首次启动应自动加载包内 seed.json
        data_dir = tmp_path / "auto_seed"
        store = InstitutionStore(data_dir=data_dir)
        # seed.json 应至少有 18 条（北京 8 + 上海 5 + 重庆 5 = 18）
        assert store.count() >= 18, f"种子数据应至少 18 条，实际 {store.count()}"

        # 应能搜到北京 8 家殡仪馆
        beijing = store.search(province="北京", type="funeral_home")
        assert len(beijing) == 8, f"北京应有 8 家殡仪馆，实际 {len(beijing)}"

        # 应能搜到上海 5 家
        shanghai = store.search(province="上海", type="funeral_home")
        assert len(shanghai) == 5, f"上海应有 5 家殡仪馆，实际 {len(shanghai)}"

        # 应能搜到重庆 5 家
        chongqing = store.search(province="重庆", type="funeral_home")
        assert len(chongqing) == 5, f"重庆应有 5 家殡仪馆，实际 {len(chongqing)}"

        # 八宝山应能通过关键词搜到
        results = store.search(keyword="八宝山")
        assert any("八宝山" in r.name for r in results)

        # 所有种子条目应标 source
        for inst in store.search():
            assert inst.source, f"{inst.name} 缺 source"

    def test_seed_data_persisted_after_first_load(self, tmp_path: Path) -> None:
        # 第一次启动加载种子并保存
        data_dir = tmp_path / "persist"
        store1 = InstitutionStore(data_dir=data_dir)
        assert store1.count() >= 18
        store_file = data_dir / "institutions.json"
        assert store_file.exists(), "首次加载后应持久化到 institutions.json"

        # 第二次启动应从持久化文件加载（不再重新加载种子，但数量一致）
        store2 = InstitutionStore(data_dir=data_dir)
        assert store2.count() == store1.count()


# =====================================================================
# 8. update / delete 边界
# =====================================================================


class TestUpdateDelete:
    """测试 update / delete"""

    def test_update_changes_fields(self, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        inst = make_institution(
            name="更新前", type="funeral_home",
            province="北京", city="北京", source="测试",
        )
        store.add(inst)
        updated = store.update(inst.institution_id, {"name": "更新后", "phone": "111"})
        assert updated is not None
        assert updated.name == "更新后"
        assert updated.phone == "111"

    def test_update_unknown_id_returns_none(self, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        assert store.update("unknown_id", {"name": "x"}) is None

    def test_delete(self, tmp_path: Path) -> None:
        store = InstitutionStore(auto_load_seed=False, data_dir=tmp_path)
        inst = make_institution(
            name="待删除", type="funeral_home",
            province="北京", city="北京", source="测试",
        )
        store.add(inst)
        assert store.delete(inst.institution_id) is True
        assert store.get(inst.institution_id) is None
        assert store.delete(inst.institution_id) is False

    def test_invalid_type_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            Institution(
                institution_id="x",
                name="x",
                type="invalid_type",
                province="x",
                city="x",
                source="x",
            )

    def test_invalid_confidence_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            Institution(
                institution_id="x",
                name="x",
                type="funeral_home",
                province="x",
                city="x",
                source="x",
                confidence=1.5,
            )
