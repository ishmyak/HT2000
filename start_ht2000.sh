#!/usr/bin/env bash
# HT2000 Monitor: запуск/остановка в фоне.
# Использование:
#   ./start_ht2000.sh [start|stop|restart|status]   (по умолчанию start)
#
# Лог: monitor.log в каталоге проекта. Повторный start не создаёт
# второй экземпляр. Требуется X-дисплей (DISPLAY).

set -u

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="ht2000_monitor.py"
LOG="$PROJ/monitor.log"

pidof_ht() {
    pgrep -of "[h]t2000_monitor[.]py([[:space:]]|$)"
}

do_start() {
    if sPID="$(pidof_ht)"; then
        echo "HT2000 уже запущен (PID=$sPID). Лог: $LOG"
        return 0
    fi
    cd "$PROJ" || return 1
    nohup python3 "$NAME" > "$LOG" 2>&1 &
    nPID=$!
    sleep 1
    if kill -0 "$nPID" 2>/dev/null; then
        echo "HT2000 запущен в фоне (PID=$nPID). Лог: $LOG"
    else
        echo "Ошибка: процесс завершился сразу. Смотрите лог: $LOG"
        return 1
    fi
}

do_stop() {
    if sPID="$(pidof_ht)"; then
        kill "$sPID" || return 1
        for _ in 1 2 3 4 5; do
            sPID="$(pidof_ht)" || break
            sleep 1
        done
        if sPID="$(pidof_ht)"; then
            echo "HT2000 (PID=$sPID) не завершился за 5 c — принудительно."
            kill -9 "$sPID" || return 1
        else
            echo "HT2000 остановлен."
        fi
    else
        echo "HT2000 не запущен."
    fi
}

do_status() {
    if sPID="$(pidof_ht)"; then
        echo "HT2000 запущен (PID=$sPID). Лог: $LOG"
    else
        echo "HT2000 не запущен."
        return 1
    fi
}

case "${1:-start}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop && do_start ;;
    status)  do_status ;;
    *) echo "Использование: $0 [start|stop|restart|status]"; exit 2 ;;
esac