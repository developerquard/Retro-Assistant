"""Coding requests: the folder, the files, and the editor only at the end.

The owner's rule is the specification: a project is made in `Coding Tasks`, the files
are written in silence, and VS Code is opened on the finished folder - never in the
middle of the work. The last test here is the one that calls the editor, so that the
order is proved rather than described.
"""
import sys

import pytest

from core import builder, files


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """A throwaway workspace, and an editor that records instead of opening."""
    monkeypatch.setattr(files, "WORKSPACE", tmp_path)
    monkeypatch.setattr(files, "PROJECTS", tmp_path / "Coding Tasks")
    monkeypatch.setattr(files, "NOTES", tmp_path / "Notes")
    return tmp_path


def test_a_request_to_build_something_is_a_coding_job():
    for text in ("make this website",
                 "use this language and this combination to make website",
                 "open VS code, make a new 3D website",
                 "make a three.js website in vs code",
                 "build me a react dashboard with tailwind"):
        assert builder.wants_project(text), text


def test_opening_the_editor_or_asking_for_words_is_not_a_coding_job():
    for text in ("open vs code",
                 "open vs code and show me my projects",
                 "show me the folder",
                 "make a note about black holes",
                 "write an essay about the web",
                 "what is on my screen",
                 "make a song about rain",
                 "open my website"):
        assert not builder.wants_project(text), text


def test_the_folder_name_is_the_thing_being_built():
    assert builder.project_name("open VS code, make a new 3D website") == "3D website"
    assert builder.project_name("make this website") == "website"
    assert builder.project_name("build me a react dashboard with tailwind") == "react dashboard"
    assert builder.project_name("write it in python") == "Coding Project"


def test_a_planned_path_cannot_leave_the_project():
    for bad in ("../escape.js", "C:/Windows/x.js", "/etc/passwd", "noextension",
                "a/b/c/d.js", ".hidden/config.js"):
        assert builder._safe_path(bad) is None, bad
    assert builder._safe_path("index.html") == "index.html"
    assert builder._safe_path("src/main.js") == "src/main.js"


def test_a_fenced_reply_becomes_the_file_without_its_fence():
    reply = "Sure, here it is:\n```html\n<h1>Hi</h1>\n```\nHope that helps."
    assert builder.clean_code(reply) == "<h1>Hi</h1>"
    assert builder.clean_code("```js\nconst a = 1;\n```") == "const a = 1;"


def test_the_project_is_built_in_coding_tasks_and_the_editor_comes_last(workspace):
    order = []

    def complete(messages, max_tokens=0):
        if "planning a real software project" in messages[0]["content"]:
            order.append("plan")
            return "index.html | the page\nstyle.css | the style\n"
        path = messages[1]["content"].split(" of ")[1].rsplit(" now.")[0]
        order.append(("write", path))
        return f"/* {path} */"

    def editor(folder):
        on_disk = [name for name in ("index.html", "style.css")
                   if (folder / name).exists()]
        order.append(("editor", len(on_disk)))
        return True, True

    builder.open_in_editor = editor
    result = builder.build("open VS code, make a new 3D website", complete)

    folder = workspace / "Coding Tasks" / "3D website"
    assert result.ok and result.verified
    assert (folder / "index.html").read_text(encoding="utf-8") == "/* index.html */"
    assert (folder / "style.css").read_text(encoding="utf-8") == "/* style.css */"
    assert order[0] == "plan", order
    assert order[-1] == ("editor", 2), "the editor must open after the files, with them all"
    assert "Coding Tasks" in result.message and "index.html" in result.message


def test_a_file_the_model_refuses_is_reported_not_hidden(workspace):
    def complete(messages, max_tokens=0):
        if "planning a real software project" in messages[0]["content"]:
            return "index.html | the page\nstyle.css | the style\n"
        path = messages[1]["content"].split(" of ")[1].rsplit(" now.")[0]
        return "" if path == "style.css" else "body{margin:0}"

    result = builder.build("make a test website", complete, editor=False)
    assert not result.verified
    assert "style.css" in result.message
    assert (workspace / "Coding Tasks" / "test website" / "index.html").exists()


def test_a_second_project_with_the_same_name_gets_its_own_folder(workspace):
    def complete(messages, max_tokens=0):
        if "planning a real software project" in messages[0]["content"]:
            return "index.html | the page\n"
        return "<h1>hello</h1>"

    first = builder.build("make a site", complete, editor=False)
    second = builder.build("make a site", complete, editor=False)
    assert first.ok and second.ok
    assert (workspace / "Coding Tasks" / "site" / "index.html").exists()
    assert (workspace / "Coding Tasks" / "site 2" / "index.html").exists()


def test_no_model_means_an_honest_refusal(workspace):
    result = builder.build("make a website", None)
    assert not result.ok
    assert not (workspace / "Coding Tasks").exists()
