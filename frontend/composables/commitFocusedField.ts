import { nextTick } from 'vue'

/**
 * Commits the field that still has the focus, then waits for the panel to take it in.
 *
 * The node panels save a field on `change`, which comes with the blur: so that a command
 * can be typed over several lines, empty lines are dropped only once the field is left.
 * A «Run» pressed straight from such a field used to blur it on mousedown: the change
 * redrew the panel under the pointer and the click was lost. The button now keeps the
 * focus on mousedown (`@mousedown.prevent`) and calls this first, so the change is
 * committed before the run, and the run starts at the first click.
 *
 * Only a text field is blurred: a keyboard user on the button itself keeps the focus.
 */
export async function commitFocusedField(): Promise<void> {
  const el = document.activeElement
  if (el instanceof HTMLElement && el.matches('input, textarea, select')) {
    el.blur()
    await nextTick()
  }
}
