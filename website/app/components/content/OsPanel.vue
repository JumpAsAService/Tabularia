<script setup lang="ts">
// One system's block of an <os-tabs> group: its heading ("#### Windows") and what follows.
import type { ComputedRef, Ref } from 'vue'
import { panelSystems } from '~/composables/useOsChoice'

const props = defineProps<{ os: string, label?: string }>()
const group = inject<{ ready: Ref<boolean>, active: ComputedRef<string>, tabId: (k: string) => string, panelId: (k: string) => string } | null>('os-tabs', null)
const tabbed = computed(() => Boolean(group?.ready.value))
</script>

<template>
  <section
    class="os-panel" :data-os="props.os" :data-for="panelSystems(props.os).join(' ')"
    :id="group ? group.panelId(props.os) : undefined"
    :role="tabbed ? 'tabpanel' : undefined"
    :aria-labelledby="tabbed ? group!.tabId(props.os) : undefined"
    :tabindex="tabbed ? 0 : undefined"
    :hidden="tabbed && group!.active.value !== props.os ? true : undefined"
  >
    <slot />
  </section>
</template>
