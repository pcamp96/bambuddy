"""Automatic chamber light shutoff for idle printers."""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from backend.app.core.database import async_session
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
    """Turns chamber lights off after they sit on while a printer is idle."""

    def __init__(self, check_interval: int = 60):
        self._check_interval = check_interval
        self._task: asyncio.Task | None = None
        self._idle_light_since: dict[int, float] = {}

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("Chamber light auto-off scheduler started")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("Chamber light auto-off scheduler stopped")

    async def _loop(self):
        while True:
            try:
                await self.check_once()
            except Exception as e:
                logger.error("Error in chamber light auto-off check: %s", e)
            await asyncio.sleep(self._check_interval)

    async def _settings(self) -> tuple[bool, int]:
        defaults = {
            "chamber_light_auto_off_enabled": "false",
            "chamber_light_auto_off_minutes": "30",
        }
        async with async_session() as db:
            result = await db.execute(
                select(Settings).where(Settings.key.in_(list(defaults.keys())))
            )
            rows = {row.key: row.value for row in result.scalars().all()}

        enabled = (rows.get("chamber_light_auto_off_enabled", defaults["chamber_light_auto_off_enabled"]) or "").lower() == "true"
        try:
            minutes = int(rows.get("chamber_light_auto_off_minutes", defaults["chamber_light_auto_off_minutes"]) or "30")
        except (TypeError, ValueError):
            minutes = 30
        minutes = max(1, min(minutes, 240))
        return enabled, minutes

    @staticmethod
    def _is_printing_or_paused(state_name: str | None) -> bool:
        normalized = (state_name or "").strip().lower()
        return normalized in PRINTING_STATES

    async def check_once(self):
        enabled, minutes = await self._settings()
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

            logger.info("Auto-turning off chamber light for idle printer %s after %s minute(s)", printer_id, minutes)
            if client.set_chamber_light(False):
                state.chamber_light = False
                self._idle_light_since.pop(printer_id, None)

        for printer_id in list(self._idle_light_since):
            if printer_id not in statuses:
                self._idle_light_since.pop(printer_id, None)


chamber_light_auto_off_service = ChamberLightAutoOffService()
