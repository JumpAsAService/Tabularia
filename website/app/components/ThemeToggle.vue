<script setup lang="ts">
// Dark / light switch. Without a choice the site follows the system (dark when unknown).
const KEY = 'tabularia-site-theme'
const theme = ref<'dark' | 'light'>('dark')
function current (): 'dark' | 'light' {
  const set = document.documentElement.dataset.theme
  if (set === 'light' || set === 'dark') return set
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}
onMounted(() => { theme.value = current() })
function toggle () {
  const next = current() === 'dark' ? 'light' : 'dark'
  document.documentElement.dataset.theme = next
  try { localStorage.setItem(KEY, next) } catch {}
  theme.value = next
}
const label = computed(() => (theme.value === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'))
</script>

<template>
  <button type="button" class="theme-toggle" :aria-label="label" :title="label" @click="toggle">
    <svg v-if="theme === 'dark'" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.5" /><path d="M12 2v2.5M12 19.5V22M4.2 4.2 6 6M18 18l1.8 1.8M2 12h2.5M19.5 12H22M4.2 19.8 6 18M18 6l1.8-1.8" /></svg>
    <svg v-else width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a7 7 0 0 0 11 11Z" /></svg>
  </button>
</template>

<style scoped>
.theme-toggle {
  display: inline-grid; place-items: center; width: 44px; height: 44px; padding: 0;
  border: 0; border-radius: var(--radius); background: transparent; color: var(--soft); cursor: pointer;
  transition: background-color 160ms ease, color 160ms ease, transform 160ms var(--ease-out);
}
.theme-toggle:hover { background: var(--tile-2); color: var(--text); }
.theme-toggle:active { transform: scale(.94); }
</style>
