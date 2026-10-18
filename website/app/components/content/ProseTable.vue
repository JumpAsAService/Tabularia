<script setup lang="ts">
// A wide table scrolls sideways inside its own box. Only then is the box a focusable,
// labelled region, so keyboard users can scroll it; a table that fits is just a table.
const box = ref<HTMLElement | null>(null)
const scrolls = ref(false)
let ro: ResizeObserver | null = null
onMounted(() => {
  const el = box.value
  if (!el) return
  const measure = () => { scrolls.value = el.scrollWidth > el.clientWidth + 1 }
  measure()
  ro = new ResizeObserver(measure)
  ro.observe(el)
})
onBeforeUnmount(() => ro?.disconnect())
</script>

<template>
  <div
    ref="box" class="table-wrap" :tabindex="scrolls ? 0 : undefined" :role="scrolls ? 'region' : undefined"
    :aria-label="scrolls ? 'Table, scrolls sideways' : undefined"
  >
    <table><slot /></table>
  </div>
</template>
