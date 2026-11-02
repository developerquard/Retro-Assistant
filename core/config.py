"""Paths and limits for Retro, plus the listing of what the cloud key can call.

Nothing in the top half touches the network or the disk. The model listing at the
bottom does both, deliberately: it is a cache of one HTTPS call, and every path
through it is best-effort, because a missing key or a dead network must never stop
the window from opening.
"""
import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen

_USER = Path(os.environ.get("USERPROFILE", str(Path.home())))
_LOCAL = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))

WORKSPACE = _USER / "Documents" / "RetroWorkspace"
WORKSPACE_DIRS = ("Notes", "Inbox", "Exports")
LOG_DIR = _LOCAL / "Retro" / "Logs"
# A log that grows forever is a log nobody can open. Retro writes a line per event
# for months of daily use, so each file is capped and one previous generation is kept
# (retro.log.1) - which is also what makes "what happened just before this" answerable
# after a restart, without shipping a logging framework.
LOG_MAX_BYTES = int(os.environ.get("RETRO_LOG_MAX_BYTES", "1500000"))
CONFIRM_TIMEOUT_S = 10
def _find_model():
    """First existing RETRO_MODEL, then the usual install spots, then the download folder."""
    env = os.environ.get("RETRO_MODEL")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates += [
        _LOCAL / "Retro" / "models" / "Llama-3.2-1B-Instruct.gguf",
        Path(__file__).resolve().parent.parent / "models" / "Llama-3.2-1B-Instruct.gguf",
        Path("D:/Downloads/Llama-3.2-1B-Instruct-Q4_K_M.gguf"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[1]          # the documented default, so the error message is useful


# The fallback models the UI can pick between. "auto" is the shipped behaviour:
# the typed rules answer first and a model handles only what they miss.
MODEL_FILES = {
    "1b": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
    "500m": "qwen2-0_5b-instruct-q4_k_m.gguf",
}
MODEL_SEARCH_DIRS = (_LOCAL / "Retro" / "models",
                     Path(__file__).resolve().parent.parent / "models",
                     Path("D:/Downloads"))


def model_path_for(choice):
    """Path to the fallback model for a UI choice. Unknown values use the 1B."""
    name = MODEL_FILES.get(str(choice).lower(), MODEL_FILES["1b"])
    for directory in MODEL_SEARCH_DIRS:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return MODEL_SEARCH_DIRS[0] / name


def model_available(choice):
    """True when the file for this choice is on disk, so the UI can grey it out."""
    name = MODEL_FILES.get(str(choice).lower())
    return name is not None and any((d / name).exists() for d in MODEL_SEARCH_DIRS)


MODEL_PATH = _find_model()
MODEL_CHOICE = os.environ.get("RETRO_MODEL_CHOICE", "auto").lower()
# The local model's context window. It has to hold the whole planning prompt: the
# rules plus the catalogue of applications is about 9k characters, roughly 3.5k
# tokens, and at 2048 every request was rejected before a single token was generated
# ("Requested tokens (3510) exceed context window of 2048") - so with no cloud key
# the offline brain could not plan anything at all. 4096 is the smallest round number
# that fits it. Prefill time is set by the length of the prompt, not by the size of
# the window, so this costs a KV cache of about 130 MB and nothing in speed.
MODEL_CTX = int(os.environ.get("RETRO_MODEL_CTX", "4096"))
MODEL_THREADS = int(os.environ.get("RETRO_MODEL_THREADS", "4"))
MODEL_IDLE_UNLOAD_S = float(os.environ.get("RETRO_MODEL_IDLE_S", "300"))   # unload after 5 min idle
WHISPER_MODEL = os.environ.get("RETRO_WHISPER", "base")

# Voice capture (M2). START_RMS is the loudness that counts as speech; measure it
# with tools/calibrate_voice.py, then set the default below or RETRO_START_RMS.
SAMPLE_RATE = 16000
BLOCK = 480                 # 30 ms at 16 kHz
START_RMS = float(os.environ.get("RETRO_START_RMS", "0.003"))
# Quieter than this is noise, not speech. It was 0.02, and that number was quietly
# throwing the owner's own voice away on this machine: the microphone array here hands
# over real words at a peak of 0.03 or so, and a phrase captured across a room can sit
# just below a threshold set from a headset. It is the floor for *both* halves of the
# microphone path - a phrase below it is never transcribed, and one above it is always
# lifted to the level the recogniser reads best - so it is one number, and it has to
# be a level a real microphone in a real room actually reaches.
MIN_PEAK = float(os.environ.get("RETRO_MIN_PEAK", "0.008"))

# Spoken replies (M3). VOICE_NAME is a substring matched against the installed
# Windows voices; tools/list_voices.py prints them and can change this choice.
VOICE_NAME = os.environ.get("RETRO_VOICE", "Zira")      # Microsoft Zira, English (US)
VOICE_RATE = int(os.environ.get("RETRO_VOICE_RATE", "180"))
VOICE_VOLUME = float(os.environ.get("RETRO_VOICE_VOLUME", "1.0"))

# One request, as the session sees it (M6). A request that is still running when it
# hits TASK_TIMEOUT_S is cancelled and the microphone is handed back, so the
# assistant can always be used again without restarting it. While a request is
# quiet for PROGRESS_EVERY_S the watchog speaks a line, because silence that long
# reads as a freeze - a small model on a CPU can take 30 seconds to plan.
TASK_TIMEOUT_S = float(os.environ.get("RETRO_TASK_TIMEOUT_S", "240"))
PROGRESS_EVERY_S = float(os.environ.get("RETRO_PROGRESS_S", "12"))
STATE_STUCK_S = float(os.environ.get("RETRO_STUCK_S", "12"))

# The cloud brain (M7). A 1B GGUF on a laptop CPU is a lookup table: it cannot
# plan six steps, cannot notice that a step failed, and cannot answer a question
# without being handed a command name. This is the way to a real assistant, with
# llama-cpp-python kept as the offline path for when there is no network.
#
# Everything here is spoken to over HTTPS with the standard library alone, so the
# project gains no new dependency.
CLOUD_BASE_URL = os.environ.get("RETRO_CLOUD_URL", "https://api.groq.com/openai/v1")
# What the window offers. "groq" is the fast one - a voice assistant should answer
# before the user wonders whether it heard them. "groq-smart" is the same provider
# with the larger model, for requests that take thought.
CLOUD_MODELS = {
    "groq": "openai/gpt-oss-20b",
    "groq-smart": "openai/gpt-oss-120b",
}
CLOUD_DEFAULT = os.environ.get("RETRO_CLOUD_MODEL", CLOUD_MODELS["groq"])
# Fifteen seconds, not thirty. This machine drops the TLS handshake to the API now
# and then, and a hung handshake is held for the whole timeout before anything is
# reported - so a short one fails fast and the retry gets a fresh socket.
CLOUD_TIMEOUT_S = float(os.environ.get("RETRO_CLOUD_TIMEOUT_S", "15"))
# How many times a request is attempted. A 429 from the free tier and a dropped
# connection are both ordinary, and both are worth trying again.
CLOUD_ATTEMPTS = int(os.environ.get("RETRO_CLOUD_ATTEMPTS", "3"))

# Speaking (M9). Hosted text to speech through Groq's Orpheus model: one short
# sentence per HTTPS call, played as soon as it arrives, so the answer starts being
# spoken while the rest of it is still being written. The SAPI voice stays as the
# offline path - a machine with no key or no network still has a voice.
# `playai-tts` used to be the model here; it was shut down at the end of 2025 and
# this is what Groq documents in its place.
# Which voice speaks. The local Windows voice is the shipped default now: it reaches
# the first word in about 0.13 s against 1.6 s for the hosted one, it cannot be rate
# limited (this machine saw 126 hosted failures), and a reply can never change voice
# halfway through. `RETRO_LOCAL_VOICE=0`, or `local_voice` in settings.json, brings
# the hosted voice back - the pipeline keeps both paths.
LOCAL_VOICE = os.environ.get("RETRO_LOCAL_VOICE", "1").strip().lower() not in ("0", "false", "no")
CLOUD_TTS_MODEL = os.environ.get("RETRO_TTS_MODEL", "canopylabs/orpheus-v1-english")
CLOUD_TTS_VOICE = os.environ.get("RETRO_TTS_VOICE", "autumn")
CLOUD_TTS_TIMEOUT_S = float(os.environ.get("RETRO_TTS_TIMEOUT_S", "20"))
# Japanese and Hindi (M16). SAPI on this machine has English voices only, so those
# two languages are spoken by Microsoft's neural voices through edge-tts, which
# needs no key. `RETRO_NEURAL_TTS=0` turns the second voice off and every sentence
# falls back to the local one. The voice names are environment-overridable because
# the endpoint's catalogue changes without notice.
NEURAL_TTS = os.environ.get("RETRO_NEURAL_TTS", "1").strip().lower() not in (
    "0", "false", "no", "off")
NEURAL_VOICES = {
    "ja": os.environ.get("RETRO_TTS_JA_VOICE", "ja-JP-NanamiNeural"),
    "hi": os.environ.get("RETRO_TTS_HI_VOICE", "hi-IN-SwaraNeural"),
}
# Eight seconds, not twelve: a request that has not answered in eight is a dropped
# connection, and the local voice should be reading the sentence by then rather than
# the reply still waiting on the network.
NEURAL_TIMEOUT_S = float(os.environ.get("RETRO_TTS_NEURAL_TIMEOUT_S", "8"))
# How long one "Retro" keeps the conversation open when "say Retro every time" is
# off. The owner asked for three minutes: long enough to talk, cook, and come back
# with a follow-up, and short enough that a room with a television in it cannot keep
# the microphone answering all evening. The conversation itself is remembered across
# the gap - only the wake word comes back.
CONVERSATION_WINDOW_S = float(os.environ.get("RETRO_CONVERSATION_WINDOW_S", "180"))
FOLLOWUP_WINDOW_S = CONVERSATION_WINDOW_S        # the old name, kept for callers
# How long Retro may say nothing at all before a "still working on it" kind of line
# is allowed. While it is speaking, or within this many seconds of having spoken,
# none of those lines may play: the owner asked for that explicitly, because a
# filler line over the top of an answer is an interruption.
FILLER_QUIET_S = float(os.environ.get("RETRO_FILLER_QUIET_S", "5"))
# How long a spoken reply may keep the microphone closed before it is cut off. Long
# enough for two or three sentences, short enough that the user can always talk over
# it: the intent is a conversation, not a broadcast.
SPEECH_QUEUE_MAX_S = float(os.environ.get("RETRO_SPEECH_MAX_S", "30"))

# Hearing the room (M12). The microphone hears this machine's speakers, and the
# follow-up window takes a command without the wake word - together those make a loop:
# the assistant says "Opening my settings", the recogniser returns "settings", and the
# same action runs again from a line nobody spoke. The log has it: one identical action
# twice, ten seconds apart, the second time from a nine-character transcript.
# ECHO_WINDOW_S is how long after the output *stops* something it said counts as its
# own voice. Four seconds is longer than the echo takes to come round and much shorter
# than the time it takes a person to repeat themselves, which is the line the owner
# complained about being crossed. ECHO_KEEP_S is how far back the words it said are
# kept to compare against; it is measured from when a line was *queued*, so it has to
# cover the longest reply as well as the gap after it.
ECHO_WINDOW_S = float(os.environ.get("RETRO_ECHO_WINDOW_S", "4.0"))
ECHO_KEEP_S = float(os.environ.get("RETRO_ECHO_KEEP_S", "45"))
# Two more guards, and both exist because one question was being answered three and
# four times. REPEAT_SUPPRESS_S: the same line must not be spoken twice inside this
# window, whatever route it arrived by - a plan's `say` step, a streamed sentence,
# the written-back reply, or a greeting the model wrote. DUPLICATE_ASK_S: the same
# *request* must not be run twice inside this window, so a recogniser that returns
# the same words twice (or an echo that slips past the window above) is dropped
# instead of doing the work - and saying it - again. Both windows are deliberately
# short: a person who really asks again says it again, which is different text.
#
# Twenty-five seconds rather than twelve, and the memory behind it forty lines rather
# than twelve, because the repeats that were still getting through were a *document*:
# a section is long, so the same sentence restated twenty seconds later fell outside
# the window and had already been forgotten. A reply that is one or two sentences is
# unaffected - it cannot reach forty lines or twenty-five seconds either way.
REPEAT_SUPPRESS_S = float(os.environ.get("RETRO_REPEAT_SUPPRESS_S", "25"))
DUPLICATE_ASK_S = float(os.environ.get("RETRO_DUPLICATE_ASK_S", "8"))
# And the machine's own playback, for the same reason: a room playing something is
# transcribed again and again, which is both wrong (the television is not talking to
# Retro) and expensive (every one of those lines is a round trip to the cloud
# recogniser). While the room is playing above ROOM_GATE_FLOOR a block has to be
# ROOM_GATE_FACTOR times louder than it to count as speech, so a person speaking over
# the television is heard and the television is not.
ROOM_GATE_FLOOR = float(os.environ.get("RETRO_ROOM_GATE_FLOOR", "0.01"))
ROOM_GATE_FACTOR = float(os.environ.get("RETRO_ROOM_GATE", "1.25"))
# The sampler reports its level amplified - x3.4, or x3.0 from the mixer fallback -
# so that quiet music still draws, while the microphone reports a raw RMS. This is how
# the two are made comparable, so the room gate above is not comparing a loudness with
# a different loudness.
AUDIENCE_GAIN = float(os.environ.get("RETRO_AUDIENCE_GAIN", "3.4"))

# Listening (M8). The local `base` Whisper on this machine's CPU is slow and is the
# reason a command comes back mis-heard; the hosted large-v3-turbo is more accurate
# and answers in a fraction of a second. The local model is kept, and is still used
# whenever there is no key or the network call fails, so Retro keeps listening.
CLOUD_STT_MODEL = os.environ.get("RETRO_CLOUD_STT", "whisper-large-v3-turbo")
CLOUD_STT_TIMEOUT_S = float(os.environ.get("RETRO_CLOUD_STT_TIMEOUT_S", "15"))
# How many past exchanges the agent is shown. Two people talking do not restart
# from nothing every sentence, and neither should Retro: "and type it" only means
# something next to what came before it.
CLOUD_HISTORY_TURNS = int(os.environ.get("RETRO_CLOUD_HISTORY", "6"))
# The API key is never kept in settings.json - that file says of itself that
# nothing in it is secret. It comes from the environment, or from a file beside
# the logs that only this user can read.
KEY_FILE = LOG_DIR.parent / "groq.key"
# What the key can call. It is cached beside the settings so that opening the window
# never waits on the network for it.
MODEL_LIST_CACHE = LOG_DIR.parent / "groq.models.json"
# Cloudflare fronts this API and answers a bare urllib request with "HTTP 403, error
# code: 1010" - a blocked user-agent signature, not a rejection by the provider. It
# lives here rather than in core/llm.py because the model listing is fetched from this
# module too, and both calls have to send the same string.
USER_AGENT = "Retro/1.0 (Windows voice assistant)"


def cloud_key():
    """The Groq API key: GROQ_API_KEY first, then the file the window writes."""
    value = os.environ.get("GROQ_API_KEY")
    if value and value.strip():
        return value.strip()
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def cloud_available():
    """True when a key is present, so the UI can offer the cloud brain."""
    return bool(cloud_key())


def save_cloud_key(key):
    """Writes the key file. Returns True when it landed. Never logs the value."""
    try:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(str(key).strip() + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


# ---------- which models this key can call ------------------------------------
#
# The picker is built from the key's own listing rather than from a table in this
# file: a free key and a paid one do not see the same models, and the two-name table
# this replaced went stale the moment the account changed. Nothing in this section is
# allowed to raise. core/config.py is imported by the window itself, and the first
# version of this code threw NameError out of the model picker and took the whole
# window down with it.

# What /models returns that is not a brain. Whisper transcribes, Orpheus speaks, and
# the guard and embedding models score text: none of them can take a turn in a chat,
# and asking one to answers with a 400 or an empty reply. They are still shown, named
# for what they are, because the list is meant to be the truth about the key rather
# than a curated subset of it.
NOT_CHAT_PARTS = ("whisper", "tts", "orpheus", "prompt-guard", "safeguard", "embed")

# The families whose answer arrives after a private thinking pass. Left alone that
# pass spends the whole completion budget before the first word of the answer is
# written, which is the one thing a spoken reply can never afford.
THINKING_MODEL_PARTS = ("gpt-oss", "reasoner", "deepseek-r1", "qwq")

# Measured on this machine by tools/model_probe.py: seconds from sending a question
# to the first word of the answer, median of five, over a connection that has already
# been opened once. It is the only latency a voice turn shows - the total hardly
# matters, because Retro speaks the first sentence while the rest is still being
# generated. An entry for a thinking model is with its reasoning switched down, which
# is what core/llm.py sends. Run-to-run spread is about 0.15 s, so anything within
# that of another row may as well be a tie; these numbers order the picker and decide
# nothing else.
MEASURED_FIRST_WORD_S = {
    "allam-2-7b": 0.28,
    "qwen/qwen3.8-27b": 0.34,
    "openai/gpt-oss-120b": 0.36,
    "openai/gpt-oss-20b": 0.59,
}
UNMEASURED_S = 9.9          # sorts after everything that was measured

# Two models inside this of each other are the same experience, so a tie goes to the
# one further down this list rather than to sixty milliseconds of noise. It runs from
# the model this project is developed against to the one it has been run least. Only
# chat models belong here, and a model outside the window never wins on rank.
TIE_WINDOW_S = 0.15
PREFERRED_MODELS = ("qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b",
                    "allam-2-7b")

# The rows offered until a listing has been fetched at least once, so a first launch
# with an empty cache still offers a working cloud brain while the refresh is in
# flight. They are the models this project has actually run against.
FALLBACK_MODELS = ("qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b")

MODEL_LIST_TTL_S = 24 * 60 * 60
_listing = None


def _read_listing():
    """The cached listing, or None. Never raises and never touches the network."""
    global _listing
    if _listing is not None:
        return _listing
    try:
        if not MODEL_LIST_CACHE.exists():
            return None
        if time.time() - MODEL_LIST_CACHE.stat().st_mtime > MODEL_LIST_TTL_S:
            return None
        data = json.loads(MODEL_LIST_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    _listing = [str(name) for name in data if str(name).strip()]
    return _listing


def _write_listing(ids):
    """Caches the listing, so the next window opens without a network call."""
    global _listing
    _listing = [str(name) for name in ids]
    try:
        MODEL_LIST_CACHE.parent.mkdir(parents=True, exist_ok=True)
        MODEL_LIST_CACHE.write_text(json.dumps(_listing, indent=2), encoding="utf-8")
    except OSError:
        pass            # a listing that cannot be cached is not worth failing over


def fetch_model_ids(timeout=6):
    """Asks Groq what this key can call. Returns (ids, error) and never raises."""
    key = cloud_key()
    if not key:
        return [], "no API key"
    requirement = Request(CLOUD_BASE_URL + "/models",
                          headers={"Authorization": f"Bearer {key}",
                                   "Accept": "application/json",
                                   "User-Agent": USER_AGENT})
    try:
        with urlopen(requirement, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except OSError as exc:
        return [], f"cannot reach Groq ({exc})"
    except ValueError as exc:
        return [], f"Groq sent something unreadable ({exc})"
    ids = []
    for entry in data.get("data") or []:
        name = str((entry or {}).get("id") or "").strip()
        if name and name not in ids:
            ids.append(name)
    if not ids:
        return [], "this key can call no models"
    return ids, None


def refresh_model_ids(timeout=6):
    """Re-reads the listing from Groq and caches it. Returns (ids, error)."""
    ids, error = fetch_model_ids(timeout=timeout)
    if ids:
        _write_listing(ids)
    return ids, error


def model_ids():
    """Every model this key can call, from the cache. Never blocks on the network."""
    return list(_read_listing() or [])


def known_model_ids():
    """The cached listing when there is one, and the built-in rows until there is."""
    return model_ids() or list(FALLBACK_MODELS)


def is_chat_model(model_id):
    """False for what the listing returns that cannot answer a chat completion."""
    name = str(model_id or "").lower()
    return bool(name) and not any(part in name for part in NOT_CHAT_PARTS)


def first_word_s(model_id):
    """The measured time to the first word, or the penalty for the unmeasured."""
    return MEASURED_FIRST_WORD_S.get(str(model_id or "").lower(), UNMEASURED_S)


def preference_rank(model_id):
    """Where a model sits in PREFERRED_MODELS; anything absent ranks last."""
    name = str(model_id or "").lower()
    for rank, known in enumerate(PREFERRED_MODELS):
        if known.lower() == name:
            return rank
    return len(PREFERRED_MODELS)


def _quickness_order(chat):
    """The chat models ordered for a voice turn: quickest, ties by preference.

    Sorting on the measurement alone put a tie in whatever order the listing happened
    to arrive in, which is how a 7B model became the assistant's default over a 27B
    one on 60 ms of noise. Only models inside TIE_WINDOW_S of the quickest are treated
    as tied, so a genuinely faster model still comes first.
    """
    if not chat:
        return []
    quickest = min(first_word_s(m) for m in chat)
    tied = [m for m in chat if first_word_s(m) - quickest <= TIE_WINDOW_S]
    rest = [m for m in chat if m not in tied]
    return (sorted(tied, key=lambda m: (preference_rank(m), m.lower()))
            + sorted(rest, key=lambda m: (first_word_s(m), preference_rank(m), m.lower())))


def reasoning_controls(model_id):
    """The chat fields that stop a thinking model spending the turn thinking.

    openai/gpt-oss is the family on this key that thinks before it speaks, and the
    thinking is charged to the same completion budget as the answer. Measured by
    tools/model_probe.py on this machine, `reasoning_effort: "low"` takes the first
    word of a short answer from 0.80 s to 0.53 s on gpt-oss-20b, and with a tight
    budget the default is worse than slow: asked for a five-word greeting with
    max_completion_tokens=60 it came back with no content at all, because the
    reasoning used the whole budget before the answer started - which reaches the
    voice as "the reply carried no text" and reaches the user as an assistant that
    said nothing.

    `reasoning_format: "hidden"` asks the provider not to send the reasoning at all.
    Nothing here reads it - only `delta.content` is ever spoken - so this is a guard
    rather than a fix, and a cheap one.

    The fields are never sent to a model that does not want them: allam-2-7b answers
    the pair with HTTP 400, and qwen3.8-27b answered with an empty content, which is
    exactly the failure this exists to prevent.
    """
    name = str(model_id or "").lower()
    if not any(part in name for part in THINKING_MODEL_PARTS):
        return {}
    return {"reasoning_effort": "low", "reasoning_format": "hidden"}


def fastest_model():
    """The quickest chat model on offer: what AUTO means. Never raises."""
    order = _quickness_order([m for m in known_model_ids() if is_chat_model(m)])
    return order[0] if order else CLOUD_MODELS["groq"]


def alternates_for(model_id):
    """The other chat models this key can call, quickest first.

    A rate limit on this provider is counted per model, so the next model down the
    list is a way out of a 429 that waiting is not. The chosen model is left out: it
    is the one that just refused.
    """
    chosen = str(model_id or "").lower()
    order = _quickness_order([m for m in known_model_ids() if is_chat_model(m)])
    return [name for name in order if name.lower() != chosen]


def model_choices():
    """The picker's rows, quickest brain first. Never raises.

    Each row is {"label", "value", "kind", "tooltip"}. `kind` is what the window
    needs to grey a row out: "auto" always runs, "cloud" needs a key, "nonchat" is a
    model that cannot answer a chat request at all, and "local" needs its GGUF on
    disk. The value is the exact id the provider is called with, so a choice means one
    model and never a nickname standing for a list that has since changed.
    """
    rows = [{"label": "AUTO - quickest available", "value": "auto", "kind": "auto",
             "tooltip": "the typed rules first, then the quickest model this key can call"}]
    listed = known_model_ids()
    brains = _quickness_order([m for m in listed if is_chat_model(m)])
    for model_id in brains:
        seconds = MEASURED_FIRST_WORD_S.get(model_id.lower())
        rows.append({
            "label": model_id,
            "value": model_id,
            "kind": "cloud",
            "tooltip": (f"first word in about {seconds:.2f} s on this machine"
                        if seconds else "not measured on this machine"),
        })
    for model_id in sorted(m for m in listed if not is_chat_model(m)):
        rows.append({"label": f"{model_id} (not a chat model)", "value": model_id,
                     "kind": "nonchat",
                     "tooltip": "this model transcribes, speaks or scores text, and "
                                "cannot answer a chat request"})
    for key in sorted(MODEL_FILES):
        rows.append({"label": f"LOCAL {key.upper()}", "value": key, "kind": "local",
                     "tooltip": MODEL_FILES[key]})
    return rows


def cloud_model_for(choice):
    """The exact model id for a picker value, or None when it is not a cloud one.

    Three kinds of value arrive here: a nickname from CLOUD_MODELS ("groq",
    "groq-smart"), the literal id a picker row carries, and "auto"/"cloud", which mean
    the quickest model on offer. Anything else - "1b", "500m" - is local, and answers
    None so the caller falls through to the GGUF.
    """
    choice = str(choice or "").lower()
    if choice in CLOUD_MODELS:
        return CLOUD_MODELS[choice]
    if choice in ("auto", "cloud"):
        return fastest_model()
    for known in known_model_ids():
        if known.lower() == choice:
            return known
    return None
