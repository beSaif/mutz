/**
 * The mutz walkthrough, filmed: one officer takes the Kestrelyn one pager from paste to the CEO's yes, a quick
 * look at Insights and Sources, then the bear (hidden until then) drafts a second company's pitch from its PDF.
 *
 *   pnpm record     (the app must run at DEMO_BASE on a fresh app DB, see capture/fresh.sh; the gateway cache makes
 *                    the Kestrelyn calls fast, the Veyra run in the finale is live and filmed as a time-lapse)
 *
 * Each roll(page, name) starts a clip; narration lines in voice/lines.json point at these names.
 * Clips are padded to their narration (voice/manifest.json), so run `pnpm voice` first.
 * Slow things (the search, building and writing the pitch, the emails, Mutz's run) are cut, not sped up,
 * except Mutz's run, which plays as a labelled time-lapse.
 */
import 'dotenv/config'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { chromium, type Locator, type Page } from 'playwright'
import { DPR, SIZE, click, cut, finish, hold, moveTo, point, prepare, roll, sleep, type } from './film'

const BASE = process.env.DEMO_BASE ?? 'http://localhost:8100'
const ROOT = new URL('..', import.meta.url).pathname
const APP = join(ROOT, '..')
const ONE_PAGER = readFileSync(join(APP, 'needs/fixtures/demo_onepager.txt'), 'utf8').trim()
const SECOND = 'Veyra_Vision_one_pager.pdf'
const SECOND_PDF = readFileSync(join(ROOT, 'capture/fixtures', SECOND)).toString('base64')
/** The finale's time-lapse: real seconds per second of video, and the longest it may run on screen. */
const LAPSE = 3
const LAPSE_MAX = 22

// --- pacing against the narration ---------------------------------------------------------------
const lines: { id: string; clip: string; offset?: number }[] = JSON.parse(readFileSync(join(ROOT, 'voice/lines.json'), 'utf8'))
const voice: Record<string, { seconds: number }> = JSON.parse(readFileSync(join(ROOT, 'voice/manifest.json'), 'utf8'))
let rolledAt = 0
let rolledName = ''
let rolledSpeed = 1

async function shoot(page: Page, name: string, speed = 1) {
  await roll(page, name, '', speed)
  rolledAt = Date.now()
  rolledName = name
  rolledSpeed = speed
}

/** Seconds of video so far in the current clip. */
const elapsed = () => (Date.now() - rolledAt) / 1000 / rolledSpeed

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
  if (left > 0) await hold(left * 1000 * rolledSpeed)
  return await cut()
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

// --- the bear stays off screen until the finale ---------------------------------------------------
// Capture-time CSS only: the app is unchanged. A flag in localStorage lifts it, so it survives navigation.
const HIDE_MUTZ = `
  (() => {
    let shown = false
    try { shown = localStorage.getItem('demo-mutz') === '1' } catch (e) {}
    if (shown) return
    const s = document.createElement('style')
    s.id = 'demo-hide-mutz'
    // the corner bear and its chat, and the little bears in the loading states; the sidebar logo stays
    s.textContent = '#mutz, #mutzcard, .mutz-svg { display: none !important } .rl-mz .mutz-svg { display: inline !important }'
    // added before the parser has made <head>, the tag is lost: put it in as soon as <head> exists
    const add = () => {
      if (!document.head || s.isConnected) return !!document.head
      document.head.appendChild(s)
      return true
    }
    if (!add()) {
      const mo = new MutationObserver(() => { if (add()) mo.disconnect() })
      mo.observe(document, { childList: true, subtree: true })
      document.addEventListener('DOMContentLoaded', add)
    }
  })()
`
const SHOW_MUTZ = `
  (() => {
    try { localStorage.setItem('demo-mutz', '1') } catch (e) {}
    const a = document.createElement('style')
    a.textContent = '@keyframes demoPop { 0% { opacity: 0; transform: translateY(60px) scale(.6) } 70% { opacity: 1; transform: translateY(-6px) scale(1.06) } 100% { transform: none } } #mutz { animation: demoPop .8s cubic-bezier(.3,1.2,.4,1) both }'
    document.head.appendChild(a)
    document.getElementById('demo-hide-mutz')?.remove()
  })()
`

/** A file icon that follows the cursor, as a PDF dragged in from the desktop looks. */
const GHOST = (name: string) => `
  (() => {
    const g = document.createElement('div')
    g.id = '__ghost'
    g.style.cssText = 'position:fixed;left:0;top:0;z-index:2147483646;pointer-events:none;display:flex;align-items:center;gap:8px;padding:8px 12px 8px 8px;background:#fff;border:1px solid #111;box-shadow:0 8px 24px rgba(0,0,0,.18);font:13px/1.2 "Helvetica Neue",Helvetica,Arial,sans-serif;opacity:.94'
    g.innerHTML = '<svg width="22" height="28" viewBox="0 0 22 28"><path d="M1 1h13l7 7v19H1z" fill="#fff" stroke="#111" stroke-width="1.4"/><path d="M14 1v7h7" fill="none" stroke="#111" stroke-width="1.4"/><rect x="3" y="16" width="16" height="7" fill="#E30613"/><text x="11" y="21.6" font-size="5.6" font-family="Helvetica" font-weight="700" fill="#fff" text-anchor="middle">PDF</text></svg><span>${name}</span>'
    document.documentElement.appendChild(g)
    const move = (e) => { g.style.transform = 'translate(' + (e.clientX + 14) + 'px,' + (e.clientY + 10) + 'px)' }
    window.addEventListener('mousemove', move, true)
    g.__off = () => window.removeEventListener('mousemove', move, true)
  })()
`

/** Drop the PDF on the chat card the way the browser would: a dragover (the card lights up), then the drop. */
const DROP = (name: string, phase: 'over' | 'drop') => `
  (() => {
    const bin = atob('${SECOND_PDF}'); const u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i)
    const dt = new DataTransfer(); dt.items.add(new File([u], '${name}', { type: 'application/pdf' }))
    const card = document.querySelector('#mutzcard')
    card.dispatchEvent(new DragEvent('dragover', { dataTransfer: dt, bubbles: true, cancelable: true }))
    if ('${phase}' === 'drop') {
      card.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }))
      const g = document.getElementById('__ghost'); if (g) { g.__off(); g.remove() }
    }
  })()
`

async function main() {
  // fresh state: four other leads so the list looks lived in
  const seeded = await fetch(`${BASE}/leads/seed`, { method: 'POST', redirect: 'manual' })
  if (seeded.status !== 303) throw new Error(`seed failed: ${seeded.status}`)
  // demo rows for the gap report and the capability map (labelled demo on screen), and the map computed once
  const signals = await fetch(`${BASE}/insights/seed`, { method: 'POST', redirect: 'manual' })
  if (signals.status !== 303) throw new Error(`insights seed failed: ${signals.status}`)
  const warm = await (await fetch(`${BASE}/insights/capability`)).text()
  if (!warm.includes('id="cloud"')) {
    // the map computes in the background; wait for it off camera
    for (let i = 0; i < 120; i++) {
      await sleep(2000)
      if ((await (await fetch(`${BASE}/insights/capability`)).text()).includes('id="cloud"')) break
    }
  }

  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: SIZE, deviceScaleFactor: DPR })
  await prepare(ctx)
  // the app's own opening splash plays once per browser; the video opens with it (pnpm intro), so mark it seen
  await ctx.addInitScript(`try { localStorage.setItem('mutz-intro', '1') } catch (e) {}`)
  await ctx.addInitScript(HIDE_MUTZ)
  const page = await ctx.newPage()
  page.on('dialog', (d) => d.accept())

  await page.goto(`${BASE}/`)
  await page.waitForLoadState('networkidle')
  await moveTo(page, 760, 520)

  // 1. leads: the list, then a new lead -----------------------------------------------------------
  await shoot(page, 'leads')
  await hold(1500)
  for (const t of ['Imaging tools for tumour surgery', 'Hearing implant software']) {
    await point(page, page.getByText(t).first())
    await hold(900)
  }
  await hold(500)
  await click(page, page.locator('form[action="/leads/new"] button').first())
  await page.waitForSelector('#op')
  await hold(700)
  await page.locator('#title').fill('')
  await type(page, page.locator('#title'), 'Kestrelyn Health Oy', { cps: 14, lead: 9 })
  await wrap()

  // 2. paste: the one pager as it came, then Find needs --------------------------------------------
  await shoot(page, 'paste')
  await click(page, page.locator('#op'))
  await hold(400)
  await page.locator('#op').fill(ONE_PAGER) // a paste lands all at once
  await hold(900)
  await page.evaluate(`(() => {
    const ta = document.querySelector('#op'); const i = ta.value.indexOf('Financials')
    const y = ta.scrollHeight * (i / ta.value.length) - 20
    ta.scrollTo({ top: y, behavior: 'smooth' })
  })()`)
  await hold(1600)
  await click(page, page.locator('#readbtn'))
  await hold(1500)
  const pasteCarry = await cutCarry()

  await page.waitForSelector('text=Step 2 of 2', { timeout: 240_000 })
  await page.waitForLoadState('networkidle')
  await moveTo(page, 900, 300)

  // 3. needs: seven of them, one source sentence, then what was struck ------------------------------
  await shoot(page, 'needs')
  await hold(1400)
  await point(page, page.getByText('Clinical study with women', { exact: false }).first())
  await hold(1200)
  await click(page, page.locator('#needs summary').first())
  await hold(2400)
  await click(page, page.locator('summary', { hasText: 'What was used' }))
  await reveal(page.locator('.ps.x').first(), 'center', 1300)
  await point(page, page.locator('.ps.x').nth(1))
  await hold(2600)
  await wrap(0.7, pasteCarry)

  // 4. search: the progress on camera, the wait off camera ------------------------------------------
  await shoot(page, 'search')
  await hold(500)
  await click(page, page.getByRole('button', { name: 'Find matches' }))
  await page.waitForURL('**/matches**')
  await moveTo(page, 760, 360)
  // the progress runs live until the line is done; if the search ends sooner, the matches arrive on camera
  await hold(5500)
  await wrap(0.3)

  await page.waitForFunction(`!document.body.innerText.includes('Searching…')`, undefined, { timeout: 600_000 })
  await page.waitForLoadState('networkidle')

  // 5. matches: quotes, coverage, units ------------------------------------------------------------
  await shoot(page, 'matches')
  await hold(1200)
  await click(page, page.locator('summary', { hasText: 'Why it matches' }).first())
  mark('why')
  await moveTo(page, 900, 470)
  await hold(3600)
  await point(page, page.getByText('Strong · 10 matches').first())
  await hold(1600)
  await point(page, page.getByText('No match · nothing with evidence').first())
  await hold(1800)
  await wrap()

  // 6. accept: one pick per need -------------------------------------------------------------------
  await shoot(page, 'accept')
  await needTab(page, 'Clinical study')
  await click(page, acceptFor(page, 'Diabetes Center Berne'), { settle: 700 })
  await needTab(page, 'R&D partner')
  await click(page, acceptFor(page, 'AI in Health and Nutrition'), { settle: 700 })
  await needTab(page, 'Clinical trial support')
  await click(page, acceptFor(page, 'SPN-005 - Pilot study on painless glucose monitoring'), { settle: 700 })
  await needTab(page, 'Funding')
  await click(page, page.getByRole('button', { name: 'Accept' }).first(), { settle: 900 })
  await wrap(0.4)

  // 7. map: one claim back to its quote and page ----------------------------------------------------
  await shoot(page, 'map')
  await click(page, page.getByRole('link', { name: 'Open evidence map' }).or(page.getByRole('button', { name: 'Open evidence map' })).first())
  await page.waitForURL('**/map**')
  await hold(1200)
  await click(page, page.getByText('Diabetes Center Berne').first())
  await hold(1200)
  await point(page, page.locator('#detail .qt').first())
  mark('quote')
  await hold(2200)
  await point(page, page.locator('#detail .src a').first()) // the page the quote was read on
  await hold(2000)
  await wrap()

  // off camera: the map's hover handlers make the cursor crawl, so leave it with a plain click
  await page.getByRole('link', { name: 'Go to pitch' }).or(page.getByRole('button', { name: 'Go to pitch' })).first().click()
  await page.waitForURL('**/pitch**')
  await page.waitForSelector('#pwait:not(.hidden)', { timeout: 20_000 }).catch(() => {})
  await moveTo(page, 1100, 600)

  // 8. pitch: the facts being read out of the matches ------------------------------------------------
  await shoot(page, 'brief')
  await hold(3800)
  await wrap(0.3)
  await page.waitForSelector('#pwait.hidden', { state: 'attached', timeout: 300_000 })
  await page.waitForLoadState('networkidle')

  // 9. check: every card against its source, on camera ----------------------------------------------
  await shoot(page, 'check')
  await hold(900)
  for (let i = 0; i < 30 && (await page.locator('[data-next]').count()); i++) {
    await click(page, page.locator('[data-next]').first(), { settle: i === 0 ? 2200 : 300 })
    const yes = page.getByRole('button', { name: 'Yes, looks right' })
    if (!(await yes.count())) continue
    await click(page, yes.first(), { settle: 250 })
  }
  // "Once they're all ticked, Apertus writes the pitch": the click comes as that line ends
  const writeLine = lines.find((l) => l.id === 'l10')!
  await hold(Math.max(600, ((writeLine.offset ?? 0.3) + voice[writeLine.id].seconds - 0.4 - elapsed()) * 1000))
  await click(page, page.locator('[data-write]'))
  await hold(3200)
  const checkCarry = await cutCarry(0.4)
  await page.waitForSelector('#pwait.hidden', { state: 'attached', timeout: 400_000 })
  await page.waitForLoadState('networkidle')
  await moveTo(page, 1250, 600)

  // 10. export: the written pitch, export, the after-sending screen -----------------------------------
  await shoot(page, 'export')
  await hold(1800)
  await click(page, page.getByRole('link', { name: 'Continue to export' }))
  await page.waitForURL('**/export')
  await hold(1000)
  await click(page, page.getByRole('button', { name: 'Export', exact: true }))
  await page.waitForURL('**/after**') // the click also downloads the clean PDF, which the context accepts
  await moveTo(page, 760, 260)
  await hold(2400)
  await wrap(0.5, checkCarry)

  // 11. accepted: the CEO says yes, the researcher emails are drafted ---------------------------------
  await shoot(page, 'accepted')
  await hold(500)
  await click(page, page.getByRole('button', { name: 'CEO accepted the intro' }))
  await page.waitForURL('**/after#emails')
  // the email is drafted live while the line runs; whatever is left of the wait is cut
  await hold(2200)
  await wrap(0.3)
  await page.waitForSelector('.ml-wait', { state: 'hidden', timeout: 180_000 })
  await sleep(600)

  await shoot(page, 'emails')
  await hold(1200)
  await point(page, page.locator('textarea').first())
  await hold(3000)
  await wrap()

  // 12. capability map and pipeline, through the sidebar ---------------------------------------------
  const side = (name: string) => page.locator('aside a', { hasText: name }).first()
  await page.goto(`${BASE}/insights/capability`)
  await page.waitForSelector('#cloud')
  await page.waitForLoadState('networkidle')
  await moveTo(page, 900, 420)

  await shoot(page, 'capability')
  await hold(1400)
  for (const w of ['Diabetes technology', 'Cardiology']) {
    await point(page, page.locator('#cloud .cw', { hasText: w }).first())
    await hold(1100)
  }
  await click(page, page.locator('#cloud .cw.none').first())
  await hold(500)
  await reveal(page.locator('#cwd'), 'center', 1200)
  await hold(1800)
  await wrap(0.4)

  await shoot(page, 'pipeline')
  await click(page, side('Pipeline'))
  await page.waitForURL('**/pipeline')
  await hold(1200)
  await point(page, page.getByText('Kestrelyn Health Oy').first())
  await hold(2000)
  await wrap(0.4)

  // 13. a new source -------------------------------------------------------------------------------
  await shoot(page, 'source')
  await click(page, side('Add a source'))
  await page.waitForSelector('#url')
  await type(page, page.locator('#url'), 'sitem-insel.ch', { cps: 22, lead: 6 })
  await click(page, page.getByRole('button', { name: 'Check this site' }))
  await page.waitForLoadState('networkidle')
  await moveTo(page, 900, 420)
  await hold(2400)
  await wrap(0.6)

  // 14. finale: the bear appears, takes the PDF, drafts the pitch -------------------------------------
  await page.goto(`${BASE}/`)
  await page.waitForLoadState('networkidle')
  await moveTo(page, 820, 420)

  await shoot(page, 'reveal')
  await hold(1200)
  await page.evaluate(SHOW_MUTZ)
  await hold(2600) // a beat on the bear
  await click(page, page.locator('#mutzbtn'))
  await page.locator('#mutzcard:not(.hidden)').waitFor()
  await hold(1000)
  // the PDF comes in from the left of the screen and is dropped on the chat
  await moveTo(page, 420, 640)
  await page.evaluate(GHOST(SECOND))
  await moveTo(page, 430, 636)
  await hold(300)
  const card = await page.locator('#mutzmsgs').boundingBox()
  if (!card) throw new Error('no chat card')
  await moveTo(page, card.x + card.width / 2 - 40, card.y + card.height / 2)
  await page.evaluate(DROP(SECOND, 'over'))
  await hold(500)
  await page.evaluate(DROP(SECOND, 'drop'))
  await page.locator('#mutzfile:not(.hidden)').waitFor()
  await hold(1600)
  const started = page.waitForResponse((r) => r.url().endsWith('/api/mutz/pitch') && r.request().method() === 'POST')
  await click(page, page.locator('#mutzform button[type=submit]'))
  const lid: string = (await (await started).json()).lead
  await hold(3500)
  const revealCarry = await cutCarry()

  // the run itself, as a time-lapse; the moment it is done is filmed at normal speed
  const status = async () => (await (await fetch(`${BASE}/api/mutz/pitch/${lid}`)).json()).step as string
  await shoot(page, 'drafting', LAPSE)
  while (elapsed() < LAPSE_MAX && (await status()) !== 'done') await sleep(1000)
  await cut()
  while ((await status()) !== 'done') await sleep(1000)

  await shoot(page, 'drafted')
  await page.waitForURL(`**/leads/${lid}/pitch`, { timeout: 30_000 })
  await page.waitForLoadState('networkidle')
  await page.waitForSelector('#pwait.hidden', { state: 'attached', timeout: 60_000 })
  await hold(1500)
  // the chat opens over the page on this screen; drag it by its header to the right-hand corner, where it snaps
  const hd = await page.locator('#mutzhd').boundingBox()
  if (!hd) throw new Error('no chat header')
  await moveTo(page, hd.x + 120, hd.y + hd.height / 2)
  await hold(250)
  await page.mouse.down()
  await moveTo(page, 1180, 560)
  await page.mouse.up()
  await hold(900)
  await point(page, page.getByText('Picked by Bärner Mutz').first())
  mark('picked')
  await hold(3000)
  await wrap(0.7, revealCarry)

  // 15. the same chat, in German -------------------------------------------------------------------
  await shoot(page, 'german')
  if (await page.locator('#mutzcard.hidden').count()) await click(page, page.locator('#mutzbtn'))
  await type(page, page.locator('#mutzask'), 'change the pitch to German', { cps: 90, lead: 12 })
  await hold(300)
  await click(page, page.locator('#mutzform button[type=submit]'))
  await hold(2600)
  const germanCarry = await cutCarry()
  await page.waitForFunction(`document.documentElement.lang === 'de' || document.body.innerText.includes('Bern für')`, undefined, { timeout: 300_000 })
  await page.waitForLoadState('networkidle')
  await sleep(1500)

  await shoot(page, 'deutsch')
  await hold(1500)
  await point(page, page.getByText('Bern für').first())
  await hold(2500)
  await wrap(0.7, germanCarry)

  await browser.close()
  finish()
  writeFileSync(join(ROOT, 'capture', 'marks.json'), JSON.stringify(marks, null, 2) + '\n')
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
