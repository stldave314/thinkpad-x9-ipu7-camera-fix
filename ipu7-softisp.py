#!/usr/bin/env python3
"""
ipu7-softisp — software ISP for the Lenovo ThinkPad X9 IPU7 / IMX471 camera.

Intel's proprietary libcamhal/psys userspace is broken on this machine (a
three-way version skew that no repo can reconcile), so it never produces a
real frame — apps get a green screen. But the *kernel* capture path works:
the IMX471 sensor streams real raw Bayer out of the IPU7 ISYS node. This
daemon does the image processing that libcamhal was supposed to do, in
software, and feeds a normal virtual camera:

    /dev/video1 (ISYS, raw SRGGB10)  ->  [debayer + auto-exposure + gray-world
    white balance + gamma]  ->  YUYV  ->  /dev/video0 (v4l2loopback)  ->  apps

I/O deliberately reuses stock, proven tools (v4l2-ctl to stream the raw node,
GStreamer to push into the loopback) so the only hard dependency is numpy.
Sensor exposure/gain are driven directly via a single VIDIOC_S_CTRL ioctl.

Run `ipu7-softisp.py --selftest out.png` to capture a few frames and save a
processed still (no loopback needed) — use it to check image quality.
"""
import sys, os, re, time, fcntl, struct, subprocess, argparse, signal
import numpy as np

MEDIA = "/dev/media0"
SENSOR_W, SENSOR_H = 1928, 1088          # IMX471 active area on this platform
MBUS = "SRGGB10_1X10"                     # Bayer order/depth the sensor emits
BAYER = "RGGB"                            # matches MBUS above
BLACK_LEVEL = 64                          # 10-bit sensor pedestal to subtract
TARGET = 0.34                             # AE target mean luma (fraction of full)
EXPO_MAX, GAIN_MAX = 2273, 960            # imx471 control ranges (from driver)

# V4L2 control ids (from `v4l2-ctl -d /dev/v4l-subdev4 --list-ctrls`)
CID_EXPOSURE      = 0x00980911
CID_HFLIP         = 0x00980914
CID_VFLIP         = 0x00980915
CID_ANALOGUE_GAIN = 0x009e0903
VIDIOC_S_CTRL     = 0xc008561c            # _IOWR('V',28, v4l2_control{u32 id; s32 val})
# This module is mounted inverted; VFLIP=1/HFLIP=0 is Intel's validated
# orientation and keeps the Bayer order at SRGGB10 (verified), so the
# RGGB debayer below stays correct. At cold boot the sensor defaults to
# VFLIP=0 (upside down), so the daemon must assert this itself.


def log(*a):
    print("ipu7-softisp:", *a, file=sys.stderr, flush=True)


def wait_for_media(timeout=60):
    """At boot the IPU7 drivers may not have created the nodes yet."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(MEDIA):
            r = subprocess.run(["media-ctl", "-d", MEDIA, "-p"],
                               capture_output=True, text=True)
            if "imx471" in r.stdout and re.search(r"/dev/video\d+", r.stdout):
                return
        time.sleep(1)
    log("warning: media graph not ready after %ds; trying anyway" % timeout)


# ---------------------------------------------------------------- media graph
def discover():
    """Parse the media graph into a dict describing the sensor, the CSI2
    receiver, the CSI2->capture link (source pad, capture entity+pad, video
    node), and the v4l2loopback sink. The link is found whether or not it is
    currently enabled — enabling it is setup_pipeline()'s job."""
    p = subprocess.run(["media-ctl", "-d", MEDIA, "-p"], capture_output=True, text=True)
    ent = {}                              # name -> {subdev, video, block}
    for block in re.split(r"\n- entity \d+: ", "\n" + p.stdout):
        m = re.match(r'([^\(]+?) \(', block)
        if not m:
            continue
        name = m.group(1).strip()
        sd = re.search(r'device node name (/dev/v4l-subdev\d+)', block)
        vd = re.search(r'device node name (/dev/video\d+)', block)
        ent[name] = {"subdev": sd.group(1) if sd else None,
                     "video": vd.group(1) if vd else None,
                     "block": block}
    sensor = next((n for n in ent if n.startswith("imx471")), None)
    if not sensor:
        sys.exit("no imx471 sensor entity in media graph (kernel fix not applied?)")
    csi2 = next((n for n in ent if n.startswith("Intel IPU7 CSI2")
                 and re.search(r'"%s":0' % re.escape(sensor), ent[n]["block"])), None)
    # walk the CSI2 block: find the Source pad linking to an ISYS Capture entity
    csi2_pad, cap_entity, cap_pad = None, None, None
    cur = None
    for line in (ent[csi2]["block"].splitlines() if csi2 else []):
        pm = re.match(r'\s*pad(\d+): Source', line)
        if pm:
            cur = int(pm.group(1))
        lm = re.search(r'-> "([^"]*ISYS Capture[^"]*)":(\d+)', line)
        if lm and cur is not None:
            csi2_pad, cap_entity, cap_pad = cur, lm.group(1), int(lm.group(2))
            break
    if not cap_entity:                    # sane default topology for port 0
        csi2_pad, cap_entity, cap_pad = 1, "Intel IPU7 ISYS Capture 0", 0
    return {"sensor_sd": ent[sensor]["subdev"], "sensor": sensor,
            "csi2": csi2, "csi2_pad": csi2_pad,
            "cap_entity": cap_entity, "cap_pad": cap_pad,
            "cap_node": ent.get(cap_entity, {}).get("video"),
            "loop": find_loopback()}


def find_loopback(label="Intel MIPI Camera"):
    for dev in sorted(_v.path for _v in os.scandir("/sys/devices/virtual/video4linux")
                      if _v.name.startswith("video")):
        try:
            name = open(os.path.join(dev, "name")).read().strip()
        except OSError:
            continue
        if name == label:
            return "/dev/" + os.path.basename(dev)
    # fallback: query each /dev/video* card type
    for n in range(0, 64):
        d = "/dev/video%d" % n
        if not os.path.exists(d):
            continue
        r = subprocess.run(["v4l2-ctl", "-d", d, "--info"], capture_output=True, text=True)
        if label in r.stdout:
            return d
    sys.exit("v4l2loopback sink '%s' not found (is v4l2loopback loaded?)" % label)


def setup_pipeline(g):
    """Set pad formats AND enable the CSI2->capture link so the raw node
    streams. On a cold boot with v4l2-relayd masked, nothing else enables
    this link, so doing it here is essential. Idempotent."""
    fmt = "%s/%dx%d" % (MBUS, SENSOR_W, SENSOR_H)

    def mc(*args):
        subprocess.run(["media-ctl", "-d", MEDIA, *args], capture_output=True)

    mc("-V", '"%s":0 [fmt:%s]' % (g["sensor"], fmt))
    if g["csi2"]:
        mc("-V", '"%s":0 [fmt:%s]' % (g["csi2"], fmt))
        mc("-V", '"%s":%d [fmt:%s]' % (g["csi2"], g["csi2_pad"], fmt))
        # THE fix: enable CSI2 source pad -> ISYS capture sink pad
        mc("-l", '"%s":%d -> "%s":%d [1]'
           % (g["csi2"], g["csi2_pad"], g["cap_entity"], g["cap_pad"]))


# ---------------------------------------------------------------- sensor ctrls
class Sensor:
    def __init__(self, subdev):
        self.fd = os.open(subdev, os.O_RDWR)
        self.expo = 1100
        self.gain = 200
        self.orient()
        self.apply()

    def set(self, cid, val):
        try:
            fcntl.ioctl(self.fd, VIDIOC_S_CTRL, struct.pack("Ii", cid, int(val)))
        except OSError:
            pass

    def orient(self):
        """Assert the mounting orientation (cached by the driver, applied on
        each stream-on). Cold boot defaults to upside-down."""
        self.set(CID_VFLIP, 1)
        self.set(CID_HFLIP, 0)

    def apply(self):
        self.set(CID_EXPOSURE, self.expo)
        self.set(CID_ANALOGUE_GAIN, self.gain)

    def auto_exposure(self, mean_frac):
        """Nudge exposure (then analogue gain) toward TARGET brightness."""
        if mean_frac <= 0:
            mean_frac = 1e-3
        err = TARGET / mean_frac
        err = max(0.5, min(2.0, err))          # damp per-step change
        if err > 1.0:                          # too dark -> raise exposure, then gain
            self.expo = min(EXPO_MAX, self.expo * err)
            if self.expo >= EXPO_MAX - 1:
                self.gain = min(GAIN_MAX, self.gain * err)
        else:                                  # too bright -> drop gain, then exposure
            if self.gain > 0:
                self.gain = max(0, self.gain * err)
            else:
                self.expo = max(1, self.expo * err)
        self.apply()


# ---------------------------------------------------------------- ISP
def make_gamma_lut(g=2.2):
    x = np.linspace(0, 1, 4096, dtype=np.float32)
    return (np.clip(x, 0, 1) ** (1.0 / g) * 255.0).astype(np.uint8)

GAMMA = make_gamma_lut()

def isp(raw16, stride_px, awb):
    """raw16: 1-D uint16 of one frame. Returns (yuyv_bytes, W, H, mean_frac, awb)."""
    a = raw16.reshape(-1, stride_px)[:SENSOR_H, :SENSOR_W].astype(np.float32)
    a -= BLACK_LEVEL
    np.clip(a, 0, None, out=a)
    # 2x2 RGGB binning -> half-res RGB (denoises + cheap debayer)
    R = a[0::2, 0::2]
    G = 0.5 * (a[0::2, 1::2] + a[1::2, 0::2])
    B = a[1::2, 1::2]
    meanG = float(G.mean())
    mean_frac = meanG / (1023.0 - BLACK_LEVEL)
    # gray-world AWB, smoothed across frames
    mR = max(float(R.mean()), 1.0); mB = max(float(B.mean()), 1.0)
    gR = min(4.0, meanG / mR); gB = min(4.0, meanG / mB)
    awb = (0.9 * awb[0] + 0.1 * gR, 0.9 * awb[1] + 0.1 * gB) if awb else (gR, gB)
    scale = 1.0 / max(1023.0 - BLACK_LEVEL, 1.0)
    Rn = np.clip(R * awb[0] * scale, 0, 1)
    Gn = np.clip(G * scale, 0, 1)
    Bn = np.clip(B * awb[1] * scale, 0, 1)
    idx = (Rn * 4095).astype(np.uint16); r8 = GAMMA[idx]
    idx = (Gn * 4095).astype(np.uint16); g8 = GAMMA[idx]
    idx = (Bn * 4095).astype(np.uint16); b8 = GAMMA[idx]
    H, W = r8.shape
    # RGB -> YUYV (BT.601 full-range-ish)
    rf = r8.astype(np.float32); gf = g8.astype(np.float32); bf = b8.astype(np.float32)
    Y = (0.299 * rf + 0.587 * gf + 0.114 * bf)
    U = (-0.169 * rf - 0.331 * gf + 0.500 * bf + 128.0)
    V = (0.500 * rf - 0.419 * gf - 0.081 * bf + 128.0)
    yuyv = np.empty((H, W * 2), dtype=np.uint8)
    yuyv[:, 0::4] = np.clip(Y[:, 0::2], 0, 255)
    yuyv[:, 2::4] = np.clip(Y[:, 1::2], 0, 255)
    yuyv[:, 1::4] = np.clip(U[:, 0::2], 0, 255)
    yuyv[:, 3::4] = np.clip(V[:, 0::2], 0, 255)
    return yuyv.tobytes(), W, H, mean_frac, awb, (r8, g8, b8)


# ---------------------------------------------------------------- capture I/O
def frame_bytes(cap):
    r = subprocess.run(["v4l2-ctl", "-d", cap, "--get-fmt-video"],
                       capture_output=True, text=True)
    bpl = int(re.search(r"Bytes per Line\s*:\s*(\d+)", r.stdout).group(1))
    return bpl, bpl * SENSOR_H

def start_capture(cap):
    subprocess.run(["v4l2-ctl", "-d", cap,
                    "--set-fmt-video=width=%d,height=%d,pixelformat=RG10" % (SENSOR_W, SENSOR_H)],
                   capture_output=True)
    return subprocess.Popen(
        ["v4l2-ctl", "-d", cap, "--stream-mmap=6", "--stream-count=0", "--stream-to=-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

PUB_W, PUB_H = 1280, 720                   # resolution published to apps

def start_output(loop, W, H, fps=30):
    # parse our native YUY2 frames, scale up to a standard 720p the apps expect
    pipe = ("fdsrc fd=0 ! rawvideoparse use-sink-caps=false format=yuy2 "
            "width=%d height=%d framerate=%d/1 ! videoscale ! "
            "video/x-raw,width=%d,height=%d ! videoconvert ! "
            "v4l2sink device=%s sync=false" % (W, H, fps, PUB_W, PUB_H, loop))
    # NOTE: buffered stdin (no bufsize=0). A raw/unbuffered write of a ~1 MB
    # frame to a 64 KB pipe does a *partial* write and returns early; a
    # BufferedWriter + flush() guarantees the whole frame reaches GStreamer,
    # otherwise frames desync and the output rolls/tears.
    return subprocess.Popen(["gst-launch-1.0", "-q"] + pipe.split(),
                            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)


def black_frame(W, H):
    f = np.empty((H, W * 2), dtype=np.uint8)
    f[:, 0::2] = 16      # Y = video black
    f[:, 1::2] = 128     # U/V = neutral
    return f.tobytes()


def read_exact(f, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def consumers(loop, exclude):
    """PIDs (other than `exclude`) that currently hold the loopback open.
    Lets the daemon idle — and stop draining the battery — when no app
    is using the webcam. Scans /proc directly, no external tools."""
    try:
        target = os.path.realpath(loop)
    except OSError:
        return set()
    pids = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) in exclude:
            continue
        fddir = "/proc/%s/fd" % pid
        try:
            for fd in os.listdir(fddir):
                try:
                    if os.readlink(os.path.join(fddir, fd)) == target:
                        pids.add(int(pid)); break
                except OSError:
                    pass
        except OSError:
            pass
    return pids


# ---------------------------------------------------------------- main loops
def run():
    wait_for_media()
    g = discover()
    cap, loop = g["cap_node"], g["loop"]
    log("sensor=%s csi2=%s capture=%s loopback=%s" % (g["sensor_sd"], g["csi2"], cap, loop))
    setup_pipeline(g)
    sensor = Sensor(g["sensor_sd"])
    bpl, fsz = frame_bytes(cap)
    stride_px = bpl // 2

    OUT_W, OUT_H = SENSOR_W // 2, SENSOR_H // 2   # 964x544 native ISP output
    black = black_frame(OUT_W, OUT_H)
    cproc = None
    oproc = start_output(loop, OUT_W, OUT_H)       # producer stays up -> /dev/video0
    awb = None                                     # always advertises a format
    n = 0
    retry_after = 0.0                              # backoff when capture won't start
    fail_count = 0
    frames_seen = 0

    def stop_capture():
        nonlocal cproc
        try:
            cproc and cproc.terminate()
        except Exception:
            pass
        cproc = None

    def cleanup(*_):
        stop_capture()
        try:
            oproc and oproc.terminate()
        except Exception:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    def push(buf):
        nonlocal oproc
        try:
            oproc.stdin.write(buf)          # BufferedWriter: consumes whole frame
            oproc.stdin.flush()             # force the full frame through the pipe
        except (BrokenPipeError, AttributeError, ValueError):
            log("output pipe broke; restarting producer")
            oproc = start_output(loop, OUT_W, OUT_H)
            try:
                oproc.stdin.write(buf); oproc.stdin.flush()
            except Exception:
                pass

    log("ready; idle (black) until an app opens %s" % loop)
    idle_since = None
    last_check = 0.0
    while True:
        now = time.time()
        if now - last_check >= 1.0:               # poll consumers at most 1/s
            last_check = now
            me = {os.getpid(), oproc.pid} | ({cproc.pid} if cproc else set())
            want = bool(consumers(loop, me))
            if want:
                idle_since = None
                if cproc is None and now >= retry_after:   # consumer present -> capture
                    log("consumer opened camera; capturing")
                    sensor.orient()                        # re-assert after power cycles
                    cproc = start_capture(cap); awb = None; frames_seen = 0
            elif cproc is not None:               # grace period before stopping
                if idle_since is None:
                    idle_since = now
                elif now - idle_since > 3:
                    log("no consumers; back to idle (black)")
                    stop_capture(); idle_since = None

        if cproc is None:                         # idle: keepalive black, no capture
            push(black); time.sleep(0.2); continue

        raw = read_exact(cproc.stdout, fsz)
        if raw is None:                           # capture ended
            stop_capture()
            if frames_seen == 0:                  # never delivered a frame -> back off
                fail_count += 1
                back = min(2 * fail_count, 15)
                retry_after = time.time() + back
                if fail_count == 2:
                    log("sensor is not streaming — is the F9 camera kill switch LED on? "
                        "(will keep retrying)")
                else:
                    log("capture produced no frames; retrying in %ds" % back)
            continue
        a = np.frombuffer(raw, dtype="<u2")
        yuyv, W, H, mean_frac, awb, _ = isp(a, stride_px, awb)
        push(yuyv)
        n += 1; frames_seen += 1; fail_count = 0
        if n % 5 == 0:
            sensor.auto_exposure(mean_frac)


def selftest(outpng, frames=12):
    g = discover(); cap = g["cap_node"]
    setup_pipeline(g)
    sensor = Sensor(g["sensor_sd"])
    bpl, fsz = frame_bytes(cap); stride_px = bpl // 2
    cproc = start_capture(cap)
    awb = None; rgb = None; mean_frac = 0
    for i in range(frames):
        raw = read_exact(cproc.stdout, fsz)
        if raw is None:
            break
        a = np.frombuffer(raw, dtype="<u2")
        _, W, H, mean_frac, awb, rgb = isp(a, stride_px, awb)
        sensor.auto_exposure(mean_frac)
    cproc.terminate()
    if rgb is None:
        sys.exit("selftest: no frames captured")
    r8, g8, b8 = rgb
    img = np.dstack([r8, g8, b8])
    with open(outpng.replace(".png", ".ppm"), "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (W, H)); f.write(img.tobytes())
    subprocess.run(["convert", outpng.replace(".png", ".ppm"), outpng], capture_output=True)
    log("selftest wrote %s  (%dx%d, mean=%.2f expo=%d gain=%d)"
        % (outpng, W, H, mean_frac, sensor.expo, sensor.gain))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", metavar="OUT.png")
    a = ap.parse_args()
    if a.selftest:
        selftest(a.selftest)
    else:
        run()
