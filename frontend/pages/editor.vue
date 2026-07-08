<script setup lang="ts">
// L'editor a nodi (Vue Flow) è client-only: renderizzato solo lato browser.
import { ArrowLeft, LogOut } from 'lucide-vue-next'

const { logout } = useAuth()
</script>

<template>
  <div class="editor-page">
    <div class="editor-overlay">
      <NuxtLink to="/" class="overlay-btn"><ArrowLeft :size="13" /> Progetti</NuxtLink>
      <button class="overlay-btn" @click="logout"><LogOut :size="13" /> Esci</button>
    </div>
    <ClientOnly>
      <FlowEditor />
      <template #fallback>
        <div style="padding: 16px" class="muted">Caricamento editor…</div>
      </template>
    </ClientOnly>
  </div>
</template>

<style scoped>
.editor-page {
  position: relative;
}
.editor-overlay {
  position: fixed;
  top: 8px;
  right: 12px;
  z-index: 50;
  display: flex;
  gap: 8px;
}
.overlay-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  padding: 4px 10px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  text-decoration: none;
  cursor: pointer;
}
.overlay-btn:hover {
  border-color: var(--accent);
}
</style>
