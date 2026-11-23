<script setup lang="ts">
// The Versions tab of a saved model (docs/specs/bayes-graph-editor.md, «The editor»; the store's
// «Restoring a version»): every version, newest first, with what it was (created, edited, only the
// drawing moved, restored from…, duplicated from…), when and by whom; «Open» shows a version read
// only, «Restore» copies it — its graph and its Stan program as stored — into a new version.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Eye, History, LoaderCircle, RotateCcw } from 'lucide-vue-next'
import { useFormat } from '~/composables/useFormat'
import { bayesVersionNote } from '~/composables/bayesModelSave'
import type { BayesVersionListed } from '~/composables/useBayesModels'

const props = defineProps<{
  versions: BayesVersionListed[]
  /** the version on the canvas */
  shown: number | null
  editable: boolean
  loading: boolean
  restoring: number | null
}>()
const emit = defineEmits<{ (e: 'open', v: number): void, (e: 'restore', v: number): void }>()
const { t } = useI18n()
const fmt = useFormat()

const list = computed(() => [...props.versions].sort((a, b) => b.version - a.version))
const noteOf = (v: BayesVersionListed) => {
  const n = bayesVersionNote(v.note)
  return t(`bayes.editor.versions.note.${n.key}`, n.params)
}
</script>

<template>
  <div class="vers">
    <p class="lead">{{ t('bayes.editor.versions.lead') }}</p>
    <p v-if="loading && !versions.length" class="none">{{ t('bayes.editor.versions.loading') }}</p>
    <ol v-else class="list" :aria-label="t('bayes.editor.versions.title')">
      <li v-for="v in list" :key="v.version" :class="{ shown: v.version === shown }">
        <div class="top">
          <strong class="vn">v{{ v.version }}</strong>
          <span v-if="v.current" class="tag cur">{{ t('bayes.editor.versions.current') }}</span>
          <span v-if="v.version === shown" class="tag on">{{ t('bayes.editor.versions.shown') }}</span>
          <span class="note">{{ noteOf(v) }}</span>
        </div>
        <div class="meta">
          <time :datetime="v.created_at">{{ fmt.dateTime(v.created_at) }}</time>
          <span v-if="v.created_by"> · {{ v.created_by }}</span>
        </div>
        <div class="acts">
          <button v-if="v.version !== shown" type="button" class="mini" @click="emit('open', v.version)">
            <Eye :size="12" aria-hidden="true" />{{ v.current ? t('bayes.editor.versions.openCurrent') : t('bayes.editor.versions.open') }}
          </button>
          <button
            v-if="editable && !v.current"
            type="button"
            class="mini"
            :disabled="restoring !== null"
            :aria-busy="restoring === v.version"
            @click="emit('restore', v.version)"
          >
            <LoaderCircle v-if="restoring === v.version" :size="12" class="spin" aria-hidden="true" /><RotateCcw v-else :size="12" aria-hidden="true" />
            {{ t('bayes.editor.versions.restore') }}
          </button>
        </div>
      </li>
    </ol>
    <p class="foot"><History :size="12" aria-hidden="true" />{{ t('bayes.editor.versions.foot') }}</p>
  </div>
</template>

<style scoped>
.vers { display: flex; flex-direction: column; gap: 10px; }
.lead, .none { margin: 0; font-size: 12.5px; line-height: 1.5; color: var(--muted); }
.list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.list li {
  display: flex; flex-direction: column; gap: 3px; padding: 8px 10px;
  border: 1px solid var(--border-soft); border-radius: 8px; background: var(--panel);
}
.list li.shown { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 5%, var(--panel)); }
.top { display: flex; align-items: baseline; gap: 7px; flex-wrap: wrap; min-width: 0; }
.vn { font-size: 13px; font-variant-numeric: tabular-nums; }
.tag {
  font-size: 10.5px; font-weight: 600; letter-spacing: 0.03em; text-transform: uppercase; padding: 0 6px; border-radius: 5px;
  border: 1px solid var(--border); color: var(--muted); background: var(--panel-2);
}
.tag.cur { color: var(--accent-2); border-color: color-mix(in srgb, var(--accent-2) 40%, var(--border)); }
.tag.on { color: var(--accent-hi); border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); background: var(--tint-accent); }
.note { font-size: 12.5px; color: var(--text); min-width: 0; overflow-wrap: anywhere; }
.meta { font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.acts { display: flex; gap: 6px; margin-top: 3px; }
.acts .mini { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; padding: 2px 8px; min-height: 24px; }
.foot { margin: 2px 0 0; display: flex; gap: 6px; align-items: flex-start; font-size: 11.5px; line-height: 1.45; color: var(--muted); }
.foot :deep(svg) { flex: none; margin-top: 2px; }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
