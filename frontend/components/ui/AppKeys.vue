<script setup lang="ts">
// The keys of every page of the app (composables/appKeys.ts holds the rules): «g» then a
// letter goes to a place of the top bar, «,» and «.» walk the page's row of sub-tabs, «/»
// leads to the search, «?» lists the keys. One listener for the whole app, mounted once
// (app.vue). Never while typing, never with a modifier, never with a dialog or a menu
// open, and not on the public pages (sign-in, a shared link). Each key does what the click
// does (the same navigation, the tab's own click): nothing more is asked of the gateway.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import GridKeys from '~/components/ui/GridKeys.vue'
import { appKey, keysAreTyping, stepTab } from '~/composables/appKeys'
import { useKeyHelp } from '~/composables/useKeyHelp'

const { t } = useI18n()
const route = useRoute()
const help = useKeyHelp()
const own = ref<InstanceType<typeof GridKeys> | null>(null)

// the pages anyone reaches without an account have no keys: nothing of the app is there
const isPublic = computed(() => /^\/(login|auth|share)(\/|$)/.test(route.path))

let pendingSince: number | null = null

function shownEl(sel: string): HTMLElement | null {
  for (const el of document.querySelectorAll<HTMLElement>(sel)) {
    if (el.getClientRects().length) return el
  }
  return null
}

function typing(e: KeyboardEvent): boolean {
  for (const el of [e.target as HTMLElement | null, document.activeElement as HTMLElement | null]) {
    if (!el || !el.tagName) continue
    if (keysAreTyping({
      tag: el.tagName.toUpperCase(),
      type: (el as HTMLInputElement).type,
      editable: el.isContentEditable,
      inEditor: !!el.closest?.('.nokey, .monaco-editor, .cm-editor, [role="textbox"], [role="combobox"], [role="searchbox"]'),
    })) return true
  }
  return false
}

/** The page's main row of sub-tabs: its tabs (or the choices of its filter), as they are. */
function mainTabs(): HTMLElement[] {
  const row = shownEl('[data-keytabs="main"]')
  if (!row) return []
  return [...row.querySelectorAll<HTMLElement>('[role="tab"], [role="radio"]')]
    .filter((b) => !(b as HTMLButtonElement).disabled)
}

function onKey(e: KeyboardEvent) {
  if (isPublic.value || e.defaultPrevented || e.isComposing) return
  const active = document.activeElement as HTMLElement | null
  // the list itself is open and has the focus: «?» closes it, every other key is its own
  if (active?.closest?.('.gk-pop')) {
    pendingSince = null
    if (e.key === '?' && !e.ctrlKey && !e.metaKey && !e.altKey) {
      take(e)
      if (!help.toggle()) own.value?.toggle()
    }
    return
  }
  if (typing(e) || shownEl('[role="dialog"], [role="menu"], .sel-panel, .menu-pop, .topbar .menu')) {
    pendingSince = null
    return
  }
  const now = Date.now()
  const k = appKey(e, pendingSince, now, help.sections.value)
  pendingSince = k.act === 'pending' ? now : k.pending ? pendingSince : null
  switch (k.act) {
    case 'pending':
    case 'swallow':
      take(e)
      return
    case 'goto':
      take(e)
      // the link's own navigation; already there, nothing happens (as a click on it)
      if (route.path !== k.to) navigateTo(k.to)
      return
    case 'tab': {
      const tabs = mainTabs()
      const i = stepTab(tabs.map((b) => ({
        selected: b.getAttribute('aria-selected') === 'true' || b.getAttribute('aria-checked') === 'true',
        disabled: b.getAttribute('aria-disabled') === 'true',
      })), k.dir)
      if (i < 0) return
      take(e)
      tabs[i].click()
      return
    }
    case 'help':
      take(e)
      if (!help.toggle()) own.value?.toggle()
      return
    case 'search': {
      // a page with its own filter under «/» (Explore) answers by itself
      if (shownEl('[aria-keyshortcuts~="/"]')) return
      const box = shownEl('.page-head .searchbox input') ?? shownEl('.topbar .gs input')
      if (!box) return
      take(e)
      box.focus()
      ;(box as HTMLInputElement).select?.()
      return
    }
  }
}
// taken before the page's own listeners see it: «g n» must not be Explore's «n»
function take(e: KeyboardEvent) {
  e.preventDefault()
  e.stopPropagation()
}

onMounted(() => window.addEventListener('keydown', onKey, true))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey, true))
</script>

<template>
  <!-- a page with no keyboard button of its own (the designers, a notebook): «?» opens the
       same list at the window's corner -->
  <GridKeys
    v-if="!isPublic && !help.hosted.value"
    ref="own"
    bare
    wide
    :groups="help.groups.value"
    :title="t('appKeys.title')"
  />
</template>
