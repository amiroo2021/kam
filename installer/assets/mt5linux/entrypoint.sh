#!/bin/sh
set -e
echo "[entrypoint] preparing mt5linux (update-safe)"
rm -f /opt/wineprefix/drive_c/server /tmp/.X0-lock /tmp/.X99-lock 2>/dev/null || true

if [ ! -e /opt/websockify ]; then
  tar xzvf /app/opt.tar.gz -C / >/dev/null
  chmod +x /opt/websockify /opt/noVNC/http-server 2>/dev/null || true
fi
if [ ! -x /opt/wineprefix/drive_c/MT5/terminal64.exe ]; then
  mkdir -p /opt/wineprefix/drive_c/MT5
  tar xzvf /app/mt5.tar.gz -C /opt/wineprefix/drive_c/MT5 >/dev/null
fi
if [ ! -f /opt/wineprefix/drive_c/mt5server.exe ] && [ -f /app/mt5server.exe ]; then
  cp -f /app/mt5server.exe /opt/wineprefix/drive_c/mt5server.exe
fi

cp /app/src/automation.sh /app/src/automation.sh.bak
awk '
  BEGIN {skip=0}
  /^init_wine\(\)/ {
    skip=1
    print "init_wine() {"
    print "  log_info \"Initializing Wine (fixed)...\""
    print "  if [ ! -d \"$WIN_ROOT/windows\" ]; then wineboot -u >&2 2>/dev/null || true; fi"
    print "  i=0; while [ ! -d \"$WIN_ROOT\" ]; do i=$((i+1)); [ \"$i\" -gt 60 ] && break; sleep 0.5; done"
    print "  rm -f \"$WIN_ROOT/server\""
    print "  mkfifo -m 666 \"$WIN_ROOT/server\""
    print "}"
    next
  }
  skip && /^}/ {skip=0; next}
  skip {next}
  {print}
' /app/src/automation.sh.bak > /app/src/automation.sh

cat >/app/src/mt5.sh << 'MT5EOF'
#!/bin/sh
set -e
. /app/src/automation.sh

start_rpyc_server() {
  log_info "Starting RPyC server on ${MT5_HOST}:${RPYC_PORT}..."
  wine64 C:/mt5server.exe --host $MT5_HOST --port $RPYC_PORT &
  RPYC_PID=$!
  log_info "RPyC server started (PID: $RPYC_PID)"
}

start_mt5() {
  wine64 C:/MT5/terminal64.exe /portable /config:C:/MT5/common.ini &
  MT5_PID=$!
  wait_for_terminal64
}

start_watchdog() {
  watchdog_rpyc() {
    while true; do
      sleep 10
      if ! kill -0 $RPYC_PID 2>/dev/null; then
        log_info "RPyC server crashed (PID: $RPYC_PID). Restarting..."
        start_rpyc_server
      fi
    done
  }
  watchdog_rpyc &
  WATCHDOG_PID=$!
}

wait_for_terminal64() {
  n=0
  while [ "$n" -lt 120 ]; do
    if xdotool search --classname 'terminal64.exe' >/dev/null 2>&1; then
      return 0
    fi
    n=$((n+1))
    sleep 1
  done
  log_info "wait_for_terminal64 timed out; continuing anyway"
  return 0
}

wait_for_mt5_and_type_server() {
  test -n "$FIRST_RUN" || return 0
  if [ "$SERVER" ] || [ "$MT5_SERVER" ]; then
    log_info "Waiting for MT5 start (server ${SERVER:-$MT5_SERVER})"
    wait_for_terminal64
  fi
}
MT5EOF

cat >/app/src/setup.sh << 'SETUPEOF'
#!/bin/sh
set -e
FIRST_RUN=
if ! [ -e /opt/websockify ]; then
  FIRST_RUN=1
  log_info "Setting up container tooling..."
  tar xzvf /app/opt.tar.gz -C / >&2 >/dev/null
  chmod +x /opt/websockify /opt/noVNC/http-server
fi
if ! [ -x "$MT5/terminal64.exe" ]; then
  FIRST_RUN=1
  log_info "Installing MT5..."
  mkdir -p "$MT5"
  tar xzvf /app/mt5.tar.gz -C "$MT5" >&2 >/dev/null
fi
if ! [ -f "$WIN_ROOT/mt5server.exe" ] && [ -f /app/mt5server.exe ]; then
  cp -f /app/mt5server.exe "$WIN_ROOT/mt5server.exe"
fi
cleanup() { rm -f /tmp/.X0-lock /tmp/.X99-lock; rm -rf /usr/lib/wine/i386-windows; }
trap cleanup EXIT
SETUPEOF

sed -i 's/test \$FIRST_RUN || return/test -n "$FIRST_RUN" || return 0/' /app/src/config.sh 2>/dev/null || true

cat >/app/src/main.sh << 'MAINEOF'
#!/bin/sh
set -e
. /app/src/log.sh
. /app/src/env.sh
. /app/src/setup.sh
. /app/src/xvfb.sh
. /app/src/vnc.sh
. /app/src/automation.sh
. /app/src/config.sh
. /app/src/mt5.sh
cleanup
start_xvfb
start_vnc
start_websockify
start_novnc_http
init_wine
server_search_automation &
update_manager_automation &
apply_mt5_config || true
echo "[main] starting MT5..."
start_mt5
echo "[main] MT5 up PID=$MT5_PID"
wait_for_mt5_and_type_server &
start_rpyc_server
start_watchdog
echo ""
echo "All services started. MT5 PID=$MT5_PID RPyC=$RPYC_PID"
echo "Access http://localhost:$NOVNC_PAGE_PORT"
echo "LOGIN=${MT5_LOGIN:-?} SERVER=${MT5_SERVER:-?}"
while true; do
  wait $MT5_PID || true
  log_info "MT5 exited; relaunching in 3s"
  sleep 3
  rm -f "$WIN_ROOT/server" /tmp/.X0-lock 2>/dev/null || true
  mkfifo -m 666 "$WIN_ROOT/server" 2>/dev/null || true
  start_mt5
  if ! kill -0 $RPYC_PID 2>/dev/null; then start_rpyc_server; fi
done
MAINEOF
chmod +x /app/src/main.sh /app/src/setup.sh /app/src/mt5.sh
echo "[entrypoint] starting main"
exec /app/src/main.sh
