"""集中式配置常量。

DEFAULT_PORT 是默认端口的唯一来源：run.py（监听端口）与 app/main.py（设置接口
回报给前端）都从这里取，避免两处硬编码各自漂移（历史上曾有三处）。
"""

DEFAULT_PORT = 6814