/**
 * Filming helpers: a visible cursor, human-paced input, and a recorder that films only between
 * roll() and cut(). Frames are high-DPR screenshots (sharper than Playwright's recordVideo, which is
 * soft VP8), each stamped with its time so the clip plays back in real time.
 *
 *   await roll(page, 'start', 'Admin')   ... on camera ...   await cut()
 *   await slowThing()                    // off camera: waits, retries
 *   await roll(page, 'result', 'Admin')  ...                 await cut()
 *   finish()                             // → public/rec/screen.mp4 + capture/cuts.json
 */
import { execFileSync } from 'node:child_process'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { BrowserContext, Locator, Page } from 'playwright'

export const SIZE = { width: 1440, height: 810 }
/** 1.5 keeps text crisp in the 1728 px screen box; drop to 1.25 if clips report < 20 fps. */
export const DPR = 1.5
const QUALITY = 90

const ROOT = new URL('..', import.meta.url).pathname
const CLIPS = join(ROOT, 'out', 'clips')
const SCREEN_OUT = join(ROOT, 'public', 'rec', 'screen.mp4')
const CUTS_OUT = join(ROOT, 'capture', 'cuts.json')

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
/** Hold the current frame for a moment (on camera). */
export const hold = (ms: number) => sleep(ms)
const log = (m: string) => console.log(`${new Date().toISOString().slice(11, 19)} ${m}`)

// --- cursor and input --------------------------------------------------------------------------
const CURSOR_SCRIPT = `
  (() => {
    const make = () => {
      if (document.getElementById('__cur')) return
      const c = document.createElement('div')
      c.id = '__cur'
      c.style.cssText = 'position:fixed;left:0;top:0;width:22px;height:30px;pointer-events:none;z-index:2147483647;transform:translate(-3px,-2px);transition:opacity .2s;opacity:0'
      c.innerHTML = '<svg width="22" height="30" viewBox="0 0 22 30"><path d="M2 2 L2 23 L7.5 18 L11 27 L15 25.5 L11.5 16.5 L19 16.5 Z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>'
      document.documentElement.appendChild(c)
      window.addEventListener('mousemove', (e) => { c.style.opacity = '1'; c.style.transform = 'translate(' + (e.clientX - 3) + 'px,' + (e.clientY - 2) + 'px)' }, true)
      window.addEventListener('mousedown', () => { c.firstElementChild.style.transform = 'scale(.85)'; c.firstElementChild.style.transformOrigin = '3px 2px' }, true)
      window.addEventListener('mouseup', () => { c.firstElementChild.style.transform = '' }, true)
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', make)
    else make()
  })()
`

/** Call once per browser context, before opening pages. */
export async function prepare(ctx: BrowserContext) {
  await ctx.addInitScript(CURSOR_SCRIPT)
}

const mouseAt = new WeakMap<Page, { x: number; y: number }>()

/** Glide the cursor to (x, y), in more steps the further it goes. */
export async function moveTo(page: Page, x: number, y: number) {
  const from = mouseAt.get(page) ?? { x: SIZE.width / 2, y: SIZE.height / 2 }
  const steps = Math.max(8, Math.min(28, Math.round(Math.hypot(x - from.x, y - from.y) / 25)))
  await page.mouse.move(x, y, { steps })
  mouseAt.set(page, { x, y })
}

/** Scroll the element to a comfortable spot and move the cursor onto it, without clicking. */
export async function point(page: Page, target: Locator) {
  await target.first().waitFor({ state: 'visible' })
  await target.first().evaluate((el) => el.scrollIntoView({ block: 'center', behavior: 'smooth' }))
  await sleep(300)
  const box = await target.first().boundingBox()
  if (!box) throw new Error('no box')
  await moveTo(page, box.x + box.width / 2 + (Math.random() * 6 - 3), box.y + box.height / 2 + (Math.random() * 4 - 2))
}

/** Point at the element and click it like a person would. */
export async function click(page: Page, target: Locator, opts: { settle?: number } = {}) {
  await point(page, target)
  await sleep(120)
  await page.mouse.down()
  await sleep(70)
  await page.mouse.up()
  await sleep(opts.settle ?? 250)
}

/**
 * Click the field, then type: the first `lead` characters at a careful pace, the rest at `cps`.
 * Rough speeds: names 14, titles 26, chat 90–110, notes 150, long prose 400+.
 */
export async function type(page: Page, target: Locator, text: string, opts: { cps?: number; lead?: number } = {}) {
  await click(page, target, { settle: 200 })
  const cps = opts.cps ?? 40
  const lead = Math.min(text.length, opts.lead ?? 12)
  await target.first().pressSequentially(text.slice(0, lead), { delay: 1000 / Math.min(cps, 18) })
  if (text.length > lead) await target.first().pressSequentially(text.slice(lead), { delay: 1000 / cps })
  await sleep(250)
}

/** Ease-in-out scroll of the window by `dy` px. */
export async function smoothScroll(page: Page, dy: number, ms = 1200) {
  // passed as a string: tsx's keepNames helper (__name) does not exist inside the page
  await page.evaluate(
    `new Promise((done) => {
      const dy = ${dy}, ms = ${ms}, y0 = window.scrollY, t0 = performance.now()
      const tick = () => {
        const p = Math.min(1, (performance.now() - t0) / ms)
        const e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2
        window.scrollTo(0, y0 + dy * e)
        if (p < 1) requestAnimationFrame(tick); else done()
      }
      requestAnimationFrame(tick)
    })`,
  )
}

// --- filming -----------------------------------------------------------------------------------
type Frame = { file: string; t: number }
type Rolling = { name: string; who: string; speed: number; dir: string; t0: number; frames: Frame[]; stop: { v: boolean }; loop: Promise<void> }
type Clip = { name: string; who: string; speed: number; file: string; dir: string }

let rolling: Rolling | null = null
const clips: Clip[] = []
rmSync(CLIPS, { recursive: true, force: true })

/**
 * Start filming `page` as a new clip. `who` is the role tag shown under the screen ('' for none).
 * `speed` > 1 films a time-lapse: frames are taken sparsely and play back `speed` times faster (labelled on screen).
 */
export async function roll(page: Page, name: string, who = '', speed = 1) {
  if (rolling) await cut()
  if (clips.some((c) => c.name === name)) throw new Error(`clip name "${name}" used twice`)
  const dir = join(CLIPS, `${String(clips.length).padStart(2, '0')}-${name}`)
  mkdirSync(dir, { recursive: true })
  const frames: Frame[] = []
  const stop = { v: false }
  const t0 = Date.now()
  const loop = (async () => {
    while (!stop.v) {
      const t = (Date.now() - t0) / 1000
      const file = join(dir, `${String(frames.length).padStart(5, '0')}.jpg`)
      try {
        await page.screenshot({ path: file, type: 'jpeg', quality: QUALITY, timeout: 1500 })
      } catch {
        // mid-navigation the page can't be captured for a moment, and a screenshot can hang there until it
        // times out: keep the previous frame on screen
        await sleep(30)
        continue
      }
      frames.push({ file, t })
      if (speed > 1) await sleep((1000 / 30) * speed)
    }
  })()
  rolling = { name, who, speed, dir, t0, frames, stop, loop }
  log(`roll ${name}${speed > 1 ? ` (×${speed})` : ''}`)
}

/** Stop filming and encode the clip. Everything until the next roll() stays off camera. */
export async function cut() {
  if (!rolling) return
  const r = rolling
  rolling = null
  r.stop.v = true
  await r.loop
  const end = (Date.now() - r.t0) / 1000
  if (!r.frames.length) throw new Error(`clip ${r.name} has no frames`)
  // concat list with per-frame durations, so the clip plays in real time
  const list = r.frames.map((f, i) => `file '${f.file}'\nduration ${(((r.frames[i + 1]?.t ?? end) - f.t) / r.speed).toFixed(3)}`).join('\n')
  writeFileSync(join(r.dir, 'list.txt'), `${list}\nfile '${r.frames.at(-1)!.file}'\n`)
  const file = join(CLIPS, `${String(clips.length).padStart(2, '0')}-${r.name}.mp4`)
  execFileSync('ffmpeg', [
    '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', join(r.dir, 'list.txt'),
    '-vf', 'fps=30,crop=trunc(iw/2)*2:trunc(ih/2)*2:0:0,format=yuv420p',
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '14', '-movflags', '+faststart', file,
  ])
  clips.push({ name: r.name, who: r.who, speed: r.speed, file, dir: r.dir })
  const fps = (r.frames.length / end) * r.speed
  log(`cut  ${r.name}: ${(end / r.speed).toFixed(1)} s, ${fps.toFixed(1)} fps captured${fps < 20 ? '  (low: lower DPR or QUALITY in film.ts)' : ''}`)
  return fps
}

/** Throw away the clip just cut, so it can be filmed again (a stall from memory pressure can drop a clip to ~1 fps). */
export function drop() {
  const c = clips.pop()
  if (!c) return
  rmSync(c.file, { force: true })
  rmSync(c.dir, { recursive: true, force: true })
  log(`drop ${c.name}`)
}

const duration = (file: string) =>
  Number(String(execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', file])).trim())

/** Join all clips into public/rec/screen.mp4 and write their times on the output timeline to capture/cuts.json. */
export function finish() {
  if (rolling) throw new Error('call await cut() before finish()')
  if (!clips.length) throw new Error('nothing filmed')
  mkdirSync(join(ROOT, 'public', 'rec'), { recursive: true })
  const list = join(CLIPS, 'clips.txt')
  writeFileSync(list, clips.map((c) => `file '${c.file}'`).join('\n') + '\n')
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy', '-movflags', '+faststart', SCREEN_OUT])
  let at = 0
  const cuts = clips.map((c) => {
    const d = duration(c.file)
    const cut = { name: c.name, who: c.who, ...(c.speed > 1 ? { speed: c.speed } : {}), from: +at.toFixed(3), until: +(at + d).toFixed(3) }
    at += d
    return cut
  })
  writeFileSync(CUTS_OUT, JSON.stringify(cuts, null, 2) + '\n')
  log(`screen.mp4: ${clips.length} clips, ${at.toFixed(1)} s → ${SCREEN_OUT}`)
}
