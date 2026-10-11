#!/usr/bin/env bash
# Mantiene el bot vivo: si se cae, o si vigilante.py lo cierra por quedarse congelado, lo vuelve a arrancar.
# Uso:  bash run_bot.sh            (cambia ARCHIVO por el que arranca tu bot)
ARCHIVO="main.py"

cd "$(dirname "$0")" || exit 1
termux-wake-lock 2>/dev/null          # solo existe en Termux; en otro lado se ignora

while true; do
  inicio=$(date +%s)
  python3 "$ARCHIVO" 2>&1 | tee -a bot.log
  codigo=${PIPESTATUS[0]}
  duro=$(( $(date +%s) - inicio ))
  echo "[$(date '+%F %T')] el bot terminó (código $codigo, estuvo vivo ${duro}s)." | tee -a bot.log
  if [ "$duro" -lt 20 ]; then           # se cae nada más arrancar: no entrar en un ciclo frenético
    echo "[$(date '+%F %T')] murió muy rápido; espero 30 s antes de reintentar." | tee -a bot.log
    sleep 30
  else
    sleep 5
  fi
done
