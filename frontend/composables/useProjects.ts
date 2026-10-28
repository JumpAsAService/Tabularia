// Control plane: projects/folders, permissions, users, groups. Through the authenticated gateway.

export interface Project {
  id: number
  name: string
  description: string
  parent_id: number | null
  owner_id: number | null
  // the person whose personal folder this is; null for every other folder
  personal_of?: number | null
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
  created_at: string | null
  last_seen_at: string | null // last authenticated activity; null = never signed in
  sso_only: boolean // no local password: signs in from the IdP only
  groups: string[]
  // is_superuser is the PERSONAL flag; here the administrators' groups that make them admin anyway
  admin_groups: string[]
  // observer: reads the administration panels, writes nothing. The same pair: personal
  // flag + groups granting it
  is_observer: boolean
  observer_groups: string[]
  // service account: an integration, it signs in with an API token only
  is_service?: boolean
}

export interface GroupOut {
  id: number
  name: string
  description: string
  created_at: string | null
  member_count: number
  is_admin: boolean // the members are administrators
  is_observer: boolean // the members read the panels, without writing
}

export const CAPABILITIES = ['view', 'run', 'edit', 'connect', 'manage'] as const

export function useProjects() {
  const { apiFetch } = useApiClient()

  return {
    // projects
    list: () => apiFetch<Project[]>('/projects'),
    create: (body: { name: string; description?: string; parent_id?: number | null }) =>
      apiFetch<Project>('/projects', { method: 'POST', body }),
    update: (id: number, body: Partial<Pick<Project, 'name' | 'description' | 'parent_id'>>) =>
      apiFetch<Project>(`/projects/${id}`, { method: 'PATCH', body }),
    remove: (id: number) => apiFetch<void>(`/projects/${id}`, { method: 'DELETE' }),

    // permissions
    permissions: (id: number) => apiFetch<Permission[]>(`/projects/${id}/permissions`),
    grant: (id: number, body: { capability: string; user_id?: number; group_id?: number }) =>
      apiFetch<Permission>(`/projects/${id}/permissions`, { method: 'POST', body }),
    revoke: (permId: number) => apiFetch<void>(`/permissions/${permId}`, { method: 'DELETE' }),

    // users / groups (to fill the permissions' selectors and the admin)
    users: () => apiFetch<UserOut[]>('/users'),
    createUser: (body: {
      email: string
      password: string
      full_name?: string
      is_superuser?: boolean
      is_observer?: boolean
    }) =>
      apiFetch<UserOut>('/users', { method: 'POST', body }),
    deleteUser: (userId: number) => apiFetch<void>(`/users/${userId}`, { method: 'DELETE' }),
    groups: () => apiFetch<GroupOut[]>('/groups'),
    createGroup: (body: { name: string; description?: string }) =>
      apiFetch<GroupOut>('/groups', { method: 'POST', body }),
    addToGroup: (userId: number, groupId: number) =>
      apiFetch<void>(`/users/${userId}/groups/${groupId}`, { method: 'PUT' }),
    removeFromGroup: (userId: number, groupId: number) =>
      apiFetch<void>(`/users/${userId}/groups/${groupId}`, { method: 'DELETE' }),
    updateUser: (
      userId: number,
      body: Partial<{
        full_name: string
        password: string
        is_active: boolean
        is_superuser: boolean
        is_observer: boolean
      }>,
    ) => apiFetch<UserOut>(`/users/${userId}`, { method: 'PATCH', body }),
    updateGroup: (groupId: number, body: Partial<{ description: string; is_admin: boolean; is_observer: boolean }>) =>
      apiFetch<GroupOut>(`/groups/${groupId}`, { method: 'PATCH', body }),
    deleteGroup: (groupId: number) => apiFetch<void>(`/groups/${groupId}`, { method: 'DELETE' }),
  }
}
