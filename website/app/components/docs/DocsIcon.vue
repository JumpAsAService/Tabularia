<script setup lang="ts">
// The icon of a documentation section (sidebar and index).
import { BookOpen, Blocks, Database, Plug, Rocket, Server, ShieldCheck, Workflow } from 'lucide-vue-next'
import type { DocsIcon } from '~/utils/docs-nav'

const props = defineProps<{ name: DocsIcon, size?: number }>()
const ICONS = {
  start: Rocket,
  capabilities: Workflow,
  sources: Database,
  connections: Plug,
  integrations: Blocks,
  governance: ShieldCheck,
  operate: Server,
  reference: BookOpen,
} as const
</script>

<template>
  <component :is="ICONS[props.name]" :size="props.size ?? 18" :stroke-width="1.75" aria-hidden="true" />
</template>
