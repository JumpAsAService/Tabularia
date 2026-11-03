<script setup lang="ts">
// The node editor (Vue Flow) is client-only: rendered only in the browser. Navigation
// (Projects/Sign out): inside the Toolbar, not in an overlay — so it never covers the
// flow's run state.
import { ref, watch } from 'vue'
import { editorKeyOf, opensAnotherFlow } from '~/composables/editorAddress'

// one editor per flow: another flow opened from here gets a new one (FlowEditor asks first
// about unsaved changes, as when leaving), a new flow saved for the first time keeps its own
const route = useRoute()
const shownFlow = useState<number | null>('editor.shownFlow', () => null)
const editorKey = ref(editorKeyOf(route.query))
watch(() => route.query, (to, from) => {
  if (route.path === '/editor' && opensAnotherFlow(to, from ?? {}, shownFlow.value)) editorKey.value = editorKeyOf(to)
})
</script>

<template>
  <div class="editor-page">
    <ClientOnly>
      <FlowEditor :key="editorKey" />
      <template #fallback>
        <div style="padding: 16px" class="muted">{{ $t('editor.loadingEditor') }}</div>
      </template>
    </ClientOnly>
  </div>
</template>

<style scoped>
.editor-page {
  position: relative;
}
</style>
