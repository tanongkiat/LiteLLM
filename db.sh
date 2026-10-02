#!/usr/bin/env bash
# Manual control for the Postgres container. NOTHING auto-starts:
# restart policy is "no" — after a reboot or Docker restart, run this.
#   ./db.sh start | stop | restart | status | logs
set -e
cd "$(dirname "$0")"

case "${1:-status}" in
  start)
    # first-ever run needs `up` (creates the container); afterwards `start` suffices
    docker compose ps -q db | grep -q . && docker compose start db || docker compose up -d db
    ;;
  stop)    docker compose stop db ;;
  restart) docker compose restart db ;;
  status)  docker compose ps db ;;
  logs)    docker compose logs -f db ;;
  *) echo "usage: $0 {start|stop|restart|status|logs}"; exit 1 ;;
esac
