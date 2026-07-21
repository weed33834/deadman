"""测试 deadman.plan_score - Phase 15 身后事规划完整度评分

覆盖点（14 个）：
    1. test_empty_user_all_zero                空用户全 0 分 + 全部缺失项
    2. test_only_ending_note_scored            仅有 ending_note 的评分
    3. test_only_vault_scored                  仅有 vault 的评分
    4. test_full_user_near_100                 完整用户评分接近 100
    5. test_weighted_total_calculation         评分加权（总分 = 各类别加权平均）
    6. test_suggestions_match_missing_items    建议生成（缺失项 → 建议条目对应）
    7. test_weighted_calculation_unit          加权计算单元测试
    8. test_cli_plan_score_command             CLI plan-score 命令
    9. test_cli_plan_score_detail_command      CLI plan-score-detail 命令
    10. test_web_endpoint_unauthorized_401     Web 端点未认证 401
    11. test_web_endpoint_authorized_200       Web 端点认证后 200
    12. test_score_upper_bound_100             评分上限 100
    13. test_score_lower_bound_0               评分下限 0
    14. test_no_fabrication_based_on_real_data 不编造（评分基于实际数据）

测试隔离：
    - 每个测试用 tmp_path 构造独立数据目录，不污染 ~/.deadman
    - 注入独立 store / registry 到 PlanScorer
    - Web 端点测试用真实 HTTP server（随机端口，daemon 线程）
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from deadman.plan_score.models import Category, PlanScore, SubScore
from deadman.plan_score.scorer import PlanScorer, WEIGHTS


# =====================================================================
# 辅助：构造独立 PlanScorer（全部 store 指向 tmp_path）
# =====================================================================


def _make_scorer(tmp_path: Path) -> PlanScorer:
    """构造一个全部 store 都指向 tmp_path 子目录的 PlanScorer"""
    from deadman.ending_note.store import EndingNoteStore
    from deadman.vault.store import VaultStore
    from deadman.decedent_id.registry import DecedentRegistry
    from deadman.deadman_switch.store import SwitchStore
    from deadman.auth.store import UserStore

    return PlanScorer(
        ending_note_store=EndingNoteStore(data_dir=tmp_path / "ending_notes"),
        vault_store=VaultStore(data_dir=tmp_path / "vault"),
        decedent_registry=DecedentRegistry(data_dir=tmp_path / "cases"),
        switch_store=SwitchStore(data_dir=tmp_path / "deadman_switch"),
        user_store=UserStore(data_dir=tmp_path / "auth"),
    )


def _fill_ending_note_all_sections(
    store, user_id: str, has_will: bool = True
) -> None:
    """填充 9 章节终活笔记"""
    from deadman.ending_note.models import EndingNote

    note = EndingNote.new(user_id)
    note.personal_info = {"full_name_masked": "张**"}
    note.family_relations = [{"relation": "配偶", "name_masked": "李**"}]
    note.assets = [{"type": "房产", "description_masked": "北京市**"}]
    note.funeral_wishes = {"type": "火葬"}
    note.medical_wishes = {"life_sustaining": False}
    note.digital_legacy = [{"platform": "微信", "account_masked": "138****1234"}]
    note.messages = [{"recipient": "配偶", "content": "感谢"}]
    note.emergency_contacts = [{"role": "律师", "name_masked": "王**"}]
    note.will_intent = (
        {"has_formal_will": True} if has_will else {"intent_to_create": True}
    )
    store.save(note)


def _fill_vault_full(store, user_id: str) -> None:
    """填充保险库 4 项指标"""
    store.add_item(
        owner_user_id=user_id,
        type="password",
        title="邮箱密码",
        content="my-password",
        beneficiary_user_ids=["u-bene-1"],
        delivery_trigger="on_death",
    )
    store.add_item(
        owner_user_id=user_id,
        type="document",
        title="遗嘱扫描件",
        content="will-bytes",
        beneficiary_user_ids=["u-bene-1"],
        delivery_trigger="on_date",
        delivery_date=datetime.utcnow() + timedelta(days=365),
    )


def _fill_decedent_case_full(reg, user_id: str) -> None:
    """填充遗码通案例 3 项指标"""
    case = reg.create_case(
        owner_user_id=user_id,
        decedent_alias="我父亲",
        relationship="父母",
    )
    reg.add_event(
        case_id=case.case_id,
        owner_user_id=user_id,
        event="死亡证明已开具",
        agent="death-aftercare",
    )
    reg.archive_case(case.case_id, user_id)


def _fill_switch_full(store, user_id: str) -> None:
    """填充失联开关 4 项指标"""
    from deadman.deadman_switch.models import SwitchConfig

    cfg = SwitchConfig(
        emergency_contacts=["u-contact-1"],
        lawyer_user_id="u-lawyer-1",
        heir_user_ids=["u-heir-1"],
    )
    store.init_switch(user_id, cfg)


def _fill_user_full(store, email: str = "alice@example.com") -> str:
    """注册一个用户（默认 created_at 是 now，需要手动改到 8 天前以拿满分）"""
    user = store.register(email, "password123", email.split("@")[0].capitalize())
    # 直接修改 users.json 让 created_at 在 8 天前
    data = store._load()
    user_id = user["user_id"]
    old_created = data[user_id]["created_at"]
    # 解析 ISO 时间戳，改为 8 天前
    try:
        dt = datetime.fromisoformat(old_created)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        dt = datetime.utcnow()
    new_created = (dt - timedelta(days=8)).isoformat()
    data[user_id]["created_at"] = new_created
    store._atomic_write(data)
    return user_id


# =====================================================================
# 1. 空用户评分测试（全 0 分 + 全部缺失项）
# =====================================================================


def test_empty_user_all_zero(tmp_path: Path):
    """空用户：所有维度 0 分，total_score = 0，5 维度都有缺失项"""
    scorer = _make_scorer(tmp_path)
    result = scorer.score("empty-user-id")

    assert isinstance(result, PlanScore)
    assert result.user_id == "empty-user-id"
    assert result.total_score == 0
    assert len(result.category_scores) == 5
    for sub in result.category_scores:
        assert sub.score == 0, f"{sub.category} 应为 0 分，实际 {sub.score}"
        assert len(sub.missing_items) > 0, f"{sub.category} 应有缺失项"
        assert len(sub.completed_items) == 0, f"{sub.category} 不应有已完成项"


# =====================================================================
# 2. 仅有 ending_note 的评分测试
# =====================================================================


def test_only_ending_note_scored(tmp_path: Path):
    """仅有 ending_note：ENDING_NOTE 维度有分数，其他维度 0"""
    scorer = _make_scorer(tmp_path)
    # 填充 9 章节
    _fill_ending_note_all_sections(scorer._ending_note_store, "u1")
    result = scorer.score("u1")

    # 找到 ENDING_NOTE 维度
    en_score = next(
        s for s in result.category_scores if s.category == Category.ENDING_NOTE
    )
    assert en_score.score == 100, f"9 章节+will_intent 应满分，实际 {en_score.score}"
    assert len(en_score.completed_items) >= 9
    assert len(en_score.missing_items) == 0

    # 其他维度应为 0
    for cat in [
        Category.VAULT,
        Category.DECEDENT_CASE,
        Category.DEADMAN_SWITCH,
        Category.BASIC_INFO,
    ]:
        sub = next(s for s in result.category_scores if s.category == cat)
        assert sub.score == 0, f"{cat} 应为 0 分"

    # 加权总分 = 100 * 0.35 = 35
    assert result.total_score == 35


# =====================================================================
# 3. 仅有 vault 的评分测试
# =====================================================================


def test_only_vault_scored(tmp_path: Path):
    """仅有 vault：VAULT 维度满分，其他 0；总分 = 100*0.25 = 25"""
    scorer = _make_scorer(tmp_path)
    _fill_vault_full(scorer._vault_store, "u2")
    result = scorer.score("u2")

    vault_score = next(
        s for s in result.category_scores if s.category == Category.VAULT
    )
    assert vault_score.score == 100, f"4 项全填应满分，实际 {vault_score.score}"
    assert len(vault_score.completed_items) == 4
    assert len(vault_score.missing_items) == 0

    # 总分 = 100 * 0.25 = 25
    assert result.total_score == 25


# =====================================================================
# 4. 完整用户评分测试（所有模块都有数据，接近 100 分）
# =====================================================================


def test_full_user_near_100(tmp_path: Path):
    """完整用户：5 维度全部满分，total_score 应为 100"""
    scorer = _make_scorer(tmp_path)
    # 注册一个用户（created_at 在 8 天前以拿满分）
    user_id = _fill_user_full(scorer._user_store, "full@example.com")
    _fill_ending_note_all_sections(scorer._ending_note_store, user_id)
    _fill_vault_full(scorer._vault_store, user_id)
    _fill_decedent_case_full(scorer._decedent_registry, user_id)
    _fill_switch_full(scorer._switch_store, user_id)

    result = scorer.score(user_id)
    # 每个维度都应满分
    for sub in result.category_scores:
        assert sub.score == 100, (
            f"{sub.category} 应为 100 分，实际 {sub.score}；"
            f"completed={sub.completed_items}, missing={sub.missing_items}"
        )
    assert result.total_score == 100


# =====================================================================
# 5. 评分加权测试（总分 = 各类别加权平均）
# =====================================================================


def test_weighted_total_calculation(tmp_path: Path):
    """总分 = sum(各维度 score * weight)"""
    scorer = _make_scorer(tmp_path)
    # 仅填 ending_note（100）+ basic_info（部分）
    # 注册一个用户（默认 created_at 是 now，basic_info 不会满分）
    user = scorer._user_store.register(
        "weighted@example.com", "password123", "Weighted"
    )
    user_id = user["user_id"]
    _fill_ending_note_all_sections(scorer._ending_note_store, user_id)
    result = scorer.score(user_id)

    # 手算期望值
    score_map = {s.category: s.score for s in result.category_scores}
    expected = sum(score_map[c] * w for c, w in WEIGHTS.items())
    expected_int = round(expected)
    if expected_int < 0:
        expected_int = 0
    if expected_int > 100:
        expected_int = 100

    assert result.total_score == expected_int, (
        f"总分应={expected_int}（加权={expected}），实际={result.total_score}"
    )


# =====================================================================
# 6. 建议生成测试（缺失项 → 建议条目对应）
# =====================================================================


def test_suggestions_match_missing_items(tmp_path: Path):
    """缺失项非空时，对应维度应有建议"""
    scorer = _make_scorer(tmp_path)
    result = scorer.score("no-data-user")

    # 至少一个维度有缺失项 + 建议
    has_suggestion = False
    for sub in result.category_scores:
        if sub.missing_items:
            assert len(sub.suggestions) > 0, (
                f"{sub.category} 有缺失项但无建议"
            )
            has_suggestion = True
    assert has_suggestion, "至少一个维度应有建议"

    # overall_suggestions 应不超过 3 条
    assert len(result.overall_suggestions) <= 3
    # 至少有 1 条 top 建议
    assert len(result.overall_suggestions) >= 1


# =====================================================================
# 7. 加权计算单元测试
# =====================================================================


def test_weighted_calculation_unit():
    """单元测试 _weighted_total 静态方法"""
    # 全 0
    subs = [
        SubScore(category=Category.ENDING_NOTE, score=0),
        SubScore(category=Category.VAULT, score=0),
        SubScore(category=Category.DECEDENT_CASE, score=0),
        SubScore(category=Category.DEADMAN_SWITCH, score=0),
        SubScore(category=Category.BASIC_INFO, score=0),
    ]
    assert PlanScorer._weighted_total(subs) == 0

    # 全 100
    subs = [
        SubScore(category=Category.ENDING_NOTE, score=100),
        SubScore(category=Category.VAULT, score=100),
        SubScore(category=Category.DECEDENT_CASE, score=100),
        SubScore(category=Category.DEADMAN_SWITCH, score=100),
        SubScore(category=Category.BASIC_INFO, score=100),
    ]
    assert PlanScorer._weighted_total(subs) == 100

    # 部分填充：ENDING_NOTE=100, 其他=0 → 35
    subs = [
        SubScore(category=Category.ENDING_NOTE, score=100),
        SubScore(category=Category.VAULT, score=0),
        SubScore(category=Category.DECEDENT_CASE, score=0),
        SubScore(category=Category.DEADMAN_SWITCH, score=0),
        SubScore(category=Category.BASIC_INFO, score=0),
    ]
    assert PlanScorer._weighted_total(subs) == 35

    # VAULT=80, BASIC_INFO=50 → 80*0.25 + 50*0.10 = 25
    subs = [
        SubScore(category=Category.ENDING_NOTE, score=0),
        SubScore(category=Category.VAULT, score=80),
        SubScore(category=Category.DECEDENT_CASE, score=0),
        SubScore(category=Category.DEADMAN_SWITCH, score=0),
        SubScore(category=Category.BASIC_INFO, score=50),
    ]
    assert PlanScorer._weighted_total(subs) == 25


# =====================================================================
# 8. CLI 命令测试
# =====================================================================


def test_cli_plan_score_command(tmp_path: Path, capsys, monkeypatch):
    """plan-score CLI 子命令"""
    # 把 home 改到 tmp_path，避免污染 ~/.deadman
    monkeypatch.setenv("HOME", str(tmp_path))
    # 重要：UserStore 用 Path.home()，所以 HOME 必须改
    # EndingNoteStore 也用 Path.home()

    from deadman._cli_extensions.phase15_score import cmd_plan_score
    from deadman.auth.store import UserStore

    # 注册一个用户（用同样的 tmp_path home）
    store = UserStore(data_dir=tmp_path / "auth")
    user = store.register("cli@example.com", "password123", "CLI")
    user_id = user["user_id"]

    args = argparse.Namespace(user_id=user_id, token=None)
    cmd_plan_score(args)

    out = capsys.readouterr().out
    assert "身后事规划完整度评分" in out
    assert "total_score" in out
    assert "top 3 优先建议" in out
    assert "边界告知" in out


def test_cli_plan_score_detail_command(tmp_path: Path, capsys, monkeypatch):
    """plan-score-detail CLI 子命令"""
    monkeypatch.setenv("HOME", str(tmp_path))

    from deadman._cli_extensions.phase15_score import cmd_plan_score_detail
    from deadman.auth.store import UserStore

    store = UserStore(data_dir=tmp_path / "auth")
    user = store.register("detail@example.com", "password123", "Detail")
    user_id = user["user_id"]

    args = argparse.Namespace(user_id=user_id, token=None)
    cmd_plan_score_detail(args)

    out = capsys.readouterr().out
    assert "身后事规划完整度评分（详细）" in out
    assert "ENDING_NOTE" in out
    assert "VAULT" in out
    assert "已完成项" in out
    assert "缺失项" in out
    assert "维度建议" in out
    assert "JSON" in out
    assert "边界告知" in out


# =====================================================================
# 9. Web 端点测试（401 / 200）
# =====================================================================


def _get_free_port() -> int:
    """获取一个可用端口"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(port: int, timeout: float = 5.0) -> bool:
    """等待服务器就绪（轮询 /api/health）"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            conn.close()
            if resp.status == 200:
                return True
        except (ConnectionError, OSError):
            pass
        time.sleep(0.1)
    return False


def _register_and_get_token(port: int) -> str:
    """通过 HTTP 注册并拿 token"""
    body = json.dumps({
        "email": "webtest@example.com",
        "password": "password123",
        "display_name": "WebTest",
    })
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/auth/register", body=body, headers={
        "Content-Type": "application/json",
    })
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return data["token"]


def test_web_endpoint_unauthorized_401(tmp_path: Path, monkeypatch):
    """未认证访问 /api/plan-score 返回 401"""
    # 重要：monkeypatch settings 的 auth_data_dir，避免污染 ~/.deadman
    from deadman.config import settings
    monkeypatch.setattr(settings, "auth_data_dir", tmp_path / "auth")
    monkeypatch.setattr(settings, "jwt_secret", "")
    monkeypatch.setattr(settings, "jwt_expiry_days", 7)
    monkeypatch.setattr(settings, "password_min_length", 8)

    port = _get_free_port()
    from deadman.web.server import WebServer
    server = WebServer()
    thread = threading.Thread(
        target=server.run,
        args=("127.0.0.1", port),
        daemon=True,
    )
    thread.start()

    try:
        assert _wait_for_server(port), "服务器未在超时内启动"
        # 不带 token
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/plan-score")
        resp = conn.getresponse()
        assert resp.status == 401, f"未认证应 401，实际 {resp.status}"
        body = json.loads(resp.read().decode("utf-8"))
        assert "error" in body
        conn.close()
    finally:
        pass  # daemon 线程随进程退出


def test_web_endpoint_authorized_200(tmp_path: Path, monkeypatch):
    """认证后访问 /api/plan-score 返回 200 + 评分 + disclaimer"""
    import secrets as _secrets
    from deadman.config import settings
    unique_dir = tmp_path / f"auth-{_secrets.token_hex(4)}"
    monkeypatch.setattr(settings, "auth_data_dir", unique_dir)
    monkeypatch.setattr(settings, "jwt_secret", "")
    monkeypatch.setattr(settings, "jwt_expiry_days", 7)
    monkeypatch.setattr(settings, "password_min_length", 8)

    port = _get_free_port()
    from deadman.web.server import WebServer
    server = WebServer()
    thread = threading.Thread(
        target=server.run,
        args=("127.0.0.1", port),
        daemon=True,
    )
    thread.start()

    try:
        assert _wait_for_server(port), "服务器未在超时内启动"
        token = _register_and_get_token(port)
        assert token

        # 带 token 访问 /api/plan-score
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/plan-score", headers={
            "Authorization": f"Bearer {token}",
        })
        resp = conn.getresponse()
        assert resp.status == 200, f"认证后应 200，实际 {resp.status}"
        body = json.loads(resp.read().decode("utf-8"))
        assert "total_score" in body
        assert "category_scores" in body
        assert "overall_suggestions" in body
        assert "disclaimer" in body
        assert "评分仅反映信息完整度" in body["disclaimer"]
        conn.close()

        # 带 token 访问 /api/plan-score/detail
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/plan-score/detail", headers={
            "Authorization": f"Bearer {token}",
        })
        resp = conn.getresponse()
        assert resp.status == 200
        body = json.loads(resp.read().decode("utf-8"))
        assert "category_scores" in body
        # 详细分解应有 5 个维度
        assert len(body["category_scores"]) == 5
        conn.close()
    finally:
        pass


# =====================================================================
# 10. 评分上限测试（不超过 100）
# =====================================================================


def test_score_upper_bound_100():
    """评分上限：即使加权后超过 100，也应 clamp 到 100"""
    # 构造一个 mock，让所有维度都返回 200 分（超上限）
    mock_en_store = MagicMock()
    mock_en_store.load.return_value = None  # 触发 0 分路径，但 mock 不重要

    # 直接用 SubScore 构造一个超上限的列表
    subs = [
        SubScore(category=Category.ENDING_NOTE, score=200),
        SubScore(category=Category.VAULT, score=200),
        SubScore(category=Category.DECEDENT_CASE, score=200),
        SubScore(category=Category.DEADMAN_SWITCH, score=200),
        SubScore(category=Category.BASIC_INFO, score=200),
    ]
    total = PlanScorer._weighted_total(subs)
    assert total == 100, f"超上限应 clamp 到 100，实际 {total}"


# =====================================================================
# 11. 评分下限测试（不低于 0）
# =====================================================================


def test_score_lower_bound_0():
    """评分下限：即使加权后为负数，也应 clamp 到 0"""
    subs = [
        SubScore(category=Category.ENDING_NOTE, score=-50),
        SubScore(category=Category.VAULT, score=-50),
        SubScore(category=Category.DECEDENT_CASE, score=-50),
        SubScore(category=Category.DEADMAN_SWITCH, score=-50),
        SubScore(category=Category.BASIC_INFO, score=-50),
    ]
    total = PlanScorer._weighted_total(subs)
    assert total == 0, f"低于 0 应 clamp 到 0，实际 {total}"


# =====================================================================
# 12. 不编造测试（评分基于实际数据）
# =====================================================================


def test_no_fabrication_based_on_real_data(tmp_path: Path):
    """integrity-framework.md L1：评分基于实际数据，不编造

    验证：
        - 空用户：completed_items 为空（不编造已完成项）
        - 空用户：missing_items 全部为"未填写/未配置"等真实缺失描述
        - 空用户：suggestions 是引导用户去填，不是"建议你已经做到"
    """
    scorer = _make_scorer(tmp_path)
    result = scorer.score("fabrication-check-user")

    for sub in result.category_scores:
        # 空用户不应有任何已完成项
        assert len(sub.completed_items) == 0, (
            f"{sub.category} 不应编造已完成项: {sub.completed_items}"
        )
        # 缺失项应明确描述"未填写/未配置/未创建"等
        assert len(sub.missing_items) > 0
        for missing in sub.missing_items:
            # 缺失项描述应含"未"/"缺少"/"无"等否定语义
            assert any(
                kw in missing for kw in ["未", "缺少", "无", "不足"]
            ), f"{sub.category} 缺失项描述不含否定语义: {missing}"
        # 建议应是引导用户去填，不是断言已完成
        for sug in sub.suggestions:
            assert any(
                kw in sug for kw in ["建议", "请"]
            ), f"{sub.category} 建议不含引导语义: {sug}"


# =====================================================================
# 13. 模型 to_dict 序列化测试（附加，验证数据可 JSON 化）
# =====================================================================


def test_plan_score_to_dict_serializable(tmp_path: Path):
    """PlanScore.to_dict() 输出可 JSON 序列化"""
    scorer = _make_scorer(tmp_path)
    result = scorer.score("serialize-user")
    d = result.to_dict()
    # 应可 JSON 序列化
    json_str = json.dumps(d, ensure_ascii=False)
    assert isinstance(json_str, str)
    # 反序列化后字段完整
    parsed = json.loads(json_str)
    assert parsed["user_id"] == "serialize-user"
    assert "total_score" in parsed
    assert "category_scores" in parsed
    assert "overall_suggestions" in parsed
    assert "generated_at" in parsed
    assert len(parsed["category_scores"]) == 5


# =====================================================================
# 14. 维度权重合计 = 1.0 测试（附加，验证权重设计正确）
# =====================================================================


def test_weights_sum_to_one():
    """5 维度权重合计 = 1.0"""
    total = sum(WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9, f"权重合计应=1.0，实际={total}"
    # 应有 5 个维度
    assert len(WEIGHTS) == 5
    # ending_note 权重最大（35%）
    assert WEIGHTS[Category.ENDING_NOTE] == 0.35
    # basic_info 权重最小（10%）
    assert WEIGHTS[Category.BASIC_INFO] == 0.10
