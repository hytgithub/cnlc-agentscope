---
name: query-and-compare
description: 用户查询已有层段结果、版本差异或报告时，指导只读 Tool 选择。
---

# 查询与比较方法

- 查询已有结果时，优先调用 `query_interpretation_result`；先用 `get_current_context` 解析用户省略的当前版本或查看层段。
- 用户只问某一层、某个已有版本或报告时，不启动完整解释。
- 读取已有报告使用 `read_report`，按用户表达选择当前版、上一版或最近成功版。
- 只有在比较的左右两版都明确时才调用 `compare_result_versions`。可从当前上下文确认“修改前/后”是否分别对应明确的上一版和当前版；若没有两个明确版本，不猜测，先请求补充。
- 查询结果必须作为 Task 012A Fixture 说明，不得把占位内容表述为真实测井结论。
