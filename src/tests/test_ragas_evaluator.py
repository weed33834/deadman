"""测试 deadman.evaluation.ragas_evaluator - P0.2 RAGAS 9 维度评估

覆盖点:
  - 降级检测:RAGAS 不可用时返回 degraded=true,不抛异常
  - 降级检测:LLM api_key 未配置时返回 degraded=true
  - LLM 适配器:DeadmanRagasLLM 包装 llm_client 成功
  - 批量评估:run_ragas_batch 加载 YAML case 正常
  - 质量门:faithfulness < threshold 时 quality_gate_passed=False
  - 扩展维度:completeness(关键词命中率) + safety(规则链结果)
  - quick 模式:仅评估 faithfulness + answer_relevancy

标记:
  - pytest.mark.slow:涉及真实 LLM 调用(默认不在 CI 跑)
  - pytest.mark.ragas:需要 ragas 包(默认不跑)

运行:
  pytest test_ragas_evaluator.py -v -m "not slow and not ragas"   # 快速跑降级路径
  pytest test_ragas_evaluator.py -v -m ragas                       # 完整 RAGAS 评估(需 LLM key)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from deadman.evaluation.ragas_evaluator import (
    ALL_METRIC_NAMES,
    DEFAULT_QUALITY_GATE_THRESHOLD,
    QUICK_METRIC_NAMES,
    DeadmanRagasLLM,
    QualityGateError,
    RAGASEvaluator,
    RagasResult,
    _load_case_for_ragas,
    run_ragas_batch,
)

# =====================================================================
# 降级检测 - RAGAS 不可用 / LLM 不可用
# =====================================================================


class TestDegradation:
    """降级保护:绝不抛异常,绝不阻断 CI"""

    async def test_ragas_unavailable_returns_degraded(self, monkeypatch):
        """RAGAS 包未安装 → 返回 degraded=true,不抛异常"""
        evaluator = RAGASEvaluator()
        # 强制设为不可用
        monkeypatch.setattr(evaluator, "available", False)

        result = await evaluator.evaluate(question="q", answer="a", contexts=["c1"])
        assert result["available"] is False
        assert result["degraded"] is True
        assert "未安装" in result["note"]

    async def test_llm_no_api_key_returns_degraded(self, monkeypatch):
        """LLM api_key 未配置 → 返回 degraded=true,不抛异常"""
        evaluator = RAGASEvaluator()
        # 强制设为 available,但 LLM client 无 api_key
        monkeypatch.setattr(evaluator, "available", True)
        mock_llm = MagicMock()
        mock_llm.api_key = None  # 无 key
        evaluator._ragas_llm = DeadmanRagasLLM(mock_llm)

        result = await evaluator.evaluate(question="q", answer="a", contexts=["c1"])
        assert result["available"] is True
        assert result["degraded"] is True
        assert "api_key" in result["note"]

    async def test_llm_init_failure_returns_degraded(self, monkeypatch):
        """LLM 客户端初始化抛异常 → 返回 degraded=true"""
        evaluator = RAGASEvaluator()
        monkeypatch.setattr(evaluator, "available", True)

        # 替换 _get_ragas_llm 让其抛异常
        def raise_exc():
            raise RuntimeError("LLM 模块缺失")

        monkeypatch.setattr(evaluator, "_get_ragas_llm", raise_exc)

        result = await evaluator.evaluate(question="q", answer="a", contexts=["c1"])
        assert result["degraded"] is True
        assert any("LLM" in err for err in result["errors"])


# =====================================================================
# DeadmanRagasLLM 适配器
# =====================================================================


class TestDeadmanRagasLLM:
    """测试 LLM 适配器"""

    def test_extract_prompt_text_str(self):
        """str prompt 直接返回"""
        adapter = DeadmanRagasLLM(MagicMock(api_key="x"))
        assert adapter._extract_prompt_text("hello") == "hello"

    def test_extract_prompt_text_none(self):
        """None prompt 返回空字符串"""
        adapter = DeadmanRagasLLM(MagicMock(api_key="x"))
        assert adapter._extract_prompt_text(None) == ""

    def test_extract_prompt_text_object_with_to_string(self):
        """有 to_string 方法的对象调用之"""
        adapter = DeadmanRagasLLM(MagicMock(api_key="x"))

        class FakePrompt:
            def to_string(self):
                return "fake-prompt-text"

        assert adapter._extract_prompt_text(FakePrompt()) == "fake-prompt-text"

    async def test_agenerate_no_api_key_raises(self):
        """无 api_key 时 agenerate_text 抛 RuntimeError"""
        adapter = DeadmanRagasLLM(MagicMock(api_key=None))
        with pytest.raises(RuntimeError, match="api_key"):
            await adapter.agenerate_text("test prompt")

    async def test_agenerate_calls_llm_chat(self):
        """有 api_key 时调用 llm_client.chat"""
        mock_llm = MagicMock(api_key="sk-test")
        mock_llm.chat = AsyncMock(return_value="LLM response")
        adapter = DeadmanRagasLLM(mock_llm)

        await adapter.agenerate_text("hello")
        # 应调用 chat
        mock_llm.chat.assert_called_once()
        # 返回应是 LLMResult 或 dict(取决于 langchain_core 是否可用)
        # 主要验证不抛异常且 chat 被调用
        assert mock_llm.chat.call_args is not None

    def test_agenerate_caches_repeated_prompts(self):
        """重复相同 prompt 应命中缓存"""
        mock_llm = MagicMock(api_key="sk-test")
        mock_llm.chat = AsyncMock(return_value="cached-response")
        adapter = DeadmanRagasLLM(mock_llm)

        # 第一次调用
        asyncio.run(adapter.agenerate_text("same-prompt", n=1, temperature=0.01))
        # 第二次相同 prompt
        asyncio.run(adapter.agenerate_text("same-prompt", n=1, temperature=0.01))
        # chat 应只被调用 1 次(第二次命中缓存)
        assert mock_llm.chat.call_count == 1


# =====================================================================
# RAGASEvaluator - 扩展维度(completeness / safety)
# =====================================================================


class TestExtensionMetrics:
    """测试 deadman 自定义的 completeness + safety 维度"""

    def test_completeness_no_keywords(self):
        """无 expected_keywords → 完整性 1.0"""
        evaluator = RAGASEvaluator()
        assert evaluator._compute_completeness("any answer", None) == 1.0

    def test_completeness_all_hits(self):
        """全部关键词命中 → 1.0"""
        evaluator = RAGASEvaluator()
        score = evaluator._compute_completeness("建议咨询当地医保部门", ["医保", "部门"])
        assert score == 1.0

    def test_completeness_partial_hits(self):
        """部分关键词命中 → 比例"""
        evaluator = RAGASEvaluator()
        score = evaluator._compute_completeness("建议咨询医保", ["医保", "社保", "商保"])
        assert score == pytest.approx(1 / 3)

    def test_completeness_case_insensitive(self):
        """关键词匹配应大小写不敏感"""
        evaluator = RAGASEvaluator()
        score = evaluator._compute_completeness("Please contact Medicare", ["medicare", "Medicaid"])
        assert score == 0.5

    def test_safety_no_rule_check(self):
        """无规则校验结果 → 中性 0.5"""
        evaluator = RAGASEvaluator()
        assert evaluator._compute_safety(None) == 0.5

    def test_safety_passed(self):
        """规则校验通过 → 1.0"""
        evaluator = RAGASEvaluator()
        rule_result = {"passed": True, "violations": [], "violations_count": 0}
        assert evaluator._compute_safety(rule_result) == 1.0

    def test_safety_failed_with_critical(self):
        """有 L0/L1/L2 严重违规 → 0.0"""
        evaluator = RAGASEvaluator()
        rule_result = {
            "passed": False,
            "violations": [{"level": "L0", "rule": "integrity"}],
            "violations_count": 1,
        }
        assert evaluator._compute_safety(rule_result) == 0.0

    def test_safety_failed_minor_only(self):
        """仅有非严重违规 → 0.3"""
        evaluator = RAGASEvaluator()
        rule_result = {
            "passed": False,
            "violations": [{"level": "L7", "rule": "style"}],
            "violations_count": 1,
        }
        assert evaluator._compute_safety(rule_result) == 0.3


# =====================================================================
# 质量门
# =====================================================================


class TestQualityGate:
    """测试质量门阈值判定"""

    def test_quality_gate_threshold_default(self):
        """默认阈值 0.7"""
        evaluator = RAGASEvaluator()
        assert evaluator.quality_gate_threshold == DEFAULT_QUALITY_GATE_THRESHOLD
        assert DEFAULT_QUALITY_GATE_THRESHOLD == 0.7

    def test_quality_gate_custom_threshold(self):
        """可自定义阈值"""
        evaluator = RAGASEvaluator(quality_gate_threshold=0.85)
        assert evaluator.quality_gate_threshold == 0.85

    def test_quality_gate_error_class(self):
        """QualityGateError 可正常实例化"""
        err = QualityGateError("faithfulness 太低", 0.5, 0.7)
        assert err.faithfulness == 0.5
        assert err.threshold == 0.7
        assert "faithfulness" in str(err) or "低" in str(err)


# =====================================================================
# quick 模式
# =====================================================================


class TestQuickMode:
    """测试 quick 模式仅选关键指标"""

    def test_quick_mode_selects_only_two(self):
        """quick 模式仅选 faithfulness + answer_relevancy"""
        evaluator = RAGASEvaluator(quick_mode=True)
        metrics = evaluator._select_metrics()
        assert set(metrics) == set(QUICK_METRIC_NAMES)
        assert len(metrics) == 2

    def test_full_mode_selects_more(self):
        """非 quick 模式选更多指标"""
        evaluator = RAGASEvaluator(quick_mode=False)
        metrics = evaluator._select_metrics()
        # 全量指标应不少于 quick 模式
        assert len(metrics) >= 2


# =====================================================================
# 9 维度名称常量
# =====================================================================


class TestMetricNames:
    """测试 9 维度名称常量"""

    def test_all_metric_names_count(self):
        """9 个维度全部声明"""
        assert len(ALL_METRIC_NAMES) == 9

    def test_all_metric_names_include_key(self):
        """关键维度都在"""
        for name in [
            "faithfulness",
            "answer_relevancy",
            "context_precision",
            "context_recall",
            "answer_correctness",
            "completeness",
            "safety",
        ]:
            assert name in ALL_METRIC_NAMES

    def test_quick_metric_names_count(self):
        """quick 模式 2 个"""
        assert len(QUICK_METRIC_NAMES) == 2
        assert "faithfulness" in QUICK_METRIC_NAMES
        assert "answer_relevancy" in QUICK_METRIC_NAMES


# =====================================================================
# RagasResult 数据类
# =====================================================================


class TestRagasResult:
    """测试 RagasResult 数据类"""

    def test_default_values(self):
        """默认值"""
        result = RagasResult()
        assert result.available is False
        assert result.degraded is True
        assert result.metrics == {}
        assert result.errors == []
        assert result.quality_gate_passed is None
        assert result.quality_gate_threshold == DEFAULT_QUALITY_GATE_THRESHOLD

    def test_to_dict(self):
        """to_dict 包含所有字段"""
        result = RagasResult(
            available=True,
            degraded=False,
            metrics={"faithfulness": 0.85},
            errors=["some warning"],
            quality_gate_passed=True,
        )
        d = result.to_dict()
        assert d["available"] is True
        assert d["degraded"] is False
        assert d["metrics"]["faithfulness"] == 0.85
        assert d["quality_gate_passed"] is True
        assert "some warning" in d["errors"]


# =====================================================================
# YAML case 加载
# =====================================================================


class TestCaseLoading:
    """测试从 YAML case 加载 RAGAS 评估字段"""

    def test_load_existing_case(self):
        """加载真实 case 文件"""
        case_path = (
            Path(__file__).resolve().parent.parent.parent
            / "tests"
            / "automated"
            / "cases"
            / "case-01-no-fabrication.yaml"
        )
        if not case_path.exists():
            pytest.skip(f"case 文件不存在: {case_path}")

        result = _load_case_for_ragas(case_path)
        assert result is not None
        assert "question" in result
        assert "expected_keywords" in result
        assert "category" in result
        assert "case_id" in result
        # case-01 应有 expected_keywords(从 keyword_must_hit 提取)
        assert len(result["expected_keywords"]) > 0
        # 应包含 "医保局" 之类
        assert any("医保" in kw for kw in result["expected_keywords"])

    def test_load_nonexistent_case(self):
        """不存在文件返回 None"""
        result = _load_case_for_ragas("/tmp/nonexistent-case-12345.yaml")
        assert result is None


# =====================================================================
# run_ragas_batch - 批量评估
# =====================================================================


class TestRunRagasBatch:
    """测试批量评估"""

    async def test_batch_empty_dir(self, tmp_path):
        """空目录 → total=0"""
        result = await run_ragas_batch(str(tmp_path))
        assert result["total"] == 0
        assert result["evaluated"] == 0
        assert result["results"] == []

    async def test_batch_nonexistent_dir(self):
        """不存在目录 → total=0"""
        result = await run_ragas_batch("/tmp/nonexistent-dir-12345")
        assert result["total"] == 0

    async def test_batch_with_mock_provider_degraded(self, tmp_path):
        """带 mock provider 但 LLM 不可用 → 全部降级"""
        # 构造 fake case YAML
        case_file = tmp_path / "case-test.yaml"
        case_file.write_text(
            "case_id: '99'\n"
            "name: 'test case'\n"
            "category: 'test'\n"
            "user_input: 'test question'\n"
            "evaluation:\n"
            "  keyword_must_hit:\n"
            "    - keywords: ['foo', 'bar']\n",
            encoding="utf-8",
        )

        # sync mock provider 返回 answer + contexts
        def provider(case):
            return ("mock answer", ["ctx1"])

        # 用无 api_key 的 evaluator
        mock_llm = MagicMock(api_key=None)
        evaluator = RAGASEvaluator()
        evaluator._ragas_llm = DeadmanRagasLLM(mock_llm)

        result = await run_ragas_batch(
            cases_dir=str(tmp_path),
            evaluator=evaluator,
            mock_answer_provider=provider,
        )
        assert result["total"] == 1
        # 应全部降级(LLM 无 key)
        assert result["degraded"] == result["evaluated"]

    async def test_batch_writes_output_jsonl(self, tmp_path):
        """--output 写 JSONL 文件"""
        case_file = tmp_path / "case-out.yaml"
        case_file.write_text(
            "case_id: 'out'\nname: 'output test'\ncategory: 'test'\n"
            "user_input: 'q'\nevaluation:\n  keyword_must_hit: []\n",
            encoding="utf-8",
        )
        output_file = tmp_path / "out.jsonl"

        mock_llm = MagicMock(api_key=None)
        evaluator = RAGASEvaluator()
        evaluator._ragas_llm = DeadmanRagasLLM(mock_llm)

        await run_ragas_batch(
            cases_dir=str(tmp_path),
            evaluator=evaluator,
            output_file=str(output_file),
        )
        # 应写出 JSONL 文件
        assert output_file.exists()
        content = output_file.read_text(encoding="utf-8").strip()
        assert content  # 非空

    async def test_batch_supports_async_provider(self, tmp_path):
        """async provider 也应被支持"""
        case_file = tmp_path / "case-async.yaml"
        case_file.write_text(
            "case_id: 'async'\nname: 'async test'\ncategory: 'test'\n"
            "user_input: 'q'\nevaluation:\n  keyword_must_hit: []\n",
            encoding="utf-8",
        )

        async def async_provider(case):
            return ("async answer", ["ctx-async"])

        mock_llm = MagicMock(api_key=None)
        evaluator = RAGASEvaluator()
        evaluator._ragas_llm = DeadmanRagasLLM(mock_llm)

        result = await run_ragas_batch(
            cases_dir=str(tmp_path),
            evaluator=evaluator,
            mock_answer_provider=async_provider,
        )
        assert result["total"] == 1
        assert result["evaluated"] == 1


# =====================================================================
# 端到端(需要真实 RAGAS + LLM) - 默认不跑
# =====================================================================


@pytest.mark.slow
@pytest.mark.ragas
class TestEndToEndWithRealLLM:
    """端到端测试 - 需要真实 ragas 包 + LLM api_key

    运行方式:
        pytest test_ragas_evaluator.py::TestEndToEndWithRealLLM -v -m ragas
    """

    async def test_real_evaluation_with_mock_llm_chat(self):
        """用 mock LLM chat 但真实 RAGAS,验证不抛异常"""
        if not RAGASEvaluator().available:
            pytest.skip("ragas 包未安装")

        mock_llm = MagicMock(api_key="sk-mock")
        mock_llm.chat = AsyncMock(return_value="mock answer text")
        mock_llm.chat_json = AsyncMock(return_value={"faithfulness": 0.8})

        evaluator = RAGASEvaluator(llm_client=mock_llm, quick_mode=True)
        result = await evaluator.evaluate(
            question="What is Medicare?",
            answer="Medicare is a US health insurance program for seniors.",
            contexts=["Medicare provides health coverage for people 65 and older."],
        )
        # 不论分数如何,应返回 dict 且不抛异常
        assert isinstance(result, dict)
        assert "available" in result
        assert "degraded" in result
