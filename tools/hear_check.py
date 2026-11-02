"""Does sound actually come out? Plays a line and reads the Windows mixer for proof.

    python tools/hear_check.py                 # the device Retro is set to use
    python tools/hear_check.py --device 18     # one device by index
    python tools/hear_check.py --all           # every output Retro offers, one after another
    python tools/hear_check.py --seconds 2     # a longer line to listen to

## Why this exists

"I can't hear it even if it speaks" was reported twice, and both times the app was
sure it had spoken. It was sure because the only thing it measured was the samples
it had just put in the buffer - a number that is the same whether the audio reached
your ears, a switched-off Bluetooth headset, or nothing at all. A peak read out of
memory cannot tell you that anybody heard anything.

So this measures the one thing that can: the **Windows mixer's peak meter for this
own process**, read while the line is playing. If the meter moves, sound left the
process on its way to a device. If it stays at zero, then whatever the app thinks,
nothing was audible - and the usual reason is a device index that pointed at those
speakers yesterday.

It uses the real speech path (`core.voice.Speaker`), not a copy of it, so a report
of "handed to the device" here is a report about the shipped code.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LINE = "This is Retro. If you can hear this line, the voice is reaching your speakers."


def hear(device, line=LINE, seconds=None):
    """Plays one line on `device` and returns (played, buffer peak, mixer peak, index).

    The mixer peak is None when the mixer could not be read at all, which is
    reported as unknown rather than as silence - the one answer this tool must never
    fake. The two peaks are different questions: the first is what we rendered, the
    second is what the machine's own meter saw leave the process.
    """
    from core.voice import Speaker
    speaker = Speaker()
    speaker.ensure_voices()
    if device != "keep":
        speaker.choose_output(device)
    chosen = speaker.device
    if seconds:
        line = line * max(1, int(round(seconds / 3.0)))
    played, mixer_peak = speaker.audible(line)
    return played, speaker.last_peak, mixer_peak, chosen


def describe(peaks, played, chosen):
    if not played:
        return "the line was cut off or written off as wedged"
    if peaks is None:
        return "the mixer could not be read, so audibility is unknown"
    if peaks < 0.001:
        return "the mixer heard NOTHING - this device is not where you are listening"
    return "the mixer confirms sound left the process"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="saved",
                        help="an index, 'saved' for the remembered one, or 'default'")
    parser.add_argument("--line", default=LINE)
    parser.add_argument("--seconds", type=float, default=None)
    parser.add_argument("--all", action="store_true",
                        help="try the saved device and every offered output in turn")
    args = parser.parse_args()

    from core import settings
    from core.voice import default_output_name, output_devices

    saved = settings.get("output")
    print(f"settings : output={saved!r}  voice={settings.get('voice')!r}")
    print(f"system   : default output is {default_output_name()!r}")
    print(f"offered  : {', '.join(str(i) for i, _n in output_devices())}")

    if args.all:
        queue = [("saved (%r)" % (saved,), saved), ("default", None)]
        queue += [(f"offered {index}", index) for index, _name in output_devices()]
    elif args.device == "saved":
        queue = [("saved (%r)" % (saved,), saved)]
    elif args.device == "default":
        queue = [("default", None)]
    else:
        queue = [(f"device {args.device}", int(args.device))]

    worst = 0
    for label, device in queue:
        print(f"\n--- {label}")
        try:
            played, peak, mixer_peak, chosen = hear(device, args.line, args.seconds)
        except Exception as exc:
            print(f"    refused  : {type(exc).__name__}: {exc}")
            worst = max(worst, 1)
            continue
        mixer_text = "unreadable" if mixer_peak is None else f"{mixer_peak:.3f}"
        print(f"    used     : device {chosen!r}, played={played}")
        print(f"    rendered : peak {peak:.2f} (in buffer, not proof)")
        print(f"    mixer    : peak {mixer_text}")
        print(f"    verdict  : {describe(mixer_peak, played, chosen)}")
        if mixer_peak is not None and mixer_peak < 0.001:
            worst = max(worst, 1)
    print()
    return worst


if __name__ == "__main__":
    sys.exit(main())
