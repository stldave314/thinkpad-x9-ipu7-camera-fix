# ThinkPad X9-14 (Lunar Lake) IPU7 / IMX471 webcam fix — Pop!_OS

**Machine:** Lenovo ThinkPad X9-14 Gen 1 (21QA0035US) · Intel Lunar Lake · IPU7 + Sony IMX471
**Kernel:** 7.0.11-76070011-generic (System76) · **Distro:** Pop!_OS 24.04

The camera was black/green for months. There were **two independent faults**,
plus a red-herring hardware switch. Both faults are fixed by the scripts here.

---

## TL;DR — how to make it work

```
sudo bash ~/ipu7-fix/install.sh            # fault 1: kernel sensor path
sudo bash ~/ipu7-fix/install-softisp.sh    # fault 2: userspace image processing
```

Then make sure the **camera kill-switch key (F9)** LED is **off** (when lit, the
camera is electrically disabled). Open any camera app and choose **“Intel MIPI
Camera.”** That’s it.

`PROOF-exposed-whitebalanced.png` / `selftest.png` are real frames produced by
this pipeline — proof the hardware works.

---

## Fault 1 — kernel: the sensor never entered the media graph

`install.sh` (details in the header of that script). The System76 `intel-ipu7`
DKMS package built the IPU7 isys driver **without `CONFIG_INTEL_IPU_ACPI`**,
which in `ipu7-isys.c` compiles out *every* sensor-discovery path (both the
ipu-acpi platform-data path and the v4l2-async fwnode notifier). So the sensor
probed but was never linked into `/dev/media0`, and nothing could ever capture.

Fix = patch the DKMS source to define that flag and build the three `ipu-acpi*`
helper modules (`enable-ipu-acpi.patch`), then `dkms build/install`. After this,
`media-ctl -d /dev/media0 -p | grep imx471` shows the sensor, and the raw ISYS
node **/dev/video1** streams real Bayer frames. **This fix is required for the
webcam to work at all** and for the software ISP below to have anything to read.

## Fault 2 — userspace: Intel’s libcamhal/psys stack is version-broken

Even with the kernel fixed, apps still saw green. Intel’s proprietary userspace
(`libcamhal` + the `ipu7x` psys plugin) is assembled on this system from **three
mismatched build eras that no repository can reconcile**:

| component | version | origin |
|---|---|---|
| `gstreamer1.0-icamera` (`icamerasrc`) | git 2025-09-26 | Lenovo `sutton` |
| `libcamhal0` / `libcamhal-common` (core) | git 2026-01-20 | **orphaned — in no repo** |
| `libcamhal-ipu7x` (plugin) | git 2026-07-06 | Intel PPA |

The plugin `dlopen`s into the core and they share an **unstable C++ ABI**; a
six-month skew corrupts pipeline state (`sensor output sub device is not set`),
the psys tasks never complete (`wait trigger time out`), and the HAL falls back
to a missing dummy file → zero-filled frame → **green**. The Intel PPA no longer
even ships the core `libcamhal0`, so there is no coherent set to `apt` back to.

**Fix = bypass Intel’s userspace entirely with a software ISP** (chosen over
rebuilding the driver or juggling packages because it’s robust and update-proof):

```
/dev/video1 (raw SRGGB10)
   → ipu7-softisp.py:  debayer + auto-exposure + gray-world white-balance + gamma
   → YUYV → GStreamer → /dev/video0 (v4l2loopback, “Intel MIPI Camera”)
   → apps
```

`install-softisp.sh` installs `numpy`, drops `ipu7-softisp.py` at
`/usr/local/bin`, installs+starts `ipu7-softisp.service`, and **masks the broken
`v4l2-relayd`** so it stops fighting for `/dev/video0`.

### About the software ISP daemon (`ipu7-softisp.py`)

- I/O reuses stock tools (`v4l2-ctl` to pull the raw node, GStreamer to push the
  loopback); the only real dependency is `numpy`. Sensor exposure/gain are set
  via a single `VIDIOC_S_CTRL` ioctl.
- **Cold-boot correct:** it enables the CSI2→capture media link itself (nothing
  else does once `v4l2-relayd` is masked — this was the post-reboot black screen)
  and asserts the sensor orientation `VFLIP=1`/`HFLIP=0` (the module is mounted
  inverted; it defaults to upside-down at power-on).
- **Idle-efficient:** it publishes a black keepalive frame until an app actually
  opens `/dev/video0`, then starts the sensor + debayer, and stops ~3 s after the
  app closes. No battery drain when the webcam isn’t in use.
- **Privacy note:** because it bypasses Intel’s HAL, it no longer honors the F9
  “camera off” privacy signal — F9 will *not* blank this virtual camera. (The
  daemon could be made to watch `SW_CAMERA_LENS_COVER` and push black if you want
  that behavior back.)
- Auto-exposure targets ~34 % mean luma; gray-world AWB is smoothed across frames.
- Publishes 1280×720 (upscaled from the sensor’s binned 964×544).
- Self-test without touching the system:
  `python3 ipu7-softisp.py --selftest out.png` (needs numpy; captures a still).
- Logs: `journalctl -u ipu7-softisp -f`

---

## The red herring — camera kill switch

`SW_CAMERA_LENS_COVER` on this laptop is the **F9 camera-disable key**, not a
lens cover. When its LED is **on, the camera is electrically off** and every path
above yields black/uniform frames. If capture is uniform black but the media
graph and service look healthy, press F9 so the LED is off.

---

## Maintenance / gotchas

- **A System76 update to the `intel-ipu7` DKMS source reverts Fault-1’s patch.**
  Symptom: camera dead again, no `imx471` in `media-ctl`. Cure: rerun
  `sudo bash ~/ipu7-fix/install.sh`. (New kernels get it automatically via DKMS
  as long as the patched source is in place.)
- The software ISP is independent of Intel/Lenovo userspace updates — those can
  no longer break it. If Intel ever ships a *coherent* libcamhal set again and you
  want hardware 3A/quality back, `install-softisp.sh --rollback` restores the
  original relay.
- **Rollbacks:** `install.sh --rollback` (kernel modules),
  `install-softisp.sh --rollback` (userspace bypass).
- **After editing `ipu7-softisp.py`:** `sudo bash install-softisp.sh --update`
  redeploys the daemon + restarts + re-verifies (no apt).
- **Report upstream (System76, `system76/pop`):** their `intel-ipu7` packaging
  must define `CONFIG_INTEL_IPU_ACPI` and ship the `ipu-acpi` modules like
  Canonical’s `linux-modules-ipu7-*-oem` does. Notes: `~/ipu7-camera-bug-report.md`.

## Files

- `install.sh`, `enable-ipu-acpi.patch`, `prebuilt-*/`, `backup-*/` — Fault-1 kernel fix.
- `install-softisp.sh`, `ipu7-softisp.py`, `ipu7-softisp.service` — Fault-2 bypass.
- `PROOF-*.png`, `selftest.png`, `softisp-live.jpg` — captured frames proving it works.
