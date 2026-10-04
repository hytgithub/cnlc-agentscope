---
name: modify-and-rerun
description: 用户提出参数修改时，指导如何整理目标、数值和范围，并按预检结果澄清、拒绝或模拟执行。
---

# 修改与重跑方法

按以下顺序处理修改请求：

1. 识别参数目标、数值和作用范围；目标、数值或范围缺失时保留缺失信息，不要自行推断为全井或某个层段；
2. 调用 `get_current_context` 获取当前 Task 012A Fixture；
3. 调用 `preflight_modify_parameter` 检查该目标和值；
4. 仅当 Tool 返回 `ALLOWED` 且目标、值与范围明确时，调用 `apply_parameter_change`；`NEED_CLARIFICATION` 时询问缺失信息，`UNSUPPORTED` 时说明当前不支持；
5. 模拟修改成功后，再调用 `query_interpretation_result` 读取新版本的 Fixture 状态。

只读查看的当前层段不会自动变成修改范围。层段级参数修改和未开放参数应由预检拒绝或要求澄清。Skill 不定义专业参数之间的计算依赖，不得推断修改一个参数会重算或改变其他参数。
