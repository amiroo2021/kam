# MT5Linux installer assets
#
# entrypoint.sh          — update-safe container entrypoint (Wine + RPyC loop).
#                          Always rewrites /app/src/vnc.sh so x11vnc starts with
#                          `-display :0 -forever -shared -rfbport $VNC_PORT`
#                          (plus `-nopw` or `-passwd` from UI_PASSWORD). Shared
#                          mode is required for multiple simultaneous noVNC
#                          browser sessions; stock image vnc.sh omits -shared and
#                          refuses the second viewer.
# python-client/mt5linux — known-good mt5linux 1.1.1 package tree used when
#                         PyPI wheels SyntaxError on import (multiline f-strings).
#                         Copied into the isolated client venv / Hermes venv.
#                         Contains no credentials.
#
# Default behavior with ./install.sh --trade:
#   MT5Linux infrastructure is provisioned automatically (Docker if needed,
#   container, volume, client). Broker credentials in HERMES_HOME/.env are
#   optional at install time. Opt out with --no-mt5linux.
#
# Entrypoint is copied to HERMES_HOME/mt5linux/entrypoint.sh and bind-mounted
# read-only into new containers. Re-running install refreshes that copy
# idempotently without destroying the mt5-wine volume or saved accounts.
