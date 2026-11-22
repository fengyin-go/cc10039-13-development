"""共享列血缘投影测试：范围截断、不可逆令牌、导出失效和复查结论。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.column_lineage_share import (
    column_lineage_share,
    CONTROLLED_LEVELS,
)
from app.services.lineage_graph import LineageGraphService


@pytest.fixture(autouse=True)
def fresh_share():
    column_lineage_share.reset()
    yield
    column_lineage_share.reset()


def _items(projection: dict) -> dict[str, dict]:
    return {item["path_id"]: item for item in projection["items"]}


def _raw_columns(path_row: dict) -> set[str]:
    columns = {path_row["origin_column"], path_row["target_column"]}
    columns.update(via["column"] for via in path_row.get("via_columns") or [])
    return columns


class TestProjection:
    def test_dual_paths_same_transform_group(self):
        """同一路转换：一条直达公开列，一条经受控列后到达另一个公开列。"""
        items = _items(column_lineage_share.build_projection("分析域"))
        direct = items["PATH-ORDER-CNT"]
        via_controlled = items["PATH-ORDER-CATEGORY"]
        assert direct["transform_group"] == via_controlled["transform_group"] == "TX-ORDER-METRIC"

        assert direct["reviewable"] is True
        assert direct["target"]["column"] == "order_cnt"

        assert via_controlled["reviewable"] is False
        assert via_controlled["mask_reason"] == "受控"
        assert via_controlled["target"] is None

    def test_controlled_column_names_never_leak(self):
        """路径与导出里不出现受控字段名，只保留不可逆令牌。"""
        projection = column_lineage_share.build_projection("分析域")
        raw = next(row for row in _lineage_rows() if row["path_id"] == "PATH-ORDER-CATEGORY")
        controlled = {
            column for column in _raw_columns(raw)
            if column_lineage_share._canonical_level(raw["origin_table"], column)
            in CONTROLLED_LEVELS
        }
        assert controlled, "样例数据应包含受控字段"

        serialized = repr(projection)
        for column in controlled:
            assert column not in serialized, f"投影泄露受控字段名 {column}"

        item = _items(projection)["PATH-ORDER-CATEGORY"]
        assert item["controlled_tokens"], "受控列应转换为令牌占位"
        assert all(token.startswith("col_") for token in item["controlled_tokens"])

    def test_identity_is_irreversible_token(self):
        """访问身份只以令牌出现，且同一身份稳定得到同一令牌。"""
        projection = column_lineage_share.build_projection("分析域")
        tokens = {item["identity_token"] for item in projection["items"]}
        assert all(token.startswith("idn_") for token in tokens)
        assert "svc_analyst_01" not in repr(projection)

        # 同一身份的多条路径共享同一令牌
        same_principal = [
            item["identity_token"] for item in projection["items"]
            if item["path_id"] in ("PATH-ORDER-CNT", "PATH-ORDER-CATEGORY")
        ]
        assert len(set(same_principal)) == 1

    def test_unregistered_domain_has_no_projection(self):
        """未登记共享范围的业务域拿不到任何路径。"""
        projection = column_lineage_share.build_projection("未知业务域")
        assert projection["total"] == 0
        assert projection["items"] == []


class TestExportAndScope:
    def test_export_is_read_only_and_scoped(self):
        artifact, message = column_lineage_share.create_export("分析域")
        assert artifact is not None, message
        assert artifact["read_only"] is True
        assert artifact["scope_version"] == 1
        assert artifact["status"] == "active"

    def test_scope_change_invalidates_old_export(self):
        artifact, _ = column_lineage_share.create_export("分析域")
        assert artifact is not None

        scope, message = column_lineage_share.change_scope("分析域", allowed_levels=["公开"])
        assert scope is not None, message
        assert scope["scope_version"] == 2

        import app.services.column_lineage_share as module
        old = module.column_lineage_share.get_export(artifact["artifact_id"])
        assert old["status"] == "stale"

        result, message = column_lineage_share.refresh_with_artifact(artifact["artifact_id"])
        assert result is None
        assert "旧导出" in message

    def test_narrowed_scope_truncates_extra_targets(self):
        """收紧到只允许公开后，经过内部等级的路径也需回原域确认。"""
        column_lineage_share.change_scope("分析域", allowed_levels=["公开"])
        items = _items(column_lineage_share.build_projection("分析域"))
        assert items["PATH-ORDER-COST"]["reviewable"] is False
        assert items["PATH-ORDER-COST"]["mask_reason"] == "超出共享范围"
        # 全程公开的两条路径仍可核
        assert items["PATH-ORDER-CNT"]["reviewable"] is True
        assert items["PATH-ORDER-GMV"]["reviewable"] is True

    def test_read_only_domain_cannot_open_write(self):
        scope, message = column_lineage_share.change_scope("分析域", read_only=False)
        assert scope is None
        assert "只读" in message

    def test_export_rows_do_not_leak(self):
        artifact, _ = column_lineage_share.create_export("客户域")
        assert artifact is not None
        rows = column_lineage_share.export_rows(artifact["artifact_id"])
        serialized = repr(rows)
        assert "customer_phone" not in serialized
        assert "svc_customer_01" not in serialized
        pending = [row for row in rows if row["reviewable"] == "否"]
        assert pending and pending[0]["masked_columns"].startswith("col_")


class TestReview:
    def test_review_distinguishes_verified_and_pending(self):
        artifact, _ = column_lineage_share.create_export("分析域")
        entry = _lineage_entry()
        service = LineageGraphService()
        refreshed, message = service.refresh_with_shared_lineage(
            entry["id"], artifact["artifact_id"])
        assert refreshed is not None, message

        reviews = column_lineage_share.list_reviews(artifact["artifact_id"])
        verdicts = {review["path_id"]: review["verdict"] for review in reviews}
        assert verdicts["PATH-ORDER-CNT"] == "可核"
        assert verdicts["PATH-ORDER-GMV"] == "可核"
        assert verdicts["PATH-ORDER-CATEGORY"] == "待回原域确认"

        assert refreshed["status"] == "待验证"
        assert refreshed["abnormal"] is True
        assert refreshed["复查投影版本"] == artifact["projection_version"]

    def test_review_is_idempotent(self):
        artifact, _ = column_lineage_share.create_export("分析域")
        entry = _lineage_entry()
        service = LineageGraphService()
        service.refresh_with_shared_lineage(entry["id"], artifact["artifact_id"])
        first = column_lineage_share.list_reviews(artifact["artifact_id"])
        service.refresh_with_shared_lineage(entry["id"], artifact["artifact_id"])
        second = column_lineage_share.list_reviews(artifact["artifact_id"])
        assert len(first) == len(second)
        assert [r["review_id"] for r in first] == [r["review_id"] for r in second]

    def test_completed_review_keeps_projection_version(self):
        """发布新投影后，已完成的复查仍保留原来的投影版本。"""
        artifact, _ = column_lineage_share.create_export("分析域")
        service = LineageGraphService()
        service.refresh_with_shared_lineage(_lineage_entry()["id"], artifact["artifact_id"])
        before = {r["review_id"]: r["projection_version"]
                  for r in column_lineage_share.list_reviews(artifact["artifact_id"])}

        published = column_lineage_share.publish_projection()
        assert published["projection_version"] == before[next(iter(before))] + 1

        after = {r["review_id"]: r["projection_version"]
                 for r in column_lineage_share.list_reviews(artifact["artifact_id"])}
        assert after == before

    def test_non_column_lineage_cannot_review(self):
        artifact, _ = column_lineage_share.create_export("分析域")
        service = LineageGraphService()
        entry, message = service.refresh_with_shared_lineage(1, artifact["artifact_id"])
        assert entry is None
        assert "列级血缘" in message

    def test_missing_artifact_is_rejected(self):
        service = LineageGraphService()
        entry, message = service.refresh_with_shared_lineage(_lineage_entry()["id"], "EXP-9999")
        assert entry is None
        assert "不存在" in message


def _lineage_rows() -> list[dict]:
    from app.store import store
    return store.rows("column_lineage")


def _lineage_entry() -> dict:
    from app.store import store
    for row in store.rows("lineage_graph"):
        if row.get("血缘类型") == "列级血缘" and row.get("下游消费") == "分析域":
            return row
    raise AssertionError("缺少分析域的列级血缘样例")
