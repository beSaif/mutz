/**
 * The opening splash for the video: steps mutz-intro/mutz-intro.html's own timeline frame by frame at 30 fps.
 *
 *   pnpm intro   → public/rec/intro.mp4 (1920×1080 on the video's paper) + public/rec/logo.png (last frame, transparent)
 */
import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { chromium } from 'playwright'
import { C, FPS, H, W } from '../src/theme'

const ROOT = new URL('..', import.meta.url).pathname
const HTML = join(ROOT, 'mutz-intro', 'mutz-intro.html')
const OUT = join(ROOT, 'public', 'rec')
/** ms of animation in mutz-intro.html */
const END = 5800
/** Size of the 400-unit square stage in the frame */
const STAGE = 1000

const STYLE = `
  .bar{display:none!important}
  html,body{background:${C.paper}!important;padding:0!important}
  .stage{padding:0!important;cursor:default!important}
  .stage svg{max-width:${STAGE}px!important;max-height:${STAGE}px!important;width:${STAGE}px!important;height:${STAGE}px!important}
`

async function main() {
  mkdirSync(OUT, { recursive: true })
  const frames = mkdtempSync(join(tmpdir(), 'mutz-intro-'))
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 })
  await page.goto(`file://${HTML}`)
  await page.evaluate('document.fonts.ready')
  await page.addStyleTag({ content: STYLE })

  let n = 0
  for (let t = 0; t <= END + 0.5; t += 1000 / FPS) {
    await page.evaluate(`anims.forEach(a => { a.pause(); a.currentTime = ${t} })`)
    await page.screenshot({ path: join(frames, `${String(n++).padStart(4, '0')}.png`) })
  }

  // the logo for the end card: the last frame on a transparent page, at twice the size for a crisp scale-down
  const logo = await browser.newPage({ viewport: { width: STAGE, height: STAGE }, deviceScaleFactor: 2 })
  await logo.goto(`file://${HTML}`)
  await logo.addStyleTag({ content: STYLE + 'html,body{background:transparent!important}' })
  await logo.evaluate(`anims.forEach(a => a.finish())`)
  await logo.screenshot({ path: join(OUT, 'logo.png'), omitBackground: true })
  await browser.close()

  execFileSync('ffmpeg', [
    '-y', '-loglevel', 'error', '-framerate', String(FPS), '-i', join(frames, '%04d.png'),
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '14', '-movflags', '+faststart', join(OUT, 'intro.mp4'),
  ])
  rmSync(frames, { recursive: true })
  console.log(`${n} frames → public/rec/intro.mp4, public/rec/logo.png`)
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
