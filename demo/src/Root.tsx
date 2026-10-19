import { Composition } from 'remotion'
import { TOTAL } from './data'
import { FPS, H, W } from './theme'
import { Walkthrough } from './Walkthrough'
import './fonts'

// Keep this id different from every folder name in demo/: on macOS `remotion render X` can resolve to a folder x/.
export function Root() {
  return <Composition id="Walkthrough" component={Walkthrough} durationInFrames={Math.max(1, Math.round(TOTAL * FPS))} fps={FPS} width={W} height={H} />
}
