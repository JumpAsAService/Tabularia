<script setup lang="ts">
// The data grid's keys, said to whoever sees the screen too (the grid's own hint is for a
// screen reader): a quiet button in the bar above the grid opens their list. The flow
// editor lists ALL its keys with the same button and panel, in groups (`groups`, `title`,
// `label`): one way of saying keys in the product, not a second component.
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Keyboard } from 'lucide-vue-next'
import { usePopover } from '~/composables/usePopover'

const props = withDefaults(defineProps<{
  profile?: boolean
  // another surface's keys in place of the grid's, group by group, what the panel calls
  // them and what the button is called
  groups?: { title: string; keys: [string, string][]; note?: string }[]
  title?: string
  label?: string
  // a long list with longer key names (the editor's): a wider panel that scrolls
  wide?: boolean
}>(), { profile: false, wide: false })
const { t } = useI18n()
const open = ref(false)
const panel = ref<HTMLElement | null>(null)
const btn = ref<HTMLButtonElement | null>(null)
// opened from the keyboard it appears without motion, as the menus do
const byKey = ref(false)
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
const shown = computed(() => props.groups ?? [{ title: '', keys: keys.value }])
function toggle(keyboard = false) {
  byKey.value = keyboard
  open.value = !open.value
}
// a key of the surface («?» in the editor) opens it too: the button takes the focus first,
// so that Esc gives it back to the button, as after a click
defineExpose({
  toggle: () => {
    if (!open.value) btn.value?.focus({ preventScroll: true })
    toggle(true)
  },
})
</script>

<template>
  <span class="gk">
    <button
      ref="btn"
      type="button"
      class="gk-btn"
      :aria-expanded="open"
      :aria-controls="uid"
      :aria-label="label ?? $t('gridKeys.button')"
      :title="label ?? $t('gridKeys.button')"
      @click="toggle($event.detail === 0)"
    ><Keyboard :size="14" aria-hidden="true" /></button>
    <Transition name="pop" :css="!byKey">
      <div v-if="open" :id="uid" ref="panel" class="gk-pop" :class="{ wide }" role="dialog" tabindex="-1" :aria-label="title ?? $t('gridKeys.title')">
        <p class="gk-title">{{ title ?? $t('gridKeys.title') }}</p>
        <template v-for="g in shown" :key="g.title">
          <p v-if="g.title" class="gk-group">{{ g.title }}</p>
          <dl v-if="g.keys.length">
            <div v-for="([k, what], i) in g.keys" :key="i">
              <dt><kbd>{{ k }}</kbd></dt>
              <dd>{{ what }}</dd>
            </div>
          </dl>
          <p v-if="g.note" class="gk-note">{{ g.note }}</p>
        </template>
      </div>
    </Transition>
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
/* the editor's whole list: wider, in groups, scrolling inside when the canvas is short */
.gk-pop.wide { width: 380px; max-height: min(70vh, 560px); overflow-y: auto; }
.gk-pop.wide dl > div { grid-template-columns: 132px 1fr; }
.gk-group { margin: 10px 0 5px; font-size: 11px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted); }
.gk-title + .gk-group { margin-top: 2px; }
.gk-note { margin: 0; line-height: 1.4; }
.gk-pop dt { margin: 0; }
.gk-pop dd { margin: 0; line-height: 1.4; }
.gk-pop kbd {
  font: 500 11px/1.4 "IBM Plex Sans", system-ui, sans-serif; white-space: nowrap; color: var(--text);
  padding: 1px 5px; border: 1px solid var(--border); border-bottom-width: 2px; border-radius: 4px; background: var(--panel-2);
}
</style>
