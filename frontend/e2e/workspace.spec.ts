import { expect, test } from '@playwright/test'
import { execFileSync } from 'node:child_process'

test('create a project, edit real files and open every production view', async ({
  page,
  request,
}) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByText('MuWeave', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '＋ 新建短剧项目', exact: true }).click()
  await page.getByRole('dialog').getByRole('textbox').fill(`浏览器验收-${Date.now()}`)
  await page.getByRole('dialog').getByRole('button', { name: '创建项目', exact: true }).click()
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await expect(page.locator('.file-state')).toContainText('episodes/01.md')
  const input = page.getByRole('textbox', { name: 'Editor content', exact: true })
  await input.focus()
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.insertText('# 第一集\n\n林舟推门进入。')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.locator('.file-state')).toContainText('已与本地文件同步')
  const projects = await (await request.get('/api/projects')).json()
  const pid = projects.at(-1).id
  const document = await (
    await request.get(`/api/projects/${pid}/file?path=episodes%2F01.md`)
  ).json()
  expect(document.content).toContain('林舟推门进入')
  await input.focus()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.insertText('\n保存请求中的正文')
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/projects/*/changes', async (route) => {
    await gate
    await route.continue()
  })
  const pending = page.waitForRequest(
    (request) => request.url().endsWith('/changes') && request.method() === 'POST',
  )
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await pending
  await input.focus()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.insertText('\n请求期间继续写作')
  release()
  await expect(page.getByRole('button', { name: '保存', exact: true })).not.toHaveClass(
    /is-loading/,
  )
  await expect(page.locator('.file-state')).toContainText('未保存')
  await expect(page.locator('.view-lines')).toContainText('请求期间继续写作')
  await page.unroute('**/api/projects/*/changes')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.locator('.file-state')).toContainText('已与本地文件同步')
  for (const name of ['素材', '分镜', '生成', '剪辑', '成片', '设置']) {
    await page.locator('nav button').filter({ hasText: name }).click()
    await expect(page.locator('h1')).toHaveText(name)
  }
  await expect(page.getByText('供应商连接', { exact: true })).toBeVisible()
  await page.screenshot({ path: 'test-results/settings.png', fullPage: true })
  expect(errors).toEqual([])
})

test('two reviewed clips play continuously and timeline saves preserve new edits', async ({
  page,
  request,
}) => {
  const title = `连续预览-${Date.now()}`
  const project = await (await request.post('/api/projects', { data: { title } })).json()
  const buffer = execFileSync('ffmpeg', [
    '-v',
    'error',
    '-f',
    'lavfi',
    '-i',
    'color=c=blue:s=160x288:r=24',
    '-f',
    'lavfi',
    '-i',
    'sine=frequency=440:sample_rate=48000',
    '-t',
    '1',
    '-c:v',
    'libx264',
    '-threads',
    '1',
    '-pix_fmt',
    'yuv420p',
    '-c:a',
    'aac',
    '-movflags',
    'frag_keyframe+empty_moov',
    '-f',
    'mp4',
    'pipe:1',
  ])
  const ids: string[] = []
  for (let i = 0; i < 2; i++) {
    const response = await request.post(`/api/projects/${project.id}/assets?kind=video`, {
      multipart: { file: { name: `take-${i}.mp4`, mimeType: 'video/mp4', buffer } },
    })
    expect(response.ok()).toBeTruthy()
    const asset = await response.json()
    await request.post(`/api/assets/${asset.id}/review`, { data: { review: 'accepted' } })
    ids.push(asset.id)
  }
  await request.post(`/api/projects/${project.id}/timeline`, {
    data: {
      expected_hash: null,
      timeline: {
        clips: ids.map((asset_id) => ({ asset_id, in_point: 0, out_point: 0.6, volume: 1 })),
        subtitles: [],
        subtitles_reviewed: true,
        width: 160,
        height: 288,
      },
    },
  })
  await page.goto('/')
  await expect(page.locator('.file-state')).toBeVisible()
  await page.locator('.sidebar .el-select').click()
  await page.getByRole('option', { name: title, exact: true }).click()
  await page.locator('nav button').filter({ hasText: '剪辑' }).click()
  await expect(page.locator('.timeline-preview video')).toBeVisible()
  await page.getByRole('button', { name: '播放', exact: true }).click()
  await expect
    .poll(async () =>
      page.locator('.timeline-preview video').evaluate((video) => ({
        source: (video as HTMLVideoElement).currentSrc,
        time: (video as HTMLVideoElement).currentTime,
      })),
    )
    .toMatchObject({ source: expect.stringContaining(ids[1]), time: expect.any(Number) })
  await expect
    .poll(async () =>
      page
        .locator('.timeline-preview video')
        .evaluate((video) => (video as HTMLVideoElement).currentTime),
    )
    .toBeGreaterThan(0.2)
  const original = (await (await request.get(`/api/projects/${project.id}/timeline`)).json())
    .timeline
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/projects/*/timeline', async (route) => {
    if (route.request().method() === 'POST') await gate
    await route.continue()
  })
  await page.getByRole('button', { name: '添加字幕', exact: true }).click()
  const subtitle = page.getByRole('textbox', { name: '实际对白', exact: true })
  await subtitle.fill('提交的对白')
  const pending = page.waitForRequest(
    (request) => request.url().endsWith('/timeline') && request.method() === 'POST',
  )
  await page.getByRole('button', { name: '保存时间线', exact: true }).click()
  await pending
  await subtitle.fill('请求期间继续修改对白')
  release()
  await expect(page.getByRole('button', { name: '保存时间线', exact: true })).not.toHaveClass(
    /is-loading/,
  )
  await expect(subtitle).toHaveValue('请求期间继续修改对白')
  await page.unroute('**/api/projects/*/timeline')
  expect(original.clips).toHaveLength(2)
})
