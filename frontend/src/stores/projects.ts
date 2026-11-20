import { defineStore } from 'pinia'
import { ref } from 'vue'

import { projectsApi } from '@/api/projects'
import { toUserMessage } from '@/errors/messages'
import { useAuthStore } from '@/stores/auth'
import type { Project } from '@/types/api'

/** The project list of the current organization. */
export const useProjectsStore = defineStore('projects', () => {
  const auth = useAuthStore()
  const items = ref<Project[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const loadedFor = ref<string | null>(null)

  async function load(): Promise<void> {
    const org = auth.organizationId
    if (!org) return
    loading.value = true
    error.value = null
    try {
      const projects = await projectsApi.list(org)
      // Ignore the answer if the user switched organization while it was in flight.
      if (auth.organizationId === org) {
        items.value = projects
        loadedFor.value = org
      }
    } catch (cause) {
      error.value = toUserMessage(cause)
    } finally {
      loading.value = false
    }
  }

  async function create(input: { name: string; description?: string | null }): Promise<Project> {
    const project = await projectsApi.create(auth.organizationId ?? '', input)
    items.value = [...items.value, project].sort((a, b) => a.name.localeCompare(b.name))
    return project
  }

  /** Replace or drop one entry after it changed elsewhere. */
  function upsert(project: Project): void {
    const others = items.value.filter((item) => item.id !== project.id)
    items.value = [...others, project].sort((a, b) => a.name.localeCompare(b.name))
  }

  function remove(id: string): void {
    items.value = items.value.filter((item) => item.id !== id)
  }

  return { items, loading, error, loadedFor, load, create, upsert, remove }
})
