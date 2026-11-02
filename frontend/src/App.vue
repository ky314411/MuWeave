<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from './api'
import LocalTranscription from './components/LocalTranscription.vue'
import TimelinePreview from './components/TimelinePreview.vue'
import { useStudio } from './stores/studio'
import type { Asset, Connection, History, Model, Run, Timeline } from './types'

const CodeEditor = defineAsyncComponent(() => import('./components/CodeEditor.vue'))
const studio = useStudio()
const hasProject = computed(() => Boolean(studio.projectId))
const tab = ref('创作')
const tabs = ['创作', '素材', '分镜', '生成', '剪辑', '成片', '设置']
const busy = ref(false)
const health = ref({ ffmpeg: false, ffprobe: false })
const projectDialog = ref(false)
const title = ref('')
const profile = ref('general')
const file = ref('')
const content = ref('')
const saved = ref('')
const fileHash = ref<string | null>(null)
const dirty = computed(() => content.value !== saved.value)
const streamedOutput = ref('')
const prompt = ref('')
const textModel = ref('')
const recipe = ref('write')
const selectedRun = ref('')
const events = ref<{ seq: number; type: string; name?: string; node?: string }[]>([])
const activeRun = computed(
  () => studio.runs.find((run) => run.id === selectedRun.value) ?? studio.runs.at(-1),
)
const history = ref<History[]>([])
const historyDialog = ref(false)
const modelForm = ref({
  name: '',
  provider_id: '',
  model_id: '',
  kind: 'text' as Model['kind'],
  protocol: 'openai-compatible',
  tools: true,
  streaming: false,
  native_audio: false,
  reference_images: false,
})
const connectionForm = ref({
  id: '',
  name: '',
  base_url: '',
  api_key: '',
  auth_header: 'Authorization',
  auth_prefix: 'Bearer ',
})
const selectedConnection = ref('')
const selectedModel = ref('')
const mediaConfig = ref('{}')
const modelSettings = ref('{}')
const presets = ref<Record<string, Record<string, unknown>>>({})
const generateModel = ref('')
const generatePrompt = ref('')
const shotId = ref('')
const references = ref<string[]>([])
const parameters = ref('{}')
const upload = ref<HTMLInputElement>()
const uploadKind = ref('image')
const timeline = ref<Timeline>({
  clips: [],
  subtitles: [],
  subtitles_reviewed: false,
  width: 720,
  height: 1280,
  crf: 18,
})
const timelineHash = ref<string | null>(null)
const timelineDirty = ref(false)
let stream: EventSource | undefined
let timer: ReturnType<typeof setInterval> | undefined
let refreshing = false

async function action(callback: () => Promise<void>) {
  busy.value = true
  try {
    await callback()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close')
      ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    busy.value = false
  }
}
async function confirmDiscard() {
  if (dirty.value || timelineDirty.value)
    await ElMessageBox.confirm('有尚未保存的编辑，确定离开并放弃这些修改？', '未保存的修改', {
      type: 'warning',
    })
}
async function chooseProject(pid: string) {
  await action(async () => {
    await confirmDiscard()
    studio.projectId = pid
    file.value = ''
    content.value = ''
    saved.value = ''
    timelineDirty.value = false
    stream?.close()
    events.value = []
    selectedRun.value = ''
    await studio.refresh()
    await loadTimeline()
    if (studio.files.includes('episodes/01.md')) await openFile('episodes/01.md', false)
  })
}
async function newProject() {
  await action(async () => {
    await confirmDiscard()
    const project = await api<{ id: string }>('/projects', {
      title: title.value,
      profile: profile.value,
    })
    await studio.load()
    projectDialog.value = false
    content.value = saved.value
    timelineDirty.value = false
    await chooseProject(project.id)
  })
}
async function openFile(path: string, check = true) {
  if (check && dirty.value) await ElMessageBox.confirm('放弃当前文件尚未保存的修改？', '切换文件')
  const document = await api<{ content: string; hash: string }>(
    `/projects/${studio.projectId}/file?path=${encodeURIComponent(path)}`,
  )
  file.value = path
  content.value = document.content
  saved.value = document.content
  fileHash.value = document.hash
}
async function saveFile() {
  await action(async () => {
    const pid = studio.projectId,
      path = file.value,
      submitted = content.value
    await api(`/projects/${pid}/changes`, {
      operation_id: crypto.randomUUID(),
      changes: [{ path, content: submitted, expected_hash: fileHash.value }],
    })
    const document = await api<{ content: string; hash: string }>(
      `/projects/${pid}/file?path=${encodeURIComponent(path)}`,
    )
    if (studio.projectId === pid && file.value === path) {
      saved.value = submitted
      fileHash.value = document.content === submitted ? document.hash : fileHash.value
    }
    await studio.refresh()
    ElMessage.success('提交时的文件版本已保存')
  })
}
async function createFile() {
  await action(async () => {
    await confirmDiscard()
    const { value } = await ElMessageBox.prompt('输入项目内路径，例如 episodes/02.md', '新建文件')
    await api(`/projects/${studio.projectId}/changes`, {
      operation_id: crypto.randomUUID(),
      changes: [{ path: value, content: '', expected_hash: null }],
    })
    await studio.refresh()
    await openFile(value, false)
  })
}
async function showHistory() {
  await action(async () => {
    history.value = (await api<History[]>(`/projects/${studio.projectId}/history`)).reverse()
    historyDialog.value = true
  })
}
async function undo(item: History) {
  await action(async () => {
    if (dirty.value) throw new Error('请先保存当前文件，再撤销历史修改')
    await ElMessageBox.confirm(
      '恢复该修改组之前的内容？当前版本有后续修改时会拒绝覆盖。',
      '撤销修改组',
    )
    await api(`/projects/${studio.projectId}/history/${item.id}/undo`, {})
    await studio.refresh()
    historyDialog.value = false
    if (file.value) await openFile(file.value, false)
  })
}
function observe(run: Run) {
  selectedRun.value = run.id
  events.value = []
  streamedOutput.value = ''
  stream?.close()
  stream = new EventSource(`/api/events/${run.id}`)
  stream.onmessage = (event) => {
    const data = JSON.parse(event.data)
    if (data.type === 'text_delta') streamedOutput.value += data.text
    events.value = [...events.value, data].slice(-80)
    if (['completed', 'failed', 'cancelled'].includes(data.type)) {
      stream?.close()
      void studio.refresh()
    }
  }
}
async function startRun(parent?: Run) {
  await action(async () => {
    if (dirty.value) throw new Error('请先保存当前文件，再让模型读取项目')
    const run = await api<Run>(`/projects/${studio.projectId}/runs`, {
      model_id: parent?.model_id ?? textModel.value,
      prompt: parent ? '继续上一次任务，先核对中断期间的文件和工具状态。' : prompt.value,
      recipe: parent?.recipe ?? recipe.value,
      current_file: file.value || undefined,
      parent_id: parent?.id,
    })
    await studio.refresh()
    observe(run)
  })
}
function editConnection() {
  const connection = studio.connections.find((item) => item.id === selectedConnection.value)
  connectionForm.value = connection
    ? { ...connection, api_key: '' }
    : {
        id: '',
        name: '',
        base_url: '',
        api_key: '',
        auth_header: 'Authorization',
        auth_prefix: 'Bearer ',
      }
}
async function saveConnection() {
  await action(async () => {
    const body = { ...connectionForm.value }
    const result = await api<Connection>('/providers', {
      ...body,
      id: body.id || undefined,
      api_key: body.api_key || undefined,
    })
    await studio.load()
    selectedConnection.value = result.id
    editConnection()
    modelForm.value.provider_id = result.id
    ElMessage.success('连接已保存，密钥保存在系统凭据库')
  })
}
function editModel() {
  const model = studio.models.find((item) => item.id === selectedModel.value)
  if (!model) return
  modelForm.value = {
    ...model,
    tools: !!model.capabilities.tools,
    streaming: !!model.capabilities.streaming,
    native_audio: !!model.capabilities.native_audio,
    reference_images: !!model.capabilities.reference_images,
  }
  mediaConfig.value = JSON.stringify(model.media, null, 2)
  modelSettings.value = JSON.stringify(model.settings, null, 2)
}
async function saveModel() {
  await action(async () => {
    const form = modelForm.value
    const result = await api<Model>('/models', {
      id: selectedModel.value || undefined,
      provider_id: form.provider_id,
      name: form.name,
      model_id: form.model_id,
      kind: form.kind,
      protocol: form.kind === 'text' ? form.protocol : 'http-template',
      capabilities: {
        tools: form.tools,
        streaming: form.streaming,
        native_audio: form.native_audio,
        reference_images: form.reference_images,
      },
      settings: JSON.parse(modelSettings.value),
      media: JSON.parse(mediaConfig.value),
    })
    await studio.load()
    selectedModel.value = result.id
    ElMessage.success('模型配置已保存；能力需实际测试确认')
  })
}
async function testModel(model: Model) {
  await action(async () => {
    const result = await api<{ passed: boolean; error?: string; output?: string }>(
      `/models/${model.id}/test`,
      {},
    )
    if (!result.passed) throw new Error(result.error ?? '模型没有完成工具调用测试')
    ElMessage.success('模型连接与声明的工具调用测试通过')
  })
}
async function uploadFile(event: Event) {
  const selected = (event.target as HTMLInputElement).files?.[0]
  if (!selected) return
  await action(async () => {
    const form = new FormData()
    form.append('file', selected)
    await api(`/projects/${studio.projectId}/assets?kind=${uploadKind.value}`, form)
    await studio.refresh()
    ElMessage.success('素材已检查并导入，请预览后决定是否选用')
  })
  ;(event.target as HTMLInputElement).value = ''
}
async function review(asset: Asset, value: string) {
  await action(async () => {
    await api(`/assets/${asset.id}/review`, { review: value })
    await studio.refresh()
  })
}
async function generate() {
  await action(async () => {
    if (dirty.value) throw new Error('请先保存相关剧本，确保生成依赖正确的文件版本')
    await api(`/projects/${studio.projectId}/jobs`, {
      model_id: generateModel.value,
      prompt: generatePrompt.value,
      shot_id: shotId.value,
      reference_ids: references.value,
      source_paths: file.value ? [file.value] : [],
      parameters: JSON.parse(parameters.value),
    })
    await studio.refresh()
    ElMessage.success('已提交生成任务')
  })
}
async function attach(jobId: string) {
  await action(async () => {
    const { value } = await ElMessageBox.prompt(
      '先在供应商处核对任务，然后填写已有任务 ID；此操作不会重新提交生成。',
      '续接远端任务',
    )
    await api(`/jobs/${jobId}/attach`, { remote_id: value })
    await studio.refresh()
  })
}
async function loadTimeline() {
  const pid = studio.projectId
  const data = await api<{ timeline: Timeline; hash: string | null }>(`/projects/${pid}/timeline`)
  if (pid !== studio.projectId) return
  if (timelineDirty.value) {
    ElMessage.warning('本地时间线还有编辑，已保留；请先处理版本差异再载入识别结果')
    return
  }
  timeline.value = data.timeline
  timelineHash.value = data.hash
  timelineDirty.value = false
}
function markTimeline(clipsChanged = false) {
  timelineDirty.value = true
  if (clipsChanged) timeline.value.subtitles_reviewed = false
}
function removeClip(index: number) {
  timeline.value.clips.splice(index, 1)
  markTimeline(true)
}
function addSubtitle() {
  timeline.value.subtitles.push({ start: 0, end: 1, text: '' })
  markTimeline()
}
function removeSubtitle(index: number) {
  timeline.value.subtitles.splice(index, 1)
  markTimeline()
}
function addClip(asset: Asset) {
  timeline.value.clips.push({
    asset_id: asset.id,
    in_point: 0,
    out_point: Number(asset.probe.format.duration ?? 1),
    volume: 1,
  })
  markTimeline(true)
  tab.value = '剪辑'
}
function moveClip(index: number, delta: number) {
  const target = index + delta
  if (target < 0 || target >= timeline.value.clips.length) return
  const item = timeline.value.clips.splice(index, 1)[0]!
  timeline.value.clips.splice(target, 0, item)
  markTimeline(true)
}
async function saveTimeline() {
  const pid = studio.projectId
  const submitted = JSON.stringify(timeline.value)
  const result = await api<{ timeline: Timeline; hash: string }>(`/projects/${pid}/timeline`, {
    timeline: JSON.parse(submitted),
    expected_hash: timelineHash.value,
  })
  if (pid !== studio.projectId) return
  timelineHash.value = result.hash
  if (JSON.stringify(timeline.value) === submitted) {
    timeline.value = result.timeline
    timelineDirty.value = false
  }
}
async function exportVideo() {
  await action(async () => {
    if (timelineDirty.value) await saveTimeline()
    await api(`/projects/${studio.projectId}/renders`, {})
    tab.value = '成片'
    await studio.refresh()
  })
}
function mediaUrl(assetId: string) {
  return `/api/projects/${studio.projectId}/media/${assetId}`
}
function exportUrl(id: string, kind: string) {
  return `/api/projects/${studio.projectId}/exports/${id}/${kind}`
}
function assetLabel(assetId: string) {
  return studio.assets.find((asset) => asset.id === assetId)?.label ?? '素材不存在'
}
const duration = computed(() =>
  timeline.value.clips.reduce((sum, clip) => sum + Math.max(0, clip.out_point - clip.in_point), 0),
)
const statusName = (value: string) =>
  ({
    queued: '排队中',
    running: '执行中',
    submitting: '提交中',
    submitted: '已提交',
    downloading: '下载中',
    checking: '检查中',
    ready: '可用',
    failed: '失败',
    uncertain: '提交待核对',
    cancelled: '本地已停止',
    interrupted: '已中断',
    completed: '完成',
    accepted: '已接受',
    rejected: '已拒绝',
    unreviewed: '待审阅',
    needs_review: '需复核',
  })[value] ?? value
function preventClose(event: BeforeUnloadEvent) {
  if (dirty.value || timelineDirty.value) event.preventDefault()
}
onMounted(() => {
  window.addEventListener('beforeunload', preventClose)
  void action(async () => {
    await studio.load()
    health.value = await api('/health')
    presets.value = (await api<{ media: typeof presets.value }>('/presets')).media
    if (studio.projects[0]) await chooseProject(studio.projects[0].id)
  })
  timer = setInterval(async () => {
    if (
      refreshing ||
      !studio.projectId ||
      ![...studio.jobs, ...studio.runs, ...studio.renders].some((item) =>
        ['queued', 'running', 'submitting', 'submitted', 'downloading', 'checking'].includes(
          item.status,
        ),
      )
    )
      return
    refreshing = true
    try {
      await studio.refresh()
    } catch {
      /* Next refresh retries without discarding local edits. */
    } finally {
      refreshing = false
    }
  }, 2000)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  stream?.close()
  window.removeEventListener('beforeunload', preventClose)
})
</script>

<template>
  <div class="studio-shell">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-mark">幕</span>
        <div><strong>MuWeave</strong><small>幕织 · 短剧工作室</small></div>
      </div>
      <div class="sidebar-label">当前项目</div>
      <el-select :model-value="studio.projectId" placeholder="选择一个项目" @change="chooseProject"
        ><el-option
          v-for="project in studio.projects"
          :key="project.id"
          :label="project.title"
          :value="project.id"
      /></el-select>
      <el-button class="new-project" @click="projectDialog = true">＋ 新建短剧项目</el-button>
      <nav>
        <button
          v-for="item in tabs"
          :key="item"
          :class="{ active: tab === item }"
          @click="tab = item"
        >
          <span>{{
            { 创作: '✎', 素材: '▧', 分镜: '▦', 生成: '✦', 剪辑: '≋', 成片: '▷', 设置: '⚙' }[item]
          }}</span
          >{{ item }}
        </button>
      </nav>
      <div class="sidebar-footer">
        <span class="local-dot" />本地工作区<small>文件与素材由你掌握</small>
      </div>
    </aside>

    <main class="main-panel">
      <header class="page-header">
        <div>
          <div class="eyebrow">MUWEAVE WORKSPACE</div>
          <h1>{{ tab }}</h1>
          <p>
            {{
              {
                创作: '让故事有据可依，让每一次修改可以追溯。',
                素材: '固定角色与场景，预览并确认每一个版本。',
                分镜: '把故事拆成可拍摄、可生成的镜头。',
                生成: '选择自己的模型，保留生成记录和每一个 take。',
                剪辑: '根据实际画面和声音，安排镜头与字幕。',
                成片: '检查完成后，导出属于你的短剧。',
                设置: '连接云端或本机服务，为不同任务选择模型。',
              }[tab]
            }}
          </p>
        </div>
        <el-tag effect="plain" type="info">质量优先 · 本地运行</el-tag>
      </header>

      <section v-if="tab === '设置'" class="settings-grid">
        <article class="panel">
          <h2>供应商连接</h2>
          <el-select
            v-model="selectedConnection"
            clearable
            placeholder="新建连接或选择已有连接"
            @change="editConnection"
            ><el-option
              v-for="connection in studio.connections"
              :key="connection.id"
              :label="connection.name"
              :value="connection.id"
          /></el-select>
          <el-form label-position="top"
            ><el-form-item label="连接名称"
              ><el-input
                v-model="connectionForm.name"
                placeholder="例如：我的云端服务 / 本机 Ollama" /></el-form-item
            ><el-form-item label="服务地址 Base URL"
              ><el-input
                v-model="connectionForm.base_url"
                placeholder="http://localhost:11434/v1" /></el-form-item
            ><el-form-item label="API 密钥（编辑时留空保留原密钥）"
              ><el-input
                v-model="connectionForm.api_key"
                type="password"
                show-password
                autocomplete="new-password"
            /></el-form-item>
            <div class="two-columns">
              <el-form-item label="鉴权请求头"
                ><el-input v-model="connectionForm.auth_header" /></el-form-item
              ><el-form-item label="密钥前缀"
                ><el-input v-model="connectionForm.auth_prefix"
              /></el-form-item></div
          ></el-form>
          <el-button type="primary" :loading="busy" @click="saveConnection">保存连接</el-button>
          <p class="muted">密钥由系统凭据库保存，项目和 Git 仓库中不包含密钥。</p>
        </article>
        <article class="panel">
          <h2>模型与协议</h2>
          <el-select
            v-model="selectedModel"
            clearable
            placeholder="新建模型或选择已有模型"
            @change="editModel"
            ><el-option
              v-for="model in studio.models"
              :key="model.id"
              :label="model.name"
              :value="model.id"
          /></el-select>
          <el-form label-position="top"
            ><el-form-item label="所属连接"
              ><el-select v-model="modelForm.provider_id"
                ><el-option
                  v-for="connection in studio.connections"
                  :key="connection.id"
                  :label="connection.name"
                  :value="connection.id" /></el-select
            ></el-form-item>
            <div class="two-columns">
              <el-form-item label="显示名称"><el-input v-model="modelForm.name" /></el-form-item
              ><el-form-item label="模型 ID"
                ><el-input v-model="modelForm.model_id"
              /></el-form-item>
            </div>
            <el-form-item label="模型类型"
              ><el-radio-group v-model="modelForm.kind"
                ><el-radio-button value="text">文本</el-radio-button
                ><el-radio-button value="image">图片</el-radio-button
                ><el-radio-button value="video">视频</el-radio-button></el-radio-group
              ></el-form-item
            ><el-form-item v-if="modelForm.kind === 'text'" label="文本协议"
              ><el-select v-model="modelForm.protocol"
                ><el-option
                  v-for="protocol in ['openai-compatible', 'ollama', 'anthropic', 'google']"
                  :key="protocol"
                  :label="protocol"
                  :value="protocol" /></el-select></el-form-item
            ><el-form-item label="能力声明（保存后需要实际验证）"
              ><el-checkbox v-if="modelForm.kind === 'text'" v-model="modelForm.tools"
                >工具调用</el-checkbox
              ><el-checkbox v-if="modelForm.kind === 'text'" v-model="modelForm.streaming"
                >流式输出</el-checkbox
              ><el-checkbox v-if="modelForm.kind === 'video'" v-model="modelForm.native_audio"
                >原生音频</el-checkbox
              ><el-checkbox v-if="modelForm.kind !== 'text'" v-model="modelForm.reference_images"
                >参考图片</el-checkbox
              ></el-form-item
            ><el-form-item label="默认参数 JSON"
              ><el-input v-model="modelSettings" type="textarea" :rows="3"
            /></el-form-item>
            <template v-if="modelForm.kind !== 'text'"
              ><el-form-item label="媒体协议预设"
                ><el-select
                  placeholder="选择预设后可自由修改"
                  @change="(key: string) => (mediaConfig = JSON.stringify(presets[key], null, 2))"
                  ><el-option
                    v-for="(_, key) in presets"
                    :key="key"
                    :label="String(key)"
                    :value="key" /></el-select></el-form-item
              ><el-form-item label="HTTP 模板与响应字段 JSON"
                ><el-input v-model="mediaConfig" type="textarea" :rows="12"
              /></el-form-item>
              <p class="muted">
                变量：${model}、${prompt}、${references}、${content}、${request_id}
                和默认/任务参数。查询路径使用 ${task_id}。
              </p></template
            ></el-form
          >
          <el-button type="primary" :loading="busy" @click="saveModel">保存模型</el-button>
        </article>
        <article class="panel full-width">
          <h2>已配置模型</h2>
          <el-table :data="studio.models"
            ><el-table-column prop="name" label="名称" /><el-table-column
              prop="kind"
              label="类型"
            /><el-table-column prop="model_id" label="模型 ID" /><el-table-column label="实际测试"
              ><template #default="scope"
                ><el-button
                  v-if="scope.row.kind === 'text'"
                  :loading="busy"
                  @click="testModel(scope.row)"
                  >测试连接与工具</el-button
                ><span v-else class="muted">在生成页明确提交任务验收</span></template
              ></el-table-column
            ></el-table
          >
        </article>
      </section>

      <section v-else-if="!hasProject" class="panel welcome">
        <div class="welcome-symbol">幕</div>
        <h2>从一个故事开始</h2>
        <p>建立项目，连接模型，将剧本一步步织成画面。</p>
        <el-button type="primary" size="large" @click="projectDialog = true">新建短剧项目</el-button
        ><el-button @click="tab = '设置'">配置供应商</el-button>
      </section>

      <section v-else-if="tab === '创作' || tab === '分镜'" class="writing-grid">
        <article class="panel editor-panel">
          <div class="toolbar">
            <el-select
              :model-value="file"
              placeholder="选择文件"
              filterable
              @change="(path: string) => action(() => openFile(path))"
              ><el-option
                v-for="path in studio.files"
                :key="path"
                :label="path"
                :value="path" /></el-select
            ><el-button @click="createFile">新建</el-button
            ><el-button @click="showHistory">历史</el-button
            ><el-button type="primary" :disabled="!dirty" :loading="busy" @click="saveFile"
              >保存</el-button
            >
          </div>
          <div class="file-state">
            {{ file || '选择项目文件' }}<span>{{ dirty ? '未保存' : '已与本地文件同步' }}</span>
          </div>
          <CodeEditor v-if="file" v-model="content" :path="file" /><el-empty
            v-else
            description="选择或新建一个文件"
          />
        </article>
        <article class="panel agent-panel">
          <h2>创作助手</h2>
          <p class="muted">读取真实项目文件，通过工具执行并保留修改历史。</p>
          <el-select v-model="textModel" placeholder="选择支持工具调用的文本模型"
            ><el-option
              v-for="model in studio.models.filter(
                (item) => item.kind === 'text' && item.capabilities.tools,
              )"
              :key="model.id"
              :label="model.name"
              :value="model.id" /></el-select
          ><el-radio-group v-model="recipe" class="recipe-picker"
            ><el-radio-button value="write">写作</el-radio-button
            ><el-radio-button value="review">审稿</el-radio-button
            ><el-radio-button value="storyboard">分镜</el-radio-button></el-radio-group
          ><el-input
            v-model="prompt"
            type="textarea"
            :rows="5"
            placeholder="例如：读取世界观和人物，完善第一集的冲突与结尾钩子。"
          /><el-button
            type="primary"
            :disabled="
              !textModel || !prompt || ['running', 'queued'].includes(activeRun?.status ?? '')
            "
            :loading="busy"
            @click="startRun()"
            >开始执行</el-button
          >
          <div v-if="activeRun" class="run-output">
            <div class="toolbar">
              <el-tag>{{ statusName(activeRun.status) }}</el-tag
              ><el-button
                v-if="['running', 'queued'].includes(activeRun.status)"
                size="small"
                @click="
                  action(async () => {
                    await api(`/runs/${activeRun!.id}/cancel`, {})
                    await studio.refresh()
                  })
                "
                >停止</el-button
              ><el-button
                v-if="['interrupted', 'failed', 'cancelled'].includes(activeRun.status)"
                size="small"
                @click="startRun(activeRun)"
                >核对后继续</el-button
              >
            </div>
            <div class="event-list">
              <div v-for="event in events" :key="event.seq">
                {{ event.type }} {{ event.name || event.node || '' }}
              </div>
            </div>
            <pre>{{
              activeRun.output ||
              activeRun.error ||
              streamedOutput ||
              '正在处理，工具和步骤进度会显示在这里。'
            }}</pre>
            <el-button
              v-if="activeRun.status === 'completed' && file"
              @click="action(() => openFile(file))"
              >载入文件最新版本</el-button
            >
          </div>
          <el-collapse
            ><el-collapse-item title="历史执行记录"
              ><button
                v-for="run in [...studio.runs].reverse()"
                :key="run.id"
                class="run-item"
                @click="observe(run)"
              >
                {{ statusName(run.status) }} · {{ run.prompt.slice(0, 45) }}
              </button></el-collapse-item
            ></el-collapse
          >
        </article>
      </section>

      <section v-else-if="tab === '素材'">
        <div class="section-toolbar">
          <div>
            <strong>{{ studio.assets.length }}</strong> 个素材版本
          </div>
          <div>
            <el-select v-model="uploadKind" style="width: 120px"
              ><el-option label="参考图片" value="image" /><el-option
                label="有声视频"
                value="video" /></el-select
            ><el-button type="primary" @click="upload?.click()">上传本地素材</el-button
            ><input
              ref="upload"
              type="file"
              hidden
              :accept="uploadKind === 'image' ? 'image/*' : 'video/*'"
              @change="uploadFile"
            />
          </div>
        </div>
        <el-empty v-if="!studio.assets.length" description="上传参考图，或到生成页创建素材" />
        <div class="asset-grid">
          <article v-for="asset in studio.assets" :key="asset.id" class="panel asset-card">
            <img v-if="asset.kind === 'image'" :src="mediaUrl(asset.id)" :alt="asset.label" /><video
              v-else
              :src="mediaUrl(asset.id)"
              controls
              preload="metadata"
            />
            <div class="asset-details">
              <strong>{{ asset.shot_id || asset.label }}</strong
              ><el-tag :type="asset.review === 'accepted' ? 'success' : 'info'">{{
                statusName(asset.review)
              }}</el-tag>
              <p class="muted">{{ asset.prompt || '本地上传素材' }}</p>
              <div class="toolbar">
                <el-button size="small" @click="review(asset, 'accepted')">接受此版本</el-button
                ><el-button size="small" @click="review(asset, 'rejected')">拒绝</el-button
                ><el-button
                  v-if="asset.kind === 'video'"
                  size="small"
                  :disabled="asset.review !== 'accepted'"
                  @click="addClip(asset)"
                  >加入时间线</el-button
                >
              </div>
            </div>
          </article>
        </div>
      </section>

      <section v-else-if="tab === '生成'" class="production-grid">
        <article class="panel">
          <h2>生成镜头与参考图</h2>
          <el-form label-position="top"
            ><el-form-item label="图片或视频模型"
              ><el-select v-model="generateModel" placeholder="选择已配置模型"
                ><el-option
                  v-for="model in studio.models.filter((item) => item.kind !== 'text')"
                  :key="model.id"
                  :label="`${model.name} · ${model.kind}`"
                  :value="model.id" /></el-select></el-form-item
            ><el-form-item label="镜头 / 素材名称"
              ><el-input v-model="shotId" placeholder="例如 EP01-SH01 / 林舟定妆" /></el-form-item
            ><el-form-item label="提示词与声音要求"
              ><el-input
                v-model="generatePrompt"
                type="textarea"
                :rows="7"
                placeholder="描述画面、人物、动作、台词、情绪与环境声。" /></el-form-item
            ><el-form-item label="已接受的参考图"
              ><el-select v-model="references" multiple clearable
                ><el-option
                  v-for="asset in studio.assets.filter(
                    (item) => item.kind === 'image' && item.review === 'accepted',
                  )"
                  :key="asset.id"
                  :label="asset.label"
                  :value="asset.id" /></el-select></el-form-item
            ><el-form-item label="本次生成参数 JSON"
              ><el-input v-model="parameters" type="textarea" :rows="3" /></el-form-item
          ></el-form>
          <p class="muted">
            关联源文件：{{ file || '未选择' }}。提交会调用所选供应商，可能产生费用。
          </p>
          <el-button
            type="primary"
            :disabled="!generateModel || !generatePrompt"
            :loading="busy"
            @click="generate"
            >提交生成任务</el-button
          >
        </article>
        <article class="panel">
          <h2>持久化任务</h2>
          <el-empty v-if="!studio.jobs.length" description="尚无生成任务" />
          <div v-for="job in [...studio.jobs].reverse()" :key="job.id" class="job-row">
            <el-tag>{{ statusName(job.status) }}</el-tag>
            <p>{{ job.prompt }}</p>
            <small v-if="job.remote_id" class="muted">远端 ID：{{ job.remote_id }}</small>
            <p v-if="job.error" class="error-text">{{ job.error }}</p>
            <div class="toolbar">
              <el-button
                v-if="job.status === 'failed'"
                size="small"
                @click="
                  action(async () => {
                    await api(`/jobs/${job.id}/retry-download`, {})
                    await studio.refresh()
                  })
                "
                >重试已有结果下载</el-button
              ><el-button
                v-if="['uncertain', 'failed', 'cancelled'].includes(job.status)"
                size="small"
                @click="attach(job.id)"
                >核对并续接任务</el-button
              ><el-button
                v-if="
                  [
                    'queued',
                    'submitting',
                    'submitted',
                    'running',
                    'downloading',
                    'checking',
                  ].includes(job.status)
                "
                size="small"
                @click="
                  action(async () => {
                    await api(`/jobs/${job.id}/cancel`, {})
                    await studio.refresh()
                  })
                "
                >停止本地等待</el-button
              >
            </div>
          </div>
        </article>
      </section>

      <section v-else-if="tab === '剪辑'">
        <article class="panel">
          <div class="section-toolbar">
            <div>
              <h2>镜头时间线</h2>
              <span class="muted"
                >合计 {{ duration.toFixed(2) }} 秒 ·
                {{ timelineDirty ? '有未保存修改' : '已保存' }}</span
              >
            </div>
            <div>
              <el-button @click="tab = '素材'">选择素材</el-button
              ><el-button :loading="busy" @click="action(saveTimeline)">保存时间线</el-button>
            </div>
          </div>
          <el-table :data="timeline.clips" empty-text="先从素材页添加已接受的视频"
            ><el-table-column type="index" width="50" /><el-table-column label="素材"
              ><template #default="scope"
                >{{ assetLabel(scope.row.asset_id)
                }}<video
                  class="clip-preview"
                  :src="mediaUrl(scope.row.asset_id)"
                  controls
                  preload="none" /></template></el-table-column
            ><el-table-column label="入点（秒）" width="150"
              ><template #default="scope"
                ><el-input-number
                  v-model="scope.row.in_point"
                  :min="0"
                  :step="0.1"
                  controls-position="right"
                  @change="markTimeline(true)" /></template></el-table-column
            ><el-table-column label="出点（秒）" width="150"
              ><template #default="scope"
                ><el-input-number
                  v-model="scope.row.out_point"
                  :min="0.1"
                  :step="0.1"
                  controls-position="right"
                  @change="markTimeline(true)" /></template></el-table-column
            ><el-table-column label="音量" width="120"
              ><template #default="scope"
                ><el-input-number
                  v-model="scope.row.volume"
                  :min="0"
                  :max="3"
                  :step="0.1"
                  controls-position="right"
                  @change="markTimeline()" /></template></el-table-column
            ><el-table-column label="顺序" width="150"
              ><template #default="scope"
                ><el-button size="small" @click="moveClip(scope.$index, -1)">↑</el-button
                ><el-button size="small" @click="moveClip(scope.$index, 1)">↓</el-button
                ><el-button size="small" @click="removeClip(scope.$index)"
                  >移除</el-button
                ></template
              ></el-table-column
            ></el-table
          >
        </article>
        <TimelinePreview
          :project-id="studio.projectId"
          :clips="timeline.clips"
          :subtitles="timeline.subtitles"
        /><LocalTranscription
          :project-id="studio.projectId"
          :dirty="timelineDirty"
          @applied="loadTimeline"
        />
        <article class="panel subtitle-panel">
          <div class="section-toolbar">
            <h2>字幕与音轨核对</h2>
            <el-button @click="addSubtitle">添加字幕</el-button>
          </div>
          <p class="muted">时间为成片中的秒数。逐镜核对实际对白；更换镜头或入出点后需重新确认。</p>
          <div v-for="(subtitle, index) in timeline.subtitles" :key="index" class="subtitle-row">
            <el-input-number
              v-model="subtitle.start"
              :min="0"
              :step="0.1"
              @change="markTimeline()"
            /><span>→</span
            ><el-input-number
              v-model="subtitle.end"
              :min="0"
              :step="0.1"
              @change="markTimeline()"
            /><el-input
              v-model="subtitle.text"
              placeholder="实际对白"
              @input="markTimeline()"
            /><el-button @click="removeSubtitle(index)">移除</el-button>
          </div>
          <el-checkbox v-model="timeline.subtitles_reviewed" @change="markTimeline()"
            >已核对所选镜头的实际音轨与字幕（无对白时也需确认）</el-checkbox
          >
        </article>
        <article class="panel export-settings">
          <h2>成片设置</h2>
          <div class="toolbar">
            <label
              >宽度
              <el-input-number
                v-model="timeline.width"
                :min="160"
                :max="2160"
                :step="2"
                @change="markTimeline()" /></label
            ><label
              >高度
              <el-input-number
                v-model="timeline.height"
                :min="160"
                :max="3840"
                :step="2"
                @change="markTimeline()" /></label
            ><label
              >CRF
              <el-input-number
                v-model="timeline.crf"
                :min="12"
                :max="28"
                @change="markTimeline()" /></label
            ><el-button
              type="primary"
              :disabled="!timeline.clips.length || !health.ffmpeg"
              :loading="busy"
              @click="exportVideo"
              >检查并导出成片</el-button
            >
          </div>
          <p class="muted">
            CRF 越低画质越高、文件越大。导出会检查素材审阅状态、依赖版本、真实时长与字幕。
          </p>
        </article>
      </section>

      <section v-else-if="tab === '成片'">
        <el-alert
          v-if="!health.ffmpeg || !health.ffprobe"
          title="请先安装 FFmpeg 与 ffprobe，并确保终端能找到它们"
          type="warning"
          :closable="false"
        /><el-empty v-if="!studio.renders.length" description="在剪辑页检查并导出第一部成片" />
        <article
          v-for="render in [...studio.renders].reverse()"
          :key="render.id"
          class="panel render-card"
        >
          <div class="section-toolbar">
            <h2>{{ render.id.slice(0, 8) }} · {{ render.duration.toFixed(2) }} 秒</h2>
            <el-tag>{{ statusName(render.status) }}</el-tag>
          </div>
          <video
            v-if="render.status === 'ready'"
            :src="exportUrl(render.id, 'video')"
            controls
            preload="metadata"
          />
          <p v-if="render.error" class="error-text">{{ render.error }}</p>
          <div v-if="render.status === 'ready'" class="toolbar">
            <a :href="exportUrl(render.id, 'video')" download>下载 MP4</a
            ><a :href="exportUrl(render.id, 'subtitle')" download>下载字幕</a
            ><a :href="exportUrl(render.id, 'manifest')" download>下载制作记录</a>
          </div>
        </article>
      </section>
    </main>
  </div>

  <el-dialog v-model="projectDialog" title="新建短剧项目" width="440px"
    ><el-form label-position="top"
      ><el-form-item label="项目名称"
        ><el-input
          v-model="title"
          placeholder="为你的故事起个名字"
          @keyup.enter="newProject" /></el-form-item
      ><el-form-item label="创作配置"
        ><el-radio-group v-model="profile"
          ><el-radio value="general">通用短剧</el-radio
          ><el-radio value="rules">规则闯关</el-radio></el-radio-group
        ></el-form-item
      ></el-form
    ><template #footer
      ><el-button @click="projectDialog = false">取消</el-button
      ><el-button type="primary" :disabled="!title.trim()" :loading="busy" @click="newProject"
        >创建项目</el-button
      ></template
    ></el-dialog
  >
  <el-dialog v-model="historyDialog" title="修改历史与恢复" width="760px"
    ><article v-for="item in history" :key="item.id" class="history-row">
      <div class="toolbar">
        <el-tag>{{ item.status }}</el-tag
        ><code>{{ item.operation_id }}</code
        ><el-button
          v-if="item.status === 'committed' && item.operation_id !== 'initial'"
          size="small"
          @click="undo(item)"
          >成组撤销 / 恢复</el-button
        >
      </div>
      <el-collapse
        ><el-collapse-item v-for="change in item.changes" :key="change.path" :title="change.path"
          ><div class="two-columns">
            <div>
              <small>修改前</small>
              <pre>{{ change.before ?? '文件不存在' }}</pre>
            </div>
            <div>
              <small>修改后</small>
              <pre>{{ change.after ?? '已软删除，可恢复' }}</pre>
            </div>
          </div></el-collapse-item
        ></el-collapse
      >
    </article></el-dialog
  >
</template>
