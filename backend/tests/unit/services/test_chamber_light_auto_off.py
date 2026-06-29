from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services import chamber_light_auto_off as module
from backend.app.services.chamber_light_auto_off import ChamberLightAutoOffService


class _FakePrinterManager:
    def __init__(self, state, client):
        self._state = state
        self._client = client

    def get_all_statuses(self):
        return {1: self._state}

    def get_client(self, printer_id: int):
        return self._client if printer_id == 1 else None


@pytest.mark.asyncio
async def test_check_once_turns_off_idle_light_after_delay(monkeypatch):
    service = ChamberLightAutoOffService()
    service._idle_light_since[1] = 0

    async def settings():
        return True, 1

    state = SimpleNamespace(connected=True, chamber_light=True, state="IDLE")
    client = SimpleNamespace(set_chamber_light=lambda on: not on)
    monkeypatch.setattr(service, "_settings", settings)
    monkeypatch.setattr(module, "printer_manager", _FakePrinterManager(state, client))
    monkeypatch.setattr(module.time, "monotonic", lambda: 61)

    await service.check_once()

    assert state.chamber_light is False
    assert 1 not in service._idle_light_since


@pytest.mark.asyncio
async def test_check_once_does_not_turn_off_while_printing(monkeypatch):
    service = ChamberLightAutoOffService()
    service._idle_light_since[1] = 0

    async def settings():
        return True, 1

    calls = []
    state = SimpleNamespace(connected=True, chamber_light=True, state="PRINTING")
    client = SimpleNamespace(set_chamber_light=lambda on: calls.append(on) or True)
    monkeypatch.setattr(service, "_settings", settings)
    monkeypatch.setattr(module, "printer_manager", _FakePrinterManager(state, client))
    monkeypatch.setattr(module.time, "monotonic", lambda: 61)

    await service.check_once()

    assert state.chamber_light is True
    assert calls == []
    assert 1 not in service._idle_light_since
