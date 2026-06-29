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

    def get_status(self, printer_id: int):
        return self._state if printer_id == 1 else None


@pytest.mark.asyncio
async def test_check_once_turns_off_idle_light_after_delay(monkeypatch):
    service = ChamberLightAutoOffService()
    service._idle_light_since[1] = 0

    async def settings():
        return True, 1, False

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
        return True, 1, False

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


@pytest.mark.asyncio
async def test_handle_status_change_flashes_once_for_new_error(monkeypatch):
    service = ChamberLightAutoOffService(flash_interval=0)

    async def settings():
        return False, 30, True

    async def enabled_for_printer(printer_id: int, global_enabled: bool):
        return global_enabled

    calls = []
    state = SimpleNamespace(
        connected=True,
        chamber_light=False,
        state="IDLE",
        hms_errors=[SimpleNamespace(attr=0x05008051, severity=2)],
    )
    client = SimpleNamespace(set_chamber_light=lambda on: calls.append(on) or True)
    monkeypatch.setattr(service, "_settings", settings)
    monkeypatch.setattr(service, "_flash_enabled_for_printer", enabled_for_printer)
    monkeypatch.setattr(module, "printer_manager", _FakePrinterManager(state, client))

    await service.handle_status_change(1, state)
    await service._flash_tasks[1]
    await service.handle_status_change(1, state)

    assert calls == [True, False, True, False, True, False]
    assert state.chamber_light is False


@pytest.mark.asyncio
async def test_handle_status_change_respects_printer_override(monkeypatch):
    service = ChamberLightAutoOffService(flash_interval=0)

    async def settings():
        return False, 30, True

    async def disabled_for_printer(printer_id: int, global_enabled: bool):
        return False

    calls = []
    state = SimpleNamespace(
        connected=True,
        chamber_light=False,
        state="IDLE",
        hms_errors=[SimpleNamespace(attr=0x05008051, severity=2)],
    )
    client = SimpleNamespace(set_chamber_light=lambda on: calls.append(on) or True)
    monkeypatch.setattr(service, "_settings", settings)
    monkeypatch.setattr(service, "_flash_enabled_for_printer", disabled_for_printer)
    monkeypatch.setattr(module, "printer_manager", _FakePrinterManager(state, client))

    await service.handle_status_change(1, state)

    assert calls == []
    assert service._flash_tasks == {}
