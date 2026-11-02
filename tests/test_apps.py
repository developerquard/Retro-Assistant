"""Opening anything that is installed, not just a list of seven programs.

The discovery tests read this machine's real Start Menu and registry. Where there
are no apps to find - a build machine - they skip rather than pretend to pass.
The matching tests are pure and always run.
"""
import sys

import pytest

from core import apps
from core.model import norm  # noqa: F401  (kept for clarity of intent)
from core.router import Match, route
from core.runner import handle
from core.validate import validate


def app(name, kind="shortcut", target="x", source="test"):
    return apps.App(name=name, target=target, kind=kind, source=source)


# ---------- matching, with no machine involved ------------------------------

def test_normalize_folds_case_and_punctuation():
    assert apps.normalize("Legacy  Launcher!") == "legacy launcher"
    assert apps.normalize("VLC media player") == "vlc media player"


def test_an_exact_name_scores_one():
    assert apps.score("discord", "Discord") == 1.0


def test_a_near_neighbour_does_not_steal_an_exact_name():
    found, others = apps.best("spotify", [app("Spotify"), app("spotify_cli")])
    assert found is not None and found.name == "Spotify" and others == []


def test_prefix_and_partial_names_resolve():
    found, _ = apps.best("blender", [app("Blender 5.1"), app("7-Zip File Manager")])
    assert found is not None and found.name == "Blender 5.1"


def test_a_real_tie_is_handed_back_as_a_shortlist():
    found, others = apps.best("edge", [app("msedge"), app("Microsoft Edge")])
    assert found is None
    assert {a.name for a in others} == {"msedge", "Microsoft Edge"}


def test_nothing_close_yields_nothing():
    found, others = apps.best("zzzznope", [app("Discord"), app("Blender 5.1")])
    assert found is None and others == []


def test_search_ranks_the_best_first():
    ranked = apps.search("launcher", apps=[app("Steam"), app("Legacy Launcher Stable")])
    assert ranked and ranked[0][1].name == "Legacy Launcher Stable"


# ---------- this machine's real apps ----------------------------------------

def has_apps():
    return sys.platform == "win32" and len(apps.load()) > 0


@pytest.mark.skipif(not has_apps(), reason="no installed apps to discover here")
def test_discovery_finds_real_apps_with_targets():
    found = apps.load()
    assert len(found) > 5
    for item in found[:20]:
        assert item.name and item.target and item.kind in ("shortcut", "exe", "shell")


@pytest.mark.skipif(not has_apps(), reason="no installed apps to discover here")
def test_discovery_is_cached_and_stable():
    first = apps.load()
    assert apps.load() is first, "the index should be built once per process"
    from core.apps import _read_cache
    assert _read_cache() is not None, "the index should survive a restart"


# ---------- the open flow ----------------------------------------------------

def test_open_any_installed_app(monkeypatch):
    launched = []
    monkeypatch.setattr(apps, "best", lambda q, apps_=None: (app("Legacy Launcher Stable"), []))
    monkeypatch.setattr(apps, "launch", lambda a: (launched.append(a.name), 4242)[1])
    result = handle("open legacy launcher", fallback=None)
    assert result.ok and result.message == "Opened Legacy Launcher Stable."
    assert launched == ["Legacy Launcher Stable"]


def test_unknown_app_launches_nothing(monkeypatch):
    launched = []
    monkeypatch.setattr(apps, "launch", lambda a: launched.append(a.name) or 1)
    result = handle("open zzzznope", fallback=None)
    assert not result.ok
    assert launched == []
    assert "zzzznope" in result.message


def install_two_lookalikes(monkeypatch):
    """A real resolver: an exact name resolves, anything else is a shortlist."""
    candidates = [app("msedge", kind="exe"), app("Microsoft Edge")]

    def fake_best(query, apps_=None):
        for candidate in candidates:
            if apps.normalize(candidate.name) == apps.normalize(query):
                return candidate, []
        return None, list(candidates)

    monkeypatch.setattr(apps, "best", fake_best)
    return candidates


def test_ambiguous_app_is_asked_about(monkeypatch):
    install_two_lookalikes(monkeypatch)
    # "edge" would be answered by the built-in table; this name is only in the index.
    result = handle("open mybrowser", fallback=None)
    assert not result.ok
    assert set(result.choices) == {"msedge", "Microsoft Edge"}
    assert "msedge" in result.message or "more than one" in result.message.lower()


def test_the_model_chooses_between_candidates(monkeypatch):
    install_two_lookalikes(monkeypatch)
    launched = []
    monkeypatch.setattr(apps, "launch", lambda a: (launched.append(a.name), 99)[1])

    class Chooser:
        def choose(self, text, names):
            return "Microsoft Edge"

    result = handle("open mybrowser", fallback=Chooser())
    assert result.ok and launched == ["Microsoft Edge"]


def test_a_path_is_never_accepted_as_an_app_name():
    assert validate("open_app", {"name": r"C:\Windows\System32\cmd.exe"}) is None
    assert validate("open_app", {"name": "../../evil"}) is None
    assert validate("open_app", {"name": "legacy launcher"}) == {"name": "legacy launcher"}


def test_launch_verbs_are_understood():
    for text in ("open blender", "launch blender", "start blender", "run blender"):
        decision = route(text)
        assert isinstance(decision, Match) and decision.action.name == "open_app"
        assert decision.args["name"] == "blender", text
