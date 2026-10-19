/** Writes out/walkthrough.srt from the same cues the video burns in. */
import { mkdirSync, writeFileSync } from 'node:fs'
import { NARRATION } from '../src/data'
import { cues } from '../src/subtitles'

const ts = (s: number) => {
  const ms = Math.round(s * 1000)
  const p = (n: number, w = 2) => String(n).padStart(w, '0')
  return `${p(Math.floor(ms / 3600000))}:${p(Math.floor(ms / 60000) % 60)}:${p(Math.floor(ms / 1000) % 60)},${p(ms % 1000, 3)}`
}
const srt = cues(NARRATION)
  .map((c, i) => `${i + 1}\n${ts(c.from)} --> ${ts(c.until)}\n${c.text}\n`)
  .join('\n')
mkdirSync(new URL('../out/', import.meta.url), { recursive: true })
writeFileSync(new URL('../out/walkthrough.srt', import.meta.url), srt)
console.log('wrote out/walkthrough.srt')
