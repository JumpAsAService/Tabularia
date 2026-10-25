<script setup lang="ts">
// Administration: users and groups. The administrator commands, the observer reads
// (AdminPanel hides the commands with its `canWrite`).
import { computed, watchEffect } from 'vue'

const { user } = useAuth()
const router = useRouter()

// the gateway refuses the admin APIs anyway: here only UX (no empty page)
watchEffect(() => {
  if (user.value && !user.value.is_superuser && !user.value.is_observer) router.replace('/')
})

// An OBSERVER opens the page for reading; the commands stay the administrator's.
// `is_observer` from /auth/me is EFFECTIVE: it accounts for the groups (see
// gateway/app/schemas/models.py, MeOut).
const canSee = computed(() => !!user.value?.is_superuser || !!user.value?.is_observer)
</script>

<template>
  <AppShell>
    <AdminPanel v-if="canSee" />
  </AppShell>
</template>
