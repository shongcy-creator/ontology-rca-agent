# -*- coding: utf-8 -*-
"""
故障注入包（chaos engineering for the credit-card ops demo）。

模块：
  core     基础设施：docker / SQL / Prometheus 访问、Fault 模型、状态文件、注入引擎
  catalog  故障目录：应用层 / 数据库层 / 资源耗尽场景的 inject / recover 实现

对外入口：`tools/fault_injector.py`（CLI）。
"""
from .catalog import FAULTS, ensure_dataset          # noqa: F401
from .core import Injector, Fault, FaultContext       # noqa: F401

__all__ = ["FAULTS", "Injector", "Fault", "FaultContext", "ensure_dataset"]
