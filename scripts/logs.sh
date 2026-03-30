#!/usr/bin/env bash
# Show clean AuraScribe server logs, filtering out the verbose torchcodec noise
LOGFILE=/tmp/aurascribe.log
NOISE="torchcodec|libtorchcodec|libnvrtc|UserWarning|warnings.warn|_internally_replaced|load_library|ctypes\.__init__|_dlopen|OSError: lib|FFmpeg version|start of lib|end of lib|torch._ops|Could not load|The following exceptions|fix torchcodec|use audio preloaded|versions 4, 5, 6"

if [ ! -f "$LOGFILE" ]; then
  echo "No log file found. Start AuraScribe first."
  exit 1
fi

if [ "$1" = "-f" ]; then
  tail -f "$LOGFILE" | grep -Ev "$NOISE"
else
  grep -Ev "$NOISE" "$LOGFILE"
fi
