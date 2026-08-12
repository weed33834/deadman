"""数字遗产清单模块测试

覆盖：数据模型校验、store 加解密 roundtrip、清单生成结构正确。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deadman.digital_legacy import (
    AssetAction,
    AssetCategory,
    AssetRegister,
    DigitalAsset,
    DigitalLegacyStore,
    Heir,
    build_checklist,
    render_plan_markdown,
)


def _make_register() -> AssetRegister:
    heir = Heir(id="h1", name="长子", relationship="子女")
    assets = [
        DigitalAsset(
            id="a1",
            category=AssetCategory.CRYPTO.value,
            name="BTC 钱包",
            location="https://example.com",
            access_hint="助记词：alpha beta ...",
            action_on_death=AssetAction.TRANSFER.value,
            assigned_heir_id="h1",
            estimated_value="自填：约 X",
            sensitivity="secret",
        ),
        DigitalAsset(
            id="a2",
            category=AssetCategory.SOCIAL.value,
            name="微博账号",
            action_on_death=AssetAction.MEMORIALIZE.value,
            assigned_heir_id="h1",
        ),
        DigitalAsset(
            id="a3",
            category=AssetCategory.ACCOUNT.value,
            name="某云盘会员",
            action_on_death=AssetAction.CLOSE.value,
        ),
    ]
    return AssetRegister(user_id="u_test", heirs=[heir], assets=assets)


def test_model_normalizes_invalid_enum():
    a = DigitalAsset(id="x", category="未知类别", name="测试")
    assert a.category == AssetCategory.OTHER.value
    assert a.action_on_death == AssetAction.DECIDE.value


def test_register_summary_counts():
    reg = _make_register()
    s = reg.summary()
    assert s["total_assets"] == 3
    assert s["total_heirs"] == 1
    assert s["unassigned"] == 1  # a3 未指派
    assert s["by_category"].get(AssetCategory.CRYPTO.value) == 1


def test_store_roundtrip_encrypts_access_hint(tmp_path):
    pw = b"test-passphrase-123"
    store = DigitalLegacyStore("u_test", passphrase=pw, root=tmp_path)
    reg = _make_register()
    store.save(reg)

    # 落盘文件不应含明文 access_hint
    raw = (tmp_path / "u_test.json").read_text(encoding="utf-8")
    assert "助记词" not in raw
    assert "_enc_access_hint" in raw

    # 重新加载能还原明文
    store2 = DigitalLegacyStore("u_test", passphrase=pw, root=tmp_path)
    loaded = store2.load()
    assert loaded.heirs[0].name == "长子"
    btc = next(a for a in loaded.assets if a.id == "a1")
    assert btc.access_hint == "助记词：alpha beta ..."


def test_store_wrong_passphrase_clears_hint(tmp_path):
    store = DigitalLegacyStore("u_test", passphrase=b"pw1", root=tmp_path)
    store.save(_make_register())
    wrong = DigitalLegacyStore("u_test", passphrase=b"wrong", root=tmp_path)
    loaded = wrong.load()
    assert loaded.heirs[0].name == "长子"  # 非敏感字段仍可读
    btc = next(a for a in loaded.assets if a.id == "a1")
    assert btc.access_hint == ""  # 敏感字段解密失败置空


def test_crud_assign_and_remove(tmp_path):
    store = DigitalLegacyStore("u_test", passphrase=b"pw", root=tmp_path)
    reg = AssetRegister(user_id="u_test")
    reg = store.add_heir(Heir(id="h2", name="次子"))
    reg = store.add_asset(DigitalAsset(id="a9", category=AssetCategory.ACCOUNT.value, name="邮箱"))
    reg = store.assign_heir("a9", "h2")
    assert reg.assets[0].assigned_heir_id == "h2"
    reg = store.remove_asset("a9")
    assert len(reg.assets) == 0


def test_build_checklist_structure():
    reg = _make_register()
    chk = build_checklist(reg)
    assert chk["summary"]["total_assets"] == 3
    assert len(chk["items"]) == 3
    crypto_item = next(i for i in chk["items"] if i["asset_id"] == "a1")
    assert crypto_item["action_label"] == "转移给继承人"
    assert any("助记词" in st for st in crypto_item["steps"])
    assert crypto_item["assigned_heir"] == "长子"


def test_render_plan_markdown_contains_assets():
    md = render_plan_markdown(_make_register())
    assert "数字遗产清单" in md
    assert "BTC 钱包" in md
    assert "微博账号" in md
    # 数据纪律：不编造深链/电话/金额估算
    assert "深链" in md
