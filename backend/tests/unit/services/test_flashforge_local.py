import json
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.flashforge_local import (
    FlashForgeLocalClient,
    _remaining_minutes,
    _seconds_to_minutes,
    _speed_percent_to_level,
    get_flashforge_current_thumbnail,
    get_flashforge_storage_info,
    is_flashforge_model,
    list_flashforge_files,
    probe_flashforge_connection,
    upload_flashforge_file,
)


def _detail_payload() -> dict:
    return {
        "code": 0,
        "detail": {
            "model": "Creator 5 Pro",
            "name": "Creator 5 Pro",
            "status": "printing",
            "printFileName": "colored_cow.gcode.3mf",
            "printProgress": 0.25,
            "estimatedTime": 1234,
            "printDuration": 456,
            "errorCode": 0,
            "printLayer": 12,
            "targetPrintLayer": 100,
            "firmwareVersion": "1.9.3",
            "camera": 1,
            "cameraStreamUrl": "http://192.0.2.211:8080/?action=stream",
            "nozzleTemps": [120, 121, 180, 209],
            "nozzleTargetTemps": [120, 120, 130, 210],
            "platTemp": 59,
            "platTargetTemp": 60,
            "chamberTemp": 29,
            "chamberTargetTemp": 45,
            "printSpeedAdjust": 124,
            "coolingFanSpeed": 70,
            "chamberFanSpeed": 0,
            "lightStatus": "open",
            "doorStatus": "close",
            "matlStationInfo": {
                "slotCnt": 4,
                "currentSlot": 2,
                "slotInfos": [
                    {"slotId": 1, "hasFilament": True, "materialName": "PLA", "materialColor": "#FCEBD7"},
                    {"slotId": 2, "hasFilament": True, "materialName": "PLA", "materialColor": "#FFFFFF"},
                ],
            },
        },
    }


@pytest.mark.parametrize(
    "model",
    [
        "Creator 5 Pro",
        "Creator5Pro",
        " FlashForge Creator 5 Pro ",
        "flashforge creator 5 pro",
    ],
)
def test_is_flashforge_model_accepts_supported_flashforge_names(model):
    assert is_flashforge_model(model)


@pytest.mark.parametrize(
    "model",
    [
        "Bambu Lab P1S",
        "Flashforge Adventurer 5M",
        "FlashForge AD5X",
        "Creator Pro",
        "Creator 5",
        "SomeFuturePrinter",
        "",
        None,
    ],
)
def test_is_flashforge_model_rejects_unconfirmed_models(model):
    assert not is_flashforge_model(model)


def test_flashforge_seconds_are_converted_to_bambuddy_minutes():
    assert _seconds_to_minutes(5798) == 97
    assert _seconds_to_minutes(30) == 1
    assert _seconds_to_minutes(0) == 0


def test_flashforge_remaining_time_prefers_total_estimate_over_stale_remaining_time():
    detail = _detail_payload()["detail"]
    detail.update(
        {
            "printProgress": 86,
            "estimatedTime": 41979,
            "printDuration": 35576,
            "remainingTime": 358800,
        }
    )

    assert _remaining_minutes(detail, "RUNNING") == 97


def test_flashforge_remaining_time_uses_progress_when_estimate_is_missing():
    detail = _detail_payload()["detail"]
    detail.update(
        {
            "printProgress": 50,
            "estimatedTime": 0,
            "printDuration": 3600,
            "remainingTime": 999999,
        }
    )

    assert _remaining_minutes(detail, "RUNNING") == 60


def test_flashforge_remaining_time_treats_low_estimate_as_remaining_time():
    detail = _detail_payload()["detail"]
    detail.update(
        {
            "printProgress": 0.6081839203834534,
            "estimatedTime": 2984,
            "printDuration": 4840,
            "remainingTime": 0,
        }
    )

    assert _remaining_minutes(detail, "RUNNING") == 50


def test_flashforge_remaining_time_accepts_remaining_time_when_it_is_plausible():
    detail = _detail_payload()["detail"]
    detail.update(
        {
            "printProgress": 50,
            "estimatedTime": 7200,
            "printDuration": 3600,
            "remainingTime": 3300,
        }
    )

    assert _remaining_minutes(detail, "RUNNING") == 55


@pytest.mark.parametrize(
    ("percent", "level"),
    [
        (50, 1),
        (100, 2),
        (124, 3),
        (166, 4),
    ],
)
def test_flashforge_speed_percent_maps_to_bambuddy_speed_level(percent, level):
    assert _speed_percent_to_level(percent) == level


def test_apply_detail_maps_creator_5_pro_status():
    client = FlashForgeLocalClient("192.0.2.211", "SN123", "code", model="Creator 5 Pro")

    client._apply_detail(_detail_payload()["detail"])

    assert client.state.connected is True
    assert client.state.state == "RUNNING"
    assert client.state.current_print == "colored_cow.gcode.3mf"
    assert client.state.gcode_file == "colored_cow.gcode.3mf"
    assert client.state.progress == 25
    assert client.state.remaining_time == 13
    assert client.state.layer_num == 12
    assert client.state.total_layers == 100
    assert client.state.firmware_version == "1.9.3"
    assert client.state.ipcam is True
    assert client.state.temperatures == {
        "nozzle": 209,
        "nozzle_target": 210,
        "nozzle_heating": False,
        "bed": 59,
        "bed_target": 60,
        "bed_heating": False,
        "chamber": 29,
        "chamber_target": 45,
        "chamber_heating": True,
    }
    assert client.state.cooling_fan_speed == 70
    assert client.state.speed_level == 3
    assert client.state.chamber_light is True
    assert client.state.door_open is False
    assert client.state.sdcard is True
    assert client.state.tray_now == 1
    assert client.state.raw_data["vendor"] == "flashforge"
    assert client.state.raw_data["estimated_time_seconds"] == 1234
    assert client.state.raw_data["print_duration_seconds"] == 456
    assert client.state.raw_data["ams"][0]["module_type"] == "flashforge_ifs"
    assert client.state.raw_data["ams"][0]["tray"][0]["tray_type"] == "PLA"
    assert client.state.raw_data["ams"][0]["tray"][0]["tray_color"] == "FCEBD7FF"
    assert client.state.hms_errors == []


def test_apply_detail_maps_flashforge_error_to_hms_error():
    client = FlashForgeLocalClient("192.0.2.211", "SN123", "code", model="Creator 5 Pro")
    detail = _detail_payload()["detail"]
    detail["status"] = "error"
    detail["errorCode"] = 42
    detail["errorMessage"] = "Platform blocked"

    client._apply_detail(detail)

    assert client.state.state == "FAILED"
    assert len(client.state.hms_errors) == 1
    assert client.state.hms_errors[0].code == "42"
    assert client.state.hms_errors[0].severity == 2
    assert client.state.hms_errors[0].message == "Platform blocked"


def test_flashforge_initial_running_poll_does_not_emit_print_start():
    starts = []
    client = FlashForgeLocalClient(
        "192.0.2.211",
        "SN123",
        "code",
        model="Creator 5 Pro",
        on_print_start=lambda payload: starts.append(payload),
    )

    client._apply_detail(_detail_payload()["detail"])

    assert starts == []


def test_flashforge_idle_to_running_emits_print_start_after_initial_poll():
    starts = []
    client = FlashForgeLocalClient(
        "192.0.2.211",
        "SN123",
        "code",
        model="Creator 5 Pro",
        on_print_start=lambda payload: starts.append(payload),
    )
    idle_detail = _detail_payload()["detail"]
    idle_detail["status"] = "ready"
    idle_detail["printFileName"] = None

    client._apply_detail(idle_detail)
    client._apply_detail(_detail_payload()["detail"])

    assert len(starts) == 1
    assert starts[0]["filename"] == "colored_cow.gcode.3mf"
    assert starts[0]["subtask_name"] == "colored_cow.gcode.3mf"
    assert starts[0]["remaining_time"] == 13 * 60
    assert starts[0]["progress"] == 25
    assert starts[0]["last_layer_num"] == 12
    assert starts[0]["raw_data"]["vendor"] == "flashforge"


def test_flashforge_finish_emits_completion_with_last_filename_when_terminal_payload_omits_file():
    completions = []
    client = FlashForgeLocalClient(
        "192.0.2.211",
        "SN123",
        "code",
        model="Creator 5 Pro",
        on_print_complete=lambda payload: completions.append(payload),
    )

    running_detail = _detail_payload()["detail"]
    idle_detail = _detail_payload()["detail"]
    idle_detail["status"] = "ready"
    idle_detail["printFileName"] = None
    finish_detail = _detail_payload()["detail"]
    finish_detail["status"] = "finish"
    finish_detail["printFileName"] = None

    client._apply_detail(idle_detail)
    client._apply_detail(running_detail)
    client._apply_detail(finish_detail)

    assert len(completions) == 1
    assert completions[0]["status"] == "completed"
    assert completions[0]["filename"] == "colored_cow.gcode.3mf"
    assert completions[0]["subtask_name"] == "colored_cow.gcode.3mf"
    assert completions[0]["remaining_time"] is None


def test_flashforge_completed_job_reports_done_even_when_estimate_remains():
    client = FlashForgeLocalClient("192.0.2.211", "SN123", "code", model="Creator 5 Pro")
    detail = _detail_payload()["detail"]
    detail.update(
        {
            "status": "completed",
            "printProgress": 0.0,
            "estimatedTime": 41979,
            "printDuration": 35576,
            "printLayer": 750,
            "targetPrintLayer": 750,
        }
    )

    client._apply_detail(detail)

    assert client.state.state == "FINISH"
    assert client.state.progress == 100.0
    assert client.state.remaining_time == 0
    assert client.state.raw_data["estimated_time_seconds"] == 41979
    assert client.state.raw_data["print_duration_seconds"] == 35576


def test_flashforge_failure_event_includes_hms_errors():
    completions = []
    client = FlashForgeLocalClient(
        "192.0.2.211",
        "SN123",
        "code",
        model="Creator 5 Pro",
        on_print_complete=lambda payload: completions.append(payload),
    )
    idle_detail = _detail_payload()["detail"]
    idle_detail["status"] = "ready"
    running_detail = _detail_payload()["detail"]
    failed_detail = _detail_payload()["detail"]
    failed_detail["status"] = "error"
    failed_detail["errorCode"] = 42
    failed_detail["errorMessage"] = "Platform blocked"

    client._apply_detail(idle_detail)
    client._apply_detail(running_detail)
    client._apply_detail(failed_detail)

    assert len(completions) == 1
    assert completions[0]["status"] == "failed"
    assert completions[0]["hms_errors"] == [
        {"code": "42", "attr": 42, "module": 0, "severity": 2, "message": "Platform blocked"}
    ]


def test_flashforge_job_control_commands_use_local_control_endpoint():
    client = FlashForgeLocalClient("192.0.2.211", "SN123", "code", model="Creator 5 Pro")
    calls = []

    def fake_post(path, payload, timeout=5):
        calls.append((path, payload, timeout))
        return {"code": 0}

    client._post_json = fake_post

    assert client.pause_print() is True
    assert client.resume_print() is True
    assert client.stop_print() is True

    assert [call[0] for call in calls] == ["control", "control", "control"]
    assert [call[1]["payload"]["args"]["action"] for call in calls] == ["pause", "continue", "cancel"]
    assert all(call[1]["payload"]["cmd"] == "jobCtl_cmd" for call in calls)


def test_flashforge_chamber_light_uses_local_control_endpoint():
    client = FlashForgeLocalClient("192.0.2.211", "SN123", "code", model="Creator 5 Pro")
    calls = []

    def fake_post(path, payload, timeout=5):
        calls.append((path, payload, timeout))
        return {"code": 0}

    client._post_json = fake_post

    assert client.set_chamber_light(False) is True
    assert client.set_chamber_light(True) is True

    assert [call[0] for call in calls] == ["control", "control"]
    assert [call[1]["payload"]["cmd"] for call in calls] == ["lightControl_cmd", "lightControl_cmd"]
    assert [call[1]["payload"]["args"]["status"] for call in calls] == ["close", "open"]


def test_flashforge_print_speed_uses_local_control_endpoint():
    client = FlashForgeLocalClient("192.0.2.211", "SN123", "code", model="Creator 5 Pro")
    calls = []

    def fake_post(path, payload, timeout=5):
        calls.append((path, payload, timeout))
        return {"code": 0}

    client._post_json = fake_post

    assert client.set_print_speed(1) is True
    assert client.set_print_speed(2) is True
    assert client.set_print_speed(3) is True
    assert client.set_print_speed(4) is True
    assert client.set_print_speed(5) is False

    assert [call[0] for call in calls] == ["control", "control", "control", "control"]
    assert [call[1]["payload"]["cmd"] for call in calls] == ["printerCtl_cmd"] * 4
    assert [call[1]["payload"]["args"]["speed"] for call in calls] == [50, 100, 124, 166]


def test_flashforge_temperature_uses_local_control_endpoint():
    client = FlashForgeLocalClient("192.0.2.211", "SN123", "code", model="Creator 5 Pro")
    calls = []

    def fake_post(path, payload, timeout=5):
        calls.append((path, payload, timeout))
        return {"code": 0}

    client._post_json = fake_post

    assert client.set_temperature("nozzle", 205) is True
    assert client.set_temperature("bed", 60) is True
    assert client.set_temperature("chamber", 35) is True
    assert client.set_temperature("laser", 12) is False

    assert [call[0] for call in calls] == ["control", "control", "control"]
    assert [call[1]["payload"]["cmd"] for call in calls] == ["temperatureCtl_cmd"] * 3
    assert [call[1]["payload"]["args"] for call in calls] == [
        {"nozzle": 205},
        {"platform": 60},
        {"chamber": 35},
    ]


def test_flashforge_start_print_uses_print_gcode_endpoint():
    client = FlashForgeLocalClient("192.0.2.211", "SN123", "code", model="Creator 5 Pro")
    calls = []

    def fake_post(path, payload, timeout=5):
        calls.append((path, payload, timeout))
        if path == "detail":
            return {"code": 0, "detail": {"firmwareVersion": "1.9.3"}}
        return {"code": 0}

    client._post_json = fake_post

    assert client.start_print("/cache/test_cube.gcode.3mf", bed_levelling=False) is True

    assert calls[1][0] == "printGcode"
    assert calls[1][1] == {
        "serialNumber": "SN123",
        "checkCode": "code",
        "fileName": "test_cube.gcode.3mf",
        "levelingBeforePrint": False,
    }


@pytest.mark.asyncio
async def test_flashforge_connection_probe_uses_detail_endpoint():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(_detail_payload()).encode()

    with patch("backend.app.services.flashforge_local.urlopen", return_value=response) as urlopen_mock:
        result = await probe_flashforge_connection("192.0.2.211", "SN123", "code")

    assert result == {"success": True, "state": "RUNNING", "model": "Creator 5 Pro"}
    request = urlopen_mock.call_args.args[0]
    assert request.full_url == "http://192.0.2.211:8898/detail"
    assert json.loads(request.data.decode()) == {"serialNumber": "SN123", "checkCode": "code"}


def test_upload_flashforge_file_uses_upload_gcode_endpoint(tmp_path, monkeypatch):
    source = tmp_path / "cube.gcode.3mf"
    source.write_bytes(b"print data")
    seen = {}

    def fake_post(url, headers, files, timeout):
        seen["url"] = url
        seen["headers"] = headers
        seen["files"] = files
        seen["timeout"] = timeout
        return MagicMock(json=lambda: {"code": 0})

    monkeypatch.setattr("backend.app.services.flashforge_local.httpx.post", fake_post)

    progress = []
    result = upload_flashforge_file(
        "192.0.2.211",
        "SN123",
        "code",
        source,
        "/cache/cube.gcode.3mf",
        progress_callback=lambda uploaded, total: progress.append((uploaded, total)),
    )

    assert result is True
    assert seen["url"] == "http://192.0.2.211:8898/uploadGcode"
    assert seen["headers"]["serialNumber"] == "SN123"
    assert seen["headers"]["checkCode"] == "code"
    assert seen["headers"]["printNow"] == "false"
    assert seen["headers"]["materialMappings"] == "W10="
    assert seen["files"]["gcodeFile"][0] == "cube.gcode.3mf"
    assert progress == [(0, len(b"print data")), (len(b"print data"), len(b"print data"))]


def test_list_flashforge_files_maps_detail_entries(monkeypatch):
    def fake_post(self, path, payload, timeout=5):
        assert path == "gcodeList"
        assert payload == {"serialNumber": "SN123", "checkCode": "code"}
        return {
            "code": 0,
            "gcodeListDetail": [
                {
                    "gcodeFileName": "cube.gcode.3mf",
                    "printingTime": 120,
                    "totalFilamentWeight": 3.5,
                    "useMatlStation": True,
                },
                {"gcodeFileName": "cube.gcode.3mf"},
                {"gcodeFileName": "benchy.gcode"},
            ],
        }

    monkeypatch.setattr(FlashForgeLocalClient, "_post_json", fake_post)

    files = list_flashforge_files("192.0.2.211", "SN123", "code")

    assert [file["name"] for file in files] == ["cube.gcode.3mf", "benchy.gcode"]
    assert files[0]["printing_time"] == 120
    assert files[0]["filament_weight"] == 3.5
    assert files[0]["use_matl_station"] is True


def test_list_flashforge_files_maps_string_entries(monkeypatch):
    def fake_post(self, path, payload, timeout=5):
        return {"code": 0, "gcodeList": ["/cache/cube.gcode.3mf", "benchy.gcode"]}

    monkeypatch.setattr(FlashForgeLocalClient, "_post_json", fake_post)

    files = list_flashforge_files("192.0.2.211", "SN123", "code", path="/cache")

    assert files == [
        {"name": "cube.gcode.3mf", "is_directory": False, "size": 0, "mtime": None},
        {"name": "benchy.gcode", "is_directory": False, "size": 0, "mtime": None},
    ]


def test_list_flashforge_files_ignores_subdirectories(monkeypatch):
    post = MagicMock()
    monkeypatch.setattr(FlashForgeLocalClient, "_post_json", post)

    assert list_flashforge_files("192.0.2.211", "SN123", "code", path="/model/subdir") == []
    post.assert_not_called()


def test_get_flashforge_current_thumbnail_prefers_gcode_thumbnail(monkeypatch):
    png = b"\x89PNG\r\n\x1a\nimage"

    def fake_post(self, path, payload, timeout=5):
        assert path == "gcodeThumb"
        return {"code": 0, "imageData": "iVBORw0KGgppbWFnZQ=="}

    monkeypatch.setattr(FlashForgeLocalClient, "_post_json", fake_post)

    assert get_flashforge_current_thumbnail("192.0.2.211", "SN123", "code", "cube.gcode.3mf") == (
        png,
        "image/png",
    )


def test_get_flashforge_storage_info_maps_remaining_disk_space(monkeypatch):
    def fake_fetch(self):
        return {"remainingDiskSpace": 1.5}

    monkeypatch.setattr(FlashForgeLocalClient, "_fetch_detail", fake_fetch)

    assert get_flashforge_storage_info("192.0.2.211", "SN123", "code") == {
        "used_bytes": None,
        "free_bytes": 1610612736,
    }
