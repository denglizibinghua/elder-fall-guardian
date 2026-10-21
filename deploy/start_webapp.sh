#!/bin/sh
# Start food_project web app (app.py) on port 8080.
# Safe to re-run: kills old instance by PID from pidfile, never self-matches.
DIR=/home/admin/food_project
PIDFILE=$DIR/webapp.pid
LOG=$DIR/webapp.log

if [ -f "$PIDFILE" ]; then
  OLD=$(cat "$PIDFILE")
  if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
    kill "$OLD" 2>/dev/null
    sleep 2
    kill -9 "$OLD" 2>/dev/null
  fi
  rm -f "$PIDFILE"
fi

cd "$DIR" || exit 1
setsid nohup /usr/bin/python3 app.py >> "$LOG" 2>&1 < /dev/null &
NEW=$!
echo "$NEW" > "$PIDFILE"
sleep 3
curl -s -o /dev/null -w 'HTTP:%{http_code}\n' http://127.0.0.1:8080/
echo "pid=$NEW"
