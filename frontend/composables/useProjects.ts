// Control plane: progetti/cartelle, permessi, utenti, gruppi. Via gateway autenticato.

export interface Project {
  id: number
  name: string
  description: string
  parent_id: number | null
  owner_id: number | null
}

export interface Permission {
  id: number
  project_id: number
  user_id: number | null
  group_id: number | null
  capability: string
}

export interface UserOut {
  id: number
  email: string
  full_name: string
  is_active: boolean
  is_superuser: boolean
}

export interface GroupOut {
  id: number
  name: string
  description: string
}

export const CAPABILITIES = ['view', 'run', 'edit', 'connect', 'manage'] as const

export function useProjects() {
  const { apiFetch } = useApiClient()

  return {
    // progetti
    list: () => apiFetch<Project[]>('/projects'),
    create: (body: { name: string; description?: string; parent_id?: number | null }) =>
      apiFetch<Project>('/projects', { method: 'POST', body }),
    update: (id: number, body: Partial<Pick<Project, 'name' | 'description' | 'parent_id'>>) =>
      apiFetch<Project>(`/projects/${id}`, { method: 'PATCH', body }),
    remove: (id: number) => apiFetch<void>(`/projects/${id}`, { method: 'DELETE' }),

    // permessi
    permissions: (id: number) => apiFetch<Permission[]>(`/projects/${id}/permissions`),
    grant: (id: number, body: { capability: string; user_id?: number; group_id?: number }) =>
      apiFetch<Permission>(`/projects/${id}/permissions`, { method: 'POST', body }),
    revoke: (permId: number) => apiFetch<void>(`/permissions/${permId}`, { method: 'DELETE' }),

    // utenti / gruppi (per popolare i selettori dei permessi e l'admin)
    users: () => apiFetch<UserOut[]>('/users'),
    createUser: (body: { email: string; password: string; full_name?: string; is_superuser?: boolean }) =>
      apiFetch<UserOut>('/users', { method: 'POST', body }),
    deleteUser: (userId: number) => apiFetch<void>(`/users/${userId}`, { method: 'DELETE' }),
    groups: () => apiFetch<GroupOut[]>('/groups'),
    createGroup: (body: { name: string; description?: string }) =>
      apiFetch<GroupOut>('/groups', { method: 'POST', body }),
    addToGroup: (userId: number, groupId: number) =>
      apiFetch<void>(`/users/${userId}/groups/${groupId}`, { method: 'PUT' }),
  }
}
