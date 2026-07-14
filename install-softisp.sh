#!/usr/bin/env bash
#
# Deploy the IPU7/IMX471 software-ISP virtual camera.
#
# The kernel now streams real raw frames from the sensor (see install.sh for
# that fix), but Intel's proprietary libcamhal/psys userspace is broken by an
# irreconcilable version skew and only ever emits a green screen. This replaces
# that userspace path entirely: a small daemon debayers the raw sensor feed and
# publishes a normal virtual camera on /dev/video0.
#
#   sudo bash install-softisp.sh            # install + enable + verify
#   sudo bash install-softisp.sh --verify   # re-test only
#   sudo bash install-softisp.sh --rollback # restore the (broken) Intel relay
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-install}"
DAEMON=/usr/local/bin/ipu7-softisp.py
UNIT=/etc/systemd/system/ipu7-softisp.service
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

say(){ printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok(){ printf '\033[32m   OK: %s\033[0m\n' "$*"; }
warn(){ printf '\033[33m WARN: %s\033[0m\n' "$*"; }
fail(){ printf '\033[31m FAIL: %s\033[0m\n' "$*"; }
need_root(){ [ "$(id -u)" = 0 ] || { fail "run with sudo: sudo bash $0 $MODE"; exit 1; }; }
giveback(){ [ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER:" "$@" 2>/dev/null || true; }

find_loopback(){
  for d in /sys/devices/virtual/video4linux/video*; do
    [ -r "$d/name" ] || continue
    if grep -qxF "Intel MIPI Camera" "$d/name"; then echo "/dev/$(basename "$d")"; return; fi
  done
  echo /dev/video0
}

verify(){
  local loop; loop="$(find_loopback)"
  say "Verifying the virtual camera ($loop)"
  systemctl is-active --quiet ipu7-softisp.service || { fail "service not running"; journalctl -u ipu7-softisp -n 20 --no-pager | sed 's/^/     /'; return 1; }
  ok "ipu7-softisp.service is running"
  say "Opening the camera as an app would (this triggers capture)"
  # act as a consumer; first frames may be black while the sensor warms up
  timeout 20 gst-launch-1.0 -q v4l2src device="$loop" num-buffers=150 \
      ! videoconvert ! jpegenc quality=92 ! multifilesink location="$TMP/f-%03d.jpg" >/dev/null 2>&1 || true
  local last; last="$(ls "$TMP"/f-*.jpg 2>/dev/null | sort | tail -1 || true)"
  if [ -z "$last" ]; then
    fail "no frames read from $loop"; journalctl -u ipu7-softisp -n 25 --no-pager | sed 's/^/     /'; return 1
  fi
  local sd; sd="$(convert "$last" -colorspace Gray -format '%[fx:standard_deviation]' info: 2>/dev/null || echo 0)"
  cp "$last" "$HERE/softisp-live.jpg"; giveback "$HERE/softisp-live.jpg"
  say "Result"
  printf '   captured %s frames, saved newest to %s\n' "$(ls "$TMP"/f-*.jpg | wc -l)" "$HERE/softisp-live.jpg"
  printf '   image detail (gray stddev): %s\n' "$sd"
  if awk -v s="$sd" 'BEGIN{exit !(s+0>0.02)}'; then
    ok "the virtual camera is delivering a REAL image — open any camera app and pick 'Intel MIPI Camera'"
  else
    warn "frames flow but look uniform; if black, make sure the camera kill switch (F9) LED is OFF, then rerun --verify"
  fi
}

case "$MODE" in
--rollback)
  need_root
  say "Removing software-ISP service"
  systemctl disable --now ipu7-softisp.service 2>/dev/null || true
  rm -f "$UNIT" "$DAEMON" /etc/modules-load.d/ipu7-softisp.conf
  systemctl daemon-reload
  say "Restoring the original Intel relay (note: it produces the green screen)"
  systemctl unmask v4l2-relayd.service 'v4l2-relayd@default.service' 2>/dev/null || true
  systemctl enable --now v4l2-relayd.service 2>/dev/null || true
  ok "rolled back"
  ;;
--verify)
  verify
  ;;
--update)
  need_root
  [ -f "$HERE/ipu7-softisp.py" ] || { fail "ipu7-softisp.py missing"; exit 1; }
  say "Updating the daemon and restarting"
  install -m0755 "$HERE/ipu7-softisp.py" "$DAEMON"
  install -m0644 "$HERE/ipu7-softisp.service" "$UNIT" 2>/dev/null || true
  systemctl daemon-reload
  systemctl restart ipu7-softisp.service
  sleep 3
  systemctl is-active --quiet ipu7-softisp.service && ok "service restarted" \
    || { fail "service failed"; journalctl -u ipu7-softisp -n 25 --no-pager | sed 's/^/     /'; exit 1; }
  verify || true
  ;;
install)
  need_root
  [ -f "$HERE/ipu7-softisp.py" ] || { fail "ipu7-softisp.py missing next to this script"; exit 1; }

  say "Installing dependencies (numpy + gstreamer/v4l tools)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || warn "apt update failed (continuing; deps may already be present)"
  apt-get install -y --no-install-recommends python3-numpy v4l-utils \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good imagemagick \
      || { fail "dependency install failed — check network/apt"; exit 1; }
  python3 -c 'import numpy' 2>/dev/null && ok "python3 + numpy ready" || { fail "numpy not importable"; exit 1; }

  say "Installing the daemon and service"
  install -m0755 "$HERE/ipu7-softisp.py" "$DAEMON"
  install -m0644 "$HERE/ipu7-softisp.service" "$UNIT"
  echo v4l2loopback > /etc/modules-load.d/ipu7-softisp.conf   # ensure loopback at boot
  modprobe v4l2loopback 2>/dev/null || true
  ok "installed $DAEMON and $UNIT"

  say "Disabling the broken Intel relay (frees /dev/video0)"
  systemctl disable --now v4l2-relayd.service 'v4l2-relayd@default.service' 2>/dev/null || true
  systemctl mask v4l2-relayd.service 'v4l2-relayd@default.service' 2>/dev/null || true
  ok "v4l2-relayd masked"

  say "Starting ipu7-softisp.service"
  systemctl daemon-reload
  systemctl enable --now ipu7-softisp.service
  sleep 3
  systemctl is-active --quiet ipu7-softisp.service && ok "service active" \
    || { fail "service failed to start"; journalctl -u ipu7-softisp -n 30 --no-pager | sed 's/^/     /'; exit 1; }

  verify || true
  say "DONE"
  echo "   The webcam appears to apps as 'Intel MIPI Camera' on $(find_loopback)."
  echo "   Logs:      journalctl -u ipu7-softisp -f"
  echo "   Rollback:  sudo bash $0 --rollback"
  ;;
*)
  echo "usage: sudo bash $0 [--verify|--rollback]"; exit 1;;
esac
