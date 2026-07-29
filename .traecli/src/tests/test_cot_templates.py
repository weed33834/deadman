"""P1.6 CoT 推理模板 - 单元测试

覆盖：
- get_cot_template_reasoning: reasoning 模板渲染
- get_cot_template_planning: planning 模板渲染
- get_cot_template_verification: verification 模板渲染
- get_cot_template_reflection: reflection 模板渲染
- get_cot_template_unknown_returns_empty: 未知模板返回空字符串

附加：
- COT_TEMPLATES 含 4 类模板
- 模板含 Jinja2 变量占位
- 缺失变量不抛异常（render_template 降级处理）
"""

from __future__ import annotations


from deadman.prompts import COT_TEMPLATES, get_cot_template


# =====================================================================
# 4 类模板渲染测试
# =====================================================================


class TestGetCotTemplateReasoning:
    def test_get_cot_template_reasoning(self):
        """reasoning 模板：渲染 question / context 变量"""
        result = get_cot_template(
            "reasoning",
            question="北京户口注销流程是什么？",
            context="用户亲人刚去世",
        )
        assert "北京户口注销流程" in result
        assert "用户亲人刚去世" in result
        assert "让我一步步思考" in result
        assert "推理过程" in result

    def test_reasoning_template_in_cot_templates(self):
        assert "reasoning" in COT_TEMPLATES
        assert "{{ question }}" in COT_TEMPLATES["reasoning"]


class TestGetCotTemplatePlanning:
    def test_get_cot_template_planning(self):
        """planning 模板：渲染 question 变量，输出 JSON 步骤"""
        result = get_cot_template(
            "planning",
            question="跨境继承适用哪国法律",
        )
        assert "跨境继承" in result
        assert "steps" in result
        assert "step_id" in result
        assert "depends_on" in result
        assert "JSON" in result

    def test_planning_template_has_json_schema(self):
        assert "step_id" in COT_TEMPLATES["planning"]
        assert "tool_hint" in COT_TEMPLATES["planning"]
        assert "expected_output" in COT_TEMPLATES["planning"]


class TestGetCotTemplateVerification:
    def test_get_cot_template_verification(self):
        """verification 模板：渲染 question / answer 变量"""
        result = get_cot_template(
            "verification",
            question="社保结算流程",
            answer="先到社保局办理结算",
        )
        assert "社保结算流程" in result
        assert "先到社保局办理结算" in result
        assert "事实准确性" in result
        assert "passed" in result

    def test_verification_template_has_score_field(self):
        assert "score" in COT_TEMPLATES["verification"]
        assert "issues" in COT_TEMPLATES["verification"]


class TestGetCotTemplateReflection:
    def test_get_cot_template_reflection(self):
        """reflection 模板：渲染 task / failure_reason / previous_output 变量"""
        result = get_cot_template(
            "reflection",
            task="查询房产继承法",
            failure_reason="工具返回空结果",
            previous_output="无",
        )
        assert "查询房产继承法" in result
        assert "工具返回空结果" in result
        assert "反思" in result
        assert "adjustment" in result

    def test_reflection_template_has_adjustment_fields(self):
        assert "failure_type" in COT_TEMPLATES["reflection"]
        assert "reason" in COT_TEMPLATES["reflection"]
        assert "adjusted_params" in COT_TEMPLATES["reflection"]


# =====================================================================
# 边界与降级
# =====================================================================


class TestGetCotTemplateEdgeCases:
    def test_get_cot_template_unknown_returns_empty(self):
        """未知模板名返回空字符串（不抛异常）"""
        assert get_cot_template("nonexistent_template") == ""
        assert get_cot_template("") == ""
        assert get_cot_template("Reasoning") == ""  # 大小写敏感

    def test_cot_templates_has_four_types(self):
        """COT_TEMPLATES 含 4 类模板"""
        expected_keys = {"reasoning", "planning", "verification", "reflection"}
        assert set(COT_TEMPLATES.keys()) == expected_keys

    def test_get_cot_template_missing_vars_does_not_raise(self):
        """缺失变量不抛异常（render_template 降级处理）"""
        # 不传任何变量
        result = get_cot_template("reasoning")
        # 应返回模板原文（变量占位可能保留或被替换为空，但不抛异常）
        assert isinstance(result, str)
        assert "推理" in result

    def test_get_cot_template_extra_vars_ignored(self):
        """多余变量被忽略，不影响渲染"""
        result = get_cot_template(
            "reasoning",
            question="问题",
            context="上下文",
            extra_var="不应出现在结果",
        )
        assert "问题" in result
        assert "上下文" in result
