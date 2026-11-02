import { expect, test } from '@playwright/test'

test('create a project, edit real files and open every production view', async ({ page, request }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByText('MuWeave', {exact: true})).toBeVisible()
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
  const document = await (await request.get(`/api/projects/${pid}/file?path=episodes%2F01.md`)).json()
  expect(document.content).toContain('林舟推门进入')
  for (const name of ['素材','分镜','生成','剪辑','成片','设置']) {
    await page.locator('nav button').filter({hasText: name}).click()
    await expect(page.locator('h1')).toHaveText(name)
  }
  await expect(page.getByText('供应商连接', {exact:true})).toBeVisible()
  await page.screenshot({path:'test-results/settings.png', fullPage:true})
  expect(errors).toEqual([])
})
