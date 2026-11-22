<template>
  <section class="share-panel" data-testid="shared-lineage-panel">
    <header class="share-head">
      <div>
        <h3>共享列血缘复查</h3>
        <p class="page-desc">
          按访问归属与字段安全等级转换血缘路径；受控列只以不可逆令牌出现，超出范围的路径截断为“需回原域确认”。
        </p>
      </div>
      <div class="page-actions">
        <label class="share-field">
          <span>访问归属</span>
          <select v-model="accessDomain" data-testid="share-domain" @change="reloadProjection">
            <option v-for="domain in domains" :key="domain" :value="domain">{{ domain }}</option>
          </select>
        </label>
        <button class="btn" data-testid="share-export" type="button" @click="exportShared">
          导出共享数据
        </button>
      </div>
    </header>

    <p class="status-legend">
      <span class="legend-item">投影版本：{{ projection?.projection_version ?? '—' }}</span>
      <span class="legend-item">范围版本：{{ projection?.scope?.scope_version ?? '—' }}</span>
      <span class="legend-item">可核：{{ projection?.reviewable_count ?? 0 }}</span>
      <span class="legend-item">需回原域：{{ projection?.pending_origin_count ?? 0 }}</span>
      <span class="legend-item">允许等级：{{ projection?.scope?.allowed_levels.join('、') ?? '—' }}</span>
    </p>

    <table class="data-table">
      <thead>
        <tr>
          <th>路径</th>
          <th>转换组</th>
          <th>路径（受控截断）</th>
          <th>末端目标</th>
          <th>复查范围</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="path in projection?.items ?? []" :key="path.path_id" :data-testid="`share-${path.path_id}`">
          <td>{{ path.path_id }}</td>
          <td>{{ path.transform_group }}</td>
          <td>
            <span v-for="(step, index) in path.steps" :key="`${path.path_id}-${index}`">
              <template v-if="step.role === 'truncated'">…（{{ step.note }}）</template>
              <template v-else>
                {{ step.column }}<span v-if="step.masked">（{{ step.mask_reason }}）</span>
              </template>
              <span v-if="index < path.steps.length - 1" class="step-arrow"> → </span>
            </span>
          </td>
          <td>{{ path.target ? `${path.target.table}.${path.target.column}` : '—' }}</td>
          <td>
            <span :class="path.reviewable ? 'chip ok' : 'chip warn'">{{ path.review_scope }}</span>
          </td>
        </tr>
        <tr v-if="!projection?.items?.length">
          <td colspan="5" class="empty-state">该业务域暂无共享列血缘</td>
        </tr>
      </tbody>
    </table>

    <div class="share-review">
      <div class="page-actions">
        <label class="share-field">
          <span>目标血缘</span>
          <select v-model="lineageChoice" data-testid="share-lineage">
            <option v-for="item in columnLineages" :key="item.id" :value="item.id">
              #{{ item.id }} {{ item.上游源 }} → {{ item.下游消费 }}
            </option>
          </select>
        </label>
        <label class="share-field">
          <span>接收共享导出</span>
          <select v-model="artifactChoice" data-testid="share-artifact">
            <option value="">选择一条导出</option>
            <option v-for="item in exports" :key="item.artifact_id" :value="item.artifact_id">
              {{ item.artifact_id }}（范围 v{{ item.scope_version }} / {{ item.status }}）
            </option>
          </select>
        </label>
        <button class="btn primary" data-testid="share-review" type="button" @click="runReview">
          发起血缘复查
        </button>
      </div>

      <p v-if="message" class="share-message" data-testid="share-message">{{ message }}</p>
      <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

      <div v-if="verified.length || pending.length" class="share-results">
        <article class="share-column">
          <h4>可核的边（{{ verified.length }}）</h4>
          <ul>
            <li v-for="review in verified" :key="review.review_id">
              {{ review.path_id }} → {{ review.target?.table }}.{{ review.target?.column }}
              <span class="chip ok">{{ review.target?.level }}</span>
            </li>
          </ul>
        </article>
        <article class="share-column">
          <h4>需回原域确认（{{ pending.length }}）</h4>
          <ul>
            <li v-for="review in pending" :key="review.review_id">
              {{ review.path_id }}（{{ review.mask_reason ?? '超范围' }}，令牌 {{ review.controlled_tokens.join('、') || '—' }}）
            </li>
          </ul>
        </article>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  createSharedExport,
  loadSharedProjection,
  runSharedReview,
  type SharedLineageExport,
  type SharedLineageProjection,
  type SharedReview,
} from '@/api/backend-service'

const domains = ['分析域', '客户域']
const accessDomain = ref(domains[0])
const projection = ref<SharedLineageProjection | null>(null)
const exports = ref<SharedLineageExport[]>([])
const artifactChoice = ref('')
const columnLineages = ref<{ id: number; 上游源: string; 下游消费: string }[]>([])
const lineageChoice = ref<number | null>(null)
const verified = ref<SharedReview[]>([])
const pending = ref<SharedReview[]>([])
const message = ref('')
const errorMessage = ref('')

async function reloadProjection() {
  errorMessage.value = ''
  try {
    projection.value = await loadSharedProjection(accessDomain.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '共享列血缘读取失败'
  }
}

async function reloadExports() {
  const response = await fetch('/api/column-lineage/exports')
  if (!response.ok) return
  const payload = (await response.json()) as { items: SharedLineageExport[] }
  exports.value = payload.items
  // 默认选最新一条仍有效的导出
  const active = [...payload.items].reverse().find((item) => item.status === 'active')
  artifactChoice.value = active?.artifact_id ?? payload.items.at(-1)?.artifact_id ?? ''
}

async function reloadColumnLineages() {
  const response = await fetch('/api/lineage_graph?size=200')
  if (!response.ok) return
  const payload = (await response.json()) as {
    items: { id: number; 血缘类型?: string; 上游源: string; 下游消费: string }[]
  }
  columnLineages.value = payload.items
    .filter((item) => item.血缘类型 === '列级血缘')
    .map((item) => ({ id: item.id, 上游源: item.上游源, 下游消费: item.下游消费 }))
  if (lineageChoice.value === null && columnLineages.value.length) {
    lineageChoice.value = columnLineages.value[0].id
  }
}

async function exportShared() {
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await createSharedExport(accessDomain.value)
    message.value = result.message
    artifactChoice.value = result.artifact.artifact_id
    await reloadExports()
    await reloadProjection()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '共享导出失败'
  }
}

async function runReview() {
  errorMessage.value = ''
  message.value = ''
  verified.value = []
  pending.value = []
  if (!artifactChoice.value) {
    errorMessage.value = '请先选择一条共享导出'
    return
  }
  if (lineageChoice.value === null) {
    errorMessage.value = '请选择要复查的列级血缘'
    return
  }
  try {
    const result = await runSharedReview(artifactChoice.value, lineageChoice.value)
    message.value = result.message
    verified.value = result.verified_edges
    pending.value = result.pending_reviews
    await reloadExports()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '血缘复查失败'
    await reloadExports()
  }
}

onMounted(async () => {
  await reloadProjection()
  await reloadExports()
  await reloadColumnLineages()
})
</script>

<style scoped>
.share-panel {
  margin-top: 20px;
  padding: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.share-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.share-head h3 { margin: 0 0 4px; font-size: 15px; }
.share-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.share-field select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.step-arrow { color: var(--muted); }
.chip { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; }
.chip.ok { background: #e7f6ec; color: #1a7f45; }
.chip.warn { background: #fdf1e0; color: #a15c00; }
.share-review { margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border); }
.share-message { font-size: 13px; color: #1a7f45; }
.share-results { display: flex; gap: 16px; margin-top: 12px; }
.share-column { flex: 1; background: #fafbfc; border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; }
.share-column h4 { margin: 4px 0 8px; font-size: 13px; }
.share-column ul { margin: 0; padding-left: 18px; font-size: 13px; }
</style>
