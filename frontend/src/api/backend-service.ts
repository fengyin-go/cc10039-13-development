import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type SharedLineageStep = {
  order: number
  role: string
  domain: string
  table: string
  column: string
  level: string
  masked: boolean
  mask_reason: string | null
  note?: string
}

export type SharedLineagePath = {
  path_id: string
  transform_group: string
  access_domain: string
  origin_domain: string
  transform_type: string
  projection_version: number
  steps: SharedLineageStep[]
  target: { domain: string; table: string; column: string; level: string } | null
  reviewable: boolean
  review_scope: string
  mask_reason: string | null
  controlled_tokens: string[]
  identity_token: string
}

export type SharedLineageProjection = {
  access_domain: string
  scope: { scope_id: string; allowed_levels: string[]; scope_version: number } | null
  projection_version: number
  total: number
  reviewable_count: number
  pending_origin_count: number
  items: SharedLineagePath[]
}

export type SharedLineageExport = {
  artifact_id: string
  access_domain: string
  scope_version: number
  projection_version: number
  status: string
  read_only: boolean
  reviewable_count: number
  pending_origin_count: number
  stale_reason?: string
}

export type SharedReview = {
  review_id: string
  path_id: string
  transform_group: string
  verdict: string
  review_scope: string
  projection_version: number
  controlled_tokens: string[]
  identity_token: string
  target: { table: string; column: string; level: string } | null
  mask_reason?: string | null
}

export async function loadSharedProjection(
  accessDomain: string,
): Promise<SharedLineageProjection> {
  const query = new URLSearchParams({ access_domain: accessDomain }).toString()
  const response = await request(`/api/column-lineage?${query}`)
  if (!response.ok) throw new Error('共享列血缘读取失败')
  return await response.json()
}

export async function createSharedExport(
  accessDomain: string,
): Promise<{ ok: boolean; message: string; artifact: SharedLineageExport }> {
  const response = await request('/api/column-lineage/exports', {
    method: 'POST',
    body: JSON.stringify({ access_domain: accessDomain }),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '共享导出失败')
  return payload
}

export async function runSharedReview(
  artifactId: string,
  lineageId: number,
): Promise<{
  ok: boolean
  message: string
  verified_edges: SharedReview[]
  pending_reviews: SharedReview[]
}> {
  const response = await request('/api/column-lineage/review', {
    method: 'POST',
    body: JSON.stringify({ artifact_id: artifactId, lineage_id: lineageId }),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '血缘复查失败')
  return payload
}
