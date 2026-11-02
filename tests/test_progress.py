"""Floating labels are built from real events - and never from nothing.

The user asked for playful text around the circle and, in the same breath, for it not
to be invented. So each label is a function of the tool that is really running and
its argument, an unknown step still says something true, and no input can make the
mapping raise in the middle of a task.
"""
from core import progress


def test_a_label_names_the_tool_and_its_argument():
    assert progress.label_for("open_app", {"name": "notepad"}) == "Opening notepad"
    assert progress.label_for("write_file", {"path": "index.html"}) == "Writing index.html"
    assert progress.label_for("click_text", {"label": "Play"}) == "Clicking Play"


def test_an_app_function_label_comes_from_the_action_it_really_is():
    assert progress.label_for("app_function", {"action": "search_play",
                                               "detail": "sunflower"}) == "Finding sunflower"
    assert progress.label_for("app_function", {"action": "send_message",
                                               "target": "Mum"}) == "Opening the chat with Mum"
    assert progress.label_for("app_function", {"action": "create_project",
                                               "target": "portfolio"}) == "Building portfolio"


def test_an_unknown_step_still_says_something_true():
    assert progress.label_for("some_new_tool", {"what": "ever"}) == "some new tool"
    assert progress.label_for(None, {}) == "working", "never an empty line to draw"


def test_labels_stay_short_enough_to_draw_beside_the_circle():
    long_name = "a folder with an extremely long and unhelpful name indeed"
    label = progress.label_for("open_folder", {"name": long_name})
    assert len(label) <= 44, label


def test_the_mapping_never_raises_on_odd_arguments():
    for args in (None, {}, {"name": None}, {"unexpected": 1}, {"detail": ["x"] * 50}):
        labels = [progress.label_for("open_app", args),
                  progress.label_for("app_function", args),
                  progress.label_for(None, args)]
        assert all(isinstance(label, str) and label for label in labels), args


def test_there_is_no_timer_that_invents_activity():
    """The module's own pools only hold labels for work that is really happening."""
    for pool in (progress.PLANNING, progress.CHECKING, progress.FINISHING, progress.READING):
        assert pool and all(isinstance(line, str) and line for line in pool)
