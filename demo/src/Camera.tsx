import type { CSSProperties, ReactNode } from 'react'
import { Easing, interpolate, useCurrentFrame, useVideoConfig } from 'remotion'
import { H, W } from './theme'

/** A camera keyframe: at `t` seconds into the scene, the point (x, y) of the layer sits at frame centre, magnified `s` times. */
export type Key = { t: number; x: number; y: number; s: number }

const ease = Easing.bezier(0.45, 0, 0.2, 1)

export function camAt(keys: Key[], t: number): { x: number; y: number; s: number } {
  if (keys.length === 1 || t <= keys[0].t) return keys[0]
  for (let i = 1; i < keys.length; i++) {
    const a = keys[i - 1]
    const b = keys[i]
    if (t <= b.t) {
      const p = b.t === a.t ? 1 : interpolate(t, [a.t, b.t], [0, 1], { easing: ease, extrapolateLeft: 'clamp', extrapolateRight: 'clamp' })
      // interpolate in log-scale so zooms feel linear
      const s = Math.exp(Math.log(a.s) + (Math.log(b.s) - Math.log(a.s)) * p)
      return { x: a.x + (b.x - a.x) * p, y: a.y + (b.y - a.y) * p, s }
    }
  }
  return keys[keys.length - 1]
}

/**
 * Pans and zooms its children (laid out in design pixels, any size) according to `keys`.
 * The viewport is the full frame unless `width`/`height` are given (then it is a pane,
 * positioned by the caller).
 */
export function Camera({
  keys,
  children,
  style,
  width = W,
  height = H,
}: {
  keys: Key[]
  children: ReactNode
  style?: CSSProperties
  width?: number
  height?: number
}) {
  const frame = useCurrentFrame()
  const { fps } = useVideoConfig()
  const { x, y, s } = camAt(keys, frame / fps)
  return (
    <div style={{ position: 'absolute', left: 0, top: 0, width, height, overflow: 'hidden', ...style }}>
      <div
        style={{
          position: 'absolute',
          left: 0,
          top: 0,
          width: W,
          height: H,
          transformOrigin: '0 0',
          transform: `translate(${width / 2 - x * s}px, ${height / 2 - y * s}px) scale(${s})`,
          willChange: 'transform',
        }}
      >
        {children}
      </div>
    </div>
  )
}
