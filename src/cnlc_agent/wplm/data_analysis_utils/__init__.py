"""GDSX 核心业务包：解析、完整度、井径扩缩径、绘图与通用工具。

模块职责（功能与类相分离，业务逻辑均为纯函数 + 轻量数据容器）：
    - reader:       GDSX 读取，返回 WellData 数据容器
    - completeness: 数据完整性校验
    - expansion:    井径扩缩径率计算
    - utils:        通用工具函数
"""