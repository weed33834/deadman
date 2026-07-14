"""测试 legacy.rules_loader - 规则加载器与规则校验器

覆盖点：
  - RuleLoader.load_rule 文件加载与缓存
  - RuleChecker.check 三类信号检测：
      1. 编造检测（FABRICATION_PATTERNS：大概/应该/差不多/估计/我记得是）
      2. 心理危机检测（CRISIS_KEYWORDS）
      3. R2 风险信号检测（R2_SIGNALS：继承争议/多继承人/无遗嘱/跨境/跨国/诉讼）
"""

from __future__ import annotations



from legacy.rules_loader import RuleChecker, RuleLoader, rule_checker
from legacy.types import RiskTier


# =====================================================================
# RuleLoader - 规则文件加载
# =====================================================================


class TestRuleLoader:
    """测试 RuleLoader.load_rule"""

    def test_load_rule_missing_returns_empty(self, tmp_path):
        # 文件不存在 → 返回空字符串
        loader = RuleLoader(rules_dir=tmp_path)
        assert loader.load_rule("non-existent-rule") == ""

    def test_load_rule_existing(self, tmp_path):
        # 文件存在 → 返回内容
        rule_file = tmp_path / "test-rule.md"
        rule_file.write_text("# 测试规则\n\n内容", encoding="utf-8")
        loader = RuleLoader(rules_dir=tmp_path)
        content = loader.load_rule("test-rule")
        assert "测试规则" in content
        assert "内容" in content

    def test_load_rule_caches(self, tmp_path):
        # 第二次加载应命中缓存
        rule_file = tmp_path / "cached.md"
        rule_file.write_text("v1", encoding="utf-8")
        loader = RuleLoader(rules_dir=tmp_path)

        first = loader.load_rule("cached")
        # 修改文件内容
        rule_file.write_text("v2", encoding="utf-8")
        # 第二次仍返回缓存中的 v1
        second = loader.load_rule("cached")
        assert first == second == "v1"
        # 缓存中确实有这条
        assert "cached" in loader._cache

    def test_load_all_rules_returns_dict(self, tmp_path):
        # 创建两个规则文件
        for name in ["safety-protocol", "integrity-framework"]:
            (tmp_path / f"{name}.md").write_text(f"# {name}", encoding="utf-8")
        loader = RuleLoader(rules_dir=tmp_path)
        rules = loader.load_all_rules()
        # 返回 dict，key 是优先级（int）
        assert isinstance(rules, dict)
        # 至少有一个被加载
        assert len(rules) >= 1

    def test_get_system_prompt_rules_returns_string(self, tmp_path):
        # system prompt 应是非空字符串
        (tmp_path / "safety-protocol.md").write_text("# 安全", encoding="utf-8")
        loader = RuleLoader(rules_dir=tmp_path)
        prompt = loader.get_system_prompt_rules()
        assert isinstance(prompt, str)


# =====================================================================
# RuleChecker - 编造检测
# =====================================================================


class TestRuleCheckerFabrication:
    """测试 RuleChecker 编造模式检测 - FABRICATION_PATTERNS"""

    def test_fabrication_patterns_count(self):
        # 5 个编造模式
        assert len(RuleChecker.FABRICATION_PATTERNS) == 5

    def test_check_clean_text_passes(self):
        # 无编造的干净文本 → 通过
        result = rule_checker.check("根据医保局官网信息，各地政策不同，建议咨询当地医保部门。")
        assert result.passed is True
        assert result.violations == []
        assert result.integrity_violations == []

    def test_check_detects_dayi_fabrication(self):
        # "大概 7" 触发编造检测
        result = rule_checker.check("异地备案大概7天就能办下来。")
        assert result.passed is False
        assert len(result.integrity_violations) >= 1
        assert len(result.violations) >= 1
        # 违反的是 integrity-framework
        assert result.violations[0]["rule"] == "integrity-framework"

    def test_check_detects_yinggai_fabrication(self):
        # "应该 15"
        result = rule_checker.check("应该15个工作日完成。")
        assert result.passed is False
        assert len(result.integrity_violations) >= 1

    def test_check_detects_chabuduo_fabrication(self):
        # "差不多 30"
        result = rule_checker.check("差不多30天。")
        assert result.passed is False

    def test_check_detects_guji_fabrication(self):
        # "估计 5"
        result = rule_checker.check("估计5天。")
        assert result.passed is False

    def test_check_detects_wojikeyword(self):
        # "我记得是" 不带数字也触发
        result = rule_checker.check("我记得是这么规定的。")
        assert result.passed is False
        assert result.integrity_violations  # 非空


# =====================================================================
# RuleChecker - 心理危机检测
# =====================================================================


class TestRuleCheckerCrisis:
    """测试 RuleChecker 心理危机关键词检测 - CRISIS_KEYWORDS"""

    def test_crisis_keywords_count(self):
        # 5 个危机关键词
        assert len(RuleChecker.CRISIS_KEYWORDS) == 5

    def test_check_detects_crisis_buxianghuo(self):
        # "不想活" 触发 R3 安全优先
        result = rule_checker.check("我最近不想活了。")
        assert result.safety_triggered is True
        assert result.risk_tier == RiskTier.R3
        assert result.passed is False

    def test_check_detects_crisis_xiangsi(self):
        # "想死"
        result = rule_checker.check("有时候想死。")
        assert result.safety_triggered is True
        assert result.risk_tier == RiskTier.R3

    def test_check_detects_crisis_genzhequ(self):
        # "跟着去"
        result = rule_checker.check("想跟着去。")
        assert result.safety_triggered is True
        assert result.risk_tier == RiskTier.R3

    def test_check_detects_crisis_huobuxiaqu(self):
        # "活不下去"
        result = rule_checker.check("真的活不下去了。")
        assert result.safety_triggered is True
        assert result.risk_tier == RiskTier.R3

    def test_check_detects_crisis_jieshu_ziji(self):
        # "结束自己"
        result = rule_checker.check("想结束自己。")
        assert result.safety_triggered is True
        assert result.risk_tier == RiskTier.R3


# =====================================================================
# RuleChecker - R2 风险信号检测
# =====================================================================


class TestRuleCheckerR2Signals:
    """测试 RuleChecker R2 风险信号检测 - R2_SIGNALS"""

    def test_r2_signals_count(self):
        # 6 个 R2 信号
        assert len(RuleChecker.R2_SIGNALS) == 6

    def test_check_detects_r2_inheritance_dispute(self):
        # "继承争议" → R2
        result = rule_checker.check("这涉及继承争议，建议咨询律师。")
        assert result.risk_tier == RiskTier.R2
        assert result.passed is False
        assert result.safety_triggered is False

    def test_check_detects_r2_multi_heirs(self):
        # "多继承人"
        result = rule_checker.check("存在多继承人的情况。")
        assert result.risk_tier == RiskTier.R2

    def test_check_detects_r2_no_will(self):
        # "无遗嘱"
        result = rule_checker.check("老人无遗嘱去世。")
        assert result.risk_tier == RiskTier.R2

    def test_check_detects_r2_cross_border(self):
        # "跨境"
        result = rule_checker.check("这是跨境继承问题。")
        assert result.risk_tier == RiskTier.R2

    def test_check_detects_r2_lawsuit(self):
        # "诉讼"
        result = rule_checker.check("可能需要诉讼解决。")
        assert result.risk_tier == RiskTier.R2

    def test_check_no_signal_returns_r0(self):
        # 无任何信号 → R0
        result = rule_checker.check("请提供更多详细信息。")
        assert result.risk_tier == RiskTier.R0
        assert result.passed is True


# =====================================================================
# RuleChecker - 综合场景
# =====================================================================


class TestRuleCheckerScenarios:
    """综合场景测试"""

    def test_check_with_context_ignored(self):
        # context 参数当前不参与校验逻辑，传入不影响结果
        text = "请咨询当地医保部门。"
        r1 = rule_checker.check(text)
        r2 = rule_checker.check(text, context={"user_input": "xxx"})
        assert r1.passed == r2.passed

    def test_check_returns_rule_check_result(self):
        # 返回类型为 RuleCheckResult
        from legacy.types import RuleCheckResult

        result = rule_checker.check("普通文本。")
        assert isinstance(result, RuleCheckResult)

    def test_check_fabrication_and_crisis_combined(self):
        # 同时出现编造与危机 → 危机优先（safety_triggered=True，risk_tier=R3）
        result = rule_checker.check("我大概3天不想活了。")
        assert result.safety_triggered is True
        assert result.risk_tier == RiskTier.R3
