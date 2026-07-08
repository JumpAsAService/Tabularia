<script setup lang="ts">
import { onMounted, computed } from 'vue'
import { LogOut, Table2 } from 'lucide-vue-next'

// Landing autenticata: header + browser dei progetti/cartelle (+ admin per superuser).
const { user, fetchMe, logout } = useAuth()

const isSuper = computed(() => !!user.value?.is_superuser)

onMounted(async () => {
  if (!user.value) await fetchMe()
})
</script>

<template>
  <div class="home">
    <header class="topbar">
      <span class="brand"><span class="brand-mark"><Table2 :size="15" /></span> Tabularia</span>
      <span class="spacer" />
      <span v-if="user" class="who muted">
        {{ user.email }}<template v-if="isSuper"> · admin</template>
      </span>
      <button @click="logout"><LogOut :size="14" /> Esci</button>
    </header>

    <main class="content">
      <ProjectBrowser />
      <AdminPanel v-if="isSuper" />
    </main>
  </div>
</template>

<style scoped>
.home { min-height: 100vh; display: flex; flex-direction: column; }
.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 18px;
  border-bottom: 1px solid var(--border-soft);
  background: rgba(20, 25, 38, 0.92);
  backdrop-filter: blur(8px);
}
.brand {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0.4px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.brand-mark {
  width: 26px;
  height: 26px;
  border-radius: 7px;
  background: var(--grad-accent);
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.spacer { flex: 1; }
.who { font-size: 13px; }
.content { padding: 18px; max-width: 1100px; width: 100%; margin: 0 auto; }
</style>
