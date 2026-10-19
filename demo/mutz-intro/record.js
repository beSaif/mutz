// Renders mutz-intro.html to mutz-intro.gif (for slides), mutz-intro.mp4 and mutz-intro-logo.png.
// Steps the animation's own timeline frame by frame, so the output is smooth on any machine.
// Needs playwright-core and ffmpeg: npm i --no-save playwright-core@1.49.1 && node record.js
// Set CHROME=/path/to/chrome if Playwright's own Chromium is not installed.
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path');

const FPS = 25, END = 5800, HOLD = 2;            // ms of animation, seconds to hold the logo at the end
const dir = __dirname, frames = fs.mkdtempSync(path.join(os.tmpdir(), 'mutz-frames-'));

(async () => {
  const browser = await chromium.launch(process.env.CHROME ? { executablePath: process.env.CHROME } : {});
  const page = await browser.newPage({ viewport: { width: 632, height: 632 }, deviceScaleFactor: 2 });
  await page.goto('file://' + path.join(dir, 'mutz-intro.html'));
  await page.evaluate(() => document.fonts.ready);
  await page.addStyleTag({ content: '.bar{display:none!important}' });
  const stage = page.locator('.stage svg');
  let n = 0;
  for (let t = 0; t <= END; t += 1000 / FPS) {
    await page.evaluate(t => anims.forEach(a => { a.pause(); a.currentTime = t; }), t);
    await stage.screenshot({ path: path.join(frames, String(n++).padStart(4, '0') + '.png') });
  }
  await browser.close();

  const input = ['-y', '-loglevel', 'error', '-framerate', String(FPS), '-i', path.join(frames, '%04d.png')];
  const hold = `tpad=stop_mode=clone:stop_duration=${HOLD}`;
  execFileSync('ffmpeg', [...input, '-vf',
    `${hold},scale=720:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=full[p];[b][p]paletteuse=dither=bayer:bayer_scale=5`,
    '-loop', '0', path.join(dir, 'mutz-intro.gif')]);
  execFileSync('ffmpeg', [...input, '-vf', `${hold},scale=1080:-2:flags=lanczos`,
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-movflags', '+faststart', path.join(dir, 'mutz-intro.mp4')]);
  fs.copyFileSync(path.join(frames, String(n - 1).padStart(4, '0') + '.png'), path.join(dir, 'mutz-intro-logo.png'));
  fs.rmSync(frames, { recursive: true });
  console.log(`${n} frames -> mutz-intro.gif, mutz-intro.mp4, mutz-intro-logo.png`);
})();
