"""vigilante.py — evita que un bloqueo (p. ej. un nodo de Lavalink que se cuelga) o una caída de red deje TODO el bot sin responder.

Qué vigila (cada cosa tiene su propio código de salida, para saber por qué se reinició)
  3  LOOP CONGELADO: una tarea del bot marca "sigo vivo" cada segundo. Un hilo aparte (que no depende del loop) avisa a los
     AVISO_S segundos escribiendo EXACTAMENTE qué línea está ejecutando el bot, y a los LIMITE_S guarda un volcado y cierra.
  4  DISCORD (gateway): bot no listo / latencia inválida más de GATEWAY_S segundos.
  5  RED/DNS: cada RED_PERIODO_S prueba DNS + internet + HTTP a Discord. Si falla más de RED_S segundos, reinicia
     (como máximo una vez cada RED_ENFRIAMIENTO_S, para no reiniciar en bucle si el internet del teléfono sigue caído).
     Distingue la causa en el log: "dns", "internet" (sin salida), "discord" (HTTP falla), y NO reinicia por 429/403/5xx.
  6  BOT CERRADO: bot.is_closed() pero el proceso sigue vivo (zombi).
  7  POOL DE HILOS SATURADO: run_in_executor ya no responde (DNS/getaddrinfo, to_thread y demás se quedan en fila).
     Este fue justo el tipo de falla que deja todo en "TimeoutError" aunque haya internet.
  8  GATEWAY SIN EVENTOS (opcional, VIGILANTE_SIN_EVENTOS_S>0): conexión "viva" pero Discord no manda ningún evento.
  9  DEMASIADAS TAREAS asyncio (fuga de tareas).
  10 MEMORIA (opcional, VIGILANTE_MEM_MB>0): RSS por encima del límite (Android mata Termux si se pasa).
  11 ARCHIVOS ABIERTOS: descriptores de archivo/sockets cerca del límite del sistema (fuga de conexiones).
  Además, solo AVISA (no reinicia): disco casi lleno, pulsos de otras tareas que dejaron de latir (ver `pulso()`),
  y un resumen de salud cada VIGILANTE_RESUMEN_S segundos.
  El cierre solo sirve si algo vuelve a arrancar el bot: usa run_bot.sh (o pm2/systemd). Cada reinicio queda anotado en
  vigilante_reinicios.log y al arrancar el bot muestra el motivo del último.

Diagnóstico de algo que se queda "pensando" (el bot responde, pero un comando/proceso no termina):
    pkill -USR1 -f "python3 main.py"     -> el log muestra qué está esperando cada tarea
    pkill -USR2 -f "python3 main.py"     -> escribe en vigilante_volcado.txt la pila de TODOS los hilos
    (o el comando !max diagnostico, solo para el dueño, si lo agregaste a main.py)

Uso (una sola vez, dentro del loop; es idempotente):
    import vigilante
    ...
    async def setup_hook(self):          # o en on_ready
        await vigilante.iniciar(self)

Opcional: en cualquier cog con una tarea de fondo importante (radio, moderación...), llama de vez en cuando:
    vigilante.pulso("radio", 120)        # si pasan 120 s sin llamarlo, el vigilante avisa en el log

Variables de entorno (todas opcionales):
  VIGILANTE_AVISO_S=8          a los cuántos segundos congelado avisa y muestra dónde está trabado
  VIGILANTE_LIMITE_S=60        a los cuántos segundos congelado cierra el proceso (para que lo reinicien)
  VIGILANTE_GATEWAY_S=180      segundos sin conexión válida con Discord antes de reiniciar (0 = no vigilar)
  VIGILANTE_RED_S=120          segundos sin red/DNS/Discord antes de reiniciar (0 = no vigilar la red)
  VIGILANTE_RED_PERIODO_S=20   cada cuántos segundos se prueba la red
  VIGILANTE_RED_ENFRIAMIENTO_S=600  mínimo de vida del proceso para poder reiniciar por red
  VIGILANTE_HILOS_S=30         segundos sin que el pool de hilos responda antes de reiniciar (0 = no vigilar)
  VIGILANTE_SIN_EVENTOS_S=0    segundos sin ningún evento del gateway antes de reiniciar (0 = apagado; en servers muy
                               tranquilos puede dar falsos positivos, usa 600 o más)
  VIGILANTE_TAREAS_MAX=3000    máximo de tareas asyncio vivas (0 = no vigilar)
  VIGILANTE_MEM_MB=0           RSS máximo en MB (0 = no vigilar)
  VIGILANTE_FDS_PCT=0.85       fracción del límite de archivos abiertos que dispara el reinicio (0 = no vigilar)
  VIGILANTE_DISCO_MB=200       avisa si queda menos disco libre que esto (0 = no vigilar)
  VIGILANTE_RESUMEN_S=600      cada cuántos segundos escribe un resumen de salud (0 = no)
  VIGILANTE_REINICIAR=0        solo avisar y volcar, sin cerrar el proceso
  VIGILANTE_VOLCADO=ruta       archivo del volcado (por defecto vigilante_volcado.txt)
  VIGILANTE_REINICIOS=ruta     archivo con el historial de reinicios (por defecto vigilante_reinicios.log)
  VIGILANTE_DEBUG_LOOP=1       DIAGNÓSTICO: asyncio avisa de cada llamada que bloquee más de 1 s (consume más CPU)
"""
import asyncio
import faulthandler
import logging
import os
import shutil
import socket
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)


def _num(nombre: str, defecto: float) -> float:
    try:
        return float(os.getenv(nombre, str(defecto)))
    except ValueError:
        return defecto


AVISO_S = _num("VIGILANTE_AVISO_S", 8)
LIMITE_S = _num("VIGILANTE_LIMITE_S", 60)
GATEWAY_S = _num("VIGILANTE_GATEWAY_S", 180)
RED_S = _num("VIGILANTE_RED_S", 120)
RED_PERIODO_S = max(5.0, _num("VIGILANTE_RED_PERIODO_S", 20))
RED_ENFRIAMIENTO_S = _num("VIGILANTE_RED_ENFRIAMIENTO_S", 600)
HILOS_S = _num("VIGILANTE_HILOS_S", 30)
SIN_EVENTOS_S = _num("VIGILANTE_SIN_EVENTOS_S", 0)
TAREAS_MAX = int(_num("VIGILANTE_TAREAS_MAX", 3000))
MEM_MB = _num("VIGILANTE_MEM_MB", 0)
FDS_PCT = _num("VIGILANTE_FDS_PCT", 0.85)
DISCO_MB = _num("VIGILANTE_DISCO_MB", 200)
RESUMEN_S = _num("VIGILANTE_RESUMEN_S", 600)
REINICIAR = os.getenv("VIGILANTE_REINICIAR", "1") != "0"
ARCHIVO_VOLCADO = os.getenv("VIGILANTE_VOLCADO", "vigilante_volcado.txt")
ARCHIVO_REINICIOS = os.getenv("VIGILANTE_REINICIOS", "vigilante_reinicios.log")
DEBUG_LOOP = os.getenv("VIGILANTE_DEBUG_LOOP", "0") == "1"

_inicio = time.monotonic()
_ultimo = time.monotonic()
_ultimo_evento = time.monotonic()
_hilo_loop = None
_iniciado = False
_tareas = []                  # referencias fuertes: si no, el recolector de basura puede borrar las tareas de fondo
_pulsos = {}                  # nombre -> (último latido, máximo de segundos sin latir)
_avisos = {}                  # clave -> cuándo se avisó por última vez (para no llenar el log)


# ───────────────────────────── utilidades ─────────────────────────────

def _stack_loop() -> str:
    """Pila de llamadas del hilo del bot AHORA MISMO (sirve aunque el loop esté bloqueado)."""
    marco = sys._current_frames().get(_hilo_loop)
    return "".join(traceback.format_stack(marco)) if marco else "(no pude leer el hilo del bot)"


def _cadena(coro, maximo: int = 14) -> str:
    """De qué cadena de `await` está colgada una tarea: archivo:línea función -> ... -> lo último que espera."""
    pasos = []
    while coro is not None and len(pasos) < maximo:
        marco = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None) or getattr(coro, "ag_frame", None)
        if marco is None:
            pasos.append(f"esperando {type(coro).__name__}")
            break
        pasos.append(f"{os.path.basename(marco.f_code.co_filename)}:{marco.f_lineno} {marco.f_code.co_name}")
        coro = getattr(coro, "cr_await", None) or getattr(coro, "gi_yieldfrom", None) or getattr(coro, "ag_await", None)
    return " -> ".join(pasos)


def volcar_tareas(limite: int = 80) -> str:
    """Tareas asyncio pendientes y la cadena de await de cada una (así se ve QUÉ está esperando algo que no llega).
    Debe llamarse DENTRO del loop."""
    filas = []
    for t in list(asyncio.all_tasks())[:limite]:
        try:
            filas.append(f"- {t.get_name()}: {_cadena(t.get_coro())}")
        except Exception as e:
            filas.append(f"- {t.get_name()}: (no pude leerla: {e!r})")
    return "\n".join(filas) or "(ninguna)"


def _volcar_a_archivo(motivo: str):
    try:
        with open(ARCHIVO_VOLCADO, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {motivo} =====\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
    except Exception as e:
        logger.error(f"[vigilante] no pude escribir el volcado: {e!r}")


def _registrar_reinicio(codigo: int, motivo: str):
    try:
        primera = (motivo.strip().splitlines() or ["?"])[0]
        with open(ARCHIVO_REINICIOS, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} código {codigo}: {primera}\n")
    except Exception:
        pass


def _ultimo_reinicio() -> str:
    try:
        with open(ARCHIVO_REINICIOS, "r", encoding="utf-8") as f:
            lineas = [l.strip() for l in f.readlines() if l.strip()]
        return lineas[-1] if lineas else ""
    except Exception:
        return ""


def _cerrar(codigo: int, motivo: str):
    _volcar_a_archivo(motivo)
    if REINICIAR:
        logger.critical(f"[vigilante] cerrando el proceso (código {codigo}: {(motivo.splitlines() or ['?'])[0]}) "
                        f"para que se reinicie. Volcado en {ARCHIVO_VOLCADO}")
        _registrar_reinicio(codigo, motivo)
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        os._exit(codigo)      # a propósito: un proceso congelado no puede cerrarse "bonito"
    else:
        logger.error(f"[vigilante] (código {codigo}) {(motivo.splitlines() or ['?'])[0]} — VIGILANTE_REINICIAR=0, no cierro el proceso")


def _avisar(clave: str, cada_s: float, nivel: int, mensaje: str):
    """Escribe en el log pero no más de una vez cada `cada_s` segundos por clave."""
    ahora = time.monotonic()
    if ahora - _avisos.get(clave, -1e9) >= cada_s:
        _avisos[clave] = ahora
        logger.log(nivel, mensaje)


def pulso(nombre: str, maximo_s: float = 300):
    """Las tareas de fondo importantes (radio, moderación...) llaman esto de vez en cuando. Si dejan de llamarlo más de
    `maximo_s` segundos, el vigilante lo avisa en el log (no reinicia). Seguro de llamar desde cualquier hilo."""
    _pulsos[nombre] = (time.monotonic(), float(maximo_s))


async def _on_socket_event_type(_evento):
    """Listener de discord.py: se llama por cada evento que manda el gateway de Discord."""
    global _ultimo_evento
    _ultimo_evento = time.monotonic()


# ───────────────────────────── lecturas del sistema ─────────────────────────────

def _rss_mb():
    try:
        with open("/proc/self/status", "r") as f:
            for linea in f:
                if linea.startswith("VmRSS:"):
                    return int(linea.split()[1]) / 1024
    except Exception:
        pass
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss          # KB en Linux (es el pico, no el actual)
        return r / 1024
    except Exception:
        return None


def _fds():
    """(abiertos, límite) o (None, None) si no se puede leer."""
    try:
        import resource
        limite = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        abiertos = len(os.listdir("/proc/self/fd"))
        if limite in (-1, resource.RLIM_INFINITY):
            return abiertos, None
        return abiertos, limite
    except Exception:
        return None, None


# ───────────────────────────── 1) hilo vigilante (loop congelado) ─────────────────────────────

def _vigilar():
    avisado = volcado = False
    while True:
        time.sleep(2)
        parado = time.monotonic() - _ultimo
        if parado <= AVISO_S:
            avisado = volcado = False
            continue
        if not avisado:
            avisado = True
            logger.error(f"[vigilante] el bot lleva {parado:.0f}s sin responder. Está ejecutando esto AHORA:\n{_stack_loop()}")
        if parado > LIMITE_S and not volcado:
            volcado = True
            logger.critical(f"[vigilante] el bot lleva {parado:.0f}s congelado")
            _cerrar(3, f"bot congelado {parado:.0f}s")


# ───────────────────────────── 2) latido + Discord (gateway) ─────────────────────────────

async def _latido(bot):
    global _ultimo
    sin_discord = 0.0
    cerrado = 0.0
    visto_listo = False
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(1)
        ahora = time.monotonic()
        _ultimo = ahora
        dt = ahora - t0
        retraso = dt - 1
        if retraso > 2:
            logger.warning(f"[vigilante] el bot se trabó {retraso:.1f}s y ya volvió")
        if bot is None:
            continue
        try:
            # bot cerrado pero el proceso sigue vivo (zombi): nadie lo va a reiniciar si no lo hacemos nosotros
            cerrado = cerrado + dt if bot.is_closed() else 0.0
            if cerrado > 30:
                _cerrar(6, f"bot cerrado (bot.is_closed()) pero el proceso sigue vivo {cerrado:.0f}s")

            if GATEWAY_S > 0:
                listo = bot.is_ready()
                lat = bot.latency
                visto_listo = visto_listo or listo
                valida = bool(listo) and lat == lat and lat < 120          # nan / inf / enorme = sin latido de Discord
                limite = GATEWAY_S if visto_listo else max(GATEWAY_S * 2, 300.0)     # más margen mientras arranca
                sin_discord = 0.0 if valida else sin_discord + dt
                if sin_discord > limite:
                    logger.critical(f"[vigilante] {sin_discord:.0f}s sin conexión válida con Discord "
                                    f"(listo={listo}, latencia={lat}). Tareas pendientes:\n{volcar_tareas()}")
                    _cerrar(4, f"sin conexión válida con Discord {sin_discord:.0f}s\n{volcar_tareas()}")
                    sin_discord = 0.0

            if SIN_EVENTOS_S > 0 and visto_listo:
                callado = ahora - _ultimo_evento
                if callado > SIN_EVENTOS_S:
                    _cerrar(8, f"el gateway no manda eventos hace {callado:.0f}s\n{volcar_tareas()}")
        except Exception as e:
            logger.debug(f"[vigilante] error revisando Discord: {e!r}")


# ───────────────────────────── 3) red / DNS ─────────────────────────────

async def _tcp(host: str, puerto: int, espera: float) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, puerto), espera)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _sondear_red():
    """Devuelve (tipo, detalle). tipo: ok | limitado | discord_5xx | dns | internet | discord."""
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.getaddrinfo("discord.com", 443, type=socket.SOCK_STREAM), 8)
        dns = True
    except Exception as e:
        dns = False
        err_dns = repr(e)
    if not dns:
        # sin DNS: ¿hay internet por IP directa? separa "DNS roto" de "no hay internet"
        if await _tcp("1.1.1.1", 443, 8):
            return "dns", (f"el DNS no resuelve ({err_dns}) pero hay internet por IP. Prueba poner "
                           f"'nameserver 1.1.1.1' en /etc/resolv.conf")
        return "internet", f"no hay salida a internet (DNS: {err_dns}). Revisa Wi-Fi/datos/VPN del teléfono"
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as s:
            async with s.get("https://discord.com/api/v10/gateway") as r:
                estado = r.status
    except Exception as e:
        return "discord", f"DNS ok pero el HTTP a Discord falla: {e!r}"
    if estado in (403, 429):
        return "limitado", f"Discord respondió {estado} (rate limit / IP bloqueada temporalmente); reiniciar no ayuda"
    if estado >= 500:
        return "discord_5xx", f"Discord respondió {estado} (¿caída de Discord?); reiniciar no ayuda"
    return "ok", ""


async def _vigilar_red():
    await asyncio.sleep(30)                     # margen de arranque
    sin_red = 0.0
    while True:
        t0 = time.monotonic()
        try:
            tipo, detalle = await _sondear_red()
        except Exception as e:
            tipo, detalle = "discord", f"error probando la red: {e!r}"
        if tipo in ("ok", "limitado", "discord_5xx"):
            if sin_red > 0:
                logger.info(f"[vigilante] la red volvió tras ~{sin_red:.0f}s")
            sin_red = 0.0
            if tipo != "ok":
                _avisar("red_" + tipo, 300, logging.WARNING, f"[vigilante] {detalle}")
        else:
            sin_red += (time.monotonic() - t0) + RED_PERIODO_S
            logger.warning(f"[vigilante] sin red ({tipo}) hace ~{sin_red:.0f}s: {detalle}")
            vida = time.monotonic() - _inicio
            if sin_red > RED_S:
                if vida >= RED_ENFRIAMIENTO_S:
                    logger.critical(f"[vigilante] {sin_red:.0f}s sin red ({tipo}). Tareas pendientes:\n{volcar_tareas()}")
                    _cerrar(5, f"sin red ({tipo}) {sin_red:.0f}s: {detalle}\n{volcar_tareas()}")
                    sin_red = 0.0
                else:
                    _avisar("red_enfria", 120, logging.WARNING,
                            f"[vigilante] sin red pero el proceso lleva solo {vida:.0f}s (< {RED_ENFRIAMIENTO_S:.0f}s): "
                            f"no reinicio todavía para no entrar en bucle")
        await asyncio.sleep(RED_PERIODO_S)


# ───────────────────────────── 4) pool de hilos ─────────────────────────────

async def _vigilar_hilos():
    loop = asyncio.get_running_loop()
    await asyncio.sleep(20)
    while True:
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(loop.run_in_executor(None, time.monotonic), HILOS_S)
            tardo = time.monotonic() - t0
            if tardo > 3:
                _avisar("hilos_lento", 60, logging.WARNING,
                        f"[vigilante] el pool de hilos tardó {tardo:.1f}s en responder (hay muchas tareas bloqueantes en fila: "
                        f"DNS, búsquedas, TTS, to_thread...)")
        except asyncio.TimeoutError:
            logger.critical(f"[vigilante] el pool de hilos no responde hace {HILOS_S:.0f}s (saturado o colgado). "
                            f"Tareas pendientes:\n{volcar_tareas()}")
            _cerrar(7, f"pool de hilos saturado {HILOS_S:.0f}s\n{volcar_tareas()}")
        except Exception as e:
            logger.debug(f"[vigilante] error probando el pool de hilos: {e!r}")
        await asyncio.sleep(10)


# ───────────────────────────── 5) recursos, pulsos y resumen ─────────────────────────────

async def _vigilar_recursos():
    await asyncio.sleep(45)
    ultimo_resumen = time.monotonic()
    while True:
        try:
            ahora = time.monotonic()
            tareas = len(asyncio.all_tasks())
            rss = _rss_mb()
            abiertos, limite_fds = _fds()

            if TAREAS_MAX > 0 and tareas > TAREAS_MAX:
                _cerrar(9, f"demasiadas tareas asyncio ({tareas} > {TAREAS_MAX})\n{volcar_tareas(200)}")
            if MEM_MB > 0 and rss is not None and rss > MEM_MB:
                _cerrar(10, f"memoria alta ({rss:.0f} MB > {MEM_MB:.0f} MB)")
            if FDS_PCT > 0 and abiertos is not None and limite_fds and abiertos / limite_fds > FDS_PCT:
                _cerrar(11, f"casi sin archivos/sockets disponibles ({abiertos}/{limite_fds}): fuga de conexiones")

            if DISCO_MB > 0:
                try:
                    libre = shutil.disk_usage(".").free / 1048576
                    if libre < DISCO_MB:
                        _avisar("disco", 1800, logging.ERROR,
                                f"[vigilante] queda poco disco ({libre:.0f} MB libres). Limpia radio_data/cache u otros temporales")
                except Exception:
                    pass

            for nombre, (visto, maximo) in list(_pulsos.items()):
                if ahora - visto > maximo:
                    _avisar("pulso_" + nombre, 300, logging.WARNING,
                            f"[vigilante] «{nombre}» no da señales de vida hace {ahora - visto:.0f}s (máximo {maximo:.0f}s): "
                            f"su tarea de fondo probablemente murió o está trabada. Usa SIGUSR1 para ver dónde")

            if RESUMEN_S > 0 and ahora - ultimo_resumen >= RESUMEN_S:
                ultimo_resumen = ahora
                logger.info(f"[vigilante] salud: vivo {(ahora - _inicio) / 60:.0f} min | tareas={tareas} | "
                            f"RAM={'?' if rss is None else f'{rss:.0f} MB'} | "
                            f"archivos={'?' if abiertos is None else abiertos}/{limite_fds or '∞'}")
        except Exception as e:
            logger.debug(f"[vigilante] error revisando recursos: {e!r}")
        await asyncio.sleep(30)


# ───────────────────────────── arranque ─────────────────────────────

async def iniciar(bot=None):
    """Arranca el latido y los vigilantes. Llamar una sola vez desde dentro del loop del bot."""
    global _hilo_loop, _iniciado, _ultimo, _ultimo_evento
    if _iniciado:
        return
    _iniciado = True
    loop = asyncio.get_running_loop()
    _hilo_loop = threading.get_ident()
    _ultimo = _ultimo_evento = time.monotonic()
    if DEBUG_LOOP:
        loop.set_debug(True)
        loop.slow_callback_duration = 1.0
    try:        # si Python muere de golpe (error fatal de C), deja la pila en el log en vez de morir en silencio
        faulthandler.enable(all_threads=True)
    except Exception:
        pass

    _tareas.append(asyncio.ensure_future(_latido(bot)))
    if RED_S > 0:
        _tareas.append(asyncio.ensure_future(_vigilar_red()))
    if HILOS_S > 0:
        _tareas.append(asyncio.ensure_future(_vigilar_hilos()))
    _tareas.append(asyncio.ensure_future(_vigilar_recursos()))
    if bot is not None and SIN_EVENTOS_S > 0:
        try:
            bot.add_listener(_on_socket_event_type, "on_socket_event_type")
        except Exception as e:
            logger.warning(f"[vigilante] no pude registrar el listener de eventos del gateway: {e!r}")

    try:
        import signal
        # `pkill -USR1 -f "python3 main.py"` escribe en el log qué está esperando cada tarea (sin reiniciar nada)
        loop.add_signal_handler(signal.SIGUSR1, lambda: logger.warning(f"[vigilante] tareas pendientes:\n{volcar_tareas()}"))
        # `pkill -USR2 ...` guarda la pila de todos los hilos en el archivo de volcado
        loop.add_signal_handler(signal.SIGUSR2, lambda: _volcar_a_archivo("volcado manual (SIGUSR2)"))
    except Exception:
        pass
    threading.Thread(target=_vigilar, name="vigilante", daemon=True).start()

    logger.info(f"[vigilante] activo: aviso a {AVISO_S:.0f}s, reinicio a {LIMITE_S:.0f}s, discord {GATEWAY_S:.0f}s, "
                f"red {RED_S:.0f}s, hilos {HILOS_S:.0f}s, eventos {SIN_EVENTOS_S:.0f}s, tareas>{TAREAS_MAX}, "
                f"RAM>{MEM_MB:.0f}MB, reiniciar={'sí' if REINICIAR else 'no'}")
    previo = _ultimo_reinicio()
    if previo:
        logger.info(f"[vigilante] último reinicio provocado por el vigilante: {previo}")