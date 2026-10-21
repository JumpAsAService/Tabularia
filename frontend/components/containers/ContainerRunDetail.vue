<script setup lang="ts">
// What a Container node's run recorded (services/container_runs.py in the gateway): the
// registry and the image by digest, the command, the attempts and the exit code, the
// variables it was given — secret ones by name only — and the end of its log.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { KeyRound } from 'lucide-vue-next'

const props = defineProps<{ info: string }>()
const { t } = useI18n()

interface ContainerInfo {
  registry?: string
  repository?: string
  reference?: string
  image?: string
  command?: string[]
  args?: string[]
  cpu?: string
  memory?: string
  input?: boolean
  output?: boolean
  attempts?: number
  exit_code?: number | null
  variables?: Record<string, { value?: string; secret?: boolean; changed_at?: string | null; node?: boolean }>
  log?: string
}

const info = computed<ContainerInfo>(() => {
  try {
    return JSON.parse(props.info)
  } catch {
    return {}
  }
})
const variables = computed(() => Object.entries(info.value.variables ?? {}))
const runs = computed(() => {
  const parts = [...(info.value.command ?? []), ...(info.value.args ?? [])]
  return parts.length ? parts.join(' ') : t('containers.run.imageDefault')
})
const io = computed(() => [
  info.value.input ? t('scripting.run.withInput') : t('scripting.run.noInput'),
  info.value.output ? t('scripting.run.withOutput') : t('scripting.run.noOutput'),
].join(' · '))
</script>

<template>
  <div class="container-run">
    <dl>
      <div class="largo"><dt>{{ $t('containers.run.image') }}</dt><dd>{{ info.registry }} · <code>{{ info.repository }}:{{ info.reference }}</code></dd></div>
      <div class="largo"><dt>{{ $t('containers.run.runs') }}</dt><dd><code>{{ runs }}</code></dd></div>
      <div><dt>{{ $t('scripting.run.attempts') }}</dt><dd>{{ info.attempts ?? 0 }}</dd></div>
      <div><dt>{{ $t('scripting.run.exitCode') }}</dt><dd>{{ info.exit_code ?? '—' }}</dd></div>
      <div><dt>{{ $t('containers.run.resources') }}</dt><dd>{{ info.cpu }} CPU · {{ info.memory }}</dd></div>
      <div><dt>{{ $t('scripting.run.data') }}</dt><dd>{{ io }}</dd></div>
    </dl>
    <details v-if="variables.length">
      <summary>{{ $t('scripting.run.variables', { n: variables.length }) }}</summary>
      <ul class="vars">
        <li v-for="[name, v] in variables" :key="name">
          <code>{{ name }}</code>
          <span v-if="v.secret" class="muted"><KeyRound :size="11" /> {{ $t('scripting.run.secret') }}</span>
          <span v-else>{{ v.value }}</span>
          <span v-if="v.node" class="muted small">{{ $t('containers.run.fromNode') }}</span>
        </li>
      </ul>
    </details>
    <details :open="!!info.log">
      <summary>{{ $t('scripting.run.log') }}</summary>
      <pre v-if="info.log" class="log">{{ info.log }}</pre>
      <p v-else class="muted small">{{ $t('scripting.run.noLog') }}</p>
    </details>
    <p v-if="info.image" class="muted small immagine"><code>{{ info.image }}</code></p>
  </div>
</template>

<style scoped>
.container-run { display: flex; flex-direction: column; gap: 8px; }
dl { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 0; }
dl div { padding: 6px 8px; border: 1px solid var(--border-soft); border-radius: 8px; min-width: 0; }
dl .largo { grid-column: span 2; }
dt { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
dd { margin: 2px 0 0; font-size: 13px; overflow-wrap: anywhere; }
summary { cursor: pointer; font-size: 12px; color: var(--muted); }
.vars { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 3px; font-size: 12.5px; }
.vars li { display: flex; gap: 8px; align-items: center; }
.log { margin: 6px 0 0; max-height: 420px; overflow: auto; font-size: 11.5px; padding: 8px; border-radius: 6px; background: var(--bg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.small { font-size: 12px; margin: 0; }
.immagine { overflow-wrap: anywhere; }
@media (max-width: 720px) { dl { grid-template-columns: 1fr 1fr; } dl .largo { grid-column: 1 / -1; } }
</style>
