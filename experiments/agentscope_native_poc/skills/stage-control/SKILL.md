---
name: stage-control
description: 用户询问当前等待阶段或表达确认继续时，指导区分只读状态查询与阶段确认。
---

# 阶段状态与确认方法

- 用户只询问当前阶段、等待内容或进度时，调用 `get_current_context` 读取 Fixture 状态；不调用 `confirm_stage`。
- 用户明确要求确认并继续时，先调用 `get_current_context` 读取准确的 `pending_stage`，再把相同阶段传给 `confirm_stage`。
- 如果上下文没有等待确认的阶段，不调用 `confirm_stage`；如 Tool 返回 `NEED_CLARIFICATION`，先向用户说明缺少的阶段信息。

本 Skill 只说明对话方法，不授予权限、不定义生产阶段流转，也不代表真实 Workflow 已执行。
