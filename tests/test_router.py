import pytest

from core.router import Ambiguous, Match, route

CASES = [
    ("What time is it?", "time"),
    ("अभी कितने बजे हैं?", "time"),
    ("What's today's date?", "date"),
    ("आज कौन-सी तारीख है?", "date"),
    ("How much battery is left?", "battery"),
    ("बैटरी कितनी बची है?", "battery"),
    ("Show my CPU and RAM usage.", "cpu_ram"),
    ("CPU और RAM का इस्तेमाल बताओ।", "cpu_ram"),
    ("Am I connected to the internet?", "internet"),
    ("इंटरनेट कनेक्ट है?", "internet"),
    ("Set the volume to 40 percent.", "set_volume"),
    ("वॉल्यूम 40 प्रतिशत कर दो।", "set_volume"),
    ("Increase the volume.", "volume_up"),
    ("आवाज़ बढ़ाओ।", "volume_up"),
    ("Decrease the volume.", "volume_down"),
    ("आवाज़ कम करो।", "volume_down"),
    ("Mute the volume.", "toggle_mute"),
    ("आवाज़ म्यूट कर दो।", "toggle_mute"),
    ("Open Notepad.", "open_app"),
    ("Notepad खोलो।", "open_app"),
    ("Open YouTube.", "open_site"),
    ("YouTube खोलो।", "open_site"),
    ("Search for Fabric modding tutorials.", "search_web"),
    ("वेब पर Fabric modding tutorial खोजो।", "search_web"),
    ("Pause the music.", "pause"),
    ("म्यूज़िक रोक दो।", "pause"),
    ("Resume the music.", "resume"),
    ("म्यूज़िक फिर से चलाओ।", "resume"),
    ("Play the next track.", "next_track"),
    ("अगला गाना चलाओ।", "next_track"),
    ("Play the previous track.", "prev_track"),
    ("पिछला गाना चलाओ।", "prev_track"),
    ("Set a timer for 10 minutes.", "timer_start"),
    ("10 मिनट का टाइमर लगाओ।", "timer_start"),
    ("Cancel the timer.", "timer_cancel"),
    ("टाइमर रद्द करो।", "timer_cancel"),
    ("Create a note: remember to test M1.", "create_note"),
    ("एक नोट बनाओ: M1 टेस्ट करना याद रखना।", "create_note"),
    ("Find plan.md in RetroWorkspace.", "find_file"),
    ("RetroWorkspace में plan.md ढूँढो।", "find_file"),
]


@pytest.mark.parametrize("text,expected", CASES)
def test_routes_to_expected_action(text, expected):
    m = route(text)
    assert isinstance(m, Match), f"no match for {text!r}: {m}"
    assert m.action.name == expected


# "find and open a file" used to be in the list below. Any "open <words>" now
# routes to open_app, because any installed application may be opened, so it is
# the runner - not the router - that has to report an unknown application.
@pytest.mark.parametrize("text", ["hello there", "", "update my notes",
                                  "what is the capital of France"])
def test_no_match(text):
    assert route(text) is None


def test_unmute_is_mute_not_date():
    m = route("unmute the volume")
    assert isinstance(m, Match) and m.action.name == "toggle_mute"


def test_open_app_keeps_notepad_out_of_note():
    m = route("Open Notepad.")
    assert m.action.name == "open_app" and m.args["name"] == "notepad"


def test_timer_minutes_are_extracted():
    assert route("Set a timer for 10 minutes.").args == {"minutes": 10}


def test_note_body_is_extracted_with_case():
    assert route("Create a note: Remember Quardify Roadmap").args == {"text": "Remember Quardify Roadmap"}


def test_find_file_hinglish_mein_is_not_part_of_the_query():
    m = route("RetroWorkspace mein roadmap dhoondho.")
    assert isinstance(m, Match) and m.action.name == "find_file"
    assert m.args == {"query": "roadmap"}
