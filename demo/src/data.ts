/**
 * Everything that is specific to this video. Times are seconds on the output timeline.
 * Clips come from capture/cuts.json (written by `pnpm record`), voice durations from voice/manifest.json
 * (written by `pnpm voice`), so re-filming or re-voicing re-times the cut by itself.
 */
import cutsJson from '../capture/cuts.json'
import manifestJson from '../voice/manifest.json'
import linesJson from '../voice/lines.json'
import marksJson from '../capture/marks.json'
import type { Key } from './Camera'
import { C } from './theme'

export const VIDEO = 'rec/screen.mp4'

/** Where the footage sits in the frame; the strip below it carries the tag and subtitles. */
export const SCREEN = { x: 96, y: 14, w: 1728, h: 972 }

// --- end card ---------------------------------------------------------------------------------
export const TITLE = 'mutz'
export const TAGLINE = 'From a company’s one pager to the right experts in Bern.'
export const CREDIT = 'Hack Apertus Liebefeld ’26 · Bern Economic Development Agency'

/** Tag colour per role passed to roll(); leave `who` empty there to show no tags. */
export const ROLE_COLORS: Record<string, string> = { Officer: C.accent, Company: C.navy }

// --- clips ------------------------------------------------------------------------------------
export type Cut = { name: string; who: string; from: number; until: number }
export const CUTS = cutsJson as unknown as Cut[]
export const VIDEO_END = CUTS.at(-1)?.until ?? 0

const clip = (name: string) => CUTS.find((c) => c.name === name)

/** `s` seconds after clip `name` starts. */
export function after(name: string, s = 0.3): number {
  const c = clip(name)
  if (!c) throw new Error(`no clip "${name}" in capture/cuts.json`)
  return c.from + s
}

// --- narration --------------------------------------------------------------------------------
type LineSpec = { id: string; clip: string; offset?: number; text: string }
export type Line = { id: string; at: number; seconds: number; text: string }

const voice = manifestJson as unknown as Record<string, { seconds: number }>

export const NARRATION: Line[] = (linesJson as LineSpec[]).flatMap((l) => {
  if (!clip(l.clip)) {
    console.warn(`line ${l.id}: no clip "${l.clip}" (run pnpm record?)`)
    return []
  }
  if (!voice[l.id]) {
    console.warn(`line ${l.id}: no voice clip (run pnpm voice)`)
    return []
  }
  return [{ id: l.id, at: after(l.clip, l.offset ?? 0.3), seconds: voice[l.id].seconds, text: l.text }]
})

NARRATION.forEach((l, i) => {
  const prev = NARRATION[i - 1]
  if (prev && prev.at + prev.seconds > l.at) console.warn(`line ${l.id} starts ${(prev.at + prev.seconds - l.at).toFixed(2)} s before ${prev.id} ends`)
})

/** The footage holds its last frame until the end card, which follows the last line after a breath. */
const lastEnd = NARRATION.reduce((a, l) => Math.max(a, l.at + l.seconds), VIDEO_END)
export const CARD_AT = lastEnd + 1.5
export const TOTAL = CARD_AT + 4.5

// --- camera -----------------------------------------------------------------------------------
/**
 * Points are in 1920×1080 px of the footage; s = 1 shows the whole screen. Stay wide; zoom only where the
 * narration is about text too small to read, at most ~1.25, never framing out what is being clicked.
 */
const WIDE = { x: 960, y: 540, s: 1 }
const k = (t: number, v: { x: number; y: number; s: number }): Key => ({ t, ...v })

/** Jump to `point` at `from` (put it on a cut), stay until `until`, then ease back over `ease` s (0 = jump, for a cut). */
export function zoom(from: number, point: { x: number; y: number; s: number }, until: number, ease = 0.9): Key[] {
  return [k(from, WIDE), k(from, point), k(until, point), k(until + ease, WIDE)]
}

const marks = marksJson as Record<string, number>
/** A moment marked in capture/record.ts with mark(label), on the output timeline. */
function at(clipName: string, label: string): number {
  const m = marks[`${clipName}:${label}`]
  if (m === undefined) throw new Error(`no mark "${clipName}:${label}" in capture/marks.json`)
  return after(clipName, m)
}

/**
 * Two zooms, both on text the narration is about. At s = 1.25 the point must stay within
 * x 768–1152 and y 432–648 (at 1.2: 800–1120, 450–630), or the frame shows past the footage's edge.
 * - the quotes under "Why it matches": eased in once the row opens, out on the cut to Mutz
 * - Mutz's answer: in on the cut to the reply, out on the cut to Infrastructure
 */
const QUOTES = { x: 1100, y: 560, s: 1.2 }
const MUTZ = { x: 1150, y: 600, s: 1.25 }
export const CAMERA: Key[] = [
  k(0, WIDE),
  k(at('matches', 'why') + 0.4, WIDE),
  k(at('matches', 'why') + 1.2, QUOTES),
  k(after('mutz', 0), QUOTES),
  k(after('mutz', 0), WIDE),
  ...zoom(after('mutzreply', 0), MUTZ, after('nomatch', 0), 0),
]
