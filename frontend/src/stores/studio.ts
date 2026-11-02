import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../api'
import type { Asset, Connection, Job, Model, Project, Render, Run } from '../types'

export const useStudio = defineStore('studio', () => {
  const projects = ref<Project[]>([])
  const projectId = ref('')
  const connections = ref<Connection[]>([])
  const models = ref<Model[]>([])
  const files = ref<string[]>([])
  const assets = ref<Asset[]>([])
  const jobs = ref<Job[]>([])
  const runs = ref<Run[]>([])
  const renders = ref<Render[]>([])
  async function load() {
    const result = await Promise.all([api<Project[]>('/projects'), api<Connection[]>('/providers'), api<Model[]>('/models')])
    ;[projects.value, connections.value, models.value] = result
  }
  async function refresh() {
    const pid = projectId.value
    if (!pid) return
    const data = await Promise.all([api<string[]>(`/projects/${pid}/files`), api<Asset[]>(`/projects/${pid}/assets`), api<Job[]>(`/projects/${pid}/jobs`), api<Run[]>(`/projects/${pid}/runs`), api<Render[]>(`/projects/${pid}/renders`)])
    if (pid !== projectId.value) return
    ;[files.value, assets.value, jobs.value, runs.value, renders.value] = data
  }
  return { projects, projectId, connections, models, files, assets, jobs, runs, renders, load, refresh }
})
