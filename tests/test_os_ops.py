import sys
import types

import pytest

from core import os_ops


@pytest.fixture
def install_fake_pycaw(monkeypatch):
    """Replace pycaw.pycaw with a stub so these tests run without pycaw or COM."""

    class IAudioEndpointVolume:         # only the COM id matters to os_ops
        _iid_ = "fake-iid"

    def install(device):
        sub = types.ModuleType("pycaw.pycaw")
        sub.AudioUtilities = types.SimpleNamespace(GetSpeakers=lambda: device)
        sub.IAudioEndpointVolume = IAudioEndpointVolume
        pkg = types.ModuleType("pycaw")
        pkg.pycaw = sub
        monkeypatch.setitem(sys.modules, "pycaw", pkg)
        monkeypatch.setitem(sys.modules, "pycaw.pycaw", sub)

    return install


class EndpointVolume:
    def __init__(self, calls):
        self._calls = calls

    def SetMasterVolumeLevelScalar(self, level, context):
        self._calls.append((level, context))


def test_set_volume_uses_the_modern_endpoint_attribute(install_fake_pycaw):
    calls = []

    class Device:                       # pycaw 2023+ shape
        EndpointVolume = EndpointVolume(calls)

    install_fake_pycaw(Device())
    ok, message = os_ops.set_volume_percent(40)
    assert ok and message == ""
    assert calls == [(0.4, None)]


def test_set_volume_falls_back_to_activate_for_old_pycaw(install_fake_pycaw, monkeypatch):
    calls = []

    class RawDevice:                    # pre-2023 shape: no EndpointVolume
        def Activate(self, _iid, _clsctx, _unknown):
            return "activated-interface"

    fake_ctypes = types.ModuleType("ctypes")
    fake_ctypes.POINTER = lambda _t: object
    fake_ctypes.cast = lambda _iface, _t: EndpointVolume(calls)

    fake_comtypes = types.ModuleType("comtypes")
    fake_comtypes.CLSCTX_ALL = 0

    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setitem(sys.modules, "comtypes", fake_comtypes)
    install_fake_pycaw(RawDevice())

    ok, message = os_ops.set_volume_percent(10)
    assert ok and message == ""
    assert calls == [(0.1, None)]


def test_set_volume_reports_a_missing_pycaw_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "pycaw", None)
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", None)
    ok, message = os_ops.set_volume_percent(40)
    assert not ok and "pycaw" in message
