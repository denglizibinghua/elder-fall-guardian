#!/bin/sh
# Cleanly restart food-web.service: kill every process holding :8080
# (by port, so we never match our own command line) then let systemd start one.
DIR=/home/admin/food_project
LOG=$DIR/webapp.log

systemctl --user stop food-web.service 2>/dev/null
sleep 1

# kill by port owner, repeatedly, until the port is free
i=0
while [ $i -lt 12 ]; do
  PIDS=$(ss -ltnpH "sport = :8080" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u)
  [ -z "$PIDS" ] && break
  for p in $PIDS; do kill -9 "$p" 2>/dev/null; done
  sleep 1
  i=$((i+1))
done

if ss -ltnH "sport = :8080" 2>/dev/null | grep -q .; then
  echo "PORT STILL BUSY - abort"
  ss -ltnpH "sport = :8080"
  exit 1
fi
echo "PORT FREE"

systemctl --user start food-web.service
sleep 5
echo "active=$(systemctl --user is-active food-web.service)"
echo "MainPID=$(systemctl --user show food-web.service -p MainPID --value)"
echo "HTTP=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/)"
ss -ltnpH "sport = :8080"
