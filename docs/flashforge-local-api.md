# FlashForge Local API Support

Bambuddy can monitor and control supported FlashForge printers through the printer's LAN HTTP API. This path is intentionally separate from the Bambu MQTT/FTP path because FlashForge printers expose a different protocol and a smaller set of commands.

## Supported Models

The current model detection is conservative:

- `Creator 5 Pro`
- `FlashForge Creator 5 Pro`

Add new models only after checking their `/detail` payload and confirming that the same upload, status, camera, and control endpoints behave the same way.

## Implemented Features

- Status polling through `/detail`
- State mapping to Bambuddy states: idle, preparing, running, paused, finished, failed
- Temperatures, targets, and heating flags for nozzle, bed, and chamber when
  present
- Fan percentages when present
- Speed percentage mapped back to Bambuddy's print-speed level for UI state
- Progress, layer count, filename, and remaining time. FlashForge reports
  `estimatedTime` as a total job estimate, so Bambuddy derives remaining time
  from `estimatedTime - printDuration`, uses progress and elapsed duration as
  a sanity fallback, ignores stale/overflowed `remainingTime` values when they
  disagree with the derived estimates, and forces completed jobs to `100%`
  with `0` minutes remaining.
- Material-station slots mapped to Bambuddy AMS-style tray data and labeled as
  IFS in the frontend
- Pause, resume, stop, and clear-error commands through local control endpoints
- Chamber light on/off through `lightControl_cmd` with `status=open|close`
- Print-speed mode control through `printerCtl_cmd`, mapped from Bambuddy's
  Silent/Standard/Sport/Ludicrous modes to FlashForge speed percentages
- Heater target control for nozzle, bed, and chamber through
  `temperatureCtl_cmd`
- Upload and start print through the local upload/start API
- File listing through the local G-code list endpoint
- File-row and current-print thumbnails when the printer exposes them
- MJPEG camera stream and snapshot proxying through Bambuddy's normal camera
  endpoints, including long-lived camera stream tokens
- Camera diagnostics using the FlashForge MJPEG port (`8080`) and first-frame
  reader instead of the Bambu RTSP/chamber-image pipeline
- Connection diagnostics using FlashForge-specific checks for local API reachability,
  camera reachability, saved device-key auth, and Bambuddy polling health
- Print start, completion, failure, and stopped notifications
- Plain-language error messages are preserved in `hms_errors[].message` when
  the printer reports them

## Known Capability Gaps

FlashForge's known LAN API does not expose every Bambu feature. Bambuddy returns explicit capability booleans and unsupported reasons so the frontend can hide or disable unsupported controls instead of showing dead buttons.

Currently unsupported for FlashForge:

- Airduct mode control
- Z jog / bed jog
- Homing
- Object skipping
- Calibration control
- Filament drying control
- File download
- File deletion
- Full 3MF preview / plate metadata
- Directory browsing beyond the local G-code list
- Timelapse retrieval

### Live Endpoint Probe Notes

Read-only probes against a Creator 5 Pro confirmed the currently known local
endpoints:

- `POST /detail`
- `POST /product`
- `POST /gcodeList`
- `POST /gcodeThumb` with `fileName`
- `GET /getThum`

The same probe found no working file-download or file-info endpoint among:

- `downloadGcode`, `downloadGCode`, `gcodeDownload`
- `getGcode`, `getGCode`, `getGcodeFile`
- `downloadFile`, `fileDownload`, `getFile`
- `gcodeFile`, `gcodeInfo`, `getFileInfo`, `gcodeDetail`, `getGcodeDetail`

The probe also found no working timelapse/video list or download endpoint among:

- `timelapseList`, `timeLapseList`, `getTimelapseList`, `getTimeLapseList`
- `listTimelapse`, `listTimeLapse`, `timelapse`, `timeLapse`
- `videoList`, `getVideoList`, `recordList`, `getRecordList`
- `cameraRecordList`, `getCameraRecordList`, `movieList`, `getMovieList`
- `mediaList`, `getMediaList`, `downloadTimelapse`, `downloadTimeLapse`
- `getTimelapse`, `getTimeLapse`, `videoDownload`, `downloadVideo`, `getVideo`

All probed download/info/timelapse candidates either returned HTTP 404 or no
response, while the known endpoints above responded successfully. Treat the
unsupported file-download/delete/preview/timelapse capabilities as confirmed
gaps for this firmware unless a different endpoint is captured from FlashMaker
or FlashPrint traffic.

If a future firmware or model exposes one of these features, add the command in the FlashForge client first, then enable the corresponding capability only for the confirmed model/firmware combination.

## Capability Contract

The main printer status response includes `capabilities`, using `PrinterCapabilities` from `backend/app/schemas/printer.py`.

For FlashForge printers, unsupported fields are set to `false` and include an explanation in `unsupported_reasons`.

The file manager response also includes a smaller `capabilities` object:

```json
{
  "can_download": false,
  "can_delete": false,
  "can_preview": false,
  "can_browse_directories": false,
  "unsupported_reason": "FlashForge's known LAN API exposes file listing/upload, but not direct file download, delete, preview, or directory browsing."
}
```

## Code Map

- Local API client: `backend/app/services/flashforge_local.py`
- Printer status and capability responses: `backend/app/api/routes/printers.py`
- Camera stream/snapshot proxy: `backend/app/api/routes/camera.py`
- Camera diagnostics: `backend/app/services/camera_diagnose.py`
- Connection diagnostics: `backend/app/services/printer_diagnostic.py`
- File-list/upload routing: `backend/app/services/bambu_ftp.py`
- Frontend capability gates: `frontend/src/pages/PrintersPage.tsx`
- File manager read-only handling: `frontend/src/components/FileManagerModal.tsx`
- Embedded camera gates: `frontend/src/components/EmbeddedCameraViewer.tsx`
- Overlay camera token handling: `frontend/src/pages/StreamOverlayPage.tsx`

## Test Coverage

Focused FlashForge tests live in:

- `backend/tests/unit/services/test_flashforge_local.py`
- `backend/tests/unit/services/test_bambu_ftp.py`
- `backend/tests/unit/services/test_camera_diagnose.py`
- `backend/tests/unit/services/test_printer_diagnostic.py`
- `backend/tests/integration/test_camera_api.py`
- `backend/tests/integration/test_printers_api.py`
- `frontend/src/__tests__/components/CameraDiagnoseModal.test.tsx`
- `frontend/src/__tests__/components/EmbeddedCameraViewer.test.tsx`
- `frontend/src/__tests__/components/FileManagerModal.test.tsx`
- `frontend/src/__tests__/pages/PrintersPage.test.tsx`
- `frontend/src/__tests__/pages/StreamOverlayPage.test.tsx`

Useful focused checks:

```bash
.venv/bin/ruff check backend/app/services/flashforge_local.py backend/app/services/camera_diagnose.py backend/app/api/routes/camera.py backend/app/api/routes/printers.py backend/app/schemas/printer.py
.venv/bin/python -m pytest -q backend/tests/unit/services/test_flashforge_local.py backend/tests/unit/services/test_bambu_ftp.py backend/tests/unit/services/test_camera_diagnose.py backend/tests/integration/test_camera_api.py backend/tests/integration/test_printers_api.py -k "flashforge or FlashForge"
cd frontend && npm test -- --run src/__tests__/components/CameraDiagnoseModal.test.tsx src/__tests__/components/EmbeddedCameraViewer.test.tsx src/__tests__/components/FileManagerModal.test.tsx src/__tests__/pages/PrintersPage.test.tsx src/__tests__/pages/StreamOverlayPage.test.tsx
```
