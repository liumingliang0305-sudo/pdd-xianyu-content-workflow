# 项目协作规范

<!-- context7 -->
需要核实库、框架、SDK、API、CLI 或云服务的版本相关行为，且现有材料不足时，使用 [find-docs](../.agents/skills/find-docs/SKILL.md)。复用当前上下文中已获取、适用版本且足以回答问题的资料；仅提及技术名称不触发检索。具体检索、版本选择与失败处理统一以该 Skill 为准。
<!-- context7 -->

## Git 提交信息

提交标题统一使用 `type(scope): 中文描述`：`type` 使用小写英文（如 `feat`、`fix`、`refactor`、`docs`、`test`、`chore`）；`scope` 必填，表示变更模块或范围；使用英文括号和冒号，冒号后留一个空格；中文描述简洁说明具体变更。

示例：`feat(skill): 接入实例排空发布与任务准入门禁`。
