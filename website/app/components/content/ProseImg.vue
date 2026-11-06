<script setup lang="ts">
// An image of the docs. A screenshot (lib/prose-blocks.mjs marks the ones under media/ that are
// not already a link) can be enlarged: without JavaScript it is a link to the file; with it, the
// link opens a full-window view (a modal <dialog>: Esc, the close button or a click outside
// close it, and the focus goes back to the image). In the view, "Zoom" doubles the size.
import { joinURL, withLeadingSlash, withTrailingSlash } from 'ufo'

const props = defineProps({
  src: { type: String, default: '' },
  alt: { type: String, default: '' },
  width: { type: [String, Number], default: undefined },
  height: { type: [String, Number], default: undefined },
  zoom: { type: [String, Boolean], default: undefined },
})
const base = useRuntimeConfig().app.baseURL
const url = computed(() => {
  const src = props.src
  if (src?.startsWith('/') && !src.startsWith('//')) {
    const b = withLeadingSlash(withTrailingSlash(base))
    if (b !== '/' && !src.startsWith(b)) return joinURL(b, src)
  }
  return src
})
const zoomable = computed(() => props.zoom !== undefined && props.zoom !== false && props.zoom !== 'false')

const shown = ref(false)
const big = ref(false)
const dialog = ref<HTMLDialogElement | null>(null)
const link = ref<HTMLAnchorElement | null>(null)
async function open (e: MouseEvent) {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
  e.preventDefault()
  big.value = false
  shown.value = true
  await nextTick()
  dialog.value?.showModal()
}
function close () { dialog.value?.close() }
function closed () {
  shown.value = false
  link.value?.focus()
}
// a click on the backdrop (outside the frame) closes
function onClick (e: MouseEvent) { if (e.target === dialog.value) close() }
</script>

<template>
  <a v-if="zoomable" ref="link" class="zoom" :href="url" :aria-label="`Enlarge: ${props.alt}`" @click="open">
    <img :src="url" :alt="props.alt" :width="props.width" :height="props.height" loading="lazy">
  </a>
  <img v-else :src="url" :alt="props.alt" :width="props.width" :height="props.height">
  <Teleport v-if="shown" to="body">
    <dialog ref="dialog" class="zoom-view" :class="{ 'is-big': big }" :aria-label="props.alt" @close="closed" @click="onClick">
      <div class="zoom-view__bar">
        <button type="button" class="zoom-view__btn" :aria-pressed="big ? 'true' : 'false'" @click="big = !big">Zoom</button>
        <button type="button" class="zoom-view__btn" autofocus @click="close">Close</button>
      </div>
      <div class="zoom-view__frame">
        <img :src="url" :alt="props.alt">
      </div>
      <p class="zoom-view__caption">{{ props.alt }}</p>
    </dialog>
  </Teleport>
</template>

<style>
.prose a.zoom { display: block; cursor: zoom-in; border-radius: var(--radius); }
.prose a.zoom img { display: block; transition: opacity 160ms ease; }
.prose a.zoom:hover img { opacity: .92; }
.prose a.zoom:focus-visible { outline-offset: 4px; }

.zoom-view {
  width: calc(100vw - 32px); max-width: none; height: calc(100dvh - 32px); max-height: none;
  margin: 16px; padding: 0; border: 0; border-radius: var(--radius);
  background: var(--ground); color: var(--text);
  display: none; flex-direction: column;
}
.zoom-view[open] { display: flex; }
.zoom-view::backdrop { background: rgb(11 14 20 / .78); }
.zoom-view__bar { display: flex; justify-content: flex-end; gap: 8px; padding: 10px 12px; flex: none; }
.zoom-view__btn {
  min-height: 40px; padding: 0 16px; border: 0; border-radius: 6px;
  background: var(--tile-2); color: var(--text); font: inherit; font-size: 15px; font-weight: 600; cursor: pointer;
}
.zoom-view__btn:hover { background: var(--tile-3); }
.zoom-view__btn[aria-pressed="true"] { background: var(--accent); color: var(--on-accent); }
.zoom-view__frame { flex: 1; min-height: 0; overflow: auto; padding: 0 12px; display: grid; place-items: start center; }
.zoom-view__frame img { width: 100%; height: auto; max-width: none; border-radius: 6px; }
.zoom-view.is-big .zoom-view__frame img { width: 200%; }
.zoom-view__caption { flex: none; margin: 0; padding: 10px 16px 14px; font-size: 14px; color: var(--soft); }
</style>
