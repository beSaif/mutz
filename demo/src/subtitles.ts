import type { Line } from './data'

export type Cue = { from: number; until: number; text: string }

const MAX = 62
/** A shorter piece would flash past; it joins its neighbour (an overlong result is halved below). */
const MIN = 24

/** Splits a line into subtitle-sized pieces at sentence ends, then at commas if still long. */
function chunks(text: string): string[] {
  const sentences = text.split(/(?<=[.?!])\s+/)
  const out: string[] = []
  for (const s of sentences) {
    if (s.length <= MAX) {
      out.push(s)
      continue
    }
    let cur = ''
    for (const part of s.split(/(?<=,|\.\.\.)\s+/)) {
      if (cur.length >= MIN && (cur + ' ' + part).length > MAX) {
        out.push(cur)
        cur = part
      } else cur = cur ? `${cur} ${part}` : part
    }
    if (cur) out.push(cur)
  }
  // the strip below the screen holds one line: halve anything still too long at the space nearest its middle
  return out.flatMap((p) => {
    if (p.length <= MAX) return [p]
    const mid = p.length / 2
    let cut = -1
    for (let i = 0; i < p.length; i++) if (p[i] === ' ' && (cut < 0 || Math.abs(i - mid) < Math.abs(cut - mid))) cut = i
    return cut < 0 ? [p] : [p.slice(0, cut), p.slice(cut + 1)]
  })
}

/** Subtitle cues, timed by each piece's share of the line's characters. */
export function cues(lines: Line[]): Cue[] {
  const all: Cue[] = []
  for (const line of lines) {
    const parts = chunks(line.text)
    const total = parts.reduce((a, p) => a + p.length, 0)
    const span = line.seconds - 0.15
    let t = line.at
    for (const p of parts) {
      const d = (span * p.length) / total
      all.push({ from: t, until: t + d, text: p.replace(/\.\.\.$/, '…').replace(/\.\.\./g, '…') })
      t += d
    }
  }
  return all
}
