<script setup lang="ts">
// L'editor a nodi (Vue Flow) è client-only: renderizzato solo lato browser.
// Navigazione (Progetti/Esci): dentro la Toolbar, non in overlay — così non
// copre mai lo stato di esecuzione del flusso.
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
