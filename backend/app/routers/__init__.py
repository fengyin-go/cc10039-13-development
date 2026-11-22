"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

固定路径路由（/api/column-lineage、/api/cross）必须排在 /api/{module} 之前，
否则会被动态模块路由抢先匹配。
"""
from __future__ import annotations

from app.routers import column_lineage, cross_module, resource

ROUTERS = [column_lineage, cross_module, resource]