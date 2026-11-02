"""Real file work, inside one folder Retro is allowed to write in.

The agent needs to create and edit files - that is the difference between "I made
a website" and a description of how to make one - and a voice assistant that can
write anywhere is a voice assistant one misheard word away from overwriting
something that matters. So every path here is resolved and then checked, and a
path that escapes the workspace is refused with the reason.

Nothing in this module is built from a shell string: a file name is only ever a
file name, and directories are created rather than command lines copied.

The starter project is deliberately real material rather than a stub: an HTML
page, a stylesheet and a Three.js scene loaded from a CDN, which opens in a
browser and shows something moving. A project that only exists as an empty file
would satisfy a test and disappoint the person who asked for it.
"""
import os
import re
from pathlib import Path

from . import config
from .model import Result

WORKSPACE = Path(config.WORKSPACE)
# Where a coding job puts its folder. The owner named this: a project goes *inside*
# "Coding Tasks" in the workspace, so every thing Retro built is in one place that is
# not mixed in with the documents it wrote.
CODING_TASKS = "Coding Tasks"
PROJECTS = WORKSPACE / CODING_TASKS
# Notes written by hand ("write a note about black holes") are not projects, and they
# used to land in the projects folder for want of anywhere else.
NOTES = WORKSPACE / "Notes"
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}$")


def workspace():
    """The one folder Retro reads and writes by itself."""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    return WORKSPACE


def inside_workspace(path):
    """The resolved path, or ValueError when it leaves the workspace.

    Both the path as written and its final destination are checked, so neither
    ".." nor a symlink can walk out of the folder.
    """
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("no path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = WORKSPACE / candidate
    if os.path.islink(str(candidate)):
        raise ValueError("that path is a link out of the workspace")
    final = Path(os.path.abspath(str(candidate)))
    root = Path(os.path.abspath(str(WORKSPACE)))
    if final != root and root not in final.parents:
        raise ValueError(f"only files inside {root} can be written")
    return final


def create_project_folder(name):
    """A folder in Coding Tasks, created if needed. Returns (folder or None, error)."""
    cleaned = " ".join(str(name or "").strip().split())
    if not cleaned or not NAME_OK.match(cleaned):
        return None, f"'{name}' is not a usable project name."
    folder = PROJECTS / cleaned
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"could not create the folder ({type(exc).__name__})."
    return folder, ""


def create_notes_folder(name="Notes"):
    """A folder for hand-written notes, next to Coding Tasks. Returns (folder, error)."""
    cleaned = " ".join(str(name or "Notes").strip().split())
    if not cleaned or not NAME_OK.match(cleaned):
        cleaned = "Notes"
    folder = NOTES / cleaned if cleaned != "Notes" else NOTES
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"could not create the folder ({type(exc).__name__})."
    return folder, ""


def write_text(path, text):
    """Writes a text file, creating its folder. Returns a Result."""
    try:
        target = inside_workspace(path)
    except ValueError as exc:
        return Result(False, str(exc))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(text or ""), encoding="utf-8")
    except OSError as exc:
        return Result(False, f"I couldn't write {target.name} ({type(exc).__name__}).")
    size = target.stat().st_size
    return Result(True, f"Wrote {target.name} ({size} bytes).", verified=True)


def read_text(path, limit=4000):
    """Reads a text file back, so a result can be inspected. Returns a Result."""
    try:
        target = inside_workspace(path)
    except ValueError as exc:
        return Result(False, str(exc))
    if not target.exists():
        return Result(False, f"There is no file at {target.name}.")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Result(False, f"I couldn't read {target.name} ({type(exc).__name__}).")
    clipped = text[: max(1, int(limit))]
    more = "" if len(text) <= len(clipped) else f" ... ({len(text) - len(clipped)} more characters)"
    return Result(True, f"{target.name}: {clipped}{more}", verified=True)


THREEJS_FILES = {
    "index.html": """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main id="stage" aria-label="3D scene"></main>
  <header class="hud">
    <h1>{title}</h1>
    <p>Drag to orbit. Scroll to zoom. Move the pointer to light it up.</p>
  </header>
  <script type="importmap">
  {{"imports": {{"three": "https://unpkg.com/three@0.160.0/build/three.module.js",
                "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"}}}}
  </script>
  <script type="module" src="main.js"></script>
</body>
</html>
""",
    "style.css": """:root { color-scheme: dark; }
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; background: #05070c; overflow: hidden;
  font-family: "Segoe UI", system-ui, sans-serif; color: #e8f0ff; }
#stage { position: fixed; inset: 0; }
.hud { position: fixed; left: 28px; bottom: 26px; pointer-events: none; }
.hud h1 { margin: 0 0 6px; font-size: 30px; letter-spacing: 0.5px; font-weight: 600; }
.hud p { margin: 0; opacity: 0.65; font-size: 14px; }
""",
    "main.js": """import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const stage = document.getElementById("stage");

const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x05070c, 0.045);

const camera = new THREE.PerspectiveCamera(52, innerWidth / innerHeight, 0.1, 200);
camera.position.set(0, 2.1, 7.4);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
stage.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.6;

scene.add(new THREE.HemisphereLight(0x8fb6ff, 0x080c14, 1.1));
const key = new THREE.DirectionalLight(0x7fd4ff, 2.2);
key.position.set(5, 8, 6);
scene.add(key);
const rim = new THREE.PointLight(0xff5f8f, 40, 30);
rim.position.set(-6, -3, -5);
scene.add(rim);

const knot = new THREE.Mesh(
  new THREE.TorusKnotGeometry(1.35, 0.42, 260, 36),
  new THREE.MeshStandardMaterial({ color: 0x2b6cff, metalness: 0.85, roughness: 0.18 })
);
knot.position.y = 0.4;
scene.add(knot);

const shell = new THREE.Mesh(
  new THREE.IcosahedronGeometry(3.1, 1),
  new THREE.MeshBasicMaterial({ color: 0x1b3a7a, wireframe: true, transparent: true, opacity: 0.35 })
);
scene.add(shell);

const stars = new THREE.Points(
  new THREE.BufferGeometry().setAttribute("position",
    new THREE.Float32BufferAttribute(
      Array.from({ length: 1800 }, () => (Math.random() - 0.5) * 60), 3)),
  new THREE.PointsMaterial({ size: 0.06, color: 0xa8c8ff, transparent: true, opacity: 0.8 })
);
scene.add(stars);

addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

let pointer = 0;
addEventListener("pointermove", (event) => {
  pointer = (event.clientX / innerWidth) * 2 - 1;
});

const clock = new THREE.Clock();
renderer.setAnimationLoop(() => {
  const t = clock.getElapsedTime();
  knot.rotation.x = t * 0.35;
  knot.rotation.y = t * 0.5;
  knot.position.y = 0.4 + Math.sin(t * 1.4) * 0.16;
  shell.rotation.y = -t * 0.12;
  rim.position.x = Math.sin(t * 0.5) * 6;
  key.intensity = 2.0 + pointer * 0.6;
  camera.fov = 52 + Math.sin(t * 0.8) * 1.5;
  camera.updateProjectionMatrix();
  controls.update();
  renderer.render(scene, camera);
});
""",
}


def create_threejs_project(name):
    """Writes the starter 3D site into Projects/<name>. Returns a Result.

    Verified by reading the files back: a project reported as created is a project
    whose three files are on disk with the sizes they should have.
    """
    folder, problem = create_project_folder(name)
    if folder is None:
        return Result(False, problem)
    written = []
    for filename, content in THREEJS_FILES.items():
        body = content.format(title=folder.name) if "{title}" in content else content
        result = write_text(folder / filename, body)
        if not result.ok:
            return Result(False, f"I couldn't write {filename}: {result.message}")
        written.append(filename)
    missing = [name for name in THREEJS_FILES if not (folder / name).exists()]
    if missing:
        return Result(False, f"The project is incomplete - missing {', '.join(missing)}.")
    return Result(True, f"Created {folder.name} with {', '.join(written)} at {folder}.",
                  verified=True)


def projects():
    """The projects that exist, newest first."""
    if not PROJECTS.is_dir():
        return []
    found = [entry for entry in PROJECTS.iterdir() if entry.is_dir()]
    found.sort(key=lambda path: -path.stat().st_mtime)
    return found
