export interface Project {
  id: string
  title: string
  profile: string
}
export interface Connection {
  id: string
  name: string
  base_url: string
  has_key: boolean
  auth_header: string
  auth_prefix: string
  headers: Record<string, string>
}
export interface Model {
  id: string
  provider_id: string
  name: string
  model_id: string
  kind: 'text' | 'image' | 'video'
  protocol: string
  capabilities: Record<string, boolean>
  settings: Record<string, unknown>
  media: Record<string, unknown>
}
export interface Run {
  id: string
  status: string
  prompt: string
  output: string
  error?: string
  model_id: string
  recipe: string
  current_file?: string
}
export interface Asset {
  id: string
  kind: string
  label: string
  path: string
  review: string
  shot_id?: string
  prompt?: string
  probe: {
    format: { duration?: string }
    streams: { codec_type: string; width?: number; height?: number }[]
  }
}
export interface Job {
  id: string
  status: string
  prompt: string
  remote_id?: string
  error?: string
  asset_id?: string
}
export interface Render {
  id: string
  status: string
  error?: string
  duration: number
}
export interface Clip {
  asset_id: string
  in_point: number
  out_point: number
  volume: number
}
export interface Subtitle {
  start: number
  end: number
  text: string
}
export interface Timeline {
  clips: Clip[]
  subtitles: Subtitle[]
  subtitles_reviewed: boolean
  width: number
  height: number
  crf: number
}
export interface History {
  id: string
  operation_id: string
  status: string
  changes: { path: string; before: string | null; after: string | null }[]
}
