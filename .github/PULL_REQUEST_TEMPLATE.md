## 变更类型

- [ ] Bug 修复（不破坏现有功能）
- [ ] 新功能（智能体 / 规则 / 知识库 / 工具 / 端点 / CLI 子命令）
- [ ] 重构 / 性能改进（不改变外部行为）
- [ ] 文档
- [ ] 测试
- [ ] 安全相关（请同时走 [SECURITY.md](../SECURITY.md) 流程）

## 改动概要

<!-- 一句话说明这个 PR 做了什么 -->

## 关联 Issue

Closes #

## 规则合规自查

参考 `.traecli/rules/` 与 `CONTRIBUTING.md`：

- [ ] 不引入代办 / 代查 / 出具法律意见 / 编造不确定信息
- [ ] 不削弱 L0-L8 优先级链（safety > integrity > input-guardrails > compliance > risk-tier > transparency > accountability > retrieval-guardrails > tone）
- [ ] 新增内容附置信度标注与来源透传
- [ ] PII 字段已脱敏（姓名 / 身份证 / 电话 / 账号 / 地址 / 出生日期）
- [ ] 主动通知场景遵守 `notification-guardrails.md`（静默时段 / 频率上限 / 7 天等待期 / 退订机制）

## 测试

- [ ] 已运行 `cd /workspace/deadman && python -m pytest .traecli/src/tests/ -q`
- [ ] 全量回归通过（无新增 fail）
- [ ] 新增功能附测试用例
- [ ] 已跑 `tests/golden-cases.md` 20 case（如涉及智能体行为）
- [ ] 已跑 `tests/scenarios.md` 8 场景（如涉及跨智能体联调）

## 文档

- [ ] 更新 `CHANGELOG.md` 顶部
- [ ] 更新 `README.md`（如涉及快速开始 / 项目结构）
- [ ] 更新相关 docs/

## 加密与安全（如涉及数据落盘）

- [ ] 用 per-user passphrase（不再用全局默认口令）
- [ ] envelope version 已升级
- [ ] 旧数据迁移路径已测试

## 检查清单

- [ ] 代码风格遵循项目约定（line-length 100，类型注解，中文注释）
- [ ] 没有引入未授权的新依赖
- [ ] 没有 hardcode 密钥 / token / 用户隐私
- [ ] commit message 遵循 conventional commits（feat / fix / docs / refactor / test / chore）
