<script setup lang="ts">
// A free note on the canvas: the user's annotation. It is NOT part of the flow and is
// IGNORED by the run (no handle, it connects to nothing). Resizable like the foreach
// container; the text is edited in place with a double click (or from the side panel).
import { ref, nextTick } from 'vue'
import { NodeResizer } from '@vue-flow/node-resizer'
import { useVueFlow } from '@vue-flow/core'
import { StickyNote } from 'lucide-vue-next'

const props = defineProps<{ id: string; data: any }>()
const { updateNodeData } = useVueFlow()

const editing = ref(false)
const draft = ref('')
const ta = ref<HTMLTextAreaElement | null>(null)

async function startEdit() {
  draft.value = props.data?.text ?? ''
  editing.value = true
  await nextTick()
  ta.value?.focus()
}
function commit() {
  editing.value = false
  updateNodeData(props.id, { text: draft.value })
}
</script>

<template>
  <div class="comment-box" :class="{ editing }">
    <NodeResizer
      :min-width="140"
      :min-height="70"
      color="#fbbf24"
      :line-style="{ borderWidth: '1px', borderColor: 'rgba(251, 191, 36, 0.4)' }"
    />
    <div class="comment-head"><StickyNote :size="12" /> {{ $t('commentNode.label') }}</div>
    <!-- the nodrag class keeps writing/selecting from moving the node -->
    <textarea
      v-if="editing"
      ref="ta"
      v-model="draft"
      class="comment-input nodrag"
      :placeholder="$t('commentNode.placeholder')"
      @blur="commit"
      @keydown.esc="commit"
    />
    <div v-else class="comment-text" :class="{ ph: !data?.text }" @dblclick="startEdit">
      {{ data?.text || $t('commentNode.emptyHint') }}
    </div>
  </div>
</template>

<style scoped>
.comment-box {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: rgba(251, 191, 36, 0.1);
  border: 1px solid rgba(251, 191, 36, 0.5);
  border-radius: 10px;
  box-shadow: var(--shadow-1);
  overflow: hidden;
}
.comment-head {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: color-mix(in srgb, #fbbf24 var(--enc-text-mix), var(--text));
  border-bottom: 1px solid rgba(251, 191, 36, 0.3);
  flex-shrink: 0;
}
.comment-text {
  flex: 1;
  padding: 8px 10px;
  font-size: 13px;
  line-height: 1.4;
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  overflow-y: auto;
  cursor: text;
}
.comment-text.ph { color: var(--muted); font-style: italic; }
.comment-input {
  flex: 1;
  width: 100%;
  padding: 8px 10px;
  background: transparent;
  border: none;
  outline: none;
  resize: none;
  font: inherit;
  font-size: 13px;
  line-height: 1.4;
  color: var(--text);
}
.comment-input::placeholder { color: var(--muted); }
</style>
