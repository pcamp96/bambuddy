"""Chamber light automation for idle printers and printer faults."""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from backend.app.core.database import async_session
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)


PRINTING_STATES = {
    "running",
    "printing",
    "prepare",
    "preparing",
    "pause",
    "paused",
    "slicing",
}

PRINT_START_STATES = {
    "running",
    "printing",
}


class ChamberLightAutoOffService:
    """Automates chamber lights for supported printers."""

    def __init__(self, check_interval: int = 60, flash_interval: float = 0.5):
        self._check_interval = check_interval
        self._flash_interval = flash_interval
        self._task: asyncio.Task | None = None
        self._idle_light_since: dict[int, float] = {}
        self._settings_cache: tuple[float, tuple[bool, int, bool, bool, int, bool]] | None = None
        self._last_error_signature: dict[int, str] = {}
        self._flash_tasks: dict[int, asyncio.Task] = {}
        self._print_light_since: dict[int, float] = {}
        self._print_light_layer_off_done: set[int] = set()

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("Chamber light automation scheduler started")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("Chamber light automation scheduler stopped")

    async def _loop(self):
        while True:
            try:
                await self.check_once()
            except Exception as e:
                logger.error("Error in chamber light auto-off check: %s", e)
            await asyncio.sleep(self._check_interval)

    async def _settings(self) -> tuple[bool, int, bool, bool, int, bool]:
        defaults = {
            "chamber_light_auto_off_enabled": "false",
            "chamber_light_auto_off_minutes": "30",
            "chamber_light_flash_on_error_enabled": "false",
            "chamber_light_print_auto_off_enabled": "false",
            "chamber_light_print_auto_off_minutes": "10",
            "chamber_light_print_auto_off_first_layer_enabled": "false",
        }
        cached = self._settings_cache
        now = time.monotonic()
        if cached and now - cached[0] < 15:
            return cached[1]

        async with async_session() as db:
            result = await db.execute(select(Settings).where(Settings.key.in_(list(defaults.keys()))))
            rows = {row.key: row.value for row in result.scalars().all()}

        enabled = (
            rows.get(
                "chamber_light_auto_off_enabled",
                defaults["chamber_light_auto_off_enabled"],
            )
            or ""
        ).lower() == "true"
        flash_on_error = (
            rows.get(
                "chamber_light_flash_on_error_enabled",
                defaults["chamber_light_flash_on_error_enabled"],
            )
            or ""
        ).lower() == "true"
        print_auto_off = (
            rows.get(
                "chamber_light_print_auto_off_enabled",
                defaults["chamber_light_print_auto_off_enabled"],
            )
            or ""
        ).lower() == "true"
        print_first_layer_off = (
            rows.get(
                "chamber_light_print_auto_off_first_layer_enabled",
                defaults["chamber_light_print_auto_off_first_layer_enabled"],
            )
            or ""
        ).lower() == "true"
        try:
            minutes = int(
                rows.get(
                    "chamber_light_auto_off_minutes",
                    defaults["chamber_light_auto_off_minutes"],
                )
                or "30"
            )
        except (TypeError, ValueError):
            minutes = 30
        minutes = max(1, min(minutes, 240))
        try:
            print_minutes = int(
                rows.get(
                    "chamber_light_print_auto_off_minutes",
                    defaults["chamber_light_print_auto_off_minutes"],
                )
                or "10"
            )
        except (TypeError, ValueError):
            print_minutes = 10
        print_minutes = max(1, min(print_minutes, 240))
        settings = (
            enabled,
            minutes,
            flash_on_error,
            print_auto_off,
            print_minutes,
            print_first_layer_off,
        )
        self._settings_cache = (now, settings)
        return settings

    @staticmethod
    def _is_printing_or_paused(state_name: str | None) -> bool:
        normalized = (state_name or "").strip().lower()
        return normalized in PRINTING_STATES

    @staticmethod
    def _error_signature(state) -> str | None:
        hms_errors = [
            error for error in (getattr(state, "hms_errors", []) or []) if getattr(error, "severity", 0) in (1, 2, 3)
        ]
        hms_codes = sorted(f"{getattr(error, 'attr', 0):08x}" for error in hms_errors)
        state_name = (getattr(state, "state", "") or "").strip().lower()
        state_error = state_name in {"error", "failed", "failure"}

        if not hms_codes and not state_error:
            return None

        return f"state:{state_name}|hms:{','.join(hms_codes)}"

    async def handle_status_change(self, printer_id: int, state):
        """Flash chamber lights once when a new active error appears."""
        signature = self._error_signature(state)
        if not signature:
            self._last_error_signature.pop(printer_id, None)
            self._stop_flash(printer_id)
            return

        if getattr(state, "door_open", False):
            self._last_error_signature.pop(printer_id, None)
            self._stop_flash(printer_id)
            return

        if self._last_error_signature.get(printer_id) == signature:
            return
        self._last_error_signature[printer_id] = signature

        _, _, global_flash_on_error, _, _, _ = await self._settings()
        if not await self._flash_enabled_for_printer(printer_id, global_flash_on_error):
            return

        existing = self._flash_tasks.get(printer_id)
        if existing and not existing.done():
            return

        task = asyncio.create_task(
            self._flash_until_cleared(
                printer_id,
                signature,
                getattr(state, "chamber_light", False),
            ),
            name=f"chamber-light-error-flash-{printer_id}",
        )
        self._flash_tasks[printer_id] = task
        task.add_done_callback(lambda _: self._flash_tasks.pop(printer_id, None))

    def _stop_flash(self, printer_id: int):
        task = self._flash_tasks.pop(printer_id, None)
        if task and not task.done():
            task.cancel()

    async def _flash_enabled_for_printer(self, printer_id: int, global_enabled: bool) -> bool:
        async with async_session() as db:
            result = await db.execute(select(Printer.chamber_light_flash_on_error).where(Printer.id == printer_id))
            override = result.scalar_one_or_none()
        return global_enabled if override is None else bool(override)

    async def _print_auto_off_enabled_for_printer(self, printer_id: int, global_enabled: bool) -> bool:
        async with async_session() as db:
            result = await db.execute(select(Printer.chamber_light_print_auto_off).where(Printer.id == printer_id))
            override = result.scalar_one_or_none()
        return global_enabled if override is None else bool(override)

    async def _flash_until_cleared(self, printer_id: int, signature: str, original_on: bool):
        logger.info("Flashing chamber light for printer %s error until acknowledged", printer_id)
        try:
            while True:
                client = printer_manager.get_client(printer_id)
                state = printer_manager.get_status(printer_id)
                if (
                    not client
                    or not hasattr(client, "set_chamber_light")
                    or not state
                    or not getattr(state, "connected", False)
                    or getattr(state, "door_open", False)
                    or self._error_signature(state) != signature
                ):
                    break

                if not client.set_chamber_light(True):
                    break
                state.chamber_light = True
                await asyncio.sleep(self._flash_interval)

                state = printer_manager.get_status(printer_id)
                if (
                    not state
                    or not getattr(state, "connected", False)
                    or getattr(state, "door_open", False)
                    or self._error_signature(state) != signature
                ):
                    break

                if not client.set_chamber_light(False):
                    break
                state.chamber_light = False
                await asyncio.sleep(self._flash_interval)
        except asyncio.CancelledError:
            raise
        finally:
            client = printer_manager.get_client(printer_id)
            state = printer_manager.get_status(printer_id)
            if client and hasattr(client, "set_chamber_light") and state and getattr(state, "connected", False):
                if client.set_chamber_light(original_on):
                    state.chamber_light = original_on

    async def check_once(self):
        (
            enabled,
            minutes,
            _,
            print_auto_off,
            print_minutes,
            print_first_layer_off,
        ) = await self._settings()
        if not enabled:
            self._idle_light_since.clear()
        await self._check_print_auto_off(print_auto_off, print_minutes, print_first_layer_off)
        if not enabled:
            return

        now = time.monotonic()
        delay_seconds = minutes * 60
        statuses = printer_manager.get_all_statuses()
        for printer_id, state in statuses.items():
            client = printer_manager.get_client(printer_id)
            if (
                not client
                or not hasattr(client, "set_chamber_light")
                or printer_id in self._flash_tasks
                or not state.connected
                or not state.chamber_light
                or self._is_printing_or_paused(state.state)
            ):
                self._idle_light_since.pop(printer_id, None)
                continue

            first_seen = self._idle_light_since.setdefault(printer_id, now)
            if now - first_seen < delay_seconds:
                continue

            logger.info(
                "Auto-turning off chamber light for idle printer %s after %s minute(s)",
                printer_id,
                minutes,
            )
            if client.set_chamber_light(False):
                state.chamber_light = False
                self._idle_light_since.pop(printer_id, None)

        for printer_id in list(self._idle_light_since):
            if printer_id not in statuses:
                self._idle_light_since.pop(printer_id, None)

    async def _check_print_auto_off(self, enabled: bool, minutes: int, first_layer_enabled: bool):
        if not enabled and not first_layer_enabled:
            self._print_light_since.clear()
            self._print_light_layer_off_done.clear()
            return

        now = time.monotonic()
        delay_seconds = minutes * 60
        statuses = printer_manager.get_all_statuses()
        for printer_id, state in statuses.items():
            state_name = (getattr(state, "state", "") or "").strip().lower()
            is_printing = state_name in PRINT_START_STATES
            if (
                not is_printing
                or not getattr(state, "connected", False)
                or not getattr(state, "chamber_light", False)
                or printer_id in self._flash_tasks
            ):
                self._print_light_since.pop(printer_id, None)
                self._print_light_layer_off_done.discard(printer_id)
                continue

            first_seen = self._print_light_since.setdefault(printer_id, now)
            layer_num = getattr(state, "layer_num", None) or 0
            printer_time_enabled = await self._print_auto_off_enabled_for_printer(printer_id, enabled)
            should_turn_off_for_layer = (
                first_layer_enabled and printer_id not in self._print_light_layer_off_done and layer_num > 1
            )
            should_turn_off_for_time = printer_time_enabled and now - first_seen >= delay_seconds
            if not should_turn_off_for_layer and not should_turn_off_for_time:
                continue

            client = printer_manager.get_client(printer_id)
            if not client or not hasattr(client, "set_chamber_light"):
                continue

            reason = "first layer completion" if should_turn_off_for_layer else f"{minutes} print minute(s)"
            logger.info("Auto-turning off chamber light for printer %s after %s", printer_id, reason)
            if client.set_chamber_light(False):
                state.chamber_light = False
                self._print_light_since.pop(printer_id, None)
                self._print_light_layer_off_done.add(printer_id)

        for printer_id in list(self._print_light_since):
            if printer_id not in statuses:
                self._print_light_since.pop(printer_id, None)
                self._print_light_layer_off_done.discard(printer_id)


chamber_light_auto_off_service = ChamberLightAutoOffService()
