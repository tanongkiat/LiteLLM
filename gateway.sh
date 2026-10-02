#!/usr/bin/env bash
# Manual control for the LiteLLM gateway container (UI at http://localhost:4242/ui).
# Starting the gateway also brings up its Postgres dependency (health-gated).
#   ./gateway.sh start | stop | restart | status | logs
set -e
cd "$(dirname "$0")"

case "${1:-status}" in
  start)
    docker compose ps -q gateway | grep -q . && docker compose start gateway \
      || docker compose up -d gateway
    ;;
  stop)    docker compose stop gateway ;;
  restart) docker compose restart gateway ;;
  status)  docker compose ps gateway ;;
  logs)    docker compose logs -f gateway ;;
  *) echo "usage: $0 {start|stop|restart|status|logs}"; exit 1 ;;
esac
