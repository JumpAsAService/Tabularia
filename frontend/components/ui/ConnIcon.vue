<script setup lang="ts">
// The pictogram of a kind of connection (database, bucket, envelope, folder...), in the
// kind's colour. Decoration only: the kind's name is always written beside it, so it is
// hidden from assistive technology.
import { computed } from 'vue'
import { Database, Columns3, Waypoints, Cylinder, Server, Archive, Mail, FolderOpen, FolderLock, Braces, Sheet, FlaskConical, Plug } from 'lucide-vue-next'
import { connIcon } from '~/composables/connectionIcons'

const props = withDefaults(defineProps<{ type: string | null | undefined; size?: number }>(), { size: 14 })

const COMPONENTS: Record<string, any> = { Database, Columns3, Waypoints, Cylinder, Server, Archive, Mail, FolderOpen, FolderLock, Braces, Sheet, FlaskConical, Plug }
const spec = computed(() => connIcon(props.type))
</script>

<template>
  <component :is="COMPONENTS[spec.icon]" class="conn-icon" :size="size" :style="{ color: spec.color }" aria-hidden="true" focusable="false" />
</template>

<style scoped>
.conn-icon { flex-shrink: 0; align-self: center; }
</style>
