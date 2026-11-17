<script setup lang="ts">
// The note widget: the person's own text. Drawn by MarkdownLite, as nodes and never as HTML: what
// is typed stays text whatever it holds.
import MarkdownLite from '~/components/ui/MarkdownLite.vue'

defineProps<{ options: Record<string, any> }>()
</script>

<template>
  <div class="note">
    <MarkdownLite v-if="(options.text ?? '').trim()" :text="options.text" />
    <p v-else class="empty">{{ $t('home.note.empty') }}</p>
  </div>
</template>

<style scoped>
.note { height: 100%; overflow: auto; font-size: 13.5px; line-height: 1.5; overflow-wrap: anywhere; }
.empty { margin: 0; color: var(--muted); font-size: 13px; }
</style>
