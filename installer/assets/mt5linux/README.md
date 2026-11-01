# MT5Linux installer assets
#
# entrypoint.sh          — update-safe container entrypoint (Wine + RPyC loop)
# python-client/mt5linux — known-good mt5linux 1.1.1 package tree used when
#                         PyPI wheels SyntaxError on import (multiline f-strings).
#                         Copied into the isolated client venv / Hermes venv.
#                         Contains no credentials.
#
# Default behavior with ./install.sh --trade:
#   MT5Linux infrastructure is provisioned automatically (Docker if needed,
#   container, volume, client). Broker credentials in HERMES_HOME/.env are
#   optional at install time. Opt out with --no-mt5linux.
