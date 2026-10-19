/**
 * The mutz walkthrough, filmed: one officer takes the Kestrelyn one pager from paste to sent pitch.
 *
 *   pnpm record     (the app must run at DEMO_BASE on a fresh app DB; the gateway cache makes Apertus calls fast)
 *
 * Each roll(page, name, who) starts a clip; narration lines in voice/lines.json point at these names.
 * Clips are padded to their narration (voice/manifest.json), so run `pnpm voice` first.
 * Slow or flaky things (Mutz's reply, checking the remaining cards, building the PDF) happen off camera.
 */
import 'dotenv/config'
import { execFileSync } from 'node:child_process'
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { chromium, type Locator, type Page } from 'playwright'
import { DPR, SIZE, click, cut, finish, hold, moveTo, point, prepare, roll, sleep, type } from './film'

const BASE = process.env.DEMO_BASE ?? 'http://localhost:8100'
const ROOT = new URL('..', import.meta.url).pathname
const APP = join(ROOT, '..')
const ONE_PAGER = readFileSync(join(APP, 'needs/fixtures/demo_onepager.txt'), 'utf8').trim()

// --- pacing against the narration ---------------------------------------------------------------
const lines: { id: string; clip: string; offset?: number }[] = JSON.parse(readFileSync(join(ROOT, 'voice/lines.json'), 'utf8'))
const voice: Record<string, { seconds: number }> = JSON.parse(readFileSync(join(ROOT, 'voice/manifest.json'), 'utf8'))
let rolledAt = 0
let rolledName = ''

async function shoot(page: Page, name: string, who: string) {
  await roll(page, name, who)
  rolledAt = Date.now()
  rolledName = name
}

const elapsed = () => (Date.now() - rolledAt) / 1000

/** Moments inside a clip, in seconds from its start, for zooms in src/data.ts (capture/marks.json). */
const marks: Record<string, number> = {}
const mark = (label: string) => {
  marks[`${rolledName}:${label}`] = +elapsed().toFixed(2)
}

/** Seconds the current clip needs for its own lines plus a breath (`extra`). */
function needed(extra: number) {
  return lines
    .filter((l) => l.clip === rolledName)
    .reduce((a, l) => Math.max(a, (l.offset ?? 0.3) + (voice[l.id]?.seconds ?? 0) + extra), 0)
}

/** Hold until the clip is long enough for its line (or for `carry` s left over from a line that began in the clip before), then cut. */
async function wrap(extra = 0.7, carry = 0) {
  const left = Math.max(needed(extra), carry) - elapsed()
  if (left > 0) await hold(left * 1000)
  await cut()
}

/** Cut now; the line still running is padded in the next clip. Returns the seconds it still needs. */
async function cutCarry(extra = 0.7) {
  const rest = needed(extra) - elapsed()
  await cut()
  return Math.max(0, rest)
}

/** Smoothly bring an element into view inside whatever pane scrolls it. */
async function reveal(target: Locator, block: 'start' | 'center' | 'end' = 'center', ms = 1100) {
  await target.first().evaluate((el, b) => el.scrollIntoView({ block: b as ScrollLogicalPosition, behavior: 'smooth' }), block)
  await sleep(ms)
}

const acceptFor = (page: Page, name: string) =>
  page.locator(`xpath=//*[normalize-space()='${name}']/following::button[normalize-space()='Accept'][1]`)

async function needTab(page: Page, label: string) {
  await click(page, page.locator('a', { hasText: label }).first())
  await page.waitForLoadState('domcontentloaded')
  await sleep(500)
}

async function main() {
  // fresh state: four other leads so the list looks lived in
  const seeded = await fetch(`${BASE}/leads/seed`, { method: 'POST', redirect: 'manual' })
  if (seeded.status !== 303) throw new Error(`seed failed: ${seeded.status}`)

  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: SIZE, deviceScaleFactor: DPR })
  await prepare(ctx)
  const page = await ctx.newPage()
  page.on('dialog', (d) => d.accept())

  await page.goto(`${BASE}/`)
  await page.waitForLoadState('networkidle')
  await moveTo(page, 760, 520)

  // 1. leads: the list, then a new lead -----------------------------------------------------------
  await shoot(page, 'leads', 'Officer')
  await hold(1500)
  for (const t of ['Imaging tools for tumour surgery', 'Hearing implant software', 'Lithium battery recycling plant']) {
    await point(page, page.getByText(t).first())
    await hold(900)
  }
  await hold(600)
  await click(page, page.locator('a, button', { hasText: 'New lead' }).first())
  await page.waitForSelector('#op')
  await hold(700)
  await page.locator('#title').fill('')
  await type(page, page.locator('#title'), 'Kestrelyn Health Oy', { cps: 14, lead: 9 })
  await wrap()

  // 2. paste: the one pager as it came, scrolled to its financials --------------------------------
  await shoot(page, 'paste', 'Officer')
  await click(page, page.locator('#op'))
  await hold(400)
  await page.locator('#op').fill(ONE_PAGER) // a paste lands all at once
  await hold(900)
  await page.evaluate(`(() => {
    const ta = document.querySelector('#op'); const i = ta.value.indexOf('Financials')
    const y = ta.scrollHeight * (i / ta.value.length) - 20
    ta.scrollTo({ top: y, behavior: 'smooth' })
  })()`)
  await hold(2200)
  await click(page, page.locator('#readbtn'))
  await hold(900)
  await wrap(0.3)

  await page.waitForSelector('text=Step 2 of 2', { timeout: 240_000 })
  await page.waitForLoadState('networkidle')
  await moveTo(page, 900, 300)

  // 3. needs: seven of them, a source sentence, then what was ignored and struck -------------------
  await shoot(page, 'needs', 'Officer')
  await hold(1600)
  await click(page, page.locator('#needs summary').first())
  await hold(2400)
  await click(page, page.locator('summary', { hasText: 'What was used' }))
  await reveal(page.locator('.ps', { hasText: 'We are passionate' }), 'start', 1300)
  await moveTo(page, 820, 260)
  await hold(2600)
  await reveal(page.locator('.ps.x').first(), 'center', 1300)
  await point(page, page.locator('.ps.x').nth(1))
  await hold(3000)
  await wrap()

  // 4. matches: the search fills in need by need; why the top match is there -----------------------
  await shoot(page, 'matches', 'Officer')
  await hold(500)
  await click(page, page.getByRole('button', { name: 'Find matches' }))
  await page.waitForURL('**/matches**')
  await page.waitForFunction(`!document.body.innerText.includes('Searching…')`, undefined, { timeout: 120_000 })
  await hold(1800)
  await click(page, page.locator('summary', { hasText: 'Why it matches' }).first())
  mark('why')
  await moveTo(page, 900, 470)
  await hold(4000)
  await wrap()

  // 5. mutz: ask why the first match is first ------------------------------------------------------
  await shoot(page, 'mutz', 'Officer')
  await click(page, page.locator('#mutzbtn'))
  await hold(700)
  await type(page, page.locator('#mutzask'), 'Why is the Diabetes Center Berne ranked first?', { cps: 95, lead: 14 })
  await hold(300)
  await page.keyboard.press('Enter')
  await hold(900)
  const carry = await cutCarry()

  await page.locator('#mutzmsgs', { hasText: /ranking log/i }).waitFor({ timeout: 120_000 })
  await sleep(800)
  // start at the top of the answer, then let the table come into view
  await page.evaluate(`(() => {
    const m = document.querySelector('#mutzmsgs'); const blocks = m.children
    const last = blocks[blocks.length - 1]; m.scrollTop = Math.max(0, last.offsetTop - m.offsetTop - 60)
  })()`)

  await shoot(page, 'mutzreply', 'Officer')
  await moveTo(page, 1200, 420)
  await hold(2600)
  await page.evaluate(`document.querySelector('#mutzmsgs').scrollBy({ top: 320, behavior: 'smooth' })`)
  await hold(2600)
  await click(page, page.locator('#mutzclose'))
  await hold(500)
  await wrap(0.7, carry)

  // 6. nomatch: Infrastructure has nothing with evidence ------------------------------------------
  await shoot(page, 'nomatch', 'Officer')
  await needTab(page, 'Infrastructure')
  await moveTo(page, 760, 330)
  await hold(3200)
  await point(page, page.getByText('ARTORG Center, Diabetes Center Berne, University of Bern').first())
  await hold(2000)
  await wrap(0.5)

  // 7. accept: one pick per need -------------------------------------------------------------------
  await shoot(page, 'accept', 'Officer')
  await needTab(page, 'Clinical study')
  await click(page, acceptFor(page, 'Diabetes Center Berne'), { settle: 700 })
  await needTab(page, 'R&D partner')
  await click(page, acceptFor(page, 'AI in Health and Nutrition'), { settle: 700 })
  await needTab(page, 'Clinical trial support')
  await click(page, acceptFor(page, 'SPN-005 - Pilot study on painless glucose monitoring'), { settle: 700 })
  await needTab(page, 'Funding')
  await click(page, page.getByRole('button', { name: 'Accept' }).first(), { settle: 900 })
  await wrap(0.4)

  // 8. map: need → match → fact → page -------------------------------------------------------------
  await shoot(page, 'map', 'Officer')
  await click(page, page.getByRole('link', { name: 'Open evidence map' }).or(page.getByRole('button', { name: 'Open evidence map' })).first())
  await page.waitForURL('**/map**')
  await hold(1200)
  await click(page, page.getByText('Diabetes Center Berne').first())
  await hold(1500)
  await point(page, page.getByText('Source · quoted on').first())
  await hold(2200)
  await wrap()

  // off camera: the map's hover handlers make the cursor crawl, so go to the pitch here and cut to it
  await page.getByRole('link', { name: 'Go to pitch' }).or(page.getByRole('button', { name: 'Go to pitch' })).first().click()
  await page.waitForURL('**/pitch**')
  await page.waitForSelector('[data-next]')
  await page.waitForLoadState('networkidle')
  await moveTo(page, 1100, 600)

  // 9. pitch: check two cards against their sources on camera -------------------------------------
  await shoot(page, 'pitch', 'Officer')
  await hold(1200)
  for (let i = 0; i < 2; i++) {
    await click(page, page.locator('[data-next]').first())
    await hold(i === 0 ? 2200 : 1500)
    await click(page, page.getByRole('button', { name: 'Yes, looks right' }).first())
    await hold(700)
  }
  await wrap(0.4)

  // the rest of the cards, off camera
  for (let i = 0; i < 20 && (await page.locator('[data-next]').count()); i++) {
    await page.locator('[data-next]').first().click()
    await sleep(500)
    const yes = page.getByRole('button', { name: 'Yes, looks right' })
    if (await yes.count()) await yes.first().click()
    await sleep(500)
  }
  const lid = new URL(page.url()).pathname.split('/')[2]
  await page.goto(`${BASE}/leads/${lid}/export`)
  await page.waitForLoadState('networkidle')
  await moveTo(page, 760, 300)

  // 10. export ------------------------------------------------------------------------------------
  await shoot(page, 'export', 'Officer')
  await hold(1000)
  await click(page, page.getByRole('button', { name: 'Export and mark as sent' }))
  await page.waitForURL('**done=**')
  await hold(1600)
  await wrap(0.4)

  // the clean PDF the company gets, rendered page by page into a plain viewer
  const pdfDir = join(ROOT, 'out', 'pdf')
  rmSync(pdfDir, { recursive: true, force: true })
  mkdirSync(pdfDir, { recursive: true })
  const pdf = await (await fetch(`${BASE}/leads/${lid}/export/v1/clean.pdf`)).arrayBuffer()
  writeFileSync(join(pdfDir, 'clean.pdf'), Buffer.from(pdf))
  execFileSync('pdftoppm', ['-r', '160', '-png', join(pdfDir, 'clean.pdf'), join(pdfDir, 'p')])
  const pages = readdirSync(pdfDir).filter((f) => f.startsWith('p') && f.endsWith('.png')).sort()
  writeFileSync(
    join(pdfDir, 'view.html'),
    `<!doctype html><meta charset="utf-8"><title>Bern for Kestrelyn Health Oy.pdf</title>
<style>html,body{margin:0;background:#E6E6E6;font:13px/1.4 "Helvetica Neue",Helvetica,Arial,sans-serif;color:#111}
.bar{position:fixed;top:0;left:0;right:0;height:44px;background:#fff;border-bottom:1px solid #D9D9D9;display:flex;align-items:center;gap:12px;padding:0 20px;z-index:2}
.bar b{font-size:14px}.bar span{color:#5E5E5E}main{padding:68px 0 40px;display:flex;flex-direction:column;align-items:center;gap:20px}
img{width:1000px;display:block;box-shadow:0 1px 3px rgba(0,0,0,.18);scroll-margin-top:64px}</style>
<div class="bar"><b>Bern for Kestrelyn Health Oy.pdf</b><span>${pages.length} pages · A4</span></div>
<main>${pages.map((p) => `<img src="${p}">`).join('')}</main>`,
  )
  const pv = await ctx.newPage()
  await pv.goto(`file://${join(pdfDir, 'view.html')}`)
  await pv.waitForLoadState('load')
  await moveTo(pv, 1330, 640)

  // 11. pdf: what Kestrelyn gets; the last frame holds under the closing line ------------------------
  await shoot(pv, 'pdf', 'Company')
  await hold(1800)
  for (const i of [1, 2]) {
    await reveal(pv.locator('img').nth(i), 'start', 1300)
    await hold(1500)
  }
  await reveal(pv.locator('img').last(), 'start', 1500)
  await hold(1200)
  await cut()

  await browser.close()
  finish()
  writeFileSync(join(ROOT, 'capture', 'marks.json'), JSON.stringify(marks, null, 2) + '\n')
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
