# Mutz opening splash

Mutz claws through a hive of crawled pages, finds the honey pot, fills it and grabs it. The animation runs 5.8 s, then holds on the logo.

| File | Use |
|---|---|
| `mutz-intro.gif` | Paste into PowerPoint or Keynote. 720 × 720, 25 fps, loops, about 5 MB. |
| `mutz-intro.mp4` | The same animation as video, 1080 × 1080. Sharper and smaller than the GIF. In PowerPoint, choose Insert > Video and set it to play automatically, so it can play once instead of looping. |
| `mutz-intro-logo.png` | The final logo frame as a still, 1200 × 1200. |
| `mutz-intro.html` | Standalone page: open it in a browser. It has Replay, Slow motion and a timeline you can drag. |
| `record.js` | Renders the GIF, MP4 and PNG from `mutz-intro.html`. |

## In the app

The app runs its own copy of the animation, served from `app/`:

- `app/static/intro.js`: the timeline and the skip and fade logic
- `app/templates/_splash.html`: the drawing
- `app/static/app.css`: the overlay styles, at the end of the file
- `app/templates/base.html`: shows the splash on first open; `/?intro=1` replays it

If you change the animation, change it in both `mutz-intro.html` and `app/static/intro.js`, then run `record.js` again.

## Re-render the GIF and video

```bash
cd demo/mutz-intro
npm i --no-save playwright-core@1.49.1
npx playwright-core install chromium   # or set CHROME=/path/to/chrome
node record.js
```

This needs `ffmpeg` on the PATH (`brew install ffmpeg`).
