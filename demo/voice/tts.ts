/**
 * Narration: voice/lines.json → public/rec/voice/<id>.mp3 + voice/manifest.json (durations).
 * Only lines whose text, voice or model changed (or whose mp3 is missing) are regenerated; --force redoes all.
 *
 * Voice ids are premade, so a key without the voices_read permission still works.
 * Jessica cgSgspJ2msm6clMCkdW9 (default) · Lily pFZP5JQG7iQjIQuC4Bku · Sarah EXAVITQu4vr4xnSDxMaL
 * George JBFqnCBsd6RMkjVDRZzb · Daniel onwK4e9ZLuTAKqWW03F9 · Brian nPczCjzI2devNBz1zQrb
 */
import 'dotenv/config'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const ROOT = new URL('..', import.meta.url).pathname
const OUT = join(ROOT, 'public', 'rec', 'voice')
const MANIFEST = join(ROOT, 'voice', 'manifest.json')
const KEY = process.env.ELEVENLABS_API_KEY ?? ''
const VOICE = process.env.ELEVENLABS_VOICE_ID || 'cgSgspJ2msm6clMCkdW9'
const MODEL = process.env.ELEVENLABS_MODEL || 'eleven_v3'
const force = process.argv.includes('--force')

if (!KEY) {
  console.error('Set ELEVENLABS_API_KEY in demo/.env')
  process.exit(1)
}

type LineSpec = { id: string; text: string }
const lines: LineSpec[] = JSON.parse(readFileSync(join(ROOT, 'voice', 'lines.json'), 'utf8'))
const old: Record<string, { seconds: number; hash: string }> = existsSync(MANIFEST) ? JSON.parse(readFileSync(MANIFEST, 'utf8')) : {}
mkdirSync(OUT, { recursive: true })

const duration = (file: string) =>
  Number(String(execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', file])).trim())

async function speak(text: string, file: string) {
  const r = await fetch(`https://api.elevenlabs.io/v1/text-to-speech/${VOICE}?output_format=mp3_44100_128`, {
    method: 'POST',
    headers: { 'xi-api-key': KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      text,
      model_id: MODEL,
      // v3 takes stability 0 (creative), 0.5 (natural) or 1 (robust); v2 models also take similarity_boost/style
      voice_settings: MODEL === 'eleven_v3' ? { stability: 0.5 } : { stability: 0.55, similarity_boost: 0.8, style: 0.15, speed: 1.0 },
    }),
  })
  if (!r.ok) throw new Error(`tts: ${r.status} ${await r.text()}`)
  writeFileSync(file, Buffer.from(await r.arrayBuffer()))
}

const manifest: Record<string, { seconds: number; hash: string }> = {}
for (const l of lines) {
  const file = join(OUT, `${l.id}.mp3`)
  const hash = createHash('sha1').update(`${VOICE}|${MODEL}|${l.text}`).digest('hex').slice(0, 12)
  if (force || old[l.id]?.hash !== hash || !existsSync(file)) {
    process.stdout.write(`${l.id} … `)
    await speak(l.text, file)
    console.log('ok')
  }
  manifest[l.id] = { seconds: +duration(file).toFixed(3), hash }
}
writeFileSync(MANIFEST, JSON.stringify(manifest, null, 2) + '\n')
const total = Object.values(manifest).reduce((a, m) => a + m.seconds, 0)
console.log(`narration: ${lines.length} lines, ${total.toFixed(1)} s`)
