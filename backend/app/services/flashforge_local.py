"""FlashForge local API client."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import httpx

from backend.app.services.bambu_mqtt import HMSError, PrinterState

logger = logging.getLogger(__name__)

DEFAULT_FLASHFORGE_PORT = 8898
FLASHFORGE_POLL_INTERVAL_SECONDS = 10.0
FLASHFORGE_STALE_AFTER_SECONDS = 45.0
FLASHFORGE_JOB_CONTROL_CMD = "jobCtl_cmd"
FLASHFORGE_STATE_CONTROL_CMD = "stateCtrl_cmd"
FLASHFORGE_LIGHT_CONTROL_CMD = "lightControl_cmd"
FLASHFORGE_PRINTER_CONTROL_CMD = "printerCtl_cmd"
FLASHFORGE_TEMPERATURE_CONTROL_CMD = "temperatureCtl_cmd"
FLASHFORGE_SPEED_MODE_TO_PERCENT = {
    1: 50,
    2: 100,
    3: 124,
    4: 166,
}


def is_flashforge_model(model: str | None) -> bool:
    """Return True when a printer model should use the FlashForge local API."""
    if not model:
        return False
    normalized = model.strip().upper()
    compact = "".join(normalized.split())
    return "CREATOR5PRO" in compact


def _first_number(values: Any, default: float = 0.0) -> float:
    if isinstance(values, list) and values:
        return _number(values[0], default)
    return _number(values, default)


def _max_number(values: Any, default: float = 0.0) -> float:
    if not isinstance(values, list) or not values:
        return _number(values, default)
    parsed = [_number(value, default) for value in values]
    return max(parsed) if parsed else default


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _seconds_to_minutes(value: Any, default: int = 0) -> int:
    seconds = _number(value, 0.0)
    if seconds <= 0:
        return default
    return max(1, int(math.ceil(seconds / 60)))


def _remaining_minutes(detail: dict[str, Any], mapped_state: str) -> int:
    """Return remaining print time in Bambuddy minutes from FlashForge detail data.

    FlashForge reports `estimatedTime` as the total job estimate, while
    Bambuddy expects `remaining_time` to be minutes left. Some firmware builds
    also expose a `remainingTime` field that can be wildly stale, so prefer
    values derived from total estimate, elapsed duration, and progress when
    those are available.
    """
    if mapped_state == "FINISH":
        return 0

    candidates: list[float] = []
    estimated_seconds = _number(detail.get("estimatedTime"), 0.0)
    duration_seconds = _number(detail.get("printDuration"), 0.0)

    if mapped_state in {"RUNNING", "PAUSE"} and estimated_seconds > 0 and duration_seconds > 0:
        seconds_left = estimated_seconds - duration_seconds
        if seconds_left > 0:
            candidates.append(seconds_left)
        else:
            return 0

    progress = _number(detail.get("printProgress", detail.get("progress")), 0.0)
    if 0 < progress <= 1:
        progress *= 100
    if mapped_state in {"RUNNING", "PAUSE"} and duration_seconds > 0 and 0 < progress < 100:
        progress_left = duration_seconds * ((100 - progress) / progress)
        if progress_left > 0:
            candidates.append(progress_left)

    remaining = _number(detail.get("remainingTime"), 0.0)
    if remaining > 0:
        if not candidates:
            candidates.append(remaining)
        else:
            # Treat explicit remaining time as advisory. The FlashForge field
            # can be a stale/overflowed value; only trust it when it is in the
            # same rough range as the derived estimates.
            nearest = min(candidates)
            if remaining <= nearest * 1.5:
                candidates.append(remaining)

    if candidates:
        return _seconds_to_minutes(min(candidates), 0)

    return _seconds_to_minutes(estimated_seconds, 0)


def _speed_percent_to_level(value: Any) -> int:
    percent = _number(value, 100.0)
    if percent <= 75:
        return 1
    if percent < 112:
        return 2
    if percent < 145:
        return 3
    return 4


def _success_code(value: Any) -> bool:
    return _int(value, -1) in {0, 200}


def _remote_filename(filename: Any) -> str:
    name = Path(str(filename or "")).name.strip()
    return name or "bambuddy-print.gcode.3mf"


def _path_is_flashforge_root(path: str | None) -> bool:
    normalized = str(path or "/").strip()
    return normalized in {"", "/", ".", "/cache", "cache"}


def _decode_base64_image(value: Any) -> bytes | None:
    if not value:
        return None
    try:
        return base64.b64decode(str(value), validate=True)
    except (ValueError, TypeError):
        return None


def _image_media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "application/octet-stream"


def _firmware_at_least(version: str | None, minimum: tuple[int, int, int]) -> bool:
    try:
        parts = [int(part) for part in str(version or "").split(".")[:3]]
    except ValueError:
        return False
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3]) >= minimum


def _material_mappings_header(material_mappings: Any = None) -> str:
    mappings = material_mappings if isinstance(material_mappings, list) else []
    return base64.b64encode(json.dumps(mappings, separators=(",", ":")).encode()).decode()


def _normalize_state(status: Any) -> str:
    """Map FlashForge status strings onto Bambuddy's broad state buckets."""
    raw = str(status or "").strip().lower()
    if raw in {"building", "printing", "print", "running"}:
        return "RUNNING"
    if raw in {"pause", "paused", "pausing"}:
        return "PAUSE"
    if raw in {"finish", "finished", "complete", "completed", "done"}:
        return "FINISH"
    if raw in {"error", "failed", "failure", "fault"}:
        return "FAILED"
    if raw in {"ready", "idle", "standby", "completed"}:
        return "IDLE"
    if raw in {"loading", "heating", "preheating", "preparing"}:
        return "PREPARE"
    return raw.upper() if raw else "unknown"


def _slot_to_tray(slot: dict[str, Any], index: int) -> dict[str, Any]:
    color = str(slot.get("materialColor") or slot.get("color") or "808080").replace("#", "")
    if len(color) == 6:
        color = f"{color}FF"
    return {
        "id": index,
        "tray_color": color,
        "tray_type": slot.get("materialName") or slot.get("materialType") or slot.get("type") or "",
        "tray_sub_brands": "",
        "tray_id_name": "",
        "tray_info_idx": "",
        "remain": 0,
        "state": 10 if slot.get("hasFilament", bool(slot)) else 9,
    }


def _active_tray_index(station: dict[str, Any], slots: list[dict[str, Any]]) -> int | None:
    raw_slot = _int(station.get("currentSlot"), -1)
    if raw_slot < 0:
        return None
    for index, slot in enumerate(slots):
        if _int(slot.get("slotId"), -999) == raw_slot:
            return index
    if 0 <= raw_slot < len(slots):
        return raw_slot
    return None


def _gcode_entry_to_file(entry: Any) -> dict[str, Any] | None:
    if isinstance(entry, str):
        name = _remote_filename(entry)
        return {
            "name": name,
            "is_directory": False,
            "size": 0,
            "mtime": None,
        }
    if not isinstance(entry, dict):
        return None
    name = (
        entry.get("gcodeFileName")
        or entry.get("fileName")
        or entry.get("name")
        or entry.get("filename")
    )
    if not name:
        return None
    seconds = _int(entry.get("printingTime") or entry.get("printTime") or entry.get("estimatedTime"), 0)
    weight = _number(entry.get("totalFilamentWeight") or entry.get("filamentWeight"), 0.0)
    return {
        "name": _remote_filename(name),
        "is_directory": False,
        "size": _int(entry.get("fileSize") or entry.get("size"), 0),
        "mtime": entry.get("modifyTime") or entry.get("modifiedTime") or entry.get("time"),
        "printing_time": seconds or None,
        "filament_weight": weight or None,
        "use_matl_station": bool(entry.get("useMatlStation")),
    }


def _hms_errors_to_dicts(errors: list[HMSError]) -> list[dict[str, Any]]:
    return [
        {
            "code": error.code,
            "attr": error.attr,
            "module": error.module,
            "severity": error.severity,
            "message": error.message,
        }
        for error in errors
    ]


@dataclass
class FlashForgeLocalClient:
    """Small polling client for FlashForge's LAN-only HTTP API."""

    ip_address: str
    serial_number: str
    access_code: str
    model: str | None = None
    on_state_change: Callable[[PrinterState], None] | None = None
    on_print_start: Callable[[dict], None] | None = None
    on_print_complete: Callable[[dict], None] | None = None

    def __post_init__(self) -> None:
        self.state = PrinterState(connected=False, state="unknown")
        self.logging_enabled = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_seen = 0.0
        self._last_state = "unknown"
        self._has_seen_state = False

    def connect(self) -> None:
        """Start polling the printer."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name=f"flashforge-{self.serial_number}",
            daemon=True,
        )
        self._thread.start()

    def disconnect(self, timeout: float = 0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout or 2.0)
        self.state.connected = False

    def check_staleness(self) -> bool:
        if self._last_seen and time.time() - self._last_seen > FLASHFORGE_STALE_AFTER_SECONDS:
            self.state.connected = False
        return self.state.connected

    def request_status_update(self) -> bool:
        detail = self._fetch_detail()
        if detail is None:
            return False
        self._apply_detail(detail)
        return True

    def start_print(self, *args: Any, **kwargs: Any) -> bool:
        filename = _remote_filename(args[0] if args else kwargs.get("filename"))
        detail = self._fetch_detail() or {}
        payload: dict[str, Any] = {
            "serialNumber": self.serial_number,
            "checkCode": self.access_code,
            "fileName": filename,
            "levelingBeforePrint": bool(kwargs.get("bed_levelling", True)),
        }
        if _firmware_at_least(str(detail.get("firmwareVersion") or ""), (3, 1, 3)):
            payload.update(
                {
                    "flowCalibration": bool(kwargs.get("flow_cali", False)),
                    "useMatlStation": bool(kwargs.get("use_ams", True)),
                    "gcodeToolCnt": _int(kwargs.get("gcode_tool_count"), 0),
                    "materialMappings": kwargs.get("material_mappings")
                    if isinstance(kwargs.get("material_mappings"), list)
                    else [],
                }
            )
        response = self._post_json("printGcode", payload, timeout=15)
        return bool(response and _success_code(response.get("code")))

    def stop_print(self) -> bool:
        return self._send_job_control("cancel")

    def pause_print(self) -> bool:
        return self._send_job_control("pause")

    def resume_print(self) -> bool:
        return self._send_job_control("continue")

    def clear_hms_errors(self) -> bool:
        return self._send_control_command(FLASHFORGE_STATE_CONTROL_CMD, {"action": "setClearPlatform"})

    def set_chamber_light(self, on: bool) -> bool:
        return self._send_control_command(FLASHFORGE_LIGHT_CONTROL_CMD, {"status": "open" if on else "close"})

    def set_print_speed(self, mode: int) -> bool:
        percent = FLASHFORGE_SPEED_MODE_TO_PERCENT.get(mode)
        if percent is None:
            logger.warning("Invalid FlashForge print speed mode for %s: %s", self.ip_address, mode)
            return False
        return self._send_control_command(FLASHFORGE_PRINTER_CONTROL_CMD, {"speed": percent})

    def set_temperature(self, heater: str, target: int) -> bool:
        if heater == "nozzle":
            args = {"nozzle": target}
        elif heater == "bed":
            args = {"platform": target}
        elif heater == "chamber":
            args = {"chamber": target}
        else:
            logger.warning("Invalid FlashForge heater for %s: %s", self.ip_address, heater)
            return False
        return self._send_control_command(FLASHFORGE_TEMPERATURE_CONTROL_CMD, args)

    def enable_logging(self, enabled: bool = True) -> None:
        self.logging_enabled = enabled

    def get_logs(self) -> list:
        return []

    def clear_logs(self) -> None:
        return None

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            detail = self._fetch_detail()
            if detail is not None:
                self._apply_detail(detail)
            else:
                self.check_staleness()
                if self.on_state_change:
                    self.on_state_change(self.state)
            self._stop_event.wait(FLASHFORGE_POLL_INTERVAL_SECONDS)

    def _fetch_detail(self) -> dict[str, Any] | None:
        payload = self._post_json(
            "detail",
            {"serialNumber": self.serial_number, "checkCode": self.access_code},
            timeout=5,
        )
        if not payload:
            return None
        if not _success_code(payload.get("code")):
            logger.warning("FlashForge detail request returned non-zero code for %s: %s", self.ip_address, payload)
            return None
        detail = payload.get("detail")
        return detail if isinstance(detail, dict) else None

    def _post_json(self, path: str, payload: dict[str, Any], timeout: float = 5) -> dict[str, Any] | None:
        body = json.dumps(payload).encode()
        request = Request(
            f"http://{self.ip_address}:{DEFAULT_FLASHFORGE_PORT}/{path.lstrip('/')}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.debug("FlashForge %s request failed for %s: %s", path, self.ip_address, exc)
            return None

    def _send_control_command(self, command: str, args: dict[str, Any]) -> bool:
        response = self._post_json(
            "control",
            {
                "serialNumber": self.serial_number,
                "checkCode": self.access_code,
                "payload": {"cmd": command, "args": args},
            },
            timeout=15,
        )
        return bool(response and _success_code(response.get("code")))

    def _send_job_control(self, action: str) -> bool:
        return self._send_control_command(FLASHFORGE_JOB_CONTROL_CMD, {"jobID": "", "action": action})

    def _event_payload(self, status: str | None = None) -> dict[str, Any]:
        return {
            "filename": self.state.gcode_file,
            "subtask_name": self.state.subtask_name,
            "status": status,
            "remaining_time": self.state.remaining_time * 60 if self.state.remaining_time else None,
            "progress": self.state.progress,
            "last_progress": self.state.progress,
            "last_layer_num": self.state.layer_num,
            "layer_num": self.state.layer_num,
            "total_layers": self.state.total_layers,
            "raw_data": self.state.raw_data,
            "hms_errors": _hms_errors_to_dicts(self.state.hms_errors or []),
        }

    def _apply_detail(self, detail: dict[str, Any]) -> None:
        now = time.time()
        raw_status = detail.get("status")
        mapped_state = _normalize_state(raw_status)
        previous_state = self.state.state
        previous_print = self.state.current_print or self.state.gcode_file or self.state.subtask_name

        self.state.connected = True
        self.state.state = mapped_state
        current_print = (
            detail.get("printFileName")
            or detail.get("currentPrintFile")
            or detail.get("fileName")
        )
        if not current_print and mapped_state in {"FINISH", "FAILED"}:
            current_print = previous_print
        self.state.current_print = current_print
        self.state.subtask_name = self.state.current_print
        self.state.gcode_file = self.state.current_print
        progress = _number(detail.get("printProgress", detail.get("progress")), 0.0)
        self.state.progress = progress * 100 if 0 <= progress <= 1 else progress
        if mapped_state == "FINISH":
            self.state.progress = max(self.state.progress, 100.0)
        self.state.remaining_time = _remaining_minutes(detail, mapped_state)
        self.state.layer_num = _int(detail.get("printLayer", detail.get("currentLayer")), 0)
        self.state.total_layers = _int(detail.get("targetPrintLayer", detail.get("totalLayer")), 0)
        self.state.firmware_version = str(detail.get("firmwareVersion") or "")
        self.state.ipcam = bool(detail.get("camera") or detail.get("cameraStreamUrl"))
        self.state.cooling_fan_speed = _int(detail.get("coolingFanSpeed"), 0)
        self.state.big_fan1_speed = _int(detail.get("leftFanSpeed"), _int(detail.get("airFanSpeed"), 0))
        self.state.big_fan2_speed = _int(detail.get("chamberFanSpeed"), 0)
        self.state.speed_level = _speed_percent_to_level(detail.get("printSpeedAdjust"))
        self.state.chamber_light = str(detail.get("lightStatus") or "").lower() == "open"
        self.state.door_open = str(detail.get("doorStatus") or "").lower() == "open"
        self.state.sdcard = True
        self.state.raw_data = {
            **detail,
            "device_model": detail.get("model") or self.model or "FlashForge",
            "vendor": "flashforge",
            "estimated_time_seconds": _int(detail.get("estimatedTime", detail.get("remainingTime")), 0),
            "print_duration_seconds": _int(detail.get("printDuration"), 0),
        }

        nozzle_temp = _max_number(detail.get("nozzleTemps"), _number(detail.get("nozzleTemp"), 0.0))
        nozzle_target = _max_number(
            detail.get("nozzleTargetTemps"),
            _number(detail.get("nozzleTargetTemp"), 0.0),
        )
        bed_temp = _number(detail.get("platTemp"), _number(detail.get("bedTemp"), 0.0))
        bed_target = _number(detail.get("platTargetTemp"), _number(detail.get("bedTargetTemp"), 0.0))
        chamber_temp = _number(detail.get("chamberTemp"), 0.0)
        chamber_target = _number(detail.get("chamberTargetTemp"), 0.0)

        self.state.temperatures = {
            "nozzle": nozzle_temp,
            "nozzle_target": nozzle_target,
            "nozzle_heating": nozzle_target > nozzle_temp + 1,
            "bed": bed_temp,
            "bed_target": bed_target,
            "bed_heating": bed_target > bed_temp + 1,
            "chamber": chamber_temp,
            "chamber_target": chamber_target,
            "chamber_heating": chamber_target > chamber_temp + 1,
        }

        station = detail.get("matlStationInfo") if isinstance(detail.get("matlStationInfo"), dict) else {}
        slots = station.get("slotInfos") if isinstance(station.get("slotInfos"), list) else []
        if slots:
            self.state.raw_data["ams"] = [
                {
                    "id": 0,
                    "tray": [_slot_to_tray(slot, idx) for idx, slot in enumerate(slots)],
                    "sn": "",
                    "module_type": "flashforge_ifs",
                }
            ]
            current_slot = _active_tray_index(station, slots)
            if current_slot is not None:
                self.state.tray_now = current_slot
                self.state.last_loaded_tray = current_slot
        else:
            self.state.raw_data["ams"] = []

        error_code = detail.get("errorCode")
        if error_code in (None, "", 0, "0", "OK", "ok"):
            self.state.hms_errors = []
        else:
            code = str(error_code)
            attr = _int(error_code, 0)
            self.state.hms_errors = [
                HMSError(
                    code=code,
                    attr=attr,
                    module=0,
                    severity=2,
                    message=str(detail.get("errorMessage") or detail.get("errorStatus") or code),
                )
            ]

        self._last_seen = now
        if previous_state != mapped_state:
            can_emit_transition = self._has_seen_state
            if (
                can_emit_transition
                and self.on_print_start
                and mapped_state == "RUNNING"
                and previous_state not in {"RUNNING", "PAUSE"}
            ):
                self.on_print_start(self._event_payload())
            if (
                can_emit_transition
                and self.on_print_complete
                and mapped_state in {"FINISH", "FAILED"}
                and previous_state in {"RUNNING", "PAUSE"}
            ):
                self.on_print_complete(self._event_payload("completed" if mapped_state == "FINISH" else "failed"))
            if self.on_state_change:
                self.on_state_change(self.state)
        elif self.on_state_change:
            self.on_state_change(self.state)
        self._has_seen_state = True


async def probe_flashforge_connection(ip_address: str, serial_number: str, access_code: str) -> dict:
    """Probe a FlashForge printer once using the LAN HTTP API."""
    client = FlashForgeLocalClient(ip_address, serial_number, access_code)
    detail = await asyncio.to_thread(client._fetch_detail)
    if detail is None:
        return {"success": False, "state": None, "model": None}
    return {
        "success": True,
        "state": _normalize_state(detail.get("status")),
        "model": detail.get("model") or detail.get("name") or "FlashForge",
    }


def list_flashforge_files(
    ip_address: str,
    serial_number: str,
    access_code: str,
    path: str = "/",
) -> list[dict]:
    """List recent/local G-code files exposed by the FlashForge LAN API."""
    if not serial_number or not _path_is_flashforge_root(path):
        return []
    client = FlashForgeLocalClient(ip_address, serial_number, access_code)
    response = client._post_json(
        "gcodeList",
        {"serialNumber": serial_number, "checkCode": access_code},
        timeout=10,
    )
    if not response or not _success_code(response.get("code")):
        return []
    entries = response.get("gcodeListDetail")
    if not isinstance(entries, list):
        entries = response.get("gcodeList")
    if not isinstance(entries, list):
        return []
    files = []
    seen = set()
    for entry in entries:
        file_info = _gcode_entry_to_file(entry)
        if not file_info or file_info["name"] in seen:
            continue
        seen.add(file_info["name"])
        files.append(file_info)
    return files


def get_flashforge_gcode_thumbnail(
    ip_address: str,
    serial_number: str,
    access_code: str,
    filename: str,
) -> tuple[bytes, str] | None:
    """Fetch a thumbnail for a stored FlashForge G-code/3MF file."""
    if not serial_number:
        return None
    remote_name = _remote_filename(filename)
    client = FlashForgeLocalClient(ip_address, serial_number, access_code)
    response = client._post_json(
        "gcodeThumb",
        {
            "serialNumber": serial_number,
            "checkCode": access_code,
            "fileName": remote_name,
        },
        timeout=10,
    )
    image = _decode_base64_image((response or {}).get("imageData"))
    if image:
        return image, _image_media_type(image)
    return None


def get_flashforge_current_thumbnail(
    ip_address: str,
    serial_number: str,
    access_code: str,
    filename: str | None = None,
) -> tuple[bytes, str] | None:
    """Fetch the current print thumbnail, falling back to the printer's getThum endpoint."""
    if filename:
        image = get_flashforge_gcode_thumbnail(ip_address, serial_number, access_code, filename)
        if image:
            return image
    try:
        with urlopen(f"http://{ip_address}:{DEFAULT_FLASHFORGE_PORT}/getThum", timeout=10) as response:
            data = response.read()
    except (OSError, URLError, TimeoutError) as exc:
        logger.debug("FlashForge current thumbnail request failed for %s: %s", ip_address, exc)
        return None
    if not data:
        return None
    return data, _image_media_type(data)


def get_flashforge_storage_info(ip_address: str, serial_number: str, access_code: str) -> dict | None:
    """Return storage information from FlashForge detail data when available."""
    if not serial_number:
        return None
    client = FlashForgeLocalClient(ip_address, serial_number, access_code)
    detail = client._fetch_detail()
    if not detail:
        return None
    free_gb = _number(detail.get("remainingDiskSpace"), 0.0)
    if free_gb <= 0:
        return {"used_bytes": None, "free_bytes": None}
    return {"used_bytes": None, "free_bytes": int(free_gb * 1024 * 1024 * 1024)}


def upload_flashforge_file(
    ip_address: str,
    serial_number: str,
    access_code: str,
    local_path: Path,
    remote_path: str,
    *,
    print_now: bool = False,
    bed_levelling: bool = True,
    timelapse: bool = False,
    use_ams: bool = True,
    progress_callback: Callable[[int, int], None] | None = None,
) -> bool:
    """Upload a 3MF/G-code file through FlashForge's local HTTP API."""
    path = Path(local_path)
    if not serial_number:
        logger.error("FlashForge upload requires a serial number for %s", ip_address)
        return False
    if not path.exists():
        logger.error("FlashForge upload source does not exist: %s", path)
        return False

    file_size = path.stat().st_size
    filename = _remote_filename(remote_path)
    headers = {
        "serialNumber": serial_number,
        "checkCode": access_code,
        "fileSize": str(file_size),
        "printNow": "true" if print_now else "false",
        "levelingBeforePrint": "true" if bed_levelling else "false",
        "flowCalibration": "false",
        "firstLayerInspection": "false",
        "timeLapseVideo": "true" if timelapse else "false",
        "useMatlStation": "true" if use_ams else "false",
        "gcodeToolCnt": "0",
        "materialMappings": _material_mappings_header(),
    }
    url = f"http://{ip_address}:{DEFAULT_FLASHFORGE_PORT}/uploadGcode"

    try:
        if progress_callback:
            progress_callback(0, file_size)
        with path.open("rb") as handle:
            response = httpx.post(
                url,
                headers=headers,
                files={"gcodeFile": (filename, handle, "application/octet-stream")},
                timeout=600,
            )
        if progress_callback:
            progress_callback(file_size, file_size)
        payload = response.json()
    except (OSError, httpx.HTTPError, ValueError) as exc:
        logger.warning("FlashForge upload failed for %s: %s", ip_address, exc)
        return False

    if not _success_code(payload.get("code")):
        logger.warning("FlashForge upload returned non-success response for %s: %s", ip_address, payload)
        return False
    return True
