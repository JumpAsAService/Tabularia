<script setup lang="ts">
// The data grid's keys, said to whoever sees the screen too (the grid's own hint is for a
// screen reader): a quiet button in the bar above the grid opens their list.
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Keyboard } from 'lucide-vue-next'
import { usePopover } from '~/composables/usePopover'

const props = withDefaults(defineProps<{ profile?: boolean }>(), { profile: false })
const { t } = useI18n()
const open = ref(false)
const panel = ref<HTMLElement | null>(null)
const uid = `gk-${Math.random().toString(36).slice(2, 8)}`
usePopover(open, panel, () => { open.value = false }, { focus: 'panel' })
const mac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || '')
const mod = mac ? '⌘' : 'Ctrl'
const keys = computed(() => {
  const list: [string, string][] = [
    ['← ↑ → ↓', t('gridKeys.arrows')],
    ['Home · End', t('gridKeys.homeEnd')],
    [`${mod}+Home · ${mod}+End`, t('gridKeys.ctrlHomeEnd')],
    ['PgUp · PgDn', t('gridKeys.page')],
    [`${mod}+C`, t('gridKeys.copy')],
  ]
  if (props.profile) {
    list.push(['Enter', t('gridKeys.enter')], ['← →', t('gridKeys.profileArrows')], ['Esc', t('gridKeys.escape')])
  }
  return list
})
</script>

<template>
  <span class="gk">
    <button
      type="button"
      class="gk-btn"
      :aria-expanded="open"
      :aria-controls="uid"
      :aria-label="$t('gridKeys.button')"
      :title="$t('gridKeys.button')"
      @click="open = !open"
    ><Keyboard :size="14" aria-hidden="true" /></button>
    <div v-if="open" :id="uid" ref="panel" class="gk-pop" role="dialog" tabindex="-1" :aria-label="$t('gridKeys.title')">
      <p class="gk-title">{{ $t('gridKeys.title') }}</p>
      <dl>
        <div v-for="[k, what] in keys" :key="k">
          <dt><kbd>{{ k }}</kbd></dt>
          <dd>{{ what }}</dd>
        </div>
      </dl>
    </div>
  </span>
</template>

<style scoped>
.gk { position: relative; flex: none; display: inline-flex; }
.gk-btn {
  display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; padding: 0;
  border: 1px solid transparent; border-radius: 6px; background: none; box-shadow: none; color: var(--muted);
}
.gk-btn:hover, .gk-btn[aria-expanded="true"] { color: var(--text); background: var(--panel-2); border-color: var(--border); box-shadow: none; }
.gk-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.gk-pop {
  position: absolute; top: calc(100% + 6px); right: 0; z-index: 40; width: 300px;
  padding: 10px 12px; background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow-2); font-size: 12px; color: var(--text);
}
.gk-pop:focus { outline: none; }
.gk-title { margin: 0 0 6px; color: var(--muted); }
.gk-pop dl { display: grid; gap: 4px; margin: 0; }
.gk-pop dl > div { display: grid; grid-template-columns: 118px 1fr; gap: 10px; align-items: baseline; }
.gk-pop dt { margin: 0; }
.gk-pop dd { margin: 0; line-height: 1.4; }
.gk-pop kbd {
  font: 500 11px/1.4 "IBM Plex Sans", system-ui, sans-serif; white-space: nowrap; color: var(--text);
  padding: 1px 5px; border: 1px solid var(--border); border-bottom-width: 2px; border-radius: 4px; background: var(--panel-2);
}
</style>
