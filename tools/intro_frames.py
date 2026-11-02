"""Draws the arrival without playing it, so it can be looked at on any machine.

    python tools/intro_frames.py                 # docs/ui/intro*.png, docs/ui/intro_film.html
    python tools/intro_frames.py --out DIR
    python tools/intro_frames.py --at 2.5        # one frame, big, and stop

The splash is a thing that happens once, at logon, in front of everything - which makes
it the worst possible thing to iterate on by launching it. So it is rendered offscreen
here instead: a contact sheet of the whole reveal as PNG, and `intro_film.html`, the
same frames played back in the order and at the pace the splash plays them. Open the
HTML and the arrival can be watched in a browser tab, over and over, without logging
out.

It prints how much of each frame is lit, which is how a change to the timeline is
checked from a terminal: the number should grow as the letters arrive and hold steady
through the fade.
"""
import argparse
import base64
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QBuffer, QByteArray, QRect, QSize  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core import intro as plan  # noqa: E402
from ui import intro as splash  # noqa: E402

# One frame per beat, then the settled wordmark and the hold the fade works on.
MOMENTS = (0.15, 0.60, 1.10, 1.60, 2.05, 2.60, 3.60, 4.90)
# How long the page sits on the finished wordmark before fading. The splash itself
# holds it for the rest of the five seconds; this is a beat, so the film can be
# watched in a tab without waiting for it every time.
HOLD_MS = 1200


def lit(image):
    total = 0
    for y in range(0, image.height(), 3):
        for x in range(0, image.width(), 3):
            colour = image.pixelColor(x, y)
            if colour.red() + colour.green() + colour.blue() > 90:
                total += 1
    return total


def png_bytes(image):
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def contact_sheet(frames, columns=4, scale=0.5):
    """The frames as one image, labelled with the second each one is drawn at."""
    thumb_w = int(frames[0][1].width() * scale)
    thumb_h = int(frames[0][1].height() * scale)
    rows = (len(frames) + columns - 1) // columns
    sheet = QImage(QSize(thumb_w * columns, thumb_h * rows),
                   QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#05070A"))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    for index, (moment, image) in enumerate(frames):
        column, row = index % columns, index // columns
        painter.drawImage(QRect(column * thumb_w, row * thumb_h, thumb_w, thumb_h), image)
        painter.setPen(QColor("#8EC1FF"))
        painter.drawText(column * thumb_w + 8, row * thumb_h + 18, f"t = {moment:.2f}s")
    painter.end()
    return sheet


def film(frames, width, height, path):
    """The frames as a page that plays them at the pace the splash does."""
    steps = []
    previous = 0.0
    for moment, image in frames:
        delay = max(1, int(round((moment - previous) * 1000)))
        previous = moment
        steps.append({"delay": delay, "src": "data:image/png;base64," +
                      base64.b64encode(png_bytes(image)).decode("ascii")})
    payload = json.dumps(steps)
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Retro - the arrival</title>
<style>
  html, body {{ margin: 0; height: 100%; background: #000; color: #7C8798;
                font: 13px system-ui, sans-serif; overflow: hidden; }}
  #stage {{ position: absolute; inset: 0; display: grid; place-items: center; }}
  img {{ max-width: 100%; max-height: 100%; transition: opacity {plan.FADE_S}s linear; }}
  #note {{ position: absolute; left: 12px; bottom: 10px; }}
</style></head>
<body>
<div id="stage"><img id="frame" alt="Retro"></div>
<div id="note">Rendered by tools/intro_frames.py - the real reveal is {plan.INTRO_S:.0f}s,
then {plan.FADE_S:.0f}s of fade. Click to play again.</div>
<script>
const steps = {payload};
const hold = {HOLD_MS};
const fade = {int(plan.FADE_S * 1000)};
const frame = document.getElementById('frame');
let timer = null;
function play() {{
  clearTimeout(timer);
  frame.style.transition = 'none';
  frame.style.opacity = 1;
  frame.src = steps[0].src;
  let index = 0, waited = steps[0].delay;
  const step = () => {{
    index += 1;
    if (index < steps.length) {{
      frame.src = steps[index].src;
      waited += steps[index].delay;
      timer = setTimeout(step, steps[index].delay);
      return;
    }}
    // The hold, and then the window's own fade: the page does what the splash does.
    timer = setTimeout(() => {{
      frame.style.transition = `opacity ${{fade}}ms linear`;
      frame.style.opacity = 0;
      timer = setTimeout(play, fade + 700);
    }}, hold);
  }};
  timer = setTimeout(step, steps[0].delay);
}}
document.body.addEventListener('click', play);
play();
</script>
</body></html>
"""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html)
    return path


def main():
    parser = argparse.ArgumentParser(description="Render the arrival without playing it")
    parser.add_argument("--out", default=os.path.join("docs", "ui"))
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--at", type=float, default=None,
                        help="render a single frame at this second, at full size")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    os.makedirs(args.out, exist_ok=True)

    if args.at is not None:
        moment, image = splash.frames([args.at], width=1280, height=720)[0]
        path = os.path.join(args.out, f"intro_at_{moment:.2f}s.png")
        image.save(path, "PNG")
        print(f"{path}  lit={lit(image)}")
        return 0

    frames = splash.frames(MOMENTS, width=args.width, height=args.height)
    for moment, image in frames:
        path = os.path.join(args.out, f"intro_{moment:04.2f}s.png")
        image.save(path, "PNG")
        print(f"{path:34s} lit={lit(image):6d}")
    sheet = contact_sheet(frames)
    sheet_path = os.path.join(args.out, "intro.png")
    sheet.save(sheet_path, "PNG")
    print(f"{sheet_path:34s} {sheet.width()}x{sheet.height()}")
    film_path = film(frames, args.width, args.height,
                     os.path.join(args.out, "intro_film.html"))
    print(f"{film_path:34s} plays the arrival in a browser tab")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
