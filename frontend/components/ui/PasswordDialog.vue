<script setup lang="ts">
// Setting a password, in two forms:
// - one's own (settings menu): the current password, then the new one twice.
//   The gateway checks the current one, closes the other sessions and the API
//   tokens, and slows down repeated wrong attempts like the sign-in;
// - another user's (administration → users): the new one twice, through
//   PATCH /users/{id}, which closes that user's sessions and tokens.
// An account that signs in through the identity provider (SSO) has no password
// here: the dialog says so instead of showing a form.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { KeyRound, LoaderCircle } from 'lucide-vue-next'
import Modal from '~/components/ui/Modal.vue'
import { errMessage, useApi } from '~/composables/useApi'
import { useAuth } from '~/composables/useAuth'
import { useProjects } from '~/composables/useProjects'

import { passwordProblem, usePasswordRules } from '~/composables/usePasswordRules'

const props = defineProps<{
  open: boolean
  // absent = one's own password; present = an administrator sets this user's
  target?: { id: number; email: string; sso_only?: boolean } | null
}>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'done'): void }>()

const { t } = useI18n()
const { user } = useAuth()
const api = useApi()
const projects = useProjects()
const toast = useToast()
// the one rule of the gateway (core/passwords.py): length, bytes, published passwords
const { rules, load: loadRules } = usePasswordRules()

const own = computed(() => !props.target)
const ssoOnly = computed(() => (own.value ? !!user.value?.sso_only : !!props.target?.sso_only))
const title = computed(() =>
  own.value ? t('password.titleOwn') : t('password.titleFor', { email: props.target?.email ?? '' }),
)

const current = ref('')
const next = ref('')
const again = ref('')
const busy = ref(false)
const error = ref('')
const tried = ref(false)

watch(() => props.open, (o) => {
  if (!o) return
  loadRules()
  current.value = next.value = again.value = error.value = ''
  tried.value = false
})

// an answer of the gateway is about what was sent: typing again clears it
watch([current, next, again], () => { error.value = '' })

// what is wrong with the form, before asking the gateway
const problem = computed(() => {
  if (own.value && !current.value) return t('password.currentRequired')
  const bad = passwordProblem(next.value, rules.value)
  if (bad) return t(bad.key, bad.values)
  if (next.value !== again.value) return t('password.mismatch')
  if (own.value && next.value === current.value) return t('password.sameAsCurrent')
  return ''
})

/** The gateway's answers, in the language of the interface. */
function message(e: any): string {
  const status = e?.status ?? e?.statusCode ?? e?.response?.status
  if (status === 400) return t('password.wrongCurrent')
  if (status === 422) return t('password.rules', { min: rules.value.min_length, max: rules.value.max_bytes })
  if (status === 409) return t('password.managedByIdp')
  if (status === 429) {
    const s = Number(e?.response?.headers?.get?.('Retry-After')) || 60
    return t('password.tooMany', { s })
  }
  if (status === 403) return own.value ? t('password.notAllowed') : errMessage(e)
  return errMessage(e)
}

async function save() {
  tried.value = true
  if (problem.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    if (own.value) {
      await api.changeMyPassword(current.value, next.value)
      toast.success(t('password.changedOwn'))
    } else {
      await projects.updateUser(props.target!.id, { password: next.value })
      toast.success(t('password.changedFor', { email: props.target!.email }))
    }
    emit('done')
    emit('close')
  } catch (e) {
    error.value = message(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Modal :open="open" :title="title" :width="440" @close="emit('close')">
    <p v-if="ssoOnly" class="pd-lead">{{ own ? $t('password.ssoOwn') : $t('password.ssoFor') }}</p>
    <form v-else id="password-form" class="pd-form" novalidate @submit.prevent="save">
      <p class="pd-lead muted">{{ own ? $t('password.leadOwn') : $t('password.leadFor') }}</p>
      <!-- the account name helps the browser's password manager file the new one -->
      <input type="email" class="pd-user" autocomplete="username" :value="own ? user?.email : target?.email" readonly tabindex="-1" aria-hidden="true" />
      <label v-if="own" class="pd-field">
        <span>{{ $t('password.current') }}</span>
        <input v-model="current" type="password" autocomplete="current-password" name="current-password" required />
      </label>
      <label class="pd-field">
        <span>{{ $t('password.new') }}</span>
        <input v-model="next" type="password" autocomplete="new-password" name="new-password" :minlength="rules.min_length" required />
      </label>
      <label class="pd-field">
        <span>{{ $t('password.repeat') }}</span>
        <input v-model="again" type="password" autocomplete="new-password" name="new-password-again" required />
      </label>
      <p class="pd-hint muted">{{ $t('password.hint', { n: rules.min_length }) }}</p>
      <p v-if="error || (tried && problem)" class="pd-error" role="alert">{{ error || problem }}</p>
    </form>

    <template #footer>
      <button type="button" @click="emit('close')">{{ ssoOnly ? $t('password.close') : $t('password.cancel') }}</button>
      <button v-if="!ssoOnly" type="submit" form="password-form" class="primary" :disabled="busy">
        <LoaderCircle v-if="busy" :size="14" class="spin" /><KeyRound v-else :size="14" />
        {{ own ? $t('password.submitOwn') : $t('password.submitFor') }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.pd-lead { margin: 0 0 4px; font-size: 13px; line-height: 1.5; }
.pd-form { display: flex; flex-direction: column; gap: 10px; }
.pd-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.pd-field input { width: 100%; }
.pd-user { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
.pd-hint { margin: 0; font-size: 12px; }
.pd-error { margin: 0; color: var(--danger); font-size: 13px; }
</style>
