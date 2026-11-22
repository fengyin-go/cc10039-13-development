"""湖表血缘领域服务：维护数据从原始源到湖表再到消费端的完整血缘链路。

上游接收入湖管道和质量监控事件，下游联动治理报告。
支持血缘链路完整性检查和断裂修复；列级血缘经共享投影复查后再回写结论。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.column_lineage_share import column_lineage_share
from app.store import store


class LineageGraphService(BaseModuleService):
    MODULE = "lineage_graph"
    ENTITY = "血缘关系"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["上游源", "中间转换", "下游消费", "血缘类型", "记录时间"]
    REQUIRED_FIELDS = ["上游源", "中间转换", "下游消费"]
    STATUS_ORDER = ["已建立", "待验证", "链路断裂"]
    ACTION_RULES = {"刷新血缘": "已建立", "标记断裂": "链路断裂"}
    NEGATIVE_ACTIONS = []

    def refresh_lineage(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """刷新血缘链路：验证上下游两端模块数据的一致性。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "血缘关系不存在"

        upstream = entry.get("上游源", "")
        transform = entry.get("中间转换", "")
        downstream = entry.get("下游消费", "")

        # 检查上游模块是否有数据
        upstream_count = len(store.rows(upstream) if upstream in set(store.module_names()) else [])

        # 检查中间转换层
        mid_count = len(store.rows(transform) if transform in set(store.module_names()) else [])

        # 检查下游模块
        down_count = len(store.rows(downstream) if downstream in set(store.module_names()) else [])

        # 链路判断
        if upstream_count == 0:
            entry["status"] = "链路断裂"
            entry["abnormal"] = True
            store.log_event("lineage_broken", "lineage_graph", "govern_report", {
                "lineage_id": entry_id,
                "reason": f"上游模块 {upstream} 无数据",
            })
            return entry, f"血缘链路断裂：上游模块 {upstream} 无数据"

        if down_count == 0:
            entry["status"] = "待验证"
            entry["abnormal"] = True
            store.log_event("lineage_uncertain", "lineage_graph", "ingest_pipeline", {
                "lineage_id": entry_id,
                "reason": f"下游模块 {downstream} 无数据",
            })
            return entry, f"血缘链路待验证：下游模块 {downstream} 无数据"

        # 链路完整
        entry["status"] = "已建立"
        entry["pending"] = False
        entry["abnormal"] = False

        # 联动治理报告记录血缘概览
        store.log_event("lineage_refreshed_to_govern", "lineage_graph", "govern_report", {
            "lineage_id": entry_id,
            "upstream": upstream,
            "transform": transform,
            "downstream": downstream,
            "upstream_count": upstream_count,
            "downstream_count": down_count,
        })

        return entry, f"血缘链路已刷新：{upstream} → {transform} → {downstream}"

    def mark_broken(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """标记血缘链路断裂：通知下游模块。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "血缘关系不存在"

        entry["status"] = "链路断裂"
        entry["pending"] = False
        entry["abnormal"] = True

        store.log_event("lineage_marked_broken", "lineage_graph", "govern_report", {
            "lineage_id": entry_id,
            "downstream": entry.get("下游消费", ""),
        })

        return entry, f"血缘链路 {entry.get('上游源')} → {entry.get('下游消费')} 已标记为断裂"

    def lineage_for_table(self, table_name: str) -> list[dict[str, Any]]:
        """查询指定湖表的血缘链路。"""
        return [
            lin for lin in store.rows(self.MODULE)
            if table_name in (lin.get("上游源", ""), lin.get("中间转换", ""),
                              lin.get("下游消费", ""))
        ]

    def refresh_with_shared_lineage(
        self, entry_id: int, artifact_id: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """接收共享列血缘导出后生成实际复查结果。

        可核的边直接落结论；经过受控列或超出共享范围的路径记为
        “待回原域确认”，路径和身份只保留不可逆令牌，不暴露受控字段名。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "血缘关系不存在"
        if entry.get("血缘类型") != "列级血缘":
            return None, f"血缘关系 {entry_id} 不是列级血缘，不能使用共享列血缘复查"

        result, message = column_lineage_share.refresh_with_artifact(
            artifact_id, lineage_id=entry_id)
        if result is None:
            return None, message
        return entry, message

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "刷新血缘":
            return self.refresh_lineage(entry_id)
        if action == "标记断裂":
            return self.mark_broken(entry_id)
        return super().run_action(entry_id, action)