"""The window builds, draws and switches shape - checked offscreen, without a desktop.

These run on the Qt "offscreen" platform, so they work on a build machine with no
display. They do not load Whisper, the model or a real voice: the session is never
started, which is also why this file is quick.
"""
import json
import math
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from core import config  # noqa: E402

pytest.importorskip("PySide6", reason="the window needs PySide6")

from PySide6.QtCore import QPoint, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit  # noqa: E402

from ui import theme  # noqa: E402
from ui import agent as agent_module  # noqa: E402
from ui.agent import AgentWidget  # noqa: E402
from ui.app import PANEL_H, PANEL_W, RetroWindow, asset_path  # noqa: E402
from ui.app import FLOAT_WINDOW_SCALE  # noqa: E402
from ui.writing import SHELF_HEIGHT  # noqa: E402


@pytest.fixture(scope="module")
def qt():
    app = QApplication.instance() or QApplication([])
    yield app


def render(widget, size=None):
    """Draws a widget into an image, exactly as the desktop would."""
    size = size or widget.size()
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(theme.BG_TRANSPARENT)
    widget.resize(size)
    painter = QPainter(image)
    widget.render(painter, QPoint(0, 0))
    painter.end()
    return image


def coloured(image, predicate):
    return sum(1 for y in range(image.height()) for x in range(image.width())
               if predicate(image.pixelColor(x, y)))


def test_icon_is_packaged(qt):
    assert os.path.exists(asset_path("retro.ico")), "build the icon with tools/make_icon.py"
    assert os.path.getsize(asset_path("retro.ico")) > 1000


def test_agent_ring_draws_in_every_state(qt):
    agent = AgentWidget()
    agent.setFixedSize(QSize(200, 200))
    for state in ("offline", "asleep", "listening", "working", "speaking"):
        agent.set_state(state)
        agent._amplitude = 0.6     # as if a level had just arrived
        image = render(agent)
        lit = coloured(image, lambda c: c.alpha() > 40)
        assert lit > 1000, f"{state}: the ring barely drew anything ({lit} pixels)"


def test_the_agent_is_a_transparent_ball_of_dots(qt):
    """The thing the owner asked for, after seeing a solid one: see-through dots.

    This test used to demand a *solid* disc, because the version before it was a
    hollow ring. That is no longer the specification - the owner asked for transparent
    dots - so the sphere is a lattice with air between the points, and the far side is
    meant to show through the near one. Three ways a broken lattice would still be
    caught: nothing in the middle, dots so sparse it reads as a swarm, or one opaque
    fill that is not transparent at all.
    """
    agent = AgentWidget()
    agent.setFixedSize(QSize(240, 240))
    agent.set_state("working")
    agent._colour = agent._target
    image = render(agent)
    middle = image.pixelColor(120, 120)
    assert middle.alpha() > 25, f"the middle of the sphere is empty (alpha {middle.alpha()})"
    assert middle.alpha() < 210, (
        f"the middle is opaque (alpha {middle.alpha()}): the far side has to show "
        f"through the near one")

    lit = [(x, y) for y in range(240) for x in range(240)
           if image.pixelColor(x, y).alpha() > 25]
    assert lit, "nothing was drawn"
    xs = [x for x, _ in lit]
    ys = [y for _, y in lit]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    reach = min(max(xs) - min(xs), max(ys) - min(ys)) / 2.0
    inside = covered = 0
    alphas = set()
    for y in range(240):
        for x in range(240):
            if ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 > reach * 0.70:
                continue
            inside += 1
            alpha = image.pixelColor(x, y).alpha()
            alphas.add(alpha)
            if alpha > 40:
                covered += 1
    share = covered / float(inside)
    assert 0.10 < share < 0.85, (
        f"{share:.0%} of the inside is covered: a sphere of dots sits between a swarm "
        f"and a solid fill")
    # Depth order with translucency: the alphas differ, because near and far dots
    # overlap to different depths. One flat fill would show a handful of levels.
    assert len(alphas) > 25, f"only {len(alphas)} levels of brightness: no depth"


def test_the_edge_of_the_sphere_is_a_dotted_circle_and_nothing_else(qt):
    """"It doesn't look like a real, perfect dotted circle" - the owner, looking at it.

    Two things made the edge ragged, and both are measured here. The volume reached
    almost to the skin, so the dots just inside the outline sat on top of the outline
    and mottled it; and the skin's own arrangement is what the eye reads as the shape.
    So: nothing but the skin comes within a tenth of the edge, and the ring the skin
    draws is evenly dotted with no bald patch in it. Measured off the geometry rather
    than off the pixels, because a picture of a dotted ring is mostly gaps.
    """
    agent = AgentWidget()
    agent.setFixedSize(QSize(240, 240))
    reach = agent._radius(240)
    centre = 120.0
    placed = []
    for index, (x, y, z) in enumerate(agent_module.DOTS_AT):
        nx, ny, nz = agent._turn(x, y, z, agent.seconds())
        px, py, _depth = agent._project(nx, ny, nz, centre, centre, reach)
        placed.append((math.hypot(px - centre, py - centre), index < agent_module.SHELL_DOTS))
    edge = max(r for r, skin in placed if skin)
    inside = max(r for r, skin in placed if not skin)
    assert inside < edge * 0.92, (
        f"a dot inside the ball reaches {inside:.1f}px against the edge's {edge:.1f}px: "
        f"the outline is drawn by dots that are not part of it")
    angles = []
    for index, (x, y, z) in enumerate(agent_module.DOTS_AT):
        if index >= agent_module.SHELL_DOTS:
            continue
        nx, ny, nz = agent._turn(x, y, z, agent.seconds())
        px, py, _depth = agent._project(nx, ny, nz, centre, centre, reach)
        if math.hypot(px - centre, py - centre) > edge * 0.92:
            angles.append(math.atan2(py - centre, px - centre))
    angles.sort()
    gaps = [b - a for a, b in zip(angles, angles[1:])]
    assert len(angles) > 100, f"the outline is only {len(angles)} dots: not a circle"
    average = sum(gaps) / len(gaps)
    assert max(gaps) < average * 6.0, (
        f"the outline has a bald patch: a {math.degrees(max(gaps)):.1f} degree gap "
        f"against an average of {math.degrees(average):.1f}")


def test_the_outline_is_one_ring_of_dots_at_exactly_one_radius(qt):
    """The line the eye reads as the shape, pinned to the projection that makes it.

    Dots spread over the sphere's surface cannot draw this line: projected, they bunch
    where the surface turns edge-on, and each dot's own size moves the outermost pixel
    of the shape. So the edge is placed instead - one dot per equal slice of a circle,
    all at the radius the projection actually has - and this checks that radius is the
    real one rather than a number that looked right.
    """
    agent = AgentWidget()
    agent.setFixedSize(QSize(240, 240))
    now = agent.seconds()
    widest = 0.0
    for lat in range(-90, 91, 2):
        for lon in range(0, 360, 3):
            rise, walk = math.radians(lat), math.radians(lon)
            x = math.cos(rise) * math.cos(walk)
            y = math.cos(rise) * math.sin(walk)
            nx, ny, nz = agent._turn(x, y, math.sin(rise), now)
            px, py, _depth = agent._project(nx, ny, nz, 0.0, 0.0, 1.0)
            widest = max(widest, math.hypot(px, py))
    assert abs(widest - agent_module.SILHOUETTE) < 0.006, (
        f"the ring is drawn at {agent_module.SILHOUETTE:.3f} of the radius while the "
        f"ball's own edge is at {widest:.3f}")
    # Even by construction, and fine enough that no gap reads as a break in the line.
    gap = math.tau / agent_module.RING_DOTS
    assert gap < 0.07, f"{agent_module.RING_DOTS} dots leaves a {math.degrees(gap):.1f} degree gap"
    assert agent_module.SHELL_REACH < 1.0, "the skin must stay behind the ring it is inside"


def test_asleep_the_agent_does_not_move_at_all(qt):
    """"Retro, sleep" is a stopped drawing, not a slower one.

    The owner asked for sleep to mean unloaded: grey, and nothing moving. A frozen
    clock is what makes that checkable - the same instant must produce the same
    geometry, however many frames go by - and the colour has to walk to grey.
    """
    agent = AgentWidget()
    agent.setFixedSize(QSize(240, 240))
    agent.set_state("listening")
    agent._colour = QColor(theme.STATE_COLOURS["listening"])
    agent.set_state("asleep")
    assert agent.asleep_still(), "sleep must freeze the drawing"
    first = [tuple(round(value, 3) for value in point)
             for point in agent._points(120, 120, 80, agent._now())]
    for _ in range(30):
        agent._tick()
    again = [tuple(round(value, 3) for value in point)
             for point in agent._points(120, 120, 80, agent._now())]
    assert first == again, "the orb is still turning while asleep"
    for _ in range(40):
        agent._tick()
    # And the blue it was listening in walks all the way to the theme's grey - not
    # close to it: rounding used to leave it stuck a few levels short for ever.
    assert agent._colour == QColor(theme.STATE_COLOURS["asleep"]), \
        f"asleep is not the grey: {agent._colour.getRgb()}"
    assert agent._colour.blue() - agent._colour.red() < 40, "asleep must not still be blue"


def test_waking_walks_the_colour_from_grey_to_blue_over_three_seconds(qt):
    """The owner asked for a slow load: three seconds, grey turning blue, then ready."""
    from ui.agent import WAKE_LOAD_S

    agent = AgentWidget()
    agent.set_state("asleep")
    agent._colour = QColor(theme.STATE_COLOURS["asleep"])
    agent.set_state("loading")
    assert agent.load_fraction() < 0.2, "a load cannot be finished the instant it starts"
    started = QColor(agent._ramp_target())          # grey, just started
    agent._load_started = time.monotonic() - WAKE_LOAD_S * 0.5
    halfway = QColor(agent._ramp_target())
    assert halfway.red() < started.red(), "halfway through, it is no longer all grey"
    # And the target it is walking towards is the blue, not the grey it came from.
    assert agent._target == QColor(theme.STATE_COLOURS["loading"])
    agent.set_state("listening")
    assert agent._target == QColor(theme.STATE_COLOURS["listening"])
    agent._load_from = QColor(80, 80, 80)
    agent._load_started = time.monotonic() - WAKE_LOAD_S - 0.1
    assert agent.load_fraction() == 1.0, "the ramp must finish"
    assert agent._ramp_target() == agent._target


def test_a_loud_voice_deforms_the_orb_it_is_not_just_a_level_bar(qt):
    """The level moves the surface itself, which is what the audio is for."""
    agent = AgentWidget()
    agent.setFixedSize(QSize(240, 240))
    agent.set_state("speaking")
    agent._colour = agent._target
    quiet = render(agent)
    for _ in range(20):
        agent.set_level(0.9)
        agent._tick()
    loud = render(agent)
    assert lit(loud) > lit(quiet), "a loud level should push the surface out"


def test_the_frame_rate_drops_when_nothing_is_happening(qt):
    """An idle orb is not worth sixty frames a second for the whole session."""
    from ui.agent import FPS, FPS_IDLE

    agent = AgentWidget()
    agent.set_state("working")
    assert agent._timer.interval() == int(1000 / FPS)
    agent.set_state("asleep")
    assert agent._timer.interval() == int(1000 / FPS_IDLE)
    agent.set_state("speaking")
    assert agent._timer.interval() == int(1000 / FPS)


def test_agent_colour_follows_state(qt):
    agent = AgentWidget()
    agent.setFixedSize(QSize(200, 200))
    for state, channel in (("working", "green"), ("listening", "blue")):
        agent.set_state(state)
        agent._colour = agent._target
        image = render(agent)
        green = coloured(image, lambda c: c.green() > c.blue() + 20 and c.alpha() > 60)
        blue = coloured(image, lambda c: c.blue() > c.green() + 20 and c.alpha() > 60)
        if channel == "green":
            assert green > blue, "working should read green"
        else:
            assert blue > green, "idle should read blue"


def test_the_body_opens_with_the_real_level(qt):
    agent = AgentWidget()
    agent.setFixedSize(QSize(200, 200))
    agent.set_state("listening")
    quiet = render(agent)
    for _ in range(20):                 # let the level ease through
        agent.set_level(0.9)
        agent._tick()
    loud = render(agent)
    assert agent._amplitude > 0.6, "a loud level should swell the body"
    assert coloured(loud, lambda c: c.alpha() > 60) > coloured(quiet, lambda c: c.alpha() > 60)


def lit(image):
    return coloured(image, lambda c: c.alpha() > 60)


def advance(agent, seconds, level=0.0, bands=None, step=0.05):
    """Runs the widget's own clock forward by `seconds`, a frame at a time.

    Every animation here is driven by elapsed time rather than by a frame counter -
    that is the whole point of it - so a test that calls `_tick` in a tight loop is
    measuring microseconds and can only ever see zero. Moving the instant the widget
    measures *from* is what makes the rate checkable at all.
    """
    for _ in range(max(1, int(round(seconds / step)))):
        # Set, not shifted: a shifted clock also carries however long the test itself
        # took to get here, which is microseconds in one run and milliseconds in a
        # loaded one - and this is the clock the rate is measured against.
        agent._since_tick = time.monotonic() - step
        agent.set_level(level)
        if bands is not None:
            agent.set_audio(level, bands)
        agent._tick()


def frame_pixels(agent, mode):
    """The pixels the frame - now the waves - adds to an image, and nothing else.

    Both images are drawn at the *same instant* with `_waves` taken out of the second
    one, so the only difference between them is the line itself - which matters because
    the body turns while it is drawn and a turning body would be its own difference.
    """
    agent.set_visual_mode(mode)
    clock = agent.seconds
    agent.seconds = lambda: 1.25
    try:
        with_frame = render(agent)
        drawn = agent._waves
        agent._waves = lambda *args: None
        try:
            bare = render(agent)
        finally:
            agent._waves = drawn
    finally:
        agent.seconds = clock
    return [(x, y, with_frame.pixelColor(x, y))
            for y in range(with_frame.height()) for x in range(with_frame.width())
            if with_frame.pixelColor(x, y).alpha() > bare.pixelColor(x, y).alpha() + 40]


def test_speaking_with_no_audio_does_not_mime_a_voice(qt):
    """The bug this replaced: the ring animated a voice nobody could hear."""
    agent = AgentWidget()
    agent.setFixedSize(QSize(200, 200))
    agent.set_state("speaking")
    for _ in range(30):
        agent._tick()
    assert agent._amplitude == 0.0, "silence must draw a flat line, not a performance"


def test_the_body_settles_again_when_the_sound_stops(qt):
    agent = AgentWidget()
    agent.set_state("speaking")
    for _ in range(20):
        agent.set_level(0.9)
        agent._tick()
    assert agent._amplitude > 0.6
    for _ in range(60):
        agent.set_level(0.0)
        agent._tick()
    assert agent._amplitude == 0.0


def test_the_frame_answers_the_rooms_own_audio(qt):
    """Background hearing: the frame is what reacts, and there are no bars anywhere.

    The owner asked for this in as many words - the outer rounded border is the
    sound-reactive element, and a ring of bars is explicitly not wanted. The audio here
    is the system's own output, never the microphone, and the pixels the frame adds are
    measured with the frame itself taken out of the other image.
    """
    agent = AgentWidget()
    agent.setFixedSize(QSize(240, 240))
    agent.set_state("asleep")
    agent.set_visual(True)
    quiet = lit(render(agent))
    talking = [0.02] * 3 + [0.90] * 6 + [0.02] * 3      # a voice: all of it in the middle
    advance(agent, 0.6, level=0.5, bands=talking)
    assert max(agent._env) > 0.3, "the envelope did not follow the room"
    assert lit(render(agent)) > quiet, "the frame did not answer the room"
    assert not hasattr(agent, "_bars"), "the bar ring is gone, not merely switched off"
    pixels = frame_pixels(agent, "media")
    assert pixels, "the frame drew nothing at all"
    assert all(colour.blue() >= colour.red() for _x, _y, colour in pixels), \
        "Media Mode's frame is white and blue, never a cycling colour"


def test_music_mode_walks_the_frame_through_the_hue_wheel_slowly(qt):
    """The other half: Music Mode's colour moves, and at a stroll rather than a strobe.

    The hue used to advance by `0.004 + 0.03 * amplitude` per *frame* - a wheel every
    five seconds at rest and every third of a second at full volume, with loudness doing
    the driving. It is one slow turn per `HUE_TURN_S` now, off the clock.
    """
    agent = AgentWidget()
    agent.setFixedSize(QSize(240, 240))
    agent.set_state("asleep")
    agent.set_visual(True)
    song = [0.95] * 3 + [0.10] * 6 + [0.85] * 3
    advance(agent, 0.5, level=0.9, bands=song)
    first = agent._frame_colour(theme.GREY, 0.5).hue()          # degrees, 0..359
    advance(agent, 12.0, level=0.9, bands=song)
    second = agent._frame_colour(theme.GREY, 0.5).hue()
    walked = abs(second - first)
    expected = 360.0 * 12.0 / agent_module.HUE_TURN_S
    assert walked > 0, "Music Mode's colour is meant to move"
    assert 0.5 * expected <= walked <= (1.0 + agent_module.HUE_AUDIO_SHARE) * expected + 4, \
        (f"{walked} degrees of the wheel in twelve seconds, against the {expected:.0f} "
         f"that one slow turn per {agent_module.HUE_TURN_S:.0f}s allows")


def test_the_waves_around_the_core_answer_the_sound(qt):
    """The owner's ask, measured on the pixels: waves around the core, not a box.

    Three geometric facts, all from the same two renders. The waves reach past the
    ball, so they are *around* it rather than a border it is drawn inside. They are
    round - corner-over-side is 1.00 - because the rounded-square outline that used to
    be here read as exactly the widget box the owner rejected. And they answer the
    room: the loud image has far more lit pixels, a higher peak opacity, and more
    rings along the axis than the quiet one.

    The rings are also checked as geometry, not just assumed: each wave of the three
    sits a clear gap outside the one before it, which is what makes the picture read
    as concentric ripples rather than as one thicker circle.
    """
    def measure(level, bands):
        agent = AgentWidget()
        agent.setFixedSize(QSize(374, 374))
        agent.set_state("asleep")
        agent.set_visual(True)
        advance(agent, 3.0, level=level, bands=bands)
        pixels = frame_pixels(agent, "media")
        centre = 187.0
        corner = max(math.hypot(x - centre, y - centre) for x, y, _c in pixels)
        side = max(abs(x - centre) for x, y, _c in pixels)
        rings = [agent._wave_reach(ring, 0.2, 1.25, 0.95)
                 for ring in range(agent_module.WAVE_RINGS)]
        return (len(pixels), max(c.alpha() for _x, _y, c in pixels),
                corner / max(side, 1.0), corner / agent._radius(374), rings)

    quiet = measure(0.0, [0.0] * 12)
    loud = measure(0.95, [0.92] * 3 + [0.16] * 6 + [0.72] * 3)
    assert quiet[0] > 200, "the waves must be drawn even in silence"
    assert quiet[3] > 1.10, \
        f"the waves sit inside the ball, not around it ({quiet[3]:.2f} radii)"
    assert loud[0] > quiet[0] * 1.6, f"the waves barely moved: {quiet[0]} -> {loud[0]}"
    assert loud[1] > quiet[1] + 60, "and they did not brighten with the room"
    assert loud[2] < 1.06, \
        f"the waves are a box, not waves (corner/side {loud[2]:.2f})"
    assert agent_module.WAVE_RINGS >= 3, "waves, plural"
    assert loud[4] == sorted(loud[4]), "the waves are ordered, innermost first"
    assert all(b - a > 0.08 for a, b in zip(loud[4], loud[4][1:])), \
        f"the waves are one outline, not concentric rings ({loud[4]})"


def test_the_turn_is_one_slow_rate_and_the_audio_cannot_race_it(qt):
    """The owner's complaint, as a number: it spun fast, and faster with the sound."""
    quiet, loud = AgentWidget(), AgentWidget()
    for agent in (quiet, loud):
        agent.set_state("listening")
        agent.set_visual(True)
    advance(quiet, 1.0, level=0.0, bands=[0.0] * 12)
    advance(loud, 1.0, level=1.0, bands=[1.0] * 12)
    assert quiet._angle == pytest.approx(1.0 / agent_module.TURN_S, rel=0.02), \
        "one second of the clock is one second of the turn"
    assert loud._angle == pytest.approx(quiet._angle, rel=0.02), \
        "the volume may not touch the rotation"
    assert agent_module.TURN_S >= 20.0, "and the one rate is a slow one"


def test_switching_the_look_moves_nothing_but_the_colour(qt):
    """A mode change may not restart, teleport or re-stream anything."""
    agent = AgentWidget()
    agent.set_state("listening")
    agent.set_visual(True)
    advance(agent, 0.5, level=0.4, bands=[0.4] * 12)
    angle, hue, env = agent._angle, agent._hue, list(agent._env)
    agent.set_visual_mode("media")
    assert agent.visual_mode == "media"
    assert agent._angle == angle, "the look is not the clock: nothing is reset"
    assert agent._env == env, "and the audio it is answering is untouched"
    assert agent._hue == hue
    agent.set_visual_mode("music")
    assert agent.visual_mode == "music"
    assert agent._angle == angle


def test_music_mode_walks_the_colour_and_media_mode_never_cycles_it(qt):
    music = AgentWidget()
    music.set_state("asleep")
    music.set_visual(True)
    advance(music, 1.0, level=0.0, bands=[0.0] * 12)
    assert 0.0 < music._hue < 0.03, \
        f"a hue wheel in a second is a strobe, not a gradient ({music._hue})"
    media = AgentWidget()
    media.set_state("asleep")
    media.set_visual(True)
    media.set_visual_mode("media")
    advance(media, 5.0, level=1.0, bands=[1.0] * 12)
    assert media._hue == 0.0, "Media Mode does not cycle colour at all"


def test_the_body_stays_white_in_media_mode(qt):
    """Media Mode: the body is Retro's own colour and the hue wheel does not touch it.

    The point is not that some particular grey is white - it is that the body's colour
    owes *nothing* to the cycling hue. Setting the wheel somewhere colourful and finding
    the same dots is the whole of "no RGB accents on the body in Media Mode".
    """
    agent = AgentWidget()
    agent.setFixedSize(QSize(240, 240))
    agent.set_state("asleep")
    agent.set_visual(True)
    agent.set_visual_mode("media")
    advance(agent, 0.6, level=1.0, bands=[1.0] * 12)
    agent._hue = 0.0
    plain = [tone.rgb() for tone in agent._tones()]
    agent._hue = 0.34                        # a green the body would wear in Music Mode
    coloured = [tone.rgb() for tone in agent._tones()]
    assert plain == coloured, "the body's colour is not the hue wheel in Media Mode"
    agent.set_visual_mode("music")
    agent._hue = 0.34
    assert [tone.rgb() for tone in agent._tones()] != plain, \
        "while Music Mode's body does wear it"


def test_the_visualiser_never_moves_the_orb_while_the_room_is_silent(qt):
    """Decoration, not activity: silence must not look like something is playing."""
    agent = AgentWidget()
    agent.setFixedSize(QSize(240, 240))
    agent.set_state("asleep")
    agent.set_visual(True)
    for _ in range(20):
        agent.set_audio(0.0, None)
        agent._tick()
    assert agent._amplitude == 0.0


def test_window_has_the_controls(qt, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODEL_LIST_CACHE", tmp_path / "groq.models.json")
    monkeypatch.setattr(config, "_listing", None)
    window = RetroWindow(floating=False, voice=False)
    keys = [window.model_combo.itemData(i) for i in range(window.model_combo.count())]
    # The cloud brains come first, quickest first, and the local GGUFs are the offline
    # fallback rather than the default. With no cached listing these are the built-in
    # rows - see config.FALLBACK_MODELS.
    assert keys == ["auto", "qwen/qwen3.8-27b", "openai/gpt-oss-120b",
                    "openai/gpt-oss-20b", "1b", "500m"]
    assert window.voice_combo.count() >= 1
    assert window.prompt.placeholderText() == "Type command for Retro..."
    assert not window.float_btn.isChecked()
    assert window.output_combo.count() >= 1, "there must be an output device to pick"
    assert window.test_btn.text() == "TEST VOICE"
    # The key field exists, and never shows the value back.
    assert window.key_input.echoMode() == QLineEdit.EchoMode.Password
    assert "groq.com" in window.key_input.placeholderText()
    window.close()


def test_the_model_picker_offers_every_model_the_key_can_call(qt, tmp_path, monkeypatch):
    """The request this picker exists for: all of them, selectable, quickest first."""
    cache = tmp_path / "groq.models.json"
    cache.write_text(json.dumps(["whisper-large-v3", "openai/gpt-oss-20b",
                                 "qwen/qwen3.8-27b"]), encoding="utf-8")
    monkeypatch.setattr(config, "MODEL_LIST_CACHE", cache)
    monkeypatch.setattr(config, "_listing", None)
    monkeypatch.setattr(config, "cloud_available", lambda: True)
    window = RetroWindow(floating=False, voice=False)
    combo = window.model_combo
    assert [combo.itemData(i) for i in range(combo.count())] == [
        "auto", "qwen/qwen3.8-27b", "openai/gpt-oss-20b", "whisper-large-v3",
        "1b", "500m"]
    # A chat model is selectable and its row says how fast it answers.
    chat = combo.model().item(combo.findData("qwen/qwen3.8-27b"))
    assert chat.isEnabled() and "first word" in chat.toolTip()
    # One that cannot answer a chat request is shown and greyed, not hidden.
    other = combo.model().item(combo.findData("whisper-large-v3"))
    assert not other.isEnabled()
    assert "cannot answer a chat request" in other.toolTip()
    window.close()


def test_a_nickname_saved_by_an_older_build_selects_the_model_it_means(qt, tmp_path,
                                                                       monkeypatch):
    monkeypatch.setattr(config, "MODEL_LIST_CACHE", tmp_path / "groq.models.json")
    monkeypatch.setattr(config, "_listing", None)
    window = RetroWindow(floating=False, voice=False)
    window.bridge.session.model_choice = "groq"           # what settings.json still holds
    window._select_current_model()
    assert window.model_combo.currentData() == config.CLOUD_MODELS["groq"]
    window.close()


def test_a_model_that_is_no_longer_offered_falls_back_to_auto(qt, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODEL_LIST_CACHE", tmp_path / "groq.models.json")
    monkeypatch.setattr(config, "_listing", None)
    window = RetroWindow(floating=False, voice=False)
    window.bridge.session.model_choice = "vendor/retired-model"
    window._select_current_model()
    assert window.model_combo.currentData() == "auto"
    window.close()


def test_the_listing_arriving_rebuilds_the_rows(qt, tmp_path, monkeypatch):
    """Groq answers after the window is up, and the picker fills in behind it."""
    cache = tmp_path / "groq.models.json"
    monkeypatch.setattr(config, "MODEL_LIST_CACHE", cache)
    monkeypatch.setattr(config, "_listing", None)
    window = RetroWindow(floating=False, voice=False)
    assert window.model_combo.findData("allam-2-7b") == -1       # no listing yet
    cache.write_text(json.dumps(["allam-2-7b"]), encoding="utf-8")
    monkeypatch.setattr(config, "_listing", None)
    window._on_models_listed()
    assert window.model_combo.findData("allam-2-7b") != -1
    window.close()


def test_the_listing_refresh_survives_the_window_closing_under_it(qt, tmp_path, monkeypatch):
    """The fetch runs on a daemon thread and can outlive the window it reports to.

    Closing Retro while the listing is in flight deletes the QObject underneath it;
    emitting into a deleted object raises RuntimeError out of that thread, which on a
    windowed build is a crash report at shutdown for a dropdown nobody is looking at.
    """
    import shiboken6

    from ui.bridge import Bridge

    monkeypatch.setattr(config, "MODEL_LIST_CACHE", tmp_path / "groq.models.json")
    monkeypatch.setattr(config, "_listing", None)
    monkeypatch.setattr(config, "cloud_available", lambda: False)   # no network here
    bridge = Bridge()
    bridge.refresh_models_async()
    shiboken6.delete(bridge)                 # the window went away mid-fetch
    with pytest.raises(RuntimeError):
        bridge.modelsListed.emit()           # what the guard exists for
    bridge._announce_models()                # must not raise


def test_output_devices_are_listed_once_each(qt):
    window = RetroWindow(floating=False, voice=False)
    window._load_outputs()
    labels = [window.output_combo.itemText(i) for i in range(window.output_combo.count())]
    assert labels[0].startswith("System default")
    assert len(labels) == len(set(labels)), f"duplicate devices in the picker: {labels}"
    window.close()


def test_panel_renders_dark_and_rounded(qt):
    window = RetroWindow(floating=False, voice=False)
    image = render(window)
    # The corners are transparent (rounded) while the middle is the dark panel.
    assert image.pixelColor(2, 2).alpha() == 0
    middle = image.pixelColor(image.width() // 2, 40)
    assert middle.red() < 40 and middle.green() < 40 and middle.blue() < 50
    window.close()


def test_in_floating_mode_the_orb_sits_in_the_middle_of_the_window(qt):
    """It used to sit low and have its bottom cut off by the window edge.

    The panel's hidden rows still leave their spacers in the layout, so an orb left
    inside that layout was pushed down about sixty pixels. This draws the *window* -
    what the owner actually sees - and not the orb widget, which was always centred
    where nobody but a test could see it.
    """
    window = RetroWindow(floating=False, voice=False)
    window.set_floating(True)
    # No caption and no mode line, so what is measured is the orb and its glow. The
    # words are drawn off-centre on purpose (labels above, the mode below) and would
    # move the midpoint of the picture without anything being wrong with the orb.
    window.agent.set_caption("")
    window.agent.set_mode("")
    window.agent.clear_progress()
    image = render(window)
    xs, ys = [], []
    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixelColor(x, y).alpha() > 12:
                xs.append(x)
                ys.append(y)
    assert xs, "nothing was drawn in floating mode"
    cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    half_w, half_h = window.width() / 2.0, window.height() / 2.0
    assert abs(cx - half_w) < 6 and abs(cy - half_h) < 6, (
        f"the orb is off centre: centre {cx:.0f},{cy:.0f} in a {window.width()}x"
        f"{window.height()} window")
    assert min(xs) > 2 and min(ys) > 2 and max(xs) < window.width() - 3 \
        and max(ys) < window.height() - 3, \
        f"the orb is clipped by the window edge: {min(xs)},{min(ys)}..{max(xs)},{max(ys)}"
    window.close()


def test_floating_mode_keeps_only_the_circle(qt):
    """One circle, nothing else on screen.

    The window is bigger than the circle on purpose - the ring is drawn at a
    fraction of it so its own glow and the floating labels have somewhere to go,
    which is what stopped the window edge cutting a square band around it - and the
    R mark used to stay visible here because it was never in the hidden list.
    """
    window = RetroWindow(floating=False, voice=False)
    # An empty workspace, so this measures the window and not whatever Retro has
    # happened to write on this machine: the shelf grows the panel for as long as it
    # is up (there is a test of its own for that), and a test asserting a pixel height
    # must not depend on the user's Documents folder.
    window.bridge.session.documents = lambda limit=8: []
    window._refresh_shelf()
    window.set_floating(True)
    assert window.float_btn.text().endswith("ON")
    assert all(not w.isVisible() for w in window._panel_widgets)
    assert not window.mark.isVisible(), "the R mark must not leak into the circle"
    grown = int(theme.FLOAT_SIZE_DEFAULT * FLOAT_WINDOW_SCALE)
    assert window.size() == QSize(grown, grown)
    assert window.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert window.agent.isVisible()
    assert not hasattr(window.agent, "_orbiters"), "no decorative beads on the circle"
    window.set_floating(False)
    assert window.size() == QSize(PANEL_W, PANEL_H)
    window.close()


def test_the_shelf_grows_the_panel_and_gives_the_space_back(qt):
    """The documents strip is worth its height while it is up, and no longer.

    The panel is a fixed size so the circle never jumps when a label wraps, so
    anything that comes and goes has to add and remove its own room. Both halves are
    checked here, with the document list supplied by the test rather than read from
    the workspace.
    """
    window = RetroWindow(floating=False, voice=False)
    window.bridge.session.documents = lambda limit=8: [
        {"name": "the-cat.md", "title": "The cat", "words": 120}]
    window._refresh_shelf()
    assert window.shelf.isVisible(), "a written document should be on the shelf"
    assert window.size() == QSize(PANEL_W, PANEL_H + SHELF_HEIGHT + 8)
    window.bridge.session.documents = lambda limit=8: []
    window._refresh_shelf()
    assert not window.shelf.isVisible()
    assert window.size() == QSize(PANEL_W, PANEL_H)
    window.close()


def test_settings_event_brings_the_window_back(qt):
    """The path behind "Retro, open your settings": core event -> bridge -> window."""
    from core import ui_hook

    window = RetroWindow(floating=True, voice=False)
    assert window._floating, "it should start as the floating circle"
    ui_hook.emit("show_settings")
    assert not window._floating, "the settings window should have come back"
    window.close()


def test_circle_size_is_adjustable(qt):
    window = RetroWindow(floating=False, voice=False)
    window.set_floating(True)
    window.size_slider.setValue(300)
    assert window._circle_size == 300
    grown = int(300 * FLOAT_WINDOW_SCALE)
    assert window.size() == QSize(grown, grown)
    window.close()


def test_the_inner_circle_is_a_circle_even_in_a_wide_widget(qt):
    """The inner circle is the orb itself, and a layout must not turn it into an oval.

    A parent with more width than height used to be able to stretch the widget; the
    radius comes from `min(width, height)` and the policy is fixed, so the drawn
    shape stays circular. This measures the pixels, not the intent.
    """
    from PySide6.QtWidgets import QSizePolicy

    agent = AgentWidget()
    assert agent.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Fixed
    assert agent.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed
    agent.set_state("working")
    agent._colour = agent._target
    image = render(agent, QSize(320, 240))
    lit_pixels = [(x, y) for y in range(image.height()) for x in range(image.width())
                  if image.pixelColor(x, y).alpha() > 30]
    assert lit_pixels, "nothing was drawn"
    xs = [x for x, _ in lit_pixels]
    ys = [y for _, y in lit_pixels]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    assert abs(width - height) <= 4, (
        f"the inner circle is {width}x{height} pixels: it has been stretched")
    assert abs((min(xs) + max(xs)) / 2.0 - 160) <= 3
    assert abs((min(ys) + max(ys)) / 2.0 - 120) <= 3


def test_the_transparent_display_draws_no_text_at_all(qt):
    """The floating orb carries no words: caption, mode and progress are all silent.

    The colours and the motion are the state indicators. Drawing a caption *and* a
    mode line *and* the progress labels around a circle that sits on somebody's
    desktop is the clutter the transparent display exists to avoid - so the same
    frame, with and without text queued, must be pixel-identical.
    """
    window = RetroWindow(floating=False, voice=False)
    window.set_floating(True)
    # Freeze the animation so the only difference between the two frames could be
    # the text itself; a turning orb would be a difference of its own.
    window.agent.seconds = lambda: 1.25
    window.agent.set_state("working")
    window.agent._colour = window.agent._target
    window.agent.clear_progress()
    window.agent.set_caption("")
    window.agent.set_mode("")
    bare = render(window)
    window.agent.set_caption("listening for a command")
    window.agent.set_mode("chat")
    window.agent.set_progress("opening notepad")
    with_text = render(window)
    assert bare.size() == with_text.size()
    for y in range(bare.height()):
        for x in range(bare.width()):
            assert bare.pixelColor(x, y) == with_text.pixelColor(x, y), (
                f"text was drawn at {x},{y} in the transparent display")
    window.close()


def test_the_microphone_switch_shows_the_real_state(qt):
    """The button follows the session, not the click that asked for it."""
    window = RetroWindow(floating=False, voice=False)
    window.mic_btn.blockSignals(True)
    window.mic_btn.setChecked(True)
    window.mic_btn.blockSignals(False)
    window._label_mic_button()
    assert window.mic_btn.text().endswith("ON")
    assert "hear commands" in window.mic_btn.toolTip()

    asked = []
    window.bridge.set_mic = lambda on: asked.append(on)
    window.mic_btn.setChecked(False)              # a click on the switch
    assert asked == [False], "the click must reach the session"
    assert window.mic_btn.text().endswith("ON"), \
        "the label must wait for the real state, not jump on the click"

    window._on_mic(False)                          # the session reporting it really is off
    assert window.mic_btn.text().endswith("OFF")
    assert "no audio is captured" in window.mic_btn.toolTip()
    assert window._mic_action.isChecked() is False, "the tray switch must agree"

    window._on_mic(True)
    assert window.mic_btn.text().endswith("ON")
    window.close()


def test_the_diagnostics_panel_lists_what_was_measured(qt, monkeypatch, tmp_path):
    from core import config, diag
    from ui.diagnostics import DiagnosticsPanel

    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")
    diag.reset()
    trace = diag.new(kind="voice", origin="voice")
    trace.add("stt", 210_000_000)
    trace.add("llm", 380_000_000)
    trace.mark("first_text")
    trace.finish(ok=True)
    diag.record(trace)
    try:
        panel = DiagnosticsPanel()
        panel.refresh()
        assert panel.table.rowCount() == 1
        assert "stt" in panel.latest.text() and "llm" in panel.latest.text()
        assert "not measured" in panel.latest.text()
        assert "last 1 requests" in panel.summary.text()
        panel.close()
    finally:
        diag.reset()


def test_the_visualiser_waits_for_a_started_session(qt):
    """Background hearing draws a *running* Retro, not a window that has not started.

    The preview renderer and the offscreen self-test build the window without
    starting a session, and `background` comes from the user's settings - so a check
    that only asked `background and not sleeping` turned the grey orb into a rainbow
    visualiser of silence in every preview.
    """
    window = RetroWindow(floating=False, voice=False)
    session = window.bridge.session
    session.background = True
    session.mic_on = False                            # what background hearing is for
    session._state = "offline"                        # constructed, never started
    window._poll_room()
    assert not window.agent.visual, "no session, no music to draw"
    session._state = "listening"
    window._poll_room()
    assert window.agent.visual, "an awake session with the switch on draws the room"
    session.mic_on = True                             # the microphone takes the look away
    window._poll_room()
    assert not window.agent.visual, \
        "with the microphone on there is no room audio and no look"
    session.mic_on = False
    window._poll_room()
    window.bridge.session._state = "asleep"           # and then it goes quiet
    window.bridge.session.loaded = False
    window._poll_room()
    assert window.agent.visual, \
        "background hearing is the switch, not the state: sleep does not stop the picture"
    window.bridge.session.background = False          # the switch itself, off
    window._poll_room()
    assert not window.agent.visual, "and with it off the orb is still again"
    window.close()


def test_the_panel_has_the_look_switch_and_it_is_remembered(qt):
    """Music Mode or Media Mode, from the panel, and it survives a restart.

    A *visual* switch: it must reach the orb and the settings file, and it must not
    touch the microphone, the sampler or the session's state - which is what
    `LOOK MUSIC` / `LOOK MEDIA` beside the privacy switch is for.
    """
    from core import settings

    window = RetroWindow(floating=False, voice=False)
    try:
        session = window.bridge.session
        mic_before, state_before = session.mic_on, session.state
        assert "LOOK" in window.look_btn.text()
        window.look_btn.setChecked(False)                 # the owner clicking it
        assert window.agent.visual_mode == "media"
        assert settings.get("visual_mode") == "media", "and it is remembered"
        assert window.look_btn.text().endswith("MEDIA")
        assert (session.mic_on, session.state) == (mic_before, state_before), \
            "a look may never change what is being heard"
        window.look_btn.setChecked(True)
        assert window.agent.visual_mode == "music"
        assert settings.get("visual_mode") == "music"

        window.agent.set_visual_mode("media")            # as a restart would find it
        settings.save(visual_mode="media")
        window._sync_look_button()                        # what startup does
        assert window.agent.visual_mode == "media"
        assert window.look_btn.isChecked() is False
        assert window.look_btn.text().endswith("MEDIA")
    finally:
        window.close()


def test_a_sleeping_orb_stops_repainting_entirely(qt):
    """Asleep is a held frame, so drawing it twenty times a second is pure waste."""
    agent = AgentWidget()
    agent.set_state("listening")
    assert agent._timer.isActive()
    agent.set_state("asleep")
    assert not agent._timer.isActive(), "a frozen drawing must not keep a frame timer"
    agent.set_state("speaking")
    assert agent._timer.isActive(), "waking the drawing must restart it"
    agent.set_visual(True)
    assert agent._timer.isActive(), "the visualiser is motion and needs the timer"
    agent.set_visual(False)
    agent.set_state("asleep")
    assert not agent._timer.isActive()


# ---------- the conversation pass ---------------------------------------------


def test_the_drawer_is_folded_away_until_it_is_asked_for(qt):
    """The pickers you touch once a week are not the first thing on screen."""
    window = RetroWindow(floating=False, voice=False)
    assert window.drawer.isHidden(), "settings start folded away"
    window.settings_btn.click()
    assert not window.drawer.isHidden()
    window._toggle_settings(False)
    assert window.drawer.isHidden()
    window.open_settings()                     # the tray and the voice land here
    assert not window.drawer.isHidden()
    window.close()


def test_a_submitted_command_becomes_a_turn_and_the_answer_stays(qt):
    """The record is the point: the answer must not vanish with the next reply."""
    window = RetroWindow(floating=False, voice=False)
    sent = []
    window.bridge.submit = lambda text: sent.append(text)
    window.prompt.setText("write a note")
    window._submit()
    assert sent == ["write a note"] and window.prompt.text() == ""
    assert [(turn.role, turn.text()) for turn in window.talk.turns] == [("user", "write a note")]
    window._on_reply("Drafted it and saved note.md.", True)
    assert [turn.role for turn in window.talk.turns] == ["user", "retro"]
    assert window.talk.turns[-1].text() == "Drafted it and saved note.md."
    assert window.reply_label.text() == "", "the answer belongs in the record, not the hint line"
    window.close()


def test_a_failed_answer_is_marked_as_one(qt):
    window = RetroWindow(floating=False, voice=False)
    window._on_reply("that could not be done", False)
    assert window.talk.turns[-1].tone == "error"
    assert window.talk.turns[-1].objectName() == "turnError"
    window.close()


def test_stop_cancels_the_running_task_and_nothing_else(qt):
    """A STOP that only hides the row would be a lie on the panel."""
    window = RetroWindow(floating=False, voice=False)
    asked = []

    def cancel_task(*_args, **_kwargs):
        asked.append(True)
        return True, "Stopped."

    window.bridge.session.cancel_task = cancel_task
    window._on_state("working", "reading the screen")
    assert not window.status.stop_btn.isHidden()
    assert not window.talk.working.isHidden()
    window.status.stop_btn.click()
    assert asked, "STOP has to reach the session"
    assert window.talk.working.isHidden()
    window.close()


def test_the_send_button_and_ctrl_l_are_real(qt):
    window = RetroWindow(floating=False, voice=False)
    keys = [shortcut.key().toString() for shortcut in window._shortcuts]
    assert keys == ["Ctrl+,", "Ctrl+L", "Esc"], f"the keys are wired: {keys}"
    window.prompt.clear()
    assert not window.send_btn.isEnabled(), "nothing to send, nothing to press"
    window.prompt.setText("hello")
    assert window.send_btn.isEnabled()
    window._on_reply("hi there", True)
    assert len(window.talk.turns) == 1
    window._clear_talk()
    assert window.talk.turns == []
    assert not window.talk.empty.isHidden(), "an empty record offers the commands again"
    window.close()


def test_the_status_strip_follows_the_session(qt):
    window = RetroWindow(floating=False, voice=False)
    window._on_state("working", "reading the screen")
    assert window.status.word.text() == "WORKING"
    assert "reading the screen" in window.status.detail.text()
    window._on_progress("writing the file")
    assert window.status.detail.text() == "writing the file"
    assert window.talk.working.label.text() == "WRITING THE FILE"
    window._on_state("listening", "ready")
    assert window.status.word.text() == "LISTENING"
    assert window.talk.working.isHidden()
    window.close()
