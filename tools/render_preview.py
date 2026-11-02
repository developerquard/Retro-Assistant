"""Draws the window offscreen and saves it as PNG, so it can be reviewed without a desktop.

    python tools/render_preview.py            # writes docs/ui/*.png
    python tools/render_preview.py --out DIR

It also prints how the ring's pixels are coloured in each state, which is how the
state colours are checked on a machine with no display.
"""
import argparse
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import theme  # noqa: E402
from ui.agent import WAKE_LOAD_S  # noqa: E402
from ui.app import RetroWindow  # noqa: E402


def render(widget):
    image = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(theme.BG_TRANSPARENT)
    painter = QPainter(image)
    widget.render(painter, QPoint(0, 0))
    painter.end()
    return image


def counts(image):
    blue = green = grey = lit = 0
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            if colour.alpha() < 40:
                continue
            lit += 1
            if colour.blue() > colour.green() + 20:
                blue += 1
            elif colour.green() > colour.blue() + 20:
                green += 1
            elif abs(colour.red() - colour.green()) < 24 and abs(colour.green() - colour.blue()) < 24:
                grey += 1
    return lit, blue, green, grey


def settle(window, state, amplitude=0.55, caption=""):
    window.agent.set_state(state)
    window.agent._colour = window.agent._target
    window.agent._yaw = 0.9                     # a quarter turn in, not edge-on
    window.agent._pitch = 0.55
    window.agent._amplitude = amplitude
    window.agent.set_level(0.08)
    if caption:
        window.agent.set_caption(caption)
    window._on_state(state, "")
    window._on_state(state, caption)            # the panel draws the phase as a caption
    QApplication.processEvents()


WRITING_TEXT = (
    "Anime is the most popular animated form on the planet, and the reason is not one "
    "show. It is the way a single season can move between a quiet kitchen and a city "
    "falling apart, and still feel like the same story being told. The audience came "
    "for the fights and stayed for the quiet episodes, which is the part nobody plans "
    "for when a series is commissioned.")


def show_writing(window):
    """The writing stage as it looks mid-document: two of four sections written."""
    window.writing.begin("The most popular anime", 4, 200)
    window.writing.writing(WRITING_TEXT)
    window.writing.section_done(1, 4, 60)
    window.writing.section(2, 4, "Why it travels")
    window.agent.set_arc(0.25)
    window.agent.set_caption("WRITING")
    window._fit_panel()
    QApplication.processEvents()
    return render(window)


def show_paused(window):
    """The card after a stop: amber, PAUSED, with the words that were kept."""
    window.writing.begin("The most popular anime", 4, 200)
    window.writing.writing(WRITING_TEXT)
    window.writing.pause("The most popular anime", 2, 4, 118)
    window.agent.set_arc(None)
    window.agent.set_caption("PAUSED")
    window._fit_panel()
    QApplication.processEvents()
    return render(window)


SHELF_DOCUMENTS = (
    ("The most popular anime.md", "Anime travelled because a single season can hold a quiet "
                                  "kitchen and a city falling apart."),
    ("Growing tomatoes.md", "Tomatoes want sun, water and something to climb, and they repay "
                            "all three within a season."),
    ("Black holes.md", "A black hole is not a hole. It is a great deal of matter in a small "
                       "enough place that nothing leaves."),
)


def show_shelf(window):
    """The strip when nothing is being written: what Retro has already written.

    Drawn against a throwaway workspace, because this shot needs documents to exist and
    no preview should leave files among the user's own.
    """
    import tempfile
    from pathlib import Path

    from core import files

    restore_shelf(window)
    real = files.WORKSPACE
    files.WORKSPACE = Path(tempfile.mkdtemp(prefix="retro-preview-"))
    try:
        for name, body in SHELF_DOCUMENTS:
            (files.WORKSPACE / name).write_text(f"# {name[:-3]}\n\n{body}\n", encoding="utf-8")
        window.writing.reset()
        window.agent.set_arc(None)
        window.agent.set_caption("SAY 'RETRO'")
        window._refresh_shelf()
        QApplication.processEvents()
        return render(window)
    finally:
        files.WORKSPACE = real
        window._refresh_shelf()


def empty_shelf(window):
    """No documents, so the panel is the canonical height and the shot is about the
    new surfaces rather than whatever this machine has written."""
    if not hasattr(window, "_preview_documents"):
        window._preview_documents = window.bridge.session.documents
    window.bridge.session.documents = lambda limit=8: []
    window._refresh_shelf()


def restore_shelf(window):
    """Give the session back its own document list, for the shot that needs it."""
    prior = getattr(window, "_preview_documents", None)
    if prior is not None:
        window.bridge.session.documents = prior
        del window._preview_documents


def show_talk(window):
    """The conversation: what was asked, what came back, and who said it."""
    empty_shelf(window)
    window._toggle_settings(False)
    window.talk.clear()
    window.talk.add("user", "Write a short note about today's launch")
    window.talk.add("retro", "Drafted it and saved today-note.md - 180 words in three "
                             "sections: the launch, the numbers, and what to watch "
                             "tomorrow.")
    window.talk.add("user", "Now make the numbers a table")
    # The microphone on and hearing something, so the level meter in the strip is in
    # the shot: a widget that only exists while a person talks is easy to get wrong.
    window.status.set_mic(True)
    window._on_level(0.62)
    for _ in range(12):
        window.status.meter._ease()
    window._on_state("listening", "ready")
    window.agent.set_caption("LISTENING")
    window._fit_panel()
    QApplication.processEvents()
    return render(window)


def show_settings(window):
    """The drawer, open: the things that are touched once a week."""
    empty_shelf(window)
    window.talk.clear()
    window._on_level(0.0)                       # a quiet microphone, at rest
    for _ in range(30):
        window.status.meter._ease()
    window._toggle_settings(True)
    window._on_state("asleep", "")
    window.agent.set_caption("SAY 'RETRO'")
    window._fit_panel()
    QApplication.processEvents()
    return render(window)


def show_task(window):
    """A task in flight: the ticking row in the record, and STOP on the strip."""
    empty_shelf(window)
    window._toggle_settings(False)
    window.talk.clear()
    window.talk.add("user", "Summarise what is on my screen")
    # Quiet microphone: the meter is in the shot, at rest, next to a running task.
    window._on_level(0.0)
    for _ in range(30):
        window.status.meter._ease()
    window._on_state("working", "reading the screen")
    window.agent.set_caption("WORKING")
    QApplication.processEvents()
    return render(window)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "ui"))
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    # The offscreen platform starts with an empty font database on this machine, and
    # an empty one draws every glyph as a box - a preview that cannot be read is not a
    # preview. One real family is enough for the text to mean something.
    from PySide6.QtGui import QFontDatabase
    for candidate in ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(candidate):
            QFontDatabase.addApplicationFont(candidate)
    window = RetroWindow(floating=False, voice=False)
    window._load_voices()

    shots = []
    settle(window, "asleep", caption="SAY 'RETRO'")
    shots.append(("panel_asleep", render(window)))
    shots.append(("panel_talk", show_talk(window)))
    shots.append(("panel_task", show_task(window)))
    shots.append(("panel_settings", show_settings(window)))
    window._toggle_settings(False)
    window.talk.clear()
    settle(window, "working", caption="WORKING")
    shots.append(("panel_working", render(window)))
    shots.append(("panel_writing", show_writing(window)))
    shots.append(("panel_paused", show_paused(window)))
    window.writing.reset()
    window._fit_panel()
    QApplication.processEvents()
    shots.append(("panel_shelf", show_shelf(window)))

    # The floating circle: everything hidden but the ring, on a transparent canvas.
    window.agent.set_arc(None)          # no document is being written in these shots
    window.set_floating(True)
    for state, name in (("offline", "offline"), ("asleep", "idle"), ("listening", "listening"),
                        ("working", "working")):
        settle(window, state, caption="WORKING" if name == "working" else "")
        shots.append((f"float_{name}", render(window)))
    # The wake, caught halfway through: the same orb walking out of the grey it slept
    # in towards the blue it will be, which is the three seconds the owner asked for
    # and the only moment in these shots where the colour is neither one.
    settle(window, "asleep", caption="WAKING UP")   # so the walk starts from the grey
    window.agent.set_state("loading")
    window.agent._load_started = time.monotonic() - WAKE_LOAD_S * 0.5
    window.agent._colour = QColor(window.agent._ramp_target())
    shots.append(("float_loading", render(window)))

    # The two looks, with the room playing. The audio is fed in exactly the way the
    # window feeds it - a real level and a real spectrum from the system's own output -
    # so these two shots are the sound-reactive waves doing their job, not a still of them.
    song = [0.92] * 3 + [0.16] * 6 + [0.72] * 3
    for name, mode in (("music", "music"), ("media", "media")):
        window.agent.set_visual_mode(mode)
        settle(window, "asleep", caption="")
        window.agent.set_visual(True)          # the room is playing, not the microphone
        for _ in range(14):                    # let the envelope and the body ease in
            window.agent.set_audio(0.85, song)
            window.agent._tick()
        QApplication.processEvents()
        shots.append((f"float_{name}", render(window)))
    window.agent.set_visual(False)
    window.agent.set_visual_mode("music")

    for name, image in shots:
        path = os.path.join(args.out, f"{name}.png")
        image.save(path)
        lit, blue, green, grey = counts(image)
        print(f"{name:16s} {image.width()}x{image.height()}  lit={lit:7d}  "
              f"blue={blue:7d}  green={green:7d}  grey={grey:7d}  -> {path}")

    write_html(shots, os.path.join(args.out, "preview.html"))
    app.quit()
    return 0


TITLES = {
    "float_music": "Music Mode - the waves and the body answer the sound, in slow colour",
    "float_media": "Media Mode - white body; the waves are the sound-reactive element",
    "panel_asleep": "Settings panel - asleep (grey, and it does not move)",
    "panel_working": "Settings panel - working (green)",
    "panel_talk": "The conversation: every turn kept - SPEAK on a reply, RETRY on a request, "
                  "and the microphone meter in the strip",
    "panel_task": "A task in flight: the ticking row in the record, STOP on the strip, the "
                  "meter at rest",
    "panel_settings": "The drawer: model, voice, microphone, circle size - folded away by default",
    "panel_writing": "Settings panel - writing a document (live stage, rings arc)",
    "panel_paused": "Settings panel - a stopped document (amber PAUSED, words kept)",
    "panel_shelf": "Settings panel - the shelf: what Retro has already written",
    "float_offline": "Floating circle - offline (grey)",
    "float_idle": "Floating circle - asleep (grey, and it does not move)",
    "float_listening": "Floating circle - listening (blue)",
    "float_loading": "Floating circle - waking up: half way from grey to blue",
    "float_working": "Floating circle - working (green)",
}


def write_html(shots, path):
    """One page with the images embedded, so it needs no server and no other files."""
    import base64

    from PySide6.QtCore import QBuffer

    cards = []
    for name, image in shots:
        # QImage writes to a QIODevice, not a Python file object.
        buffer = QBuffer()
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        encoded = base64.b64encode(bytes(buffer.data())).decode("ascii")
        cards.append(f'<figure><img src="data:image/png;base64,{encoded}" alt="{name}">'
                     f"<figcaption>{TITLES.get(name, name)}</figcaption></figure>")
    html = f"""<!doctype html><meta charset="utf-8"><title>Retro UI preview</title>
<style>
  body {{ background:#07090c; color:#c8d0dc; font:14px/1.5 system-ui, sans-serif;
         margin:0; padding:28px; }}
  h1 {{ font-size:18px; font-weight:600; color:#eef2f8; margin:0 0 4px; }}
  p  {{ margin:0 0 22px; color:#7c8798; }}
  main {{ display:flex; flex-wrap:wrap; gap:26px; }}
  figure {{ margin:0; }}
  img {{ display:block; border-radius:8px; background:#0b0e13;
        box-shadow:0 6px 26px rgba(0,0,0,.55); }}
  figcaption {{ margin-top:8px; color:#8b95a6; font-size:12px; }}
</style>
<h1>Retro UI preview</h1>
<p>Rendered offscreen by tools/render_preview.py, at 1:1 scale.</p>
<main>{''.join(cards)}</main>
"""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html)
    print(f"preview page: {path}")


if __name__ == "__main__":
    sys.exit(main())
