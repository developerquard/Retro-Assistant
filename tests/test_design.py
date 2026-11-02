"""The page audit: the agent's only way to see what it cannot see.

A model with file tools and no eyes can rewrite a page for an hour and never notice it
produced a purple banner over three grey cards. These tests pin the two things that
make the audit worth having: a template scores low and is told exactly what is missing,
and a page that actually has structure, art, type, motion and interaction scores high.
The reports are also checked as text, because the agent acts on the words, not the
number.
"""
from pathlib import Path

from retrocli import design, tools


BASIC = """<!doctype html>
<html>
<head><title>Re:ZERO</title>
<style>
  body { background: #f2f2f2; font-family: Arial, sans-serif; }
  header { background: purple; color: white; text-align: center; padding: 40px; }
  .card { background: white; border-radius: 12px; padding: 20px;
          max-width: 1100px; margin: 20px auto; box-shadow: 0 2px 6px #0002; }
</style>
</head>
<body>
  <header><h1>Re:ZERO</h1></header>
  <nav><a href="#story">Story</a></nav>
  <div class="card">
    <h2>Welcome to the Re:ZERO World</h2>
    <p>Explore the epic journey of Subaru Natsuki and his party as they fight to save
    the world from the Witch Cult.</p>
  </div>
  <div class="card">
    <h2>The Story</h2>
    <p>The story begins with Subaru Natsuki, a young man transported to a world where
    magic is real. He joins a party of adventurers and together they embark on a
    journey to save the world from the Witch Cult, again and again and again.</p>
  </div>
  <div class="card">
    <h2>Characters</h2>
    <p>Meet the main characters of the Re:ZERO anime, each with their own story.</p>
  </div>
  <footer>© 2023 Re:ZERO Visual Masterpiece</footer>
</body>
</html>
"""

RICH = """<!doctype html>
<html lang="en">
<head>
  <title>Return by Death - a Re:ZERO field guide</title>
  <meta name="description" content="Every loop, every death, every promise in Lugunica.">
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="rail"><span>RETURN BY DEATH</span></header>
  <main>
    <section class="hero">
      <h1>Seventeen deaths, one promise.</h1>
      <figure><img src="hero.jpg" alt="Subaru in the rain outside the capital">
        <figcaption>Loop 1, the capital</figcaption></figure>
      <svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="40"/></svg>
    </section>
    <section class="loops">
      <h2>Every loop, in order</h2>
      <p>Subaru Natsuki arrives in Lugunica on a warm afternoon in the capital and dies
      before midnight for the first time. Emilia finds him in the alley behind the fruit
      stall where the insignia was stolen. Each return costs him something he cannot get
      back, and the page reads the cost in sequence, from the first knife to the last
      mabeast.</p>
      <ol><li>Loop one</li><li>Loop two</li><li>Loop three</li><li>Loop four</li></ol>
    </section>
    <section class="characters">
      <h2>Who is in the room</h2>
      <article><h3>Emilia</h3><p>Half-elf, candidate for the throne, and the reason he
      keeps going back. She remembers nothing between loops and he remembers all of
      it.</p></article>
      <article><h3>Rem</h3><p>The maid who forgets him in every world except one, and
      the one who says the sentence the page is built around.</p></article>
    </section>
  </main>
  <footer><p>An unofficial field guide. Content is invented for this example.</p></footer>
  <script src="app.js"></script>
</body>
</html>
"""

CSS = """
:root { --ink: #0d0b12; --bone: #efe7dc; --blood: #b3122e; --mist: #6f6a7a;
        --ease: cubic-bezier(.2,.7,.2,1); }
@font-face { font-family: "Fraunce"; src: url(fonts/fraunce.woff2); }
body { background: var(--ink); color: var(--bone); font-family: "Fraunce", serif;
       font-size: clamp(1rem, 1.1vw, 1.15rem); line-height: 1.65; }
h1 { font-size: clamp(3rem, 8vw, 7rem); line-height: .95; }
h2 { font-size: clamp(2rem, 4vw, 3rem); }
h3 { font-size: 1.4rem; }
.lead { font-size: 1.25rem; }
.hero { display: grid; grid-template-columns: 1.2fr .8fr; gap: 4vw; padding: 12vh 6vw;
        background: radial-gradient(circle at 70% 20%, #b3122e22, transparent 60%),
                    url(texture.png); }
.rail { position: fixed; mix-blend-mode: difference; letter-spacing: .2em; }
.loops { display: flex; gap: 3vw; }
.card { box-shadow: 0 40px 80px #00000080; backdrop-filter: blur(12px); }
img { transition: transform .4s var(--ease), filter .3s var(--ease); }
a { transition: color .2s var(--ease); }
button { transition: background .2s var(--ease); }
@keyframes rise { from { opacity: 0; transform: translateY(24px); } to { opacity: 1; } }
@keyframes drift { from { transform: translate3d(0,0,0); } to { transform: translate3d(0,-18px,0); } }
.reveal { animation: rise .8s var(--ease) both; }
.glow { animation: drift 14s linear infinite; }
a:focus-visible, button:focus-visible { outline: 2px solid var(--blood); }
@media (max-width: 900px) { .hero { grid-template-columns: 1fr; } }
@media (max-width: 600px) { .loops { flex-direction: column; } h1 { font-size: 3rem; } }
@media (prefers-reduced-motion: reduce) { * { animation: none; transition: none; } }
"""

JS = """
const reveals = document.querySelectorAll('.reveal, section, article');
const seen = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (entry.isIntersecting) { entry.target.classList.add('is-in'); }
  });
}, { threshold: 0.15 });
reveals.forEach((node) => seen.observe(node));

document.querySelectorAll('a[href^="#"]').forEach((link) => {
  link.addEventListener('click', (event) => {
    event.preventDefault();
    const target = document.querySelector(link.getAttribute('href'));
    if (target) { target.scrollIntoView({ behavior: 'smooth' }); }
  });
});

document.querySelectorAll('.card').forEach((card) => {
  card.addEventListener('mouseenter', () => card.classList.add('lift'));
  card.addEventListener('mouseleave', () => card.classList.remove('lift'));
});

async function loadLoops() {
  const response = await fetch('loops.json');
  const loops = await response.json();
  const list = document.querySelector('.loops ol');
  loops.forEach((loop) => {
    const item = document.createElement('li');
    item.textContent = loop.title;
    list.appendChild(item);
  });
}
loadLoops();
"""


def test_the_basic_site_scores_as_a_template_and_names_what_is_missing():
    result = design.audit(BASIC)
    assert result.score < 50, result.text()
    labels = {check.label for check in result.failed}
    assert {"art", "motion", "layout", "type", "accessibility"} <= labels
    text = result.text()
    assert "basic" in result.verdict
    assert "NO  art" in text
    assert "inline SVG" in text, "the report has to say what to do, not only what is wrong"
    assert "download_asset" in text


def test_a_page_with_structure_art_type_motion_and_interaction_scores_high():
    result = design.audit(RICH, CSS, JS)
    assert result.score >= 85, result.text()
    assert result.verdict == "distinctive"
    assert not result.failed, [check.label for check in result.failed]


def test_placeholder_copy_fails_even_when_there_is_enough_of_it():
    page = BASIC.replace("Explore the epic journey", "Welcome to the epic journey")
    result = design.audit(page)
    copy = next(check for check in result.checks if check.label == "copy")
    assert not copy.ok
    assert "welcome to the" in copy.detail.lower()


def test_the_report_ends_with_the_floor_so_done_has_a_number():
    result = design.audit(BASIC, floor=82)
    assert "below 82/100" in result.text()


def test_a_missing_image_alt_is_reported(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(RICH.replace(' alt="Subaru in the rain outside the capital"',
                                                  ""), encoding="utf-8")
    result = design.audit_files(root, root / "index.html")
    access = next(check for check in result.checks if check.label == "accessibility")
    assert not access.ok
    assert "0/1 image(s) with alt" in access.detail


def test_the_audit_reads_the_local_stylesheet_and_script(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(
        '<!doctype html><html lang="en"><head><title>a real page title</title>'
        '<link rel="stylesheet" href="styles.css"></head>'
        '<body><main><section>x</section></main>'
        '<script src="app.js"></script></body></html>', encoding="utf-8")
    (root / "styles.css").write_text(CSS, encoding="utf-8")
    (root / "app.js").write_text(JS, encoding="utf-8")
    result = design.audit_files(root, root / "index.html")
    assert set(result.files) == {"index.html", "styles.css", "app.js"}
    motion = next(check for check in result.checks if check.label == "motion")
    assert motion.ok, motion.detail


def test_a_stylesheet_outside_the_project_is_not_read(tmp_path):
    outside = tmp_path / "secret.css"
    outside.write_text("@keyframes x { to { opacity: 0 } }", encoding="utf-8")
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(
        '<!doctype html><html><head><title>t</title>'
        '<link rel="stylesheet" href="../secret.css"></head><body></body></html>',
        encoding="utf-8")
    result = design.audit_files(root, root / "index.html")
    assert result.files == ["index.html"]
    motion = next(check for check in result.checks if check.label == "motion")
    assert not motion.ok


def test_the_tool_audits_a_page_or_a_folder_and_refuses_anything_else(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(BASIC, encoding="utf-8")
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    ctx = tools.Context(tools.Workspace(str(root)), auto=True)

    report = tools.call("page_audit", {"path": "index.html"}, ctx)
    assert "page audit of Re:ZERO" in report and "/100" in report

    folder = tools.call("page_audit", {"path": "."}, ctx)
    assert "page audit" in folder, "a folder holding index.html should just work"

    assert tools.call("page_audit", {"path": "notes.txt"}, ctx).startswith("ERROR:")

    missing = tools.call("page_audit", {}, ctx)
    assert missing.startswith("ERROR:") and "path" in missing


def test_the_tool_is_offered_to_the_model_and_allowed_in_plan_mode():
    names = [entry["function"]["name"] for entry in tools.catalogue()]
    assert "page_audit" in names
    plan = [entry["function"]["name"] for entry in tools.catalogue(read_only=True)]
    assert "page_audit" in plan, "plan mode has to be able to judge a page it proposes"
