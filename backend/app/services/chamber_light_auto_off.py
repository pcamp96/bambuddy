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


class ChamberLightAutoOffService:
    """Automates chamber lights for supported printers."""

    def __init__(self, check_interval: int = 60, flash_interval: float = 0.5):
        self._check_interval = check_interval
        self._flash_interval = flash_interval
        self._task: asyncio.Task | None = None
        self._idle_light_since: dict[int, float] = {}
        self._settings_cache: tuple[float, tuple[bool, int, bool]] | None = None
        self._last_error_signature: dict[int, str] = {}
        self._flash_tasks: dict[int, asyncio.Task] = {}

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

    async def _settings(self) -> tuple[bool, int, bool]:
        defaults = {
            "chamber_light_auto_off_enabled": "false",
            "chamber_light_auto_off_minutes": "30",
            "chamber_light_flash_on_error_enabled": "false",
        }
        cached = self._settings_cache
        now = time.monotonic()
        if cached and now - cached[0] < 15:
            return cached[1]

        async with async_session() as db:
            result = await db.execute(
                select(Settings).where(Settings.key.in_(list(defaults.keys())))
            )
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
        settings = (enabled, minutes, flash_on_error)
        self._settings_cache = (now, settings)
        return settings

    @staticmethod
    def _is_printing_or_paused(state_name: str | None) -> bool:
        normalized = (state_name or "").strip().lower()
        return normalized in PRINTING_STATES

    @staticmethod
    def _error_signature(state) -> str | None:
        hms_errors = [
            error
            for error in (getattr(state, "hms_errors", []) or [])
            if getattr(error, "severity", 0) in (1, 2, 3)
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
            return

        if self._last_error_signature.get(printer_id) == signature:
            return
        self._last_error_signature[printer_id] = signature

        _, _, global_flash_on_error = await self._settings()
        if not await self._flash_enabled_for_printer(printer_id, global_flash_on_error):
            return

        existing = self._flash_tasks.get(printer_id)
        if existing and not existing.done():
            return

        task = asyncio.create_task(
            self._flash_light(printer_id, getattr(state, "chamber_light", False)),
            name=f"chamber-light-error-flash-{printer_id}",
        )
        self._flash_tasks[printer_id] = task
        task.add_done_callback(lambda _: self._flash_tasks.pop(printer_id, None))

    async def _flash_enabled_for_printer(self, printer_id: int, global_enabled: bool) -> bool:
        async with async_session() as db:
            result = await db.execute(
                select(Printer.chamber_light_flash_on_error).where(Printer.id == printer_id)
            )
            override = result.scalar_one_or_none()
        return global_enabled if override is None else bool(override)

    async def _flash_light(self, printer_id: int, original_on: bool):
        client = printer_manager.get_client(printer_id)
        state = printer_manager.get_status(printer_id)
        if (
            not client
            or not hasattr(client, "set_chamber_light")
            or not state
            or not getattr(state, "connected", False)
        ):
            return

        logger.info("Flashing chamber light for printer %s error", printer_id)
        for _ in range(3):
            if not client.set_chamber_light(True):
                return
            state.chamber_light = True
            await asyncio.sleep(self._flash_interval)
            if not client.set_chamber_light(False):
                return
            state.chamber_light = False
            await asyncio.sleep(self._flash_interval)

        if original_on:
            if client.set_chamber_light(True):
                state.chamber_light = True

    async def check_once(self):
        enabled, minutes, _ = await self._settings()
        if not enabled:
            self._idle_light_since.clear()
            return

        now = time.monotonic()
        delay_seconds = minutes * 60
        statuses = printer_manager.get_all_statuses()
        for printer_id, state in statuses.items():
            client = printer_manager.get_client(printer_id)
            if (
                not client
                or not hasattr(client, "set_chamber_light")
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


chamber_light_auto_off_service = ChamberLightAutoOffService()
