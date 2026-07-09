<script setup lang="ts">
// Amministrazione (solo superuser): utenti e gruppi.
import { watchEffect } from 'vue'

const { user } = useAuth()
const router = useRouter()

// il gateway rifiuta comunque le API admin: qui solo UX (niente pagina vuota)
watchEffect(() => {
  if (user.value && !user.value.is_superuser) router.replace('/')
})
</script>

<template>
  <AppShell>
    <AdminPanel v-if="user?.is_superuser" />
  </AppShell>
</template>
