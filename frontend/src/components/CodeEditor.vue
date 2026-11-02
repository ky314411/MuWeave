<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api.js'
import 'monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution.js'
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'

const props = defineProps<{ modelValue: string; path: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const container = ref<HTMLElement>()
let editor: monaco.editor.IStandaloneCodeEditor | undefined
;(self as unknown as {MonacoEnvironment: {getWorker: () => Worker}}).MonacoEnvironment = { getWorker: () => new EditorWorker() }
onMounted(() => {
  editor = monaco.editor.create(container.value!, { value: props.modelValue, language: language(), editContext: false, automaticLayout: true, minimap: {enabled: false}, fontSize: 14, lineHeight: 25, wordWrap: 'on', scrollBeyondLastLine: false, padding: {top: 18} })
  editor.onDidChangeModelContent(() => emit('update:modelValue', editor!.getValue()))
})
function language() { return /\.ya?ml$/.test(props.path) ? 'yaml' : props.path.endsWith('.json') ? 'json' : 'markdown' }
watch(() => props.modelValue, value => { if (editor && editor.getValue() !== value) editor.setValue(value) })
watch(() => props.path, () => { if (editor?.getModel()) monaco.editor.setModelLanguage(editor.getModel()!, language()) })
onBeforeUnmount(() => { const model = editor?.getModel(); editor?.dispose(); model?.dispose() })
</script>
<template><div ref="container" class="code-editor" aria-label="项目文件编辑器" /></template>
