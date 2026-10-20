#!/usr/bin/env bash
# Installs two launchd jobs for the pilot on this Mac:
#   08:00 daily  -> lead-engine run-daily   (events -> review pull -> send -> CRM)
#   02:30 daily  -> lead-engine backup && lead-engine retention
# Review the plists before loading. Remove with: launchctl unload <plist> && rm <plist>
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$REPO/data/logs"

make_plist() {  # label hour minute command...
  local label="$1" hour="$2" minute="$3"; shift 3
  cat > "$AGENTS/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>-lc</string><string>$*</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer></dict>
  <key>StandardOutPath</key><string>$REPO/data/logs/$label.out</string>
  <key>StandardErrorPath</key><string>$REPO/data/logs/$label.err</string>
</dict></plist>
PLIST
  echo "wrote $AGENTS/$label.plist"
}

make_plist com.indoko.lead-engine.daily 8 0 "$UV run lead-engine run-daily"
make_plist com.indoko.lead-engine.backup 2 30 "$UV run lead-engine backup && $UV run lead-engine retention"
echo "Load with:"
echo "  launchctl load $AGENTS/com.indoko.lead-engine.daily.plist"
echo "  launchctl load $AGENTS/com.indoko.lead-engine.backup.plist"
echo "launchd runs a missed job when the Mac wakes, so a sleeping laptop catches up the next time it's on."
