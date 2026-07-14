#!/usr/bin/env bash
#
# Fix the Intel IPU7 / IMX471 MIPI webcam on Pop!_OS (ThinkPad X9-14 Gen 1)
#
# Root cause: the intel-ipu7 DKMS package builds the isys driver WITHOUT
# CONFIG_INTEL_IPU_ACPI. That #ifdef doesn't just gate the optional
# ipu-acpi platform-data path — it gates the v4l2-async fwnode notifier
# too (ipu7-isys.c lines 266-385). The shipped isys therefore has NO
# sensor-discovery path at all: ipu_bridge builds the fwnode graph, the
# imx471 driver probes and registers, and nobody ever links them, so
# media-ctl shows no imx471 entity and the camera HAL finds no sensors
# ("green/black screen" = empty frames from v4l2loopback).
#
# Fix: patch the DKMS source to define CONFIG_INTEL_IPU_ACPI=1 and build
# the three ipu-acpi helper modules the core then links against — the
# same module set Canonical ships on working Ubuntu OEM kernels.
#
# Usage:
#   sudo bash install.sh              # patch + rebuild + reload + test
#   sudo bash install.sh --verify     # re-check after a reboot
#   sudo bash install.sh --rollback   # restore the original modules
#
set -euo pipefail

SRC=/usr/src/intel-ipu7-0.1.0
KVER="$(uname -r)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="$HERE/enable-ipu-acpi.patch"
BACKUP="$HERE/backup-$KVER"
MODDIR="/lib/modules/$KVER/updates/dkms"
MODE="${1:-install}"
CAPDIR="$(mktemp -d)"
trap 'rm -rf "$CAPDIR"' EXIT

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m   OK: %s\033[0m\n' "$*"; }
warn() { printf '\033[33m WARN: %s\033[0m\n' "$*"; }
fail() { printf '\033[31m FAIL: %s\033[0m\n' "$*"; }

need_root() {
  [ "$(id -u)" = 0 ] || { fail "this mode needs root: sudo bash $0 $MODE"; exit 1; }
}

give_back_to_user() {  # chown artifacts to the invoking user
  [ -n "${SUDO_USER:-}" ] && chown -R "$SUDO_USER:" "$@" 2>/dev/null || true
}

verify_graph() {
  say "Checking media graph for the imx471 sensor entity"
  if media-ctl -d /dev/media0 -p 2>/dev/null | grep -q 'imx471'; then
    ok "imx471 is linked into the IPU7 media graph:"
    media-ctl -d /dev/media0 -p | grep -E -A4 'entity.*imx471' | sed 's/^/     /'
    return 0
  fi
  fail "no imx471 entity in /dev/media0"
  dmesg 2>/dev/null | grep -iE 'ipu7|imx471' | tail -6 | sed 's/^/     /' || true
  return 1
}

capture_test() {
  say "Restarting the camera relay"
  systemctl restart v4l2-relayd.service 2>/dev/null || true
  systemctl list-units --all 'v4l2-relayd@*' --no-legend 2>/dev/null \
    | awk '{print $1}' | xargs -r -n1 systemctl restart 2>/dev/null || true
  sleep 2

  say "Checking the privacy shutter (Fn+F9)"
  if python3 - <<'PYEOF'
import os, fcntl, array, glob, sys
for dev in sorted(glob.glob('/dev/input/event*')):
    try:
        fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        sw = array.array('B', [0]*16)
        fcntl.ioctl(fd, 0x8080451b, sw)
        os.close(fd)
        if sw[1] & 2:
            sys.exit(1)
    except OSError:
        pass
sys.exit(0)
PYEOF
  then
    ok "lens cover switch is open"
  else
    fail "electronic privacy shutter is CLOSED — press the camera key (Fn+F9), then rerun: sudo bash $0 --verify"
    return 1
  fi

  # test the app-facing path (loopback via relay); a direct icamerasrc run
  # would contend with the relay for the HAL and fail spuriously
  say "Capture test: what apps see (/dev/video0 loopback)"
  mkdir -p "$CAPDIR/app"
  if timeout 40 gst-launch-1.0 -q v4l2src device=/dev/video0 num-buffers=60 \
       ! videoconvert ! jpegenc quality=92 \
       ! multifilesink location="$CAPDIR/app/f-%03d.jpg" >/dev/null 2>&1 \
     && ls "$CAPDIR"/app/f-*.jpg >/dev/null 2>&1; then
    ok "/dev/video0 is delivering frames to applications"
  else
    fail "no frames from /dev/video0"
    journalctl -u v4l2-relayd@default -b --no-pager 2>/dev/null | grep -v run3A | tail -10 | sed 's/^/     /' || true
    return 1
  fi

  for d in app; do
    last="$(ls "$CAPDIR/$d"/f-*.jpg 2>/dev/null | sort | tail -1 || true)"
    [ -n "$last" ] || continue
    dest="$HERE/test-capture-$d.jpg"
    cp "$last" "$dest"
    stats="$(convert "$dest" -colorspace Gray -format '%[fx:mean] %[fx:standard_deviation]' info: 2>/dev/null || true)"
    printf '   saved %s  (gray mean/stddev: %s)\n' "$dest" "${stats:-n/a}"
    if [ -n "$stats" ] && awk -v s="$(echo "$stats" | awk '{print $2+0}')" 'BEGIN{exit !(s<0.01)}'; then
      warn "$(basename "$dest") is a uniform image — if black, open the physical privacy shutter / check Fn+F9, then retest"
    fi
  done
  give_back_to_user "$HERE"/test-capture-*.jpg
}

case "$MODE" in
# ---------------------------------------------------------------- rollback
--rollback)
  need_root
  [ -d "$BACKUP" ] || { fail "no backup at $BACKUP"; exit 1; }
  say "Restoring original modules from $BACKUP"
  cp -av "$BACKUP"/. "$MODDIR"/
  rm -fv "$MODDIR"/ipu-acpi*.ko*
  if grep -q 'CONFIG_INTEL_IPU_ACPI' "$SRC/Makefile" 2>/dev/null; then
    patch -R -p1 -d "$SRC" < "$PATCH" && ok "source patch reverted"
  fi
  depmod -a "$KVER"
  ok "rolled back — reboot to finish"
  ;;
# ---------------------------------------------------------------- verify
--verify)
  verify_graph && capture_test && say "ALL GOOD — the webcam works. Open any camera app."
  ;;
# ---------------------------------------------------------------- install
install)
  need_root
  [ -d "$SRC" ] || { fail "$SRC not found"; exit 1; }
  [ -f "$PATCH" ] || { fail "$PATCH not found"; exit 1; }
  [ -d "/usr/src/linux-headers-$KVER" ] || { fail "kernel headers for $KVER missing"; exit 1; }

  say "Backing up current modules to $BACKUP"
  if [ -d "$BACKUP" ]; then
    ok "backup already exists — keeping the original files"
  else
    mkdir -p "$BACKUP"
    cp -a "$MODDIR"/intel-ipu7*.ko* "$MODDIR"/imx471.ko* "$MODDIR"/ipu-bridge.ko* "$BACKUP"/ 2>/dev/null || true
    ls "$BACKUP" | sed 's/^/     /'
  fi

  say "Patching DKMS source in $SRC"
  if grep -q 'CONFIG_INTEL_IPU_ACPI' "$SRC/Makefile"; then
    ok "already patched — skipping"
  else
    patch -p1 -d "$SRC" < "$PATCH"
    ok "patch applied"
  fi

  say "Rebuilding intel-ipu7 DKMS for $KVER (takes a minute)"
  # this dkms refuses to rebuild over an existing built tree and its
  # build --force does not clear it. Worse: the patched dkms.conf declares
  # 8 modules while the old tree holds 5, so dkms reports "not built /
  # not installed" and remove skips, yet the stale directory still blocks
  # build. Clear the per-kernel tree by hand (module backups: $BACKUP).
  dkms remove intel-ipu7/0.1.0 -k "$KVER" 2>/dev/null || true
  rm -rf "/var/lib/dkms/intel-ipu7/0.1.0/$KVER"
  rm -f  "/var/lib/dkms/intel-ipu7/kernel-$KVER-$(uname -m)"
  dkms build intel-ipu7/0.1.0 -k "$KVER"
  dkms install intel-ipu7/0.1.0 -k "$KVER" --force
  depmod -a "$KVER"
  modinfo -k "$KVER" ipu-acpi >/dev/null 2>&1 && ok "ipu-acpi modules installed" \
    || { fail "ipu-acpi module not found after install"; exit 1; }

  say "Checking whether the initramfs carries IPU7 modules"
  INITRD="$(sed 's/.*initrd=\([^ ]*\).*/\1/;s#\\#/#g' /proc/cmdline)"
  INITRD="/boot/efi$INITRD"
  if [ -f "$INITRD" ] && lsinitramfs "$INITRD" 2>/dev/null | grep -qE 'intel-ipu7|ipu-bridge|imx471'; then
    warn "initramfs contains IPU7 modules — regenerating"
    update-initramfs -u -k "$KVER"
  else
    ok "initramfs does not bundle IPU7 modules (nothing to do)"
  fi

  say "Reloading the camera driver stack in place"
  if modprobe -r intel_ipu7_psys intel_ipu7_isys imx471 intel_ipu7 2>/dev/null; then
    modprobe intel_ipu7        # pulls in new ipu-bridge + ipu-acpi chain
    modprobe intel_ipu7_isys
    modprobe intel_ipu7_psys
    modprobe imx471
    sleep 4
    if verify_graph; then
      capture_test || true
      say "DONE. Modules are installed permanently; a reboot will use the same fixed set."
      echo "   Rollback any time:  sudo bash $0 --rollback"
    else
      warn "live reload didn't bind the sensor — REBOOT, then run: sudo bash $0 --verify"
    fi
  else
    warn "modules are busy (something holds the camera) — REBOOT, then run: sudo bash $0 --verify"
  fi
  ;;
*)
  echo "usage: sudo bash $0 [--verify|--rollback]"; exit 1;;
esac
