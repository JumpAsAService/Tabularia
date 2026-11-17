<script setup lang="ts">
// The shortcuts widget: buttons to pages of this installation. A place of the bar shows with its
// name and icon in the reader's language; any other path shows as it is. A place the person does
// not have in their bar (Scripting without an environment, Administration) is not drawn.
import { computed, type Component } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  Activity, Cpu, Database, FileChartColumn, FolderTree, Gauge, History, House, LayoutDashboard, Link2, PieChart, Plug,
  ScrollText, Server, Share2, Shield, Sparkles, Terminal, Workflow,
} from 'lucide-vue-next'
import { NAV_SECTIONS } from '~/composables/appKeys'
import { internalPath } from '~/composables/homeLayout'
import { useKeyHelp } from '~/composables/useKeyHelp'

const props = defineProps<{ options: Record<string, any> }>()
const { t } = useI18n()
const keyHelp = useKeyHelp()
const ICONS: Record<string, Component> = {
  home: House, explore: FolderTree, flows: Workflow, datasources: Database, lineage: Share2, viewer: PieChart,
  reports: FileChartColumn, dashboards: LayoutDashboard, assistant: Sparkles, connections: Plug, scripting: Terminal,
  runs: History, admin: Shield, queue: Cpu, monitoring: Activity, performance: Gauge, cluster: Server, audit: ScrollText,
}
const items = computed(() => {
  const visible = new Set(keyHelp.sections.value.map((s) => s.to))
  return ((props.options.items ?? []) as string[]).filter(internalPath).flatMap((to) => {
    const place = NAV_SECTIONS.find((s) => s.to === to)
    if (place && !visible.has(place.to)) return []
    return [{ to, label: place ? t(place.label) : to, icon: place ? ICONS[place.id] ?? Link2 : Link2 }]
  })
})
</script>

<template>
  <nav class="shortcuts" :aria-label="$t('home.widgets.shortcuts')">
    <p v-if="!items.length" class="empty">{{ $t('home.shortcuts.empty') }}</p>
    <NuxtLink v-for="i in items" :key="i.to" :to="i.to" class="sc">
      <component :is="i.icon" :size="16" aria-hidden="true" /> <span>{{ i.label }}</span>
    </NuxtLink>
  </nav>
</template>

<style scoped>
.shortcuts { display: flex; flex-wrap: wrap; align-content: flex-start; gap: 8px; height: 100%; overflow: auto; }
.sc {
  display: inline-flex; align-items: center; gap: 8px; min-height: 36px; padding: 6px 12px;
  border: 1px solid var(--border-soft); border-radius: 9px; background: var(--panel-2);
  color: var(--text); text-decoration: none; font-size: 13px; font-weight: 550;
}
.sc:hover, .sc:focus-visible { border-color: var(--accent); background: var(--tint-accent); }
.sc span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 220px; }
.empty { margin: 0; color: var(--muted); font-size: 13px; }
</style>
