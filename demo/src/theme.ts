/** From app/static/app.css and docs/DESIGN.md, so the video and the app read as one thing. */
export const C = {
  paper: '#F2F2F2', // --panel2: background behind the screen box and the end card
  ink: '#111111', // --black: end card title, shadows
  body: '#5E5E5E', // --grey: end card tagline
  muted: '#6F6F6F', // --mute: end card credit line
  accent: '#E30613', // --red: the officer's tag
  navy: '#111111', // black: the company's tag (the design allows no other hue)
}

export const W = 1920
export const H = 1080
export const FPS = 30

// Helvetica Neue is the app's own face; it is a macOS system font, so the render picks it up locally
export const font = {
  sans: "'Helvetica Neue', Helvetica, Arial, sans-serif",
  mono: "'IBM Plex Mono', ui-monospace, monospace",
  display: "'Helvetica Neue', Helvetica, Arial, sans-serif",
}
