// Where Explore's «New» creates a flow, a report, a script…: in the open folder; at the top
// level, where nothing but folders can be, in the person's own folder. Before, the menu
// offered only «New folder» there (to an administrator; to anyone else no «New» at all),
// and a first flow, made from where one lands after signing in, seemed impossible.
import type { Project } from './useProjects'

export function personalFolderOf(projects: Project[], userId: number | null | undefined): Project | null {
  if (userId == null) return null
  return projects.find((p) => p.parent_id == null && p.personal_of === userId) ?? null
}

export function creationFolder(currentId: number | null, projects: Project[], userId: number | null | undefined): number | null {
  return currentId ?? personalFolderOf(projects, userId)?.id ?? null
}
