- **Day view: preview tomorrow** ✅ — the visual day-shape (`GET /day`, the
  `/day/board` timeline) now takes an `offset` (`0` today, `1` tomorrow) and the
  web board gains a **Today / Tomorrow** switch. A future day is drawn as a
  forward-looking preview: the whole waking band is available for fitting, nothing
  is dimmed as past, and no "you are here" marker is shown. The payload carries
  `day_offset`, and the CLI/monochrome render names the day it drew
  (`# Tomorrow — …`). `build_day_shape(..., day_offset=1)` walks forward whole
  local days DST-correctly and anchors "now" to the day's start.
