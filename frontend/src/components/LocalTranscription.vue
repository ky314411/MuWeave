<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const props = defineProps<{ projectId: string; dirty: boolean }>()
const emit = defineEmits<{ applied: [] }>()
const modelPath = ref('')
const device = ref('cpu')
const busy = ref(false)
const records = ref<{ id: string; status: string; error?: string; segments?: unknown[] }[]>([])
let timer: ReturnType<typeof setInterval>
async function load() {
  records.value = await api(`/projects/${props.projectId}/transcriptions`)
}
async function start() {
  busy.value = true
  try {
    await api(`/projects/${props.projectId}/transcriptions`, {
      model_path: modelPath.value,
      device: device.value,
      language: 'zh',
    })
    await load()
  } catch (error) {
    ElMessage.error(String(error))
  } finally {
    busy.value = false
  }
}
async function apply(id: string) {
  if (props.dirty) {
    ElMessage.warning('请先保存时间线中的编辑，再应用识别结果')
    return
  }
  const pid = props.projectId
  try {
    await api(`/transcriptions/${id}/apply`, {})
    if (pid !== props.projectId || props.dirty) {
      ElMessage.warning('识别结果已保存，本地新编辑已保留；请核对时间线版本差异')
      return
    }
    emit('applied')
    ElMessage.success('已应用实际音轨字幕，请校对后确认')
  } catch (error) {
    ElMessage.error(String(error))
  }
}
onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (records.value.some((record) => ['queued', 'running'].includes(record.status)))
      void load().catch(() => {})
  }, 2000)
})
watch(
  () => props.projectId,
  () => {
    void load()
  },
)
onBeforeUnmount(() => clearInterval(timer))
</script>
<template>
  <article class="panel subtitle-panel">
    <h2>从实际音轨识别字幕</h2>
    <p class="muted">
      使用本地 faster-whisper
      模型识别所选镜头的声音。模型文件需要预先准备；识别结果可以编辑，确认后才能正式导出。
    </p>
    <div class="toolbar">
      <el-input
        v-model="modelPath"
        placeholder="本地已转换模型目录（包含 model.bin）"
        style="max-width: 420px"
      /><el-select v-model="device" style="width: 110px"
        ><el-option label="CPU" value="cpu" /><el-option label="CUDA" value="cuda" /></el-select
      ><el-button :loading="busy" :disabled="!modelPath || dirty" @click="start"
        >识别已保存时间线</el-button
      >
    </div>
    <div v-for="record in [...records].reverse()" :key="record.id" class="job-row">
      <el-tag>{{ record.status }}</el-tag
      ><span v-if="record.segments"> {{ record.segments.length }} 条字幕</span
      ><el-button
        v-if="record.status === 'ready'"
        size="small"
        :disabled="dirty"
        @click="apply(record.id)"
        >应用并人工复核</el-button
      >
      <p v-if="record.error" class="error-text">{{ record.error }}</p>
    </div>
  </article>
</template>
