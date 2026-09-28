<script setup lang="ts">
// Amministrazione: utenti e gruppi. L'amministratore comanda, l'osservatore
// legge (i comandi li nasconde AdminPanel col suo `canWrite`).
import { computed, watchEffect } from 'vue'

const { user } = useAuth()
const router = useRouter()

// il gateway rifiuta comunque le API admin: qui solo UX (niente pagina vuota)
watchEffect(() => {
  if (user.value && !user.value.is_superuser && !user.value.is_observer) router.replace('/')
})

// Un OSSERVATORE apre la pagina in lettura; i comandi restano
// all'amministratore. `is_observer` da /auth/me è EFFETTIVO: tiene conto dei
// gruppi (vedi gateway/app/schemas/models.py, MeOut).
const canSee = computed(() => !!user.value?.is_superuser || !!user.value?.is_observer)
</script>

<template>
  <AppShell>
    <AdminPanel v-if="canSee" />
  </AppShell>
</template>
