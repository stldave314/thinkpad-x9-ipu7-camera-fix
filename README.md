# ThinkPad X9-14 (Lunar Lake) IPU7 / IMX471 webcam fix — Pop!_OS / Linux

**Machine:** Lenovo ThinkPad X9-14 Gen 1 (21QA0035US) · Intel Lunar Lake · IPU7 + Sony IMX471
**Developed on:** Pop!_OS 24.04, kernel 7.0.11-76070011-generic (System76)

The MIPI webcam produced only a black or green screen. There are **two
independent faults** (a kernel one and a userspace one), plus a red-herring
hardware switch. The scripts here fix both.

> Built and tested on the exact machine/kernel above. It should apply to other
> Lunar Lake laptops using the System76 `intel-ipu7` DKMS driver + an IMX471
> sensor, but treat anything outside that as unverified — read the scripts first.

---

## Requirements

- **Platform:** Pop!_OS 24.04 (or Ubuntu 24.04 on a System76 kernel) on the IPU7 +
  IMX471 hardware above.
- **System76 `intel-ipu7` DKMS package installed** — it provides the driver
  source at `/usr/src/intel-ipu7-0.1.0` that Fault 1 patches. Check with
  `dkms status | grep intel-ipu7`. Without it, `install.sh` has nothing to fix.
- **`v4l2loopback` + `v4l2-relayd`** (part of the Pop!_OS camera stack) — they
  create the `/dev/video0` virtual-camera node the software ISP publishes to.
- **Kernel headers for the running kernel** (`linux-headers-$(uname -r)`) so DKMS
  can rebuild the driver.
- Root/sudo. Secure Boot: the rebuilt modules are unsigned — if you enforce
  Secure Boot module signing you must sign them or the driver won't load.

## Dependencies

**Fault 1 — `install.sh`** (kernel driver). Ensure these are present:

```
sudo apt install dkms build-essential patch linux-headers-$(uname -r) v4l-utils
```

**Fault 2 — `install-softisp.sh`** (software ISP). The installer apt-installs its
own dependencies, so you normally don't need to do this by hand:

```
python3-numpy v4l-utils acl gstreamer1.0-tools \
gstreamer1.0-plugins-base gstreamer1.0-plugins-good imagemagick
```

At runtime the daemon needs only `python3` + `numpy` and the stock `v4l2-ctl` /
`gst-launch-1.0` / `media-ctl` binaries; it drives the sensor via a raw
`VIDIOC_S_CTRL` ioctl (no extra Python bindings).

---

## Install

```
git clone https://github.com/stldave314/thinkpad-x9-ipu7-camera-fix.git
cd thinkpad-x9-ipu7-camera-fix

sudo bash ./install.sh            # fault 1: kernel sensor path
sudo bash ./install-softisp.sh    # fault 2: userspace image processing
```

Then make sure the **camera key (F9)** is not in "privacy" mode, open any camera
app, and choose **“Intel MIPI Camera.”** Each script backs up what it changes and
verifies the result at the end. The scripts run from wherever you cloned them.

## Usage / management

```
sudo bash ./install.sh --rollback           # restore original kernel modules
sudo bash ./install-softisp.sh --verify     # re-test the virtual camera
sudo bash ./install-softisp.sh --update     # redeploy the daemon after editing it (no apt)
sudo bash ./install-softisp.sh --rollback   # restore Intel's original relay path

journalctl -u ipu7-softisp -f               # daemon logs
python3 ipu7-softisp.py --selftest out.png  # capture one processed still (needs numpy)
```

---

## Fault 1 — kernel: the sensor never entered the media graph

The System76 `intel-ipu7` DKMS package builds the IPU7 isys driver **without
`CONFIG_INTEL_IPU_ACPI`**, which in `ipu7-isys.c` compiles out *every*
sensor-discovery path (both the ipu-acpi platform-data path and the v4l2-async
fwnode notifier). So the sensor probed but was never linked into `/dev/media0`,
and nothing could ever capture.

`install.sh` patches the DKMS source to define that flag and build the three
`ipu-acpi*` helper modules (`enable-ipu-acpi.patch`), then `dkms build/install`.
After it, `media-ctl -d /dev/media0 -p | grep imx471` shows the sensor and the
raw ISYS node **/dev/video1** streams real Bayer frames. **This fix is required
for the webcam to work at all** and for the software ISP below to have anything
to read.

## Fault 2 — userspace: Intel’s libcamhal/psys stack is version-broken

Even with the kernel fixed, apps still saw green. Intel’s proprietary userspace
(`libcamhal` + the `ipu7x` psys plugin) is assembled on this system from **three
mismatched build eras that no repository can reconcile**:

| component | version | origin |
|---|---|---|
| `gstreamer1.0-icamera` (`icamerasrc`) | git 2025-09-26 | Lenovo `sutton` |
| `libcamhal0` / `libcamhal-common` (core) | git 2026-01-20 | **orphaned — in no repo** |
| `libcamhal-ipu7x` (plugin) | git 2026-07-06 | Intel PPA |

The plugin `dlopen`s into the core and they share an **unstable C++ ABI**; the
skew corrupts pipeline state (`sensor output sub device is not set`), the psys
tasks never complete (`wait trigger time out`), and the HAL falls back to a
missing dummy file → zero-filled frame → **green**. The Intel PPA no longer even
ships the core `libcamhal0`, so there is no coherent set to `apt` back to.

**Fix = bypass Intel’s userspace entirely with a software ISP** (chosen over
rebuilding the driver or juggling packages because it’s robust and update-proof):

```
/dev/video1 (raw SRGGB10)
   → ipu7-softisp.py:  debayer + auto-exposure + gray-world white-balance + gamma
   → YUYV → GStreamer → /dev/video0 (v4l2loopback, “Intel MIPI Camera”)
   → apps
```

`install-softisp.sh` installs the dependencies, drops `ipu7-softisp.py` at
`/usr/local/bin`, installs+starts `ipu7-softisp.service`, **masks the broken
`v4l2-relayd`** so it stops fighting for `/dev/video0`, and installs a udev rule
(`99-ipu7-hide-raw-nodes.rules`) that makes the 32 raw ISYS capture nodes
root-only — so camera apps list **only** “Intel MIPI Camera” instead of 33
devices. The daemon runs as root and still uses the raw node.

### About the software ISP daemon (`ipu7-softisp.py`)

- I/O reuses stock tools (`v4l2-ctl` to pull the raw node, GStreamer to push the
  loopback); the only real dependency is `numpy`. Sensor exposure/gain/flip are
  set via a single `VIDIOC_S_CTRL` ioctl.
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

---

## The red herring — camera "kill switch"

`SW_CAMERA_LENS_COVER` on this laptop is the **F9 camera key**, not a lens cover.
It only ever fed a *privacy signal* that Intel’s HAL honored — it does **not** cut
the kernel capture path, so the software ISP streams regardless of it. (During
early diagnosis this key looked like it was disabling the camera; it wasn’t.)

---

## Maintenance / gotchas

- **A System76 update to the `intel-ipu7` DKMS source reverts Fault 1’s patch.**
  Symptom: camera dead again, no `imx471` in `media-ctl`. Cure: rerun
  `sudo bash ./install.sh`. (New kernels get it automatically via DKMS as long as
  the patched source is in place.)
- The software ISP is independent of Intel/Lenovo userspace updates — those can no
  longer break it. If Intel ever ships a *coherent* libcamhal set again and you
  want hardware 3A/quality back, `install-softisp.sh --rollback` restores the
  original relay.
- **Report upstream (System76, `system76/pop`):** their `intel-ipu7` packaging
  should define `CONFIG_INTEL_IPU_ACPI` and ship the `ipu-acpi` modules like
  Canonical’s `linux-modules-ipu7-*-oem` does.

## Files

- `install.sh` + `enable-ipu-acpi.patch` — Fault 1 kernel fix.
- `prebuilt-7.0.11-76070011-generic/` — the 8 modules prebuilt for that exact
  kernel, as a fallback if DKMS can’t rebuild (kernel-version specific).
- `install-softisp.sh` + `ipu7-softisp.py` + `ipu7-softisp.service` — Fault 2
  software-ISP bypass.
- `99-ipu7-hide-raw-nodes.rules` — udev rule that hides the raw ISYS capture
  nodes so apps list only the virtual camera.

## License

GPL-2.0 (`LICENSE`). Fault 1 patches GPL-2.0 kernel driver source; the software
ISP is original work released under the same license.
