"""Status display. M1 prints status lines in the console. A status window comes later."""


def status(state, detail=""):
    print(f"  [{state}] {detail}".rstrip(), flush=True)
