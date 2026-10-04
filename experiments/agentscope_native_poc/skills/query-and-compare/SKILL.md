---
name: query-and-compare
description: 用户提出有条件的结果查询、复合修改比较或报告读取时，指导先读上下文、再按结果分支选择只读 Tool。
---

# 查询与比较方法

- 查询已有结果时，先用 `get_current_context` 解析省略的当前版本或查看层段，再调用 `query_interpretation_result`。如果用户明确授权“查无结果后才启动”，只有查询返回 `NOT_FOUND` 时才考虑 `start_full_interpretation`；查到结果时停止在只读查询，不启动完整解释。
- 用户只问某一层、某个已有版本或报告时，不启动完整解释。
- 读取已有报告使用 `read_report`，按用户表达选择当前版、上一版或最近成功版。
- 复合修改并比较时，先通过 `get_current_context` 记录修改前版本，再预检；只有预检允许且模拟修改返回新版本后，才按“修改前版本 → 新版本”调用 `compare_result_versions`。只有在比较的左右两版都明确时才比较；若没有两个明确版本，不猜测，先请求补充。
- 查询结果必须作为 Task 012A Fixture 说明，不得把占位内容表述为真实测井结论。
