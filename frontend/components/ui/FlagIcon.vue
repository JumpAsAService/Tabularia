<script setup lang="ts">
// The flag beside a language's name, where a language is chosen. Drawn here (five small
// vector flags, 3:2) and not as emoji: a flag emoji is two letters on Windows and a
// different picture on every system. Decoration only: the name beside it says the language.
import { useId } from 'vue'

defineProps<{ code: string }>()
// the Union Jack needs two clip paths, and an id must be one in the page
const uid = useId()
</script>

<template>
  <svg class="flag" viewBox="0 0 21 14" aria-hidden="true" focusable="false">
    <template v-if="code === 'it'">
      <path fill="#009246" d="M0 0h7v14H0z" /><path fill="#fff" d="M7 0h7v14H7z" /><path fill="#ce2b37" d="M14 0h7v14h-7z" />
    </template>
    <template v-else-if="code === 'fr'">
      <path fill="#0055a4" d="M0 0h7v14H0z" /><path fill="#fff" d="M7 0h7v14H7z" /><path fill="#ef4135" d="M14 0h7v14h-7z" />
    </template>
    <template v-else-if="code === 'de'">
      <path fill="#000" d="M0 0h21v4.67H0z" /><path fill="#d00" d="M0 4.67h21v4.66H0z" /><path fill="#ffce00" d="M0 9.33h21V14H0z" />
    </template>
    <template v-else-if="code === 'es'">
      <path fill="#aa151b" d="M0 0h21v14H0z" /><path fill="#f1bf00" d="M0 3.5h21v7H0z" />
    </template>
    <template v-else-if="code === 'en'">
      <!-- the Union Jack on its own 60×30 field, cut to 3:2 from the middle -->
      <svg viewBox="0 0 60 30" width="21" height="14" preserveAspectRatio="xMidYMid slice">
        <clipPath :id="`${uid}-f`"><path d="M0 0v30h60V0z" /></clipPath>
        <clipPath :id="`${uid}-d`"><path d="M30 15h30v15zv15H0zH0V0zV0h30z" /></clipPath>
        <g :clip-path="`url(#${uid}-f)`">
          <path fill="#012169" d="M0 0v30h60V0z" />
          <path stroke="#fff" stroke-width="6" d="M0 0l60 30M60 0L0 30" />
          <path stroke="#c8102e" stroke-width="4" :clip-path="`url(#${uid}-d)`" d="M0 0l60 30M60 0L0 30" />
          <path stroke="#fff" stroke-width="10" d="M30 0v30M0 15h60" />
          <path stroke="#c8102e" stroke-width="6" d="M30 0v30M0 15h60" />
        </g>
      </svg>
    </template>
    <path v-else fill="var(--border)" d="M0 0h21v14H0z" />
  </svg>
</template>

<style scoped>
/* a hairline around it: a white stripe would have no edge on the light themes, a black
   one none on the dark */
.flag {
  width: 18px;
  height: 12px;
  flex: none;
  align-self: center;
  border-radius: 2px;
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--text) 22%, transparent);
}
</style>
