"""列血缘共享投影：把数据目录的字段定义和安全等级，转换成可交给其他业务域复查的共享列血缘。

原始列血缘（``column_lineage``）只作为本域内部输入：
- 按访问归属（``access_domain``）筛选，别的业务域看不到不属于自己的路径；
- 受控字段名（安全等级为受控/机密）以及访问身份替换成 HMAC 不可逆令牌；
- 一旦路径上出现受控列（或因共享范围看不到的等级），投影在这里截断，
  只保留“需回原域确认”的占位，不暴露受控列之后的目标字段名。

共享导出绑定“范围版本 + 投影版本”：范围一变，旧导出立即失效，不能被当成
新权限范围继续使用；已经完成的复查保留当时使用的投影版本，不随新投影回写。
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
from typing import Any

from app.config import settings
from app.store import store

ALL_LEVELS = ("公开", "内部", "受控", "机密")
CONTROLLED_LEVELS = ("受控", "机密")

# 各业务域的共享范围：跨部门只读，默认只允许公开/内部等级。
DEFAULT_SCOPES: dict[str, dict[str, Any]] = {
    "分析域": {"scope_id": "SCP-ANALYSIS", "allowed_levels": ["公开", "内部"]},
    "客户域": {"scope_id": "SCP-CUSTOMER", "allowed_levels": ["公开"]},
}


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _token(kind: str, value: str) -> str:
    """生成稳定但不可逆的令牌：同一输入得到同一令牌，无法从令牌还原原文。"""
    digest = hmac.new(
        settings.lineage_hash_secret.encode("utf-8"),
        f"{kind}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{kind}_{digest[:16]}"


class ColumnLineageShareService:
    """共享列血缘的投影、导出、范围控制和复查结果管理。"""

    def __init__(self) -> None:
        self._scopes: dict[str, dict[str, Any]] = {}
        self._artifacts: dict[str, dict[str, Any]] = {}
        self._reviews: list[dict[str, Any]] = []
        self._batches: list[dict[str, Any]] = []
        self._projection_version = 1
        self._artifact_seq = 0
        self._review_seq = 0
        self._batch_seq = 0
        self._reset_state()

    # ---- 基础状态 ----

    def _reset_state(self) -> None:
        self._scopes = {
            domain: {
                "access_domain": domain,
                "scope_id": config["scope_id"],
                "allowed_levels": list(config["allowed_levels"]),
                "read_only": True,
                "scope_version": 1,
                "updated_at": _now(),
            }
            for domain, config in DEFAULT_SCOPES.items()
        }
        self._artifacts = {}
        self._reviews = []
        self._batches = []
        self._projection_version = 1
        self._artifact_seq = 0
        self._review_seq = 0
        self._batch_seq = 0

    def reset(self) -> dict[str, Any]:
        """恢复到初始共享范围与版本；测试和演示可重复执行。"""
        self._reset_state()
        return {"ok": True, "projection_version": self._projection_version}

    # ---- 数据目录：字段安全等级 ----

    def _catalog_level(self, table: str, column: str) -> str | None:
        for row in store.rows("data_catalog"):
            if row.get("所属湖表") == table and row.get("字段定义") == column:
                level = str(row.get("安全等级") or "").strip()
                if level in ALL_LEVELS:
                    return level
        return None

    def _canonical_level(self, table: str, column: str) -> str:
        """目录里查不到等级的字段按受控处理，宁可截断也不带出未知字段。"""
        level = self._catalog_level(table, column)
        return level if level in ALL_LEVELS else "受控"

    # ---- 共享范围 ----

    def get_scope(self, access_domain: str) -> dict[str, Any] | None:
        scope = self._scopes.get(access_domain)
        return dict(scope) if scope else None

    def list_scopes(self) -> list[dict[str, Any]]:
        return [dict(scope) for _, scope in sorted(self._scopes.items())]

    def change_scope(
        self,
        access_domain: str,
        *,
        allowed_levels: list[str] | None = None,
        read_only: bool | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """变更共享范围：版本号 +1，并让该域所有旧导出立即失效。"""
        scope = self._scopes.get(access_domain)
        if scope is None:
            return None, f"业务域 {access_domain} 尚未登记共享范围"
        if read_only is False:
            return None, "跨部门共享输出只允许只读范围，不能开放写权限"
        if allowed_levels is not None:
            cleaned = [level for level in allowed_levels if level in ALL_LEVELS]
            if not cleaned:
                return None, f"允许等级必须取自：{'、'.join(ALL_LEVELS)}"
            if not set(cleaned).issubset(set(ALL_LEVELS)):
                return None, "允许等级存在非法取值"
            scope["allowed_levels"] = sorted(set(cleaned), key=ALL_LEVELS.index)
        scope["scope_version"] += 1
        scope["updated_at"] = _now()

        for artifact in self._artifacts.values():
            if artifact["access_domain"] == access_domain and artifact["status"] == "active":
                artifact["status"] = "stale"
                artifact["stale_reason"] = "共享范围已变更"

        store.log_event("lineage_scope_changed", "data_catalog", "lineage_graph", {
            "access_domain": access_domain,
            "scope_version": scope["scope_version"],
            "allowed_levels": list(scope["allowed_levels"]),
        })
        return dict(scope), (
            f"{access_domain} 共享范围已更新到版本 {scope['scope_version']}，"
            "旧导出不能再作为新范围使用"
        )

    # ---- 投影 ----

    def _steps_for(self, row: dict[str, Any]) -> list[dict[str, str]]:
        steps = [{
            "role": "origin",
            "domain": str(row.get("origin_domain") or ""),
            "table": str(row.get("origin_table") or ""),
            "column": str(row.get("origin_column") or ""),
        }]
        for via in row.get("via_columns") or []:
            steps.append({
                "role": "transform",
                "domain": str(via.get("domain") or ""),
                "table": str(via.get("table") or ""),
                "column": str(via.get("column") or ""),
            })
        steps.append({
            "role": "target",
            "domain": str(row.get("target_domain") or ""),
            "table": str(row.get("target_table") or ""),
            "column": str(row.get("target_column") or ""),
        })
        return steps

    def _project_path(
        self, row: dict[str, Any], version: int, allowed_levels: list[str],
    ) -> dict[str, Any]:
        allowed = set(allowed_levels)
        steps: list[dict[str, Any]] = []
        controlled_tokens: list[str] = []
        mask_reason: str | None = None

        for order, step in enumerate(self._steps_for(row), start=1):
            level = self._canonical_level(step["table"], step["column"])
            reason: str | None = None
            if level in CONTROLLED_LEVELS:
                reason = "受控"
            elif level not in allowed:
                reason = "超出共享范围"

            if reason:
                token = _token("col", f"{step['table']}.{step['column']}")
                controlled_tokens.append(token)
                steps.append({
                    "order": order,
                    "role": step["role"],
                    "domain": step["domain"],
                    "table": step["table"],
                    "column": token,
                    "level": level,
                    "masked": True,
                    "mask_reason": reason,
                })
                mask_reason = reason
                break

            steps.append({
                "order": order,
                "role": step["role"],
                "domain": step["domain"],
                "table": step["table"],
                "column": step["column"],
                "level": level,
                "masked": False,
                "mask_reason": None,
            })

        if mask_reason:
            steps.append({
                "order": len(steps) + 1,
                "role": "truncated",
                "domain": str(row.get("origin_domain") or ""),
                "table": "",
                "column": "",
                "level": "",
                "masked": True,
                "mask_reason": mask_reason,
                "note": (
                    "受控列之后的下游路径需回原域确认"
                    if mask_reason == "受控"
                    else "路径超出共享范围，需回原域确认"
                ),
            })

        reviewable = mask_reason is None and bool(steps) and steps[-1]["role"] == "target"
        target = None
        if reviewable:
            last = steps[-1]
            target = {
                "domain": last["domain"],
                "table": last["table"],
                "column": last["column"],
                "level": last["level"],
            }

        principal = str(row.get("access_principal") or "")
        return {
            "path_id": str(row.get("path_id") or ""),
            "transform_group": str(row.get("transform_group") or ""),
            "access_domain": str(row.get("access_domain") or ""),
            "origin_domain": str(row.get("origin_domain") or ""),
            "target_domain": str(row.get("target_domain") or ""),
            "transform_type": str(row.get("transform_type") or ""),
            "projection_version": version,
            "steps": steps,
            "target": target,
            "reviewable": reviewable,
            "review_scope": "本域可核" if reviewable else "需回原域确认",
            "mask_reason": mask_reason,
            "controlled_tokens": controlled_tokens,
            # 访问身份只以不可逆令牌出现，导出里不会出现原始身份。
            "identity_token": _token("idn", principal) if principal else "",
        }

    def build_projection(self, access_domain: str) -> dict[str, Any]:
        scope = self._scopes.get(access_domain)
        allowed_levels = list(scope["allowed_levels"]) if scope else ["公开"]
        rows = [
            row for row in store.rows("column_lineage")
            if str(row.get("access_domain") or "") == access_domain
        ]
        items = [self._project_path(row, self._projection_version, allowed_levels) for row in rows]
        return {
            "access_domain": access_domain,
            "scope": dict(scope) if scope else None,
            "projection_version": self._projection_version,
            "generated_at": _now(),
            "total": len(items),
            "reviewable_count": sum(1 for item in items if item["reviewable"]),
            "pending_origin_count": sum(1 for item in items if not item["reviewable"]),
            "items": items,
        }

    def publish_projection(self) -> dict[str, Any]:
        """发布新投影版本；已完成的复查保留各自的历史投影版本。"""
        self._projection_version += 1
        return {
            "projection_version": self._projection_version,
            "published_at": _now(),
            "retained_reviews": len(self._reviews),
        }

    # ---- 共享导出 ----

    def create_export(self, access_domain: str) -> tuple[dict[str, Any] | None, str]:
        scope = self._scopes.get(access_domain)
        if scope is None:
            return None, f"业务域 {access_domain} 尚未登记共享范围，不能导出"

        projection = self.build_projection(access_domain)
        self._artifact_seq += 1
        artifact = {
            "artifact_id": f"EXP-{self._artifact_seq:04d}",
            "access_domain": access_domain,
            "scope_id": scope["scope_id"],
            "scope_version": scope["scope_version"],
            "projection_version": projection["projection_version"],
            "read_only": True,
            "status": "active",
            "created_at": _now(),
            "reviewable_count": projection["reviewable_count"],
            "pending_origin_count": projection["pending_origin_count"],
            "items": projection["items"],
        }
        self._artifacts[artifact["artifact_id"]] = artifact
        store.log_event("lineage_shared_export", "data_catalog", "lineage_graph", {
            "artifact_id": artifact["artifact_id"],
            "access_domain": access_domain,
            "scope_version": artifact["scope_version"],
            "projection_version": artifact["projection_version"],
            "item_count": len(artifact["items"]),
        })
        return self.get_export(artifact["artifact_id"]), (
            f"已生成 {access_domain} 的只读共享列血缘导出 {artifact['artifact_id']}"
            f"（范围版本 {artifact['scope_version']}，投影版本 {artifact['projection_version']}）"
        )

    def get_export(self, artifact_id: str) -> dict[str, Any] | None:
        artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            return None
        result = dict(artifact)
        result["items"] = [dict(item) for item in artifact["items"]]
        return result

    def list_exports(self) -> list[dict[str, Any]]:
        return [
            {
                key: value for key, value in artifact.items() if key != "items"
            }
            for artifact in self._artifacts.values()
        ]

    def export_rows(self, artifact_id: str) -> list[dict[str, Any]]:
        """把共享导出摊平成可下载的只读行：不包含受控字段名和原始身份。"""
        artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            return []
        rows: list[dict[str, Any]] = []
        for item in artifact["items"]:
            target = item.get("target") or {}
            masked_columns = [
                step["column"] for step in item["steps"]
                if step.get("masked") and step.get("column")
            ]
            rows.append({
                "artifact_id": artifact_id,
                "path_id": item["path_id"],
                "transform_group": item["transform_group"],
                "access_domain": item["access_domain"],
                "origin_domain": item["origin_domain"],
                "transform_type": item["transform_type"],
                "reviewable": "是" if item["reviewable"] else "否",
                "review_scope": item["review_scope"],
                "target_table": target.get("table", ""),
                "target_column": target.get("column", ""),
                "masked_columns": "|".join(masked_columns),
                "controlled_tokens": "|".join(item["controlled_tokens"]),
                "identity_token": item["identity_token"],
                "projection_version": item["projection_version"],
                "scope_version": artifact["scope_version"],
            })
        return rows

    # ---- 复查 ----

    def validate_export(self, artifact_id: str) -> tuple[dict[str, Any] | None, str]:
        """校验共享导出仍可用于复查：必须存在、未被范围变更作废、范围版本仍一致。"""
        artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            return None, f"共享导出 {artifact_id} 不存在，请先导出共享列血缘"
        scope = self._scopes.get(artifact["access_domain"])
        if scope is None:
            return None, f"业务域 {artifact['access_domain']} 已没有有效共享范围"
        if artifact["status"] != "active":
            return None, (
                f"共享导出 {artifact_id} 已失效（{artifact.get('stale_reason') or artifact['status']}），"
                "旧导出不能作为新的权限范围重新使用，请按当前范围重新导出"
            )
        if artifact["scope_version"] != scope["scope_version"]:
            artifact["status"] = "stale"
            artifact["stale_reason"] = "共享范围已变更"
            return None, (
                f"共享导出 {artifact_id} 的范围版本为 {artifact['scope_version']}，"
                f"当前范围为 {scope['scope_version']}，旧导出不能继续使用，请重新导出"
            )
        return artifact, ""

    def refresh_with_artifact(
        self, artifact_id: str, lineage_id: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """血缘刷新接收共享导出：区分可核边和需回原域确认的路径，形成复查结果。"""
        artifact, message = self.validate_export(artifact_id)
        if artifact is None:
            return None, message
        scope = self._scopes[artifact["access_domain"]]

        lineage_entry = None
        if lineage_id is not None:
            lineage_entry = store.find("lineage_graph", lineage_id)
            if lineage_entry is None:
                return None, f"血缘关系 {lineage_id} 不存在"

        verified: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []

        for item in artifact["items"]:
            existing = next(
                (
                    review for review in self._reviews
                    if review["artifact_id"] == artifact_id
                    and review["path_id"] == item["path_id"]
                ),
                None,
            )
            if existing is not None:
                results.append(existing)
                if existing["verdict"] == "可核":
                    verified.append(existing)
                else:
                    pending.append(existing)
                continue

            self._review_seq += 1
            review: dict[str, Any] = {
                "review_id": f"RVW-{self._review_seq:04d}",
                "artifact_id": artifact_id,
                "lineage_id": lineage_id,
                "path_id": item["path_id"],
                "transform_group": item["transform_group"],
                "access_domain": artifact["access_domain"],
                "origin_domain": item["origin_domain"],
                "projection_version": artifact["projection_version"],
                "scope_version": artifact["scope_version"],
                "controlled_tokens": list(item["controlled_tokens"]),
                "identity_token": item["identity_token"],
                "created_at": _now(),
            }

            if item["reviewable"]:
                target = item["target"] or {}
                level = self._canonical_level(target.get("table", ""), target.get("column", ""))
                if level in CONTROLLED_LEVELS or level not in scope["allowed_levels"]:
                    review.update({
                        "verdict": "待回原域确认",
                        "review_scope": "目标等级已超出共享范围",
                        "target": None,
                    })
                else:
                    review.update({
                        "verdict": "可核",
                        "review_scope": "本域可核",
                        "target": dict(target),
                        "verified_level": level,
                    })
            else:
                review.update({
                    "verdict": "待回原域确认",
                    "review_scope": item["review_scope"],
                    "mask_reason": item.get("mask_reason"),
                    "target": None,
                })

            self._reviews.append(review)
            results.append(review)
            if review["verdict"] == "可核":
                verified.append(review)
            else:
                pending.append(review)

        self._batch_seq += 1
        batch = {
            "batch_id": f"RVB-{self._batch_seq:04d}",
            "artifact_id": artifact_id,
            "lineage_id": lineage_id,
            "access_domain": artifact["access_domain"],
            "projection_version": artifact["projection_version"],
            "scope_version": artifact["scope_version"],
            "verified_count": len(verified),
            "pending_count": len(pending),
            "created_at": _now(),
        }
        self._batches.append(batch)

        if lineage_entry is not None:
            lineage_entry["status"] = "已建立" if not pending else "待验证"
            lineage_entry["pending"] = bool(pending)
            lineage_entry["abnormal"] = bool(pending)
            lineage_entry["复查结论"] = (
                f"可核 {len(verified)} 条，待回原域确认 {len(pending)} 条"
            )
            lineage_entry["复查投影版本"] = artifact["projection_version"]

        store.log_event("lineage_refreshed_with_shared", "lineage_graph", "govern_report", {
            "artifact_id": artifact_id,
            "lineage_id": lineage_id,
            "verified_count": len(verified),
            "pending_count": len(pending),
            "projection_version": artifact["projection_version"],
        })
        if pending:
            store.log_event("lineage_pending_origin_confirm", "lineage_graph", "data_catalog", {
                "artifact_id": artifact_id,
                "lineage_id": lineage_id,
                "pending_paths": [review["path_id"] for review in pending],
                "origin_domains": sorted({review["origin_domain"] for review in pending}),
                "controlled_tokens": sorted({
                    token for review in pending for token in review["controlled_tokens"]
                }),
            })

        result = {
            "batch": batch,
            "verified_edges": [self._review_view(review) for review in verified],
            "pending_reviews": [self._review_view(review) for review in pending],
        }
        return result, (
            f"血缘复查完成：可核 {len(verified)} 条边，待回原域确认 {len(pending)} 条路径"
        )

    @staticmethod
    def _review_view(review: dict[str, Any]) -> dict[str, Any]:
        return dict(review)

    def list_reviews(self, artifact_id: str | None = None) -> list[dict[str, Any]]:
        reviews = self._reviews
        if artifact_id:
            reviews = [review for review in reviews if review["artifact_id"] == artifact_id]
        return [dict(review) for review in reviews]

    def list_batches(self) -> list[dict[str, Any]]:
        return [dict(batch) for batch in self._batches]


column_lineage_share = ColumnLineageShareService()
