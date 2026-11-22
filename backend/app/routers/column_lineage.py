"""共享列血缘接口：投影、只读导出、范围变更和血缘复查。

固定路径必须早于 /api/{module} 动态路由注册，否则会被模块路由吞掉。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.services.column_lineage_share import column_lineage_share
from app.services.lineage_graph import LineageGraphService

router = APIRouter(prefix="/api/column-lineage", tags=["共享列血缘"])

_lineage_service = LineageGraphService()


@router.get("")
def list_projection(access_domain: str = Query(...)) -> dict[str, Any]:
    """生成并返回指定业务域的共享列血缘投影。"""
    scope = column_lineage_share.get_scope(access_domain)
    if scope is None:
        raise HTTPException(status_code=404, detail=f"业务域 {access_domain} 尚未登记共享范围")
    return column_lineage_share.build_projection(access_domain)


@router.get("/scopes")
def list_scopes() -> dict[str, Any]:
    return {"items": column_lineage_share.list_scopes()}


@router.post("/scopes/{access_domain}")
def change_scope(access_domain: str, payload: dict[str, Any]) -> dict[str, Any]:
    scope, message = column_lineage_share.change_scope(
        access_domain,
        allowed_levels=payload.get("allowed_levels"),
        read_only=payload.get("read_only"),
    )
    if scope is None:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "scope": scope}


@router.post("/review")
def run_review(payload: dict[str, Any]) -> dict[str, Any]:
    """血缘刷新接收共享导出，形成复查结果。"""
    lineage_id = payload.get("lineage_id")
    artifact_id = str(payload.get("artifact_id") or "").strip()
    if not artifact_id:
        raise HTTPException(status_code=400, detail="缺少共享导出标识 artifact_id")
    if lineage_id is not None:
        try:
            lineage_id = int(lineage_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="lineage_id 必须是整数") from None
        entry, message = _lineage_service.refresh_with_shared_lineage(lineage_id, artifact_id)
        if entry is None:
            raise HTTPException(status_code=400, detail=message)
        result = _reviews_batch(artifact_id)
        return {"ok": True, "message": message, "entry": entry, **result}

    if column_lineage_share.get_export(artifact_id) is None:
        raise HTTPException(status_code=404, detail=f"共享导出 {artifact_id} 不存在")
    artifact, message = column_lineage_share.validate_export(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": "共享导出已登记", **_reviews_batch(artifact_id)}


def _reviews_batch(artifact_id: str) -> dict[str, Any]:
    reviews = column_lineage_share.list_reviews(artifact_id)
    verified = [review for review in reviews if review["verdict"] == "可核"]
    pending = [review for review in reviews if review["verdict"] != "可核"]
    return {
        "verified_edges": verified,
        "pending_reviews": pending,
        "summary": {
            "artifact_id": artifact_id,
            "verified_count": len(verified),
            "pending_count": len(pending),
        },
    }


@router.post("/publish")
def publish_projection() -> dict[str, Any]:
    result = column_lineage_share.publish_projection()
    return {"ok": True, "message": f"共享列血缘已发布到投影版本 {result['projection_version']}", **result}


@router.get("/exports")
def list_exports() -> dict[str, Any]:
    items = column_lineage_share.list_exports()
    return {"total": len(items), "items": items}


@router.post("/exports")
def create_export(payload: dict[str, Any]) -> dict[str, Any]:
    access_domain = str(payload.get("access_domain") or "").strip()
    if not access_domain:
        raise HTTPException(status_code=400, detail="缺少 access_domain")
    artifact, message = column_lineage_share.create_export(access_domain)
    if artifact is None:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "artifact": artifact}


@router.get("/exports/{artifact_id}")
def get_export(artifact_id: str) -> dict[str, Any]:
    artifact = column_lineage_share.get_export(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"共享导出 {artifact_id} 不存在")
    return artifact


@router.get("/exports/{artifact_id}/download")
def download_export(artifact_id: str) -> StreamingResponse:
    rows = column_lineage_share.export_rows(artifact_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"共享导出 {artifact_id} 不存在或没有可导出内容")
    fields = list(rows[0])
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in (row[field] for field in fields)
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="column-lineage-{artifact_id}.csv"'},
    )


@router.get("/reviews")
def list_reviews(artifact_id: str | None = Query(default=None)) -> dict[str, Any]:
    items = column_lineage_share.list_reviews(artifact_id)
    return {"total": len(items), "items": items}
