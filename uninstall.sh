#!/usr/bin/env bash
# Stops the service and removes autostart. Leaves config and venv alone; their path is printed at the end.
set -euo pipefail
LABEL="dev.agentbelt"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ -f "$PLIST" ]; then
  mv "$PLIST" "$PLIST.removed"
  echo "Автозапуск удалён ($PLIST.removed)"
fi
echo "Конфигурация и окружение Python сохранены в ${AGENTBELT_HOME:-$HOME/.config/agentbelt}; при необходимости удалите их вручную"
