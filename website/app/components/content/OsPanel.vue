<script setup lang="ts">
// One system's block of an <os-tabs> group: its heading ("#### Windows") and what follows.
import type { ComputedRef, Ref } from 'vue'
import { panelSystems } from '~/composables/useOsChoice'

const props = defineProps<{ os: string, label?: string }>()
const group = inject<{ ready: Ref<boolean>, active: ComputedRef<string>, tabId: (k: string) => string, panelId: (k: string) => string } | null>('os-tabs', null)
const tabbed = computed(() => Boolean(group?.ready.value))
// the code blocks inside say which terminal they run in (ProsePre.vue)
provide('os-panel', props.os)

// As tabs, the block's heading is hidden from sight (the tab says the system) and stays for
// screen readers; its anchor link must then leave the Tab order, or the keyboard stops on
// something that cannot be seen (WCAG 2.4.7).
const root = ref<HTMLElement | null>(null)
function headingLink () {
  const a = root.value?.querySelector<HTMLElement>(':scope > h4 a')
  if (!a) return
  if (tabbed.value) a.setAttribute('tabindex', '-1')
  else a.removeAttribute('tabindex')
}
onMounted(headingLink)
watch(tabbed, () => nextTick(headingLink))
</script>

<template>
  <section
    ref="root" class="os-panel" :data-os="props.os" :data-for="panelSystems(props.os).join(' ')"
    :id="group ? group.panelId(props.os) : undefined"
    :role="tabbed ? 'tabpanel' : undefined"
    :aria-labelledby="tabbed ? group!.tabId(props.os) : undefined"
    :tabindex="tabbed ? 0 : undefined"
    :hidden="tabbed && group!.active.value !== props.os ? true : undefined"
  >
    <slot />
  </section>
</template>
