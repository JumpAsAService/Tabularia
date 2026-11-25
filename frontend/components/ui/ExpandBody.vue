<script setup lang="ts">
// The body of an expander that is not a `<details>`: around the ONE element that comes and goes
// (by `v-if` or `v-show`) under a head of its own, it makes it open and close over a moment, like
// every expander of the app (composables/expandMotion.ts). It draws nothing itself, so the
// element stays where it was for its styles; with reduced motion it comes and goes at once.
//
//   <ExpandBody><div v-if="open" class="body">…</div></ExpandBody>
//
// A table row works too, when each of its cells holds one block and nothing else:
//
//   <ExpandBody><tr v-if="open"><td colspan="4"><div>…</div></td></tr></ExpandBody>
//
// `instant` is for a change nobody asked for with a click (a preference read when the page
// opens): the body then comes or goes at once. Transition's own events (@after-leave) pass.
import { expandEnter, expandLeave } from '~/composables/expandMotion'

const props = defineProps<{ instant?: boolean }>()
const enter = (el: Element, done: () => void) => (props.instant ? done() : expandEnter(el, done))
const leave = (el: Element, done: () => void) => (props.instant ? done() : expandLeave(el, done))
</script>

<template>
  <Transition :css="false" @enter="enter" @leave="leave"><slot /></Transition>
</template>
