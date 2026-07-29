"""测试 deadman.evaluation - 三层判定 + 工具调用校验

覆盖点：
  - RegexChecker 正则黑名单检测
  - KeywordChecker 关键词必中检测
  - ArgValidator 5 种校验类型（optional/non_empty/exact/contains/regex）
  - validate_tool_calls 综合校验
"""

from __future__ import annotations

from deadman.evaluation.three_layer import (
    KeywordChecker,
    RegexChecker,
    ThreeLayerEvaluator,
)
from deadman.evaluation.tool_calls import (
    ArgValidator,
    _is_subsequence,
    _match_result,
    validate_tool_calls,
)

# =====================================================================
# RegexChecker - 正则黑名单
# =====================================================================


class TestRegexChecker:
    """测试 RegexChecker 正则黑名单检测"""

    async def test_clean_response_passes(self):
        # 干净响应不命中黑名单
        checker = RegexChecker()
        patterns = [
            {"pattern": r"大概\s*\d+", "reason": "编造数字"},
            {"pattern": r"应该\s*\d+", "reason": "编造数字"},
        ]
        passed, failures = await checker.check_regex_blacklist(
            "建议咨询当地医保部门。", patterns
        )
        assert passed is True
        assert failures == []

    async def test_hit_blacklist_fails(self):
        # 命中黑名单 → 不通过
        checker = RegexChecker()
        patterns = [{"pattern": r"大概\s*\d+", "reason": "编造数字"}]
        passed, failures = await checker.check_regex_blacklist(
            "大概7天就能办下来", patterns
        )
        assert passed is False
        assert len(failures) == 1
        assert failures[0]["reason"] == "编造数字"
        assert "matched_text" in failures[0]

    async def test_multiple_patterns_multiple_failures(self):
        # 多个模式同时命中 → 多条 failure
        checker = RegexChecker()
        patterns = [
            {"pattern": r"大概\s*\d+", "reason": "大概"},
            {"pattern": r"应该\s*\d+", "reason": "应该"},
        ]
        passed, failures = await checker.check_regex_blacklist(
            "大概7天，应该15天", patterns
        )
        assert passed is False
        assert len(failures) == 2

    async def test_empty_patterns_passes(self):
        # 空模式列表 → 通过
        checker = RegexChecker()
        passed, failures = await checker.check_regex_blacklist("任何文本", [])
        assert passed is True
        assert failures == []

    async def test_invalid_regex_skipped(self):
        # 非法正则被跳过，不阻断流程
        checker = RegexChecker()
        patterns = [
            {"pattern": r"[invalid", "reason": "非法正则"},
            {"pattern": r"正常", "reason": "正常模式"},
        ]
        passed, failures = await checker.check_regex_blacklist("正常文本", patterns)
        # 非法正则被跳过，正常模式命中
        assert len(failures) == 1
        assert failures[0]["reason"] == "正常模式"


# =====================================================================
# KeywordChecker - 关键词必中
# =====================================================================


class TestKeywordChecker:
    """测试 KeywordChecker 关键词必中检测"""

    async def test_all_groups_hit_passes(self):
        # 所有关键词组都命中 → 通过
        checker = KeywordChecker()
        groups = [
            {"keywords": ["不能编造", "不编造"], "reason": "必须拒绝", "min_hits": 1},
            {"keywords": ["医保局", "12393"], "reason": "引导官方", "min_hits": 1},
        ]
        passed, failures = await checker.check_keyword_must_hit(
            "我们不能编造数字，建议拨打12393咨询。", groups
        )
        assert passed is True
        assert failures == []

    async def test_missing_group_fails(self):
        # 某组未命中 → 失败
        checker = KeywordChecker()
        groups = [
            {"keywords": ["不能编造"], "reason": "必须拒绝", "min_hits": 1},
            {"keywords": ["医保局"], "reason": "引导官方", "min_hits": 1},
        ]
        passed, failures = await checker.check_keyword_must_hit(
            "我们不能编造数字。", groups  # 缺 "医保局"
        )
        assert passed is False
        assert len(failures) == 1
        assert failures[0]["reason"] == "引导官方"

    async def test_min_hits_threshold(self):
        # min_hits=2 但只命中 1 个 → 失败
        checker = KeywordChecker()
        groups = [
            {"keywords": ["A", "B", "C"], "reason": "需命中2个", "min_hits": 2},
        ]
        passed, failures = await checker.check_keyword_must_hit(
            "只含 A", groups
        )
        assert passed is False
        assert failures[0]["hits"] == 1
        assert failures[0]["required"] == 2

    async def test_empty_groups_passes(self):
        # 空组列表 → 通过
        checker = KeywordChecker()
        passed, _ = await checker.check_keyword_must_hit("任何文本", [])
        assert passed is True

    async def test_multiple_hits_in_one_group(self):
        # 同组多个关键词都命中也只算该组通过
        checker = KeywordChecker()
        groups = [
            {"keywords": ["A", "B"], "reason": "需1个", "min_hits": 1},
        ]
        passed, failures = await checker.check_keyword_must_hit("包含 A 和 B", groups)
        assert passed is True


# =====================================================================
# ArgValidator - 5 种校验类型
# =====================================================================


class TestArgValidator:
    """测试 ArgValidator 5 种校验类型"""

    def setup_method(self):
        self.validator = ArgValidator()

    def test_optional_always_passes(self):
        # optional 类型始终通过
        assert self.validator.validate("any", {"type": "optional"}) is True
        assert self.validator.validate(None, {"type": "optional"}) is True
        assert self.validator.validate("", {"type": "optional"}) is True

    def test_non_empty_string(self):
        # non_empty：非空字符串通过
        assert self.validator.validate("内容", {"type": "non_empty"}) is True
        assert self.validator.validate("", {"type": "non_empty"}) is False
        assert self.validator.validate("   ", {"type": "non_empty"}) is False
        assert self.validator.validate(None, {"type": "non_empty"}) is False

    def test_non_empty_list(self):
        # non_empty：非空列表通过
        assert self.validator.validate([1, 2], {"type": "non_empty"}) is True
        assert self.validator.validate([], {"type": "non_empty"}) is False

    def test_non_empty_dict(self):
        # non_empty：非空 dict 通过
        assert self.validator.validate({"k": "v"}, {"type": "non_empty"}) is True
        assert self.validator.validate({}, {"type": "non_empty"}) is False

    def test_exact_match(self):
        # exact：精确匹配
        assert self.validator.validate("CN", {"type": "exact", "value": "CN"}) is True
        assert self.validator.validate("US", {"type": "exact", "value": "CN"}) is False
        assert self.validator.validate(42, {"type": "exact", "value": 42}) is True

    def test_contains_string(self):
        # contains：字符串包含子串
        assert self.validator.validate(
            "medical_insurance_topic", {"type": "contains", "value": "medical_insurance"}
        ) is True
        assert self.validator.validate(
            "legal_topic", {"type": "contains", "value": "medical_insurance"}
        ) is False

    def test_contains_list(self):
        # contains：列表中某项包含子串
        assert self.validator.validate(
            ["a", "medical_insurance_x"], {"type": "contains", "value": "medical_insurance"}
        ) is True
        assert self.validator.validate(
            ["a", "b"], {"type": "contains", "value": "medical_insurance"}
        ) is False

    def test_regex_match(self):
        # regex：正则匹配
        assert self.validator.validate(
            "13800138000", {"type": "regex", "value": r"1[3-9]\d{9}"}
        ) is True
        assert self.validator.validate(
            "abc", {"type": "regex", "value": r"1[3-9]\d{9}"}
        ) is False

    def test_regex_invalid_pattern(self):
        # 非法正则 → 不通过
        assert self.validator.validate(
            "x", {"type": "regex", "value": r"[invalid"}
        ) is False

    def test_none_spec_passes(self):
        # None spec → 视为无校验要求
        assert self.validator.validate("any", None) is True

    def test_scalar_spec_as_exact(self):
        # 非 dict spec（标量）→ 当作 exact
        assert self.validator.validate("CN", "CN") is True
        assert self.validator.validate("US", "CN") is False

    def test_unknown_type_fails(self):
        # 未知校验类型 → 不通过
        assert self.validator.validate("x", {"type": "unknown_type"}) is False


# =====================================================================
# _is_subsequence / _match_result - 辅助函数
# =====================================================================


class TestSubsequenceAndMatch:
    """测试 _is_subsequence 和 _match_result"""

    def test_subsequence_true(self):
        # expected 是 actual 的子序列
        assert _is_subsequence(["a", "c"], ["a", "b", "c", "d"]) is True

    def test_subsequence_false(self):
        # 顺序不对 → False
        assert _is_subsequence(["c", "a"], ["a", "b", "c"]) is False

    def test_subsequence_empty_expected(self):
        # 空 expected → True
        assert _is_subsequence([], ["a", "b"]) is True

    def test_subsequence_empty_actual(self):
        # 空 actual 但非空 expected → False
        assert _is_subsequence(["a"], []) is False

    def test_match_result_exact(self):
        # 精确匹配
        assert _match_result({"found": False}, {"found": False}) is True
        assert _match_result({"found": True}, {"found": False}) is False

    def test_match_result_or_alternatives(self):
        # "|" 分隔的多个可选值
        assert _match_result(
            {"execution_mode": "success"}, {"execution_mode": "success|fallback"}
        ) is True
        assert _match_result(
            {"execution_mode": "fallback"}, {"execution_mode": "success|fallback"}
        ) is True
        assert _match_result(
            {"execution_mode": "failed"}, {"execution_mode": "success|fallback"}
        ) is False

    def test_match_result_gte_suffix(self):
        # _gte 后缀：>= 比较
        assert _match_result(
            {"violations_count": 5}, {"violations_count_gte": 1}
        ) is True
        assert _match_result(
            {"violations_count": 0}, {"violations_count_gte": 1}
        ) is False

    def test_match_result_non_dict_actual(self):
        # 实际结果非 dict → False
        assert _match_result("not dict", {"k": "v"}) is False


# =====================================================================
# validate_tool_calls - 综合校验
# =====================================================================


class TestValidateToolCalls:
    """测试 validate_tool_calls 综合校验"""

    async def test_all_required_tools_called_passes(self):
        # 所有必须工具都调用 → selection_accuracy=1.0
        actual = [
            {"tool": "query_knowledge", "args": {"country": "CN", "topic": "x"}, "result": {"found": False}},
            {"tool": "check_rules", "args": {"agent_name": "a"}, "result": {"passed": True}},
        ]
        expected = [
            {"step": 1, "tool": "query_knowledge", "required": True,
             "args_validation": {"country": {"type": "exact", "value": "CN"}},
             "expected_result": {"found": False}},
            {"step": 2, "tool": "check_rules", "required": True},
        ]
        result = await validate_tool_calls(actual, expected)
        assert result["tool_selection_accuracy"] == 1.0
        assert result["passed"] is True
        assert result["order_match"] is True

    async def test_missing_required_tool_fails(self):
        # 缺失必须工具 → selection_accuracy < 1.0
        actual = [
            {"tool": "query_knowledge", "args": {}, "result": {}},
        ]
        expected = [
            {"step": 1, "tool": "query_knowledge", "required": True},
            {"step": 2, "tool": "check_rules", "required": True},  # 缺失
        ]
        result = await validate_tool_calls(actual, expected)
        assert result["tool_selection_accuracy"] == 0.5
        assert result["passed"] is False

    async def test_forbidden_tool_called_violation(self):
        # 调用了 forbidden 工具 → 严重违规
        actual = [
            {"tool": "query_knowledge", "args": {}, "result": {}},
            {"tool": "web_search", "args": {}, "result": {}},  # forbidden
        ]
        expected = [
            {"step": 1, "tool": "query_knowledge", "required": True},
            {"step": 2, "tool": "web_search", "required": "forbidden", "purpose": "禁止联网"},
        ]
        result = await validate_tool_calls(actual, expected)
        assert len(result["forbidden_violations"]) == 1
        assert result["forbidden_violations"][0]["tool"] == "web_search"
        assert result["passed"] is False

    async def test_argument_accuracy(self):
        # 参数校验
        actual = [
            {"tool": "query_knowledge", "args": {"country": "CN"}, "result": {}},
        ]
        expected = [
            {"step": 1, "tool": "query_knowledge", "required": True,
             "args_validation": {
                 "country": {"type": "exact", "value": "CN"},  # 通过
                 "topic": {"type": "non_empty"},  # 缺失 → 不通过
             }},
        ]
        result = await validate_tool_calls(actual, expected)
        # 1 个通过 1 个不通过 → argument_accuracy=0.5
        assert result["argument_accuracy"] == 0.5

    async def test_order_match(self):
        # 顺序校验
        actual = [
            {"tool": "check_rules", "args": {}, "result": {}},
            {"tool": "query_knowledge", "args": {}, "result": {}},
        ]
        expected = [
            {"step": 1, "tool": "query_knowledge", "required": True},
            {"step": 2, "tool": "check_rules", "required": True},
        ]
        result = await validate_tool_calls(actual, expected)
        # 顺序反了 → order_match=False
        assert result["order_match"] is False

    async def test_unnecessary_calls_counted(self):
        # 冗余调用计数
        actual = [
            {"tool": "query_knowledge", "args": {}, "result": {}},
            {"tool": "unknown_tool", "args": {}, "result": {}},  # 冗余
        ]
        expected = [
            {"step": 1, "tool": "query_knowledge", "required": True},
        ]
        result = await validate_tool_calls(actual, expected)
        assert result["unnecessary_calls"] == 1

    async def test_result_match_rate(self):
        # 结果匹配率
        actual = [
            {"tool": "query_knowledge", "args": {}, "result": {"found": False}},
        ]
        expected = [
            {"step": 1, "tool": "query_knowledge", "required": True,
             "expected_result": {"found": False}},
        ]
        result = await validate_tool_calls(actual, expected)
        assert result["result_match_rate"] == 1.0

    async def test_empty_expected_passes(self):
        # 无期望调用 → 默认通过
        actual = [{"tool": "x", "args": {}, "result": {}}]
        result = await validate_tool_calls(actual, [])
        assert result["passed"] is True

    async def test_returns_required_fields(self):
        # 返回应包含 5 个核心指标 + passed
        actual = []
        expected = []
        result = await validate_tool_calls(actual, expected)
        for key in [
            "tool_selection_accuracy", "argument_accuracy", "order_match",
            "unnecessary_calls", "result_match_rate", "passed", "details",
        ]:
            assert key in result, f"缺少字段 {key}"


# =====================================================================
# ThreeLayerEvaluator - 三层判定集成
# =====================================================================


class TestThreeLayerEvaluator:
    """测试 ThreeLayerEvaluator 三层判定"""

    async def test_regex_fail_returns_regex_layer(self):
        # 正则不通过 → layer=regex
        evaluator = ThreeLayerEvaluator()
        case_yaml = {
            "evaluation": {
                "regex_blacklist": [{"pattern": r"大概\s*\d+", "reason": "编造"}],
            }
        }
        result = await evaluator.evaluate("大概7天", case_yaml)
        assert result["passed"] is False
        assert result["layer"] == "regex"

    async def test_keyword_fail_returns_keyword_layer(self):
        # 正则通过但关键词不通过 → layer=keyword
        # 注意：关键词为子串匹配，response 中不得包含任一必中关键词
        evaluator = ThreeLayerEvaluator()
        case_yaml = {
            "evaluation": {
                "regex_blacklist": [{"pattern": r"不存在", "reason": "x"}],
                "keyword_must_hit": [
                    {"keywords": ["必须出现的词"], "reason": "必中", "min_hits": 1},
                ],
            }
        }
        result = await evaluator.evaluate("一段普通文本，没有命中关键词", case_yaml)
        assert result["passed"] is False
        assert result["layer"] == "keyword"

    async def test_no_llm_judge_passes_at_keyword(self):
        # 前两层通过且无 llm_judge 配置 → layer=keyword, passed=True
        evaluator = ThreeLayerEvaluator()
        case_yaml = {
            "evaluation": {
                "regex_blacklist": [{"pattern": r"不存在", "reason": "x"}],
                "keyword_must_hit": [
                    {"keywords": ["存在"], "reason": "必中", "min_hits": 1},
                ],
            }
        }
        result = await evaluator.evaluate("包含存在词", case_yaml)
        assert result["passed"] is True
        assert result["layer"] == "keyword"
