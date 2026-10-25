<script setup lang="ts">
// Output node (terminal): where the chain's result ends up — a catalog datasource, a
// database table, a file/dataset on S3, an email attachment or a file on an SFTP server.
import { computed, inject } from 'vue'
import { useI18n } from 'vue-i18n'
import { Handle, Position } from '@vue-flow/core'
import { useNodeStats } from '~/composables/useNodeStats'
import { Database, HardDriveDownload, CloudUpload, Mail, RefreshCw, Server } from 'lucide-vue-next'

const props = defineProps<{ id: string; data: any }>()
const { t } = useI18n()
const stats = useNodeStats(() => props.id, inject, t)

const destType = computed(() => props.data?.destType ?? 'datasource')
const icon = computed(() =>
  destType.value === 'database'
    ? Database
    : destType.value === 's3'
      ? CloudUpload
      : destType.value === 'email'
        ? Mail
        : destType.value === 'sftp'
          ? Server
          : HardDriveDownload,
)
const title = computed(() =>
  destType.value === 'database'
    ? t('outputNode.titleDatabase')
    : destType.value === 's3'
      ? t('outputNode.titleS3')
      : destType.value === 'email'
        ? t('outputNode.titleEmail')
        : destType.value === 'sftp'
          ? t('sftp.outputTitle')
          : t('outputNode.titleDatasource'),
)
const summary = computed(() => {
  const d = props.data ?? {}
  if (destType.value === 'database') {
    const tbl = d.table?.trim()
    return tbl
      ? t('outputNode.tableSummary', { table: tbl, mode: d.mode === 'replace' ? t('outputNode.modeReplace') : t('outputNode.modeAppend') })
      : t('outputNode.chooseTable')
  }
  if (destType.value === 's3') {
    const k = d.s3Key?.trim()
    if (!k) return t('outputNode.chooseKeyBucket')
    const parts = (d.partitionBy ?? []).length ? t('outputNode.partitionsSuffix', { n: d.partitionBy.length }) : ''
    return `${k} (${d.s3Format ?? 'parquet'})${parts}`
  }
  if (destType.value === 'sftp') {
    const name = (d.sftpFileName ?? '').trim()
    return name ? t('sftp.outputSummary', { name, format: d.sftpFormat ?? 'csv' }) : t('sftp.chooseFileName')
  }
  if (destType.value === 'email') {
    const to: string[] = d.emailTo ?? []
    return to.length ? t('outputNode.emailSummary', { to: to.join(', ') }) : t('outputNode.chooseRecipients')
  }
  const n = d.name?.trim()
  return n ? t('outputNode.datasourceSummary', { name: n }) : t('outputNode.nameDatasource')
})
</script>

<template>
  <div class="node node-output">
    <!-- DATA input: the transformation chain (on the left, at the top) -->
    <Handle id="left" type="target" :position="Position.Left" class="h-left-top" />
    <!-- SEQUENCE input: orchestration order (on the left, below) -->
    <Handle id="seq-in" type="target" :position="Position.Left" class="handle-seq h-left-bottom" />
    <div class="node-title">
      <component :is="icon" :size="13" class="node-icon" />
      {{ title }}
    </div>
    <div class="node-body muted">{{ summary }}</div>
    <div v-if="stats" class="node-stats" :title="stats.title">
      <span v-if="stats.rows">{{ stats.rows }}</span>
      <span v-if="stats.rows" class="dot" aria-hidden="true">·</span>
      <span>{{ stats.cols }}</span>
      <span v-if="stats.noCache" class="nocache" :title="stats.noCache" role="img" :aria-label="stats.noCache"><RefreshCw :size="11" /></span>
    </div>
    <!-- SEQUENCE output towards the next control/output node -->
    <Handle id="seq-out" type="source" :position="Position.Right" class="handle-seq" />
  </div>
</template>

<style scoped>
.node-output { --node-accent: #fbbf24; }
.node-icon { color: var(--node-accent); }
.node-body { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.handle-seq { background: var(--node-accent) !important; }
</style>
