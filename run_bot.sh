#!/bin/bash
cd /data/data/com.termux/files/home/storage/downloads/maxia
while true; do
    python3 main.py
    code=$?
    echo "[run_bot] salió con código $code ($(date))"
    # 0 = apagado a propósito, 130 = Ctrl+C: no reiniciar
    if [ $code -eq 0 ] || [ $code -eq 130 ]; then break; fi
    sleep 5
done
