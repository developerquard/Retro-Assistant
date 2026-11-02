"""Runs one request end to end: the typed rules first, the agent when they can't.

The order matters and was chosen from what actually went wrong:

1. the twenty typed rules answer instantly and are measured at 100% on the intent
   set, so they stay first;
2. a request that is clearly more than one thing - "open spotify and play
   sunflower" - goes straight to the agent, because picking the single best
   command for a two-part sentence is exactly how the second half got dropped;
3. otherwise one cheap model call tries to name a single command, which is what
   catches phrasings the rules miss;
4. if that finds nothing, the agent gets the request and the whole desktop.

Refusals still come before the model, and before the agent: the tools in
core/tools.py cannot shut the machine down or message anybody, so the answer is a
spoken reason rather than a wrong action.
"""
import re
import threading

from . import builder, compose, config, diag, guard, log, modes, runtime
from .control import Stopped
from .model import Result
from .router import Ambiguous, route

# Verbs that mean the sentence is asking for something to be done rather than
# asking a question. This is the line between the two paths: a request that names an
# action goes to the agent (which can choose a tool or explain why it can't), and
# everything else is conversation, answered by the model and spoken as it is written.
_ACTION_WORDS = re.compile(
    r"\b(?:open|launch|start|play|pause|resume|skip|next|previous|prev|type|click|"
    r"press|write|create|make|build|save|send|message|run|set|switch|search|find|"
    r"focus|scroll|turn|increase|decrease|mute|unmute|volume|timer|note|delete|close|"
    r"quit|install|uninstall|download|screenshot|copy|paste|refresh|rename|move|"
    r"copy|join|call|dial|book|schedule|add|change|edit|fill|log|sign)\b", re.I)

# Words that mean the sentence asks for more than one thing. Deliberately crude:
# the agent plans a single request perfectly well, so a false positive costs a
# little latency, while a false negative silently drops half of what was asked.
CONNECTORS = re.compile(r"\b(?:and then|then|after that|and|aur|phir|uske baad|ke baad)\b")


def ask_yes_no(question, timeout=config.CONFIRM_TIMEOUT_S):
    """Typed yes/no with a timeout. Anything other than an explicit yes is a no."""
    box = {}

    def read():
        try:
            box["answer"] = input(question + " ")
        except EOFError:
            box["answer"] = ""

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(timeout)
    if "answer" not in box:
        print()
        return False
    return box["answer"].strip().lower() in ("yes", "y", "haan", "ha", "हाँ", "हां")


def looks_multi(text):
    """True when the sentence asks for more than one thing."""
    return bool(CONNECTORS.search(" ".join(str(text).lower().split())))

_UI_AUTOMATION = re.compile(
    r"\b(?:type|click|press|enter|search for|find and|go to and|open and|launch and|start and)\b",
    re.I,
)


def describes_ui_flow(text):
    """True when the text describes typing, clicking or pressing in a window.

    Such a sentence is always a multi-step UI automation job: bring the window
    to the front, then type or click in it. The agent is the right tool for that,
    even when a single typed rule also matches the first verb.
    """
    return bool(_UI_AUTOMATION.search(" ".join(str(text).lower().split())))


def _safe_args(args):
    """Replace free text with its length before it reaches the log."""
    out = {}
    for k, v in args.items():
        if k == "text":
            out["text_chars"] = len(v)
        elif k == "query":
            out["query_chars"] = len(v)
        else:
            out[k] = v
    return out


def execute(action, args, confirm=ask_yes_no, task=None):
    if task is not None and not task.alive():
        return Result(False, "Stopped.")
    base = {"action": action.name, "args": _safe_args(args)}
    if action.tier == "sensitive":
        question = f"Retro: {action.description} with {_safe_args(args)}. Proceed? (yes/no)"
        if not confirm(question):
            log.record({**base, "result": "cancelled"})
            return Result(False, "Cancelled.")
    try:
        res = action.run(args)
    except Exception as exc:    # one failed action must not crash the loop
        log.record({**base, "result": "error", "error": type(exc).__name__})
        return Result(False, f"Something went wrong ({type(exc).__name__}).")
    log.record({**base, "result": "ok" if res.ok else "failed", "verified": res.verified})
    return res


def describe_work(text):
    """True when a request is a job rather than a one-step command.

    Used for the one thing said at the very start of a request. A multi-part
    sentence, a flow that types or clicks, and a document being written are all jobs
    that will take real work, and they get a short acknowledgement immediately so the
    first thing heard is not the answer arriving eight seconds later. A single
    command keeps its silence and its instant result.
    """
    return bool(looks_multi(text) or describes_ui_flow(text)
                or compose.wants_document(text) or builder.wants_project(text))


def _run_agent(text, planner, task, on_slow, mode=None, on_work=None):
    """Hands the request to the agent and logs what it did, step by step."""
    if on_work is not None:
        on_work(text)
    if on_slow is not None:
        on_slow()
    log.record({"route": "agent"})
    result = planner.run(text, task=task, note=note_agent_step, mode=mode)
    log.record({"route": "agent", "result": "ok" if result.ok else "failed",
                "steps": getattr(task, "steps", 0)})
    return result


def _run_chat(text, planner, task, on_slow, on_sentence, on_work=None):
    """A conversational reply, written and spoken sentence by sentence.

    `on_sentence` is the pipeline's mouth: every finished sentence is handed over as
    soon as it exists, so the first words are heard while the rest of the answer is
    still being written. Nothing here can touch the machine - that is the point of
    routing conversation through its own path rather than through the planner.
    """
    if on_work is not None:
        on_work(text)
    if on_slow is not None:
        on_slow()
    log.record({"route": "chat"})
    try:
        reply = planner.respond(text, task=task, emit=on_sentence)
    except Stopped:
        # The user said stop while the answer was still being written. The session
        # has already said so; there is no failure to report on top of it.
        return Result(False, "Stopped.")
    except Exception as exc:
        log.event("chat_failed", error=type(exc).__name__)
        return Result(False, "My thinking failed there - the language model didn't answer.")
    if not reply:
        return Result(False, "I didn't have an answer for that.")
    return Result(True, reply, verified=False)


def note_agent_step(name, args):
    """One line per step: the event log, and the label beside the circle.

    The label is drawn from the step that is really running, which is what the user
    asked the floating text to be - a real execution event, not invented activity.
    Arguments are summarised before they are logged, never stored.
    """
    log.event("agent_step", tool=name, args=_safe_args(args))
    active = runtime.session()
    if active is not None:
        try:
            active.step_progress(name, args)
        except Exception:                 # a label must never break a step
            pass


def handle(text, confirm=ask_yes_no, fallback=None, planner=None, task=None, on_slow=None,
           converse=False, mode=None, on_sentence=None, on_work=None):
    """One request end to end. `converse` means the model behind `planner` is good
    enough to talk to, not only to name commands. `mode` is chat or work, and it is
    checked before a typed rule is allowed to act."""
    # Intent classification is the typed rules; its cost is measured separately from
    # the model that is asked afterwards, which is the difference between "the rules
    # are slow" and "the brain is slow".
    with diag.span("route"):
        m = route(text)
    if m is not None:
        diag.mark("rules")
    if isinstance(m, Ambiguous):
        return Result(False, "That could mean: " + ", ".join(m.names) + ". Please say it another way.")
    if m is not None and mode is not None and not modes.allows_tool(mode, m.action):
        # Chat mode, and a real command matched. The rule does not run; the user is
        # told why in the model's own words, and offered the switch.
        said = (planner.line("chat", detail=text, fallback="")
                if converse and planner is not None else "")
        log.record({"route": "mode_refused", "action": m.action.name})
        return Result(False, said or f"I'm in chat mode, so I didn't {m.action.name}. "
                                     f"Say 'work mode' and I'll do it.")
    # A request that describes a UI automation flow - typing, clicking, pressing -
    # is always a multi-step job for the agent, even when a single typed rule also
    # matches the first verb. Without this, "open whatsapp and type hi" opened
    # WhatsApp and dropped the typing step, and "open notepad and type hello"
    # opened Notepad and stopped.
    if (looks_multi(text) or describes_ui_flow(text)) and planner is not None and planner.available:
        diag.mark("agent")
        return _run_agent(text, planner, task, on_slow, mode=mode, on_work=on_work)
    if m is None:
        # Refusals come before the model: it was forcing commands onto sentences
        # that asked for none, and some of those mistakes act on the machine.
        # A conversational brain only keeps the refusals that protect the machine;
        # the canned small-talk lines are skipped so it can answer in its own words.
        refusal = guard.check(text, only_hard=converse)
        if refusal is not None:
            log.record({"route": "refused"})
            return Result(False, refusal)
    # With a real model the sentence is either a job for the agent or something to
    # answer, and the difference is decided by whether it asks for an action at all -
    # not by asking the model twice. Conversation goes down its own path, where the
    # reply is streamed and spoken as it is written and no tool can run by accident.
    if converse and m is None and planner is not None and planner.available:
        if _ACTION_WORDS.search(" ".join(str(text).lower().split())):
            diag.mark("agent")
            return _run_agent(text, planner, task, on_slow, mode=mode, on_work=on_work)
        diag.mark("chat")
        return _run_chat(text, planner, task, on_slow, on_sentence, on_work=on_work)
    if m is None and fallback is not None:
        m, reason = fallback(text)
        log.record({"route": "model", "reason": reason})   # the reason label only, never the text
    if m is None and planner is not None and planner.available:
        diag.mark("agent")
        return _run_agent(text, planner, task, on_slow, on_work=on_work)
    if m is None:
        return Result(False, "I don't know that command yet.")
    result = execute(m.action, m.args, confirm=confirm, task=task)
    # A spoken application name often fits more than one installed app. The rules
    # can't pick between them, so the model chooses - from the real shortlist only.
    if not result.ok and result.choices and m.action.name == "open_app":
        chooser = getattr(fallback, "choose", None)
        if chooser is not None:
            picked = chooser(text, result.choices)
            if picked:
                log.record({"route": "model_choice"})
                return execute(m.action, {"name": picked}, confirm=confirm, task=task)
    return result
