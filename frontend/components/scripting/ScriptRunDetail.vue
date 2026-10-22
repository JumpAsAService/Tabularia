<script setup lang="ts">
// What a Script node's run recorded (services/script_runs.py in the gateway): the
// environment and its version, the files, the attempts and the exit code, the
// variables it was given — secret ones by name only — and the end of its log.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { KeyRound } from 'lucide-vue-next'

const props = defineProps<{ script: string }>()
const { t } = useI18n()

interface ScriptInfo {
  label?: string
  environment?: string
  language?: string
  version?: number
  image?: string
  entrypoint?: string
  files?: string[]
  input?: boolean
  output?: boolean
  attempts?: number
  exit_code?: number | null
  variables?: Record<string, { value?: string; secret?: boolean; changed_at?: string | null; from_tabularia?: string }>
  log?: string
  log_expired?: boolean // emptied after ORCHESTRATOR__LOG_RETENTION_DAYS
  // a saved script (docs/specs/saved-scripts.md): run on its own, or by a Script node
  saved_script_id?: number
  script?: string
  script_version?: number
  inputs?: { alias: string; datasource_id: number; name: string }[]
}

const info = computed<ScriptInfo>(() => {
  try {
    return JSON.parse(props.script)
  } catch {
    return {}
  }
})
const variables = computed(() => Object.entries(info.value.variables ?? {}))
const savedName = computed(() => (info.value.saved_script_id ? (info.value.script ?? info.value.label ?? '') : ''))
const io = computed(() => [
  info.value.input ? t('scripting.run.withInput') : t('scripting.run.noInput'),
  info.value.output ? t('scripting.run.withOutput') : t('scripting.run.noOutput'),
].join(' · '))
</script>

<template>
  <div class="script-run">
    <dl>
      <div v-if="info.saved_script_id" class="largo">
        <dt>{{ $t('scripting.run.savedScript') }}</dt>
        <dd><NuxtLink :to="`/script?id=${info.saved_script_id}`">{{ savedName }}</NuxtLink> · v{{ info.script_version }}</dd>
      </div>
      <div v-if="info.inputs?.length" class="largo">
        <dt>{{ $t('scripting.run.inputs') }}</dt>
        <dd><span v-for="(i, k) in info.inputs" :key="i.alias"><template v-if="k"> · </template><code>{{ i.alias }}</code> {{ i.name }}</span></dd>
      </div>
      <div><dt>{{ $t('scripting.run.environment') }}</dt><dd>{{ info.environment }} · v{{ info.version }}</dd></div>
      <div><dt>{{ $t('scripting.run.runs') }}</dt><dd><code>{{ info.entrypoint }}</code> <span class="muted">({{ (info.files ?? []).length }})</span></dd></div>
      <div><dt>{{ $t('scripting.run.attempts') }}</dt><dd>{{ info.attempts ?? 0 }}</dd></div>
      <div><dt>{{ $t('scripting.run.exitCode') }}</dt><dd>{{ info.exit_code ?? '—' }}</dd></div>
      <div class="largo"><dt>{{ $t('scripting.run.data') }}</dt><dd>{{ io }}</dd></div>
    </dl>
    <details v-if="variables.length">
      <summary>{{ $t('scripting.run.variables', { n: variables.length }) }}</summary>
      <ul class="vars">
        <li v-for="[name, v] in variables" :key="name">
          <code>{{ name }}</code>
          <span v-if="v.secret" class="muted"><KeyRound :size="11" /> {{ $t('scripting.run.secret') }}</span>
          <span v-else-if="v.from_tabularia" class="muted">{{ $t('scripting.run.fromTabularia', { name: v.from_tabularia }) }}</span>
          <span v-else>{{ v.value }}</span>
        </li>
      </ul>
    </details>
    <details :open="!!info.log">
      <summary>{{ $t('scripting.run.log') }}</summary>
      <pre v-if="info.log" class="log">{{ info.log }}</pre>
      <p v-else-if="info.log_expired" class="muted small">{{ $t('scripting.run.logExpired') }}</p>
      <p v-else class="muted small">{{ $t('scripting.run.noLog') }}</p>
    </details>
    <p v-if="info.image" class="muted small immagine"><code>{{ info.image }}</code></p>
  </div>
</template>

<style scoped>
.script-run { display: flex; flex-direction: column; gap: 8px; }
dl { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 0; }
dl div { padding: 6px 8px; border: 1px solid var(--border-soft); border-radius: 8px; min-width: 0; }
dl .largo { grid-column: 1 / -1; }
dt { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
dd { margin: 2px 0 0; font-size: 13px; overflow-wrap: anywhere; }
summary { cursor: pointer; font-size: 12px; color: var(--muted); }
.vars { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 3px; font-size: 12.5px; }
.vars li { display: flex; gap: 8px; align-items: center; }
.log { margin: 6px 0 0; max-height: 420px; overflow: auto; font-size: 11.5px; padding: 8px; border-radius: 6px; background: var(--bg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.small { font-size: 12px; margin: 0; }
.immagine { overflow-wrap: anywhere; }
@media (max-width: 720px) { dl { grid-template-columns: 1fr 1fr; } }
</style>
