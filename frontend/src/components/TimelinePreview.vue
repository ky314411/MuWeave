<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Clip, Subtitle } from '../types'

const props = defineProps<{ projectId: string; clips: Clip[]; subtitles: Subtitle[] }>()
const video = ref<HTMLVideoElement>()
const index = ref(0)
const time = ref(0)
const playing = ref(false)
let advancing = false
const clip = computed(() => props.clips[index.value])
const source = computed(() =>
  clip.value ? `/api/projects/${props.projectId}/media/${clip.value.asset_id}` : '',
)
const timelineTime = computed(
  () =>
    props.clips
      .slice(0, index.value)
      .reduce((sum, item) => sum + item.out_point - item.in_point, 0) +
    Math.max(0, time.value - (clip.value?.in_point ?? 0)),
)
const caption = computed(
  () =>
    props.subtitles.find(
      (item) => item.start <= timelineTime.value && item.end > timelineTime.value,
    )?.text ?? '',
)
function loaded() {
  advancing = false
  if (video.value && clip.value) {
    video.value.currentTime = clip.value.in_point
    video.value.volume = Math.min(1, clip.value.volume)
    if (playing.value)
      void video.value.play().catch(() => {
        playing.value = false
      })
  }
}
function next() {
  if (advancing) return
  if (index.value + 1 < props.clips.length) {
    advancing = true
    index.value++
  } else {
    video.value?.pause()
    playing.value = false
  }
}
function tick() {
  time.value = video.value?.currentTime ?? 0
  if (clip.value && time.value >= clip.value.out_point - 0.02) next()
}
function play() {
  playing.value = true
  if (video.value) {
    if (video.value.currentTime >= (clip.value?.out_point ?? 0))
      video.value.currentTime = clip.value?.in_point ?? 0
    void video.value.play()
  }
}
function resetPreview() {
  index.value = 0
  advancing = false
  loaded()
}
function paused(event: Event) {
  if (!advancing && event.currentTarget === video.value) playing.value = false
}
watch(
  () => JSON.stringify(props.clips),
  () => {
    index.value = 0
    playing.value = false
    advancing = false
    video.value?.pause()
  },
)
</script>
<template>
  <article v-if="clips.length" class="panel subtitle-panel">
    <div class="section-toolbar">
      <h2>连续预览</h2>
      <div>
        <span class="muted"
          >镜头 {{ index + 1 }} / {{ clips.length }} · {{ timelineTime.toFixed(2) }} 秒</span
        ><el-button @click="resetPreview">回到开头</el-button
        ><el-button type="primary" @click="play">播放</el-button>
      </div>
    </div>
    <div class="timeline-preview">
      <video
        ref="video"
        :key="`${index}-${source}`"
        :src="source"
        controls
        preload="metadata"
        @loadedmetadata="loaded"
        @timeupdate="tick"
        @ended="next"
        @play="playing = true"
        @pause="paused"
      />
      <div v-if="caption" class="preview-caption">{{ caption }}</div>
    </div>
  </article>
</template>
