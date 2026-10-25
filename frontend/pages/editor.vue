<script setup lang="ts">
// The node editor (Vue Flow) is client-only: rendered only in the browser. Navigation
// (Projects/Sign out): inside the Toolbar, not in an overlay — so it never covers the
// flow's run state.
</script>

<template>
  <div class="editor-page">
    <ClientOnly>
      <FlowEditor />
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
