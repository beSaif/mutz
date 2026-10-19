import type { ReactNode } from 'react'
import { AbsoluteFill, Audio, Freeze, OffthreadVideo, Sequence, interpolate, staticFile, useCurrentFrame, useVideoConfig } from 'remotion'
import { Camera, type Key } from './Camera'
import { C, font } from './theme'
import { CAMERA, CARD_AT, CREDIT, CUTS, NARRATION, ROLE_COLORS, SCREEN, TAGLINE, TITLE, VIDEO, VIDEO_END } from './data'
import { cues } from './subtitles'

const CUES = cues(NARRATION)

function useT() {
  const frame = useCurrentFrame()
  const { fps } = useVideoConfig()
  return frame / fps
}

const clamp = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const

function fadeSlide(t: number, from: number, until: number) {
  const a = interpolate(t, [from, from + 0.45], [0, 1], clamp)
  const b = interpolate(t, [until - 0.35, until], [1, 0], clamp)
  const dy = interpolate(t, [from, from + 0.45], [14, 0], clamp)
  return { opacity: a * b, transform: `translateY(${dy}px)` }
}

/** Whose screen this is, bottom-left under the screen box. */
function Tag({ children, from, until, color }: { children: ReactNode; from: number; until: number; color: string }) {
  const t = useT()
  if (t < from || t > until) return null
  return (
    <div
      style={{
        position: 'absolute',
        left: SCREEN.x,
        top: SCREEN.y + SCREEN.h + 22,
        background: color,
        color: '#fff',
        fontFamily: font.mono,
        fontSize: 20,
        letterSpacing: 1.6,
        textTransform: 'uppercase',
        padding: '8px 16px',
        borderRadius: 8,
        boxShadow: '0 8px 24px rgba(27,27,31,0.22)',
        ...fadeSlide(t, from, until),
      }}
    >
      {children}
    </div>
  )
}

/** Consecutive clips of the same role share one tag; the last one runs through the held frame. */
const TAGS = CUTS.filter((c) => c.who).reduce<{ who: string; from: number; until: number }[]>((acc, c) => {
  const prev = acc.at(-1)
  if (prev && prev.who === c.who && Math.abs(prev.until - c.from) < 0.05) prev.until = c.until
  else acc.push({ who: c.who, from: c.from, until: c.until })
  return acc
}, [])
if (TAGS.length) TAGS[TAGS.length - 1].until = CARD_AT

function Subtitles() {
  const t = useT()
  const cue = CUES.find((c) => t >= c.from && t < c.until)
  if (!cue) return null
  return (
    <div style={{ position: 'absolute', left: 0, right: 0, top: SCREEN.y + SCREEN.h + 16, display: 'flex', justifyContent: 'center' }}>
      <div
        style={{
          maxWidth: 1360,
          background: 'rgba(27,27,31,0.84)',
          color: '#fff',
          fontFamily: font.sans,
          fontWeight: 500,
          fontSize: 38,
          lineHeight: 1.25,
          textAlign: 'center',
          padding: '8px 26px',
          borderRadius: 10,
        }}
      >
        {cue.text}
      </div>
    </div>
  )
}

function EndCard() {
  const t = useT()
  const a = interpolate(t, [0, 0.7], [0, 1], clamp)
  const dy = interpolate(t, [0.3, 1.1], [16, 0], clamp)
  const b = interpolate(t, [0.3, 1.1], [0, 1], clamp)
  return (
    <AbsoluteFill style={{ background: C.paper, opacity: a, alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ textAlign: 'center', opacity: b, transform: `translateY(${dy}px)`, marginTop: -80 }}>
        <div style={{ fontFamily: font.display, fontWeight: 700, fontSize: 112, color: C.ink, lineHeight: 1, letterSpacing: -2 }}>{TITLE}</div>
        <div style={{ fontFamily: font.display, fontSize: 44, color: C.body, marginTop: 26 }}>{TAGLINE}</div>
        <div style={{ fontFamily: font.mono, fontSize: 22, color: C.muted, marginTop: 44, letterSpacing: 1.2 }}>{CREDIT}</div>
      </div>
    </AbsoluteFill>
  )
}

/** The footage, frozen on its last frame once it runs out. */
function Footage() {
  const frame = useCurrentFrame()
  const { fps } = useVideoConfig()
  const last = Math.max(0, Math.floor(VIDEO_END * fps) - 2)
  const video = <OffthreadVideo src={staticFile(VIDEO)} muted style={{ width: 1920, height: 1080 }} />
  return frame >= last ? <Freeze frame={last}>{video}</Freeze> : video
}

/** Camera zoom is relative to the screen box; the layer itself is 1920 px wide. */
const screenKeys = (keys: Key[]) => keys.map((k) => ({ ...k, s: k.s * (SCREEN.w / 1920) }))
const screenStyle = { left: SCREEN.x, top: SCREEN.y, borderRadius: 10, boxShadow: '0 0 0 1px rgba(27,27,31,0.10), 0 10px 30px rgba(27,27,31,0.10)' }

export function Walkthrough() {
  const { fps } = useVideoConfig()
  const f = (s: number) => Math.round(s * fps)
  return (
    <AbsoluteFill style={{ background: C.paper }}>
      {VIDEO_END > 0 && (
        <Sequence durationInFrames={f(CARD_AT) + 30} name="screen">
          <Camera keys={screenKeys(CAMERA)} width={SCREEN.w} height={SCREEN.h} style={screenStyle}>
            <Footage />
          </Camera>
        </Sequence>
      )}
      {TAGS.map((tag) => (
        <Tag key={tag.from} from={tag.from} until={tag.until} color={ROLE_COLORS[tag.who] ?? C.navy}>
          {tag.who} view
        </Tag>
      ))}
      <Sequence from={f(CARD_AT)} name="end card">
        <EndCard />
      </Sequence>
      <Subtitles />
      {NARRATION.map((l) => (
        <Sequence key={l.id} from={f(l.at)} name={l.id}>
          <Audio src={staticFile(`rec/voice/${l.id}.mp3`)} />
        </Sequence>
      ))}
    </AbsoluteFill>
  )
}
