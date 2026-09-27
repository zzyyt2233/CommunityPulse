"""导入即注册所有采集器。"""
from .base import all_collectors, route, CollectResult, Comment, BaseCollector  # noqa: F401
from . import api_based  # noqa: F401
from . import web_based  # noqa: F401
from . import generic  # noqa: F401
from .generic import GenericCollector, ManualCollector, parse_import  # noqa: F401
from .base import STABLE, BEST_EFFORT, RESTRICTED, MANUAL  # noqa: F401

__all__ = ["all_collectors", "route", "parse_import", "GenericCollector"]
