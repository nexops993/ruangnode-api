#!/bin/bash
# Upgrade mingguan yt-dlp di venv app; restart service bila versi berubah.
VENV=/home/adit/ruangnode-api/.venv/bin
BEFORE=$($VENV/python -c "import yt_dlp; print(yt_dlp.version.__version__)" 2>/dev/null)
$VENV/pip install -q -U yt-dlp
AFTER=$($VENV/python -c "import yt_dlp; print(yt_dlp.version.__version__)" 2>/dev/null)
echo "yt-dlp: ${BEFORE:-?} -> ${AFTER:-?}"
if [ -n "$AFTER" ] && [ "$BEFORE" != "$AFTER" ]; then
  systemctl restart ruangnode-api
  echo "ruangnode-api direstart (yt-dlp diperbarui)"
else
  echo "tidak ada perubahan versi"
fi
