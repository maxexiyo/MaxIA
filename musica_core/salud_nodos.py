"""
Monitor de salud de nodos Lavalink (proceso en segundo plano, independiente de la música).

Cada INTERVALO_REVISION segundos revisa TODOS los nodos de LAVALINK_NODES_CONFIG por REST (sin tocar
los players): que respondan, que sean v4, que no estén sobrecargados, que puedan resolver una canción
(loadtracks) y, NUEVO, que de verdad puedan REPRODUCIRLA unos segundos (prueba de reproducción por
websocket, con un player de prueba que se borra al terminar). El resultado se guarda en
nodos.ESTADO_NODOS y el código de música solo rota entre los nodos marcados como sanos.

- Nodo sano y no conectado -> se conecta.
- Nodo enfermo con players encima -> NO se toca (la música sigue); simplemente no se le asignan cosas nuevas.
- Nodo enfermo sin players -> se desconecta del Pool (así wavelink deja de reintentar y de llenar la consola).

Ritmo de las revisiones (adaptativo):
- Hay nodos sanos -> cada INTERVALO_REVISION (5 min).
- NO hay ningún nodo sano -> revisa cada pocos segundos (INTERVALOS_SIN_NODOS: 15, 30, 60 s, subiendo poco a poco para no
  martillar a los nodos públicos) hasta que aparezca uno; en cuanto hay uno vuelve al ritmo normal.
- Un nodo falló una vez pero aún cuenta como sano (hacen falta FALLAS_PARA_DESCARTAR) -> reconfirma en INTERVALO_CONFIRMAR s,
  en vez de esperar 5 min con un nodo muerto marcado como bueno.
- forzar_revision() despierta el monitor ya (con un mínimo de MIN_ENTRE_REVISIONES s entre pasadas): úsalo si el código de
  música/radio se queda sin nodos.
"""
import asyncio
import logging
import time
from urllib.parse import quote

import aiohttp
import wavelink

from musica_core import nodos as N

logger = logging.getLogger(__name__)

INTERVALO_REVISION = 300      # seg. entre revisiones (5 min) cuando todo va bien
INTERVALOS_SIN_NODOS = (15, 30, 60)   # seg. entre revisiones si NO hay ningún nodo sano (sube por escalones hasta el último)
INTERVALO_CONFIRMAR = 20      # seg. para reconfirmar un nodo que falló una vez pero todavía cuenta como sano
MIN_ENTRE_REVISIONES = 10     # seg. mínimos entre dos pasadas (aunque alguien llame a forzar_revision())
PRIMERA_REVISION_DELAY = 1    # seg. de espera antes de la primera (arranque)
TIMEOUT_BASICO = 5            # seg. para /version y /stats
TIMEOUT_BUSQUEDA = 10         # seg. para la búsqueda de prueba
FALLAS_PARA_DESCARTAR = 2     # fallas seguidas para marcar un nodo como enfermo (1 éxito basta para sanarlo)
CARGA_MAXIMA = 0.92           # systemLoad por encima de esto = nodo saturado
DEFICIT_MAXIMO = 600          # déficit de frames (por minuto) a partir del cual el audio se entrecorta
QUERY_PRUEBA = "ytmsearch:rick astley never gonna give you up"

# Canción que en los nodos malos da "This video requires login". Cámbiala por otra restringida si quieres
# (puede ser "ytsearch:..." o "ytmsearch:..."). Pon "" para desactivar esta segunda prueba.
PRUEBA_RESTRINGIDA = ""

REPRODUCCION_PRUEBA = 6      # seg. que debe sonar la canción de prueba sin fallar
GUILD_PRUEBA = "1"            # id falso de servidor para el player de prueba (no existe en Discord)

_tarea = None
_fallas = {}                  # idx -> fallas seguidas
_ultimo_resumen = None
_despertar = None             # asyncio.Event: despierta al monitor antes de que se acabe su espera
_ultima_pasada = 0.0          # time.monotonic() de la última revisión


def _uri_base(cfg: dict) -> str:
    return f"{'https' if cfg['secure'] else 'http'}://{cfg['host']}:{cfg['port']}"


async def _probar_reproduccion(sesion: aiohttp.ClientSession, cfg: dict, encoded: str) -> tuple:
    """Reproduce `encoded` en un player de prueba durante REPRODUCCION_PRUEBA seg. y devuelve (ok, motivo).
    Falla si Lavalink manda TrackException / TrackStuck / TrackEnd(loadFailed) o si la pista nunca arranca
    (justo el caso 'This video requires login' que antes solo se descubría con usuarios reales)."""
    base = _uri_base(cfg)
    ws_url = base.replace("http", "ws", 1) + "/v4/websocket"      # http->ws, https->wss
    headers = {"Authorization": cfg["password"], "User-Id": "1", "Client-Name": "salud-nodos/1.0"}
    session_id = None
    try:
        async with sesion.ws_connect(ws_url, headers=headers, timeout=TIMEOUT_BASICO, heartbeat=None) as ws:
            # 1) esperar el "ready" con el sessionId
            limite = time.monotonic() + TIMEOUT_BASICO
            while session_id is None:
                msg = await ws.receive(timeout=max(0.1, limite - time.monotonic()))
                if msg.type != aiohttp.WSMsgType.TEXT:
                    return False, "websocket cerrado antes de 'ready'"
                data = msg.json()
                if data.get("op") == "ready":
                    session_id = data.get("sessionId")

            # 2) mandar la pista al player de prueba
            url = f"{base}/v4/sessions/{session_id}/players/{GUILD_PRUEBA}?noReplace=false"
            async with sesion.patch(url, headers=headers, json={"track": {"encoded": encoded}, "volume": 100},
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT_BASICO)) as r:
                if r.status != 200:
                    return False, f"play HTTP {r.status}"

            # 3) vigilar los eventos durante REPRODUCCION_PRUEBA segundos
            arranco = False
            fin = time.monotonic() + REPRODUCCION_PRUEBA
            while True:
                restante = fin - time.monotonic()
                if restante <= 0:
                    break
                try:
                    msg = await ws.receive(timeout=restante)
                except asyncio.TimeoutError:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    return False, "websocket se cerró durante la reproducción"
                data = msg.json()
                if data.get("op") != "event":
                    continue
                tipo = data.get("type")
                if tipo == "TrackStartEvent":
                    arranco = True
                elif tipo == "TrackExceptionEvent":
                    err = (data.get("exception") or {})
                    texto = (err.get("cause") or err.get("message") or "error").replace("\n", " ")
                    return False, f"falla al reproducir: {texto[:70]}"
                elif tipo == "TrackStuckEvent":
                    return False, "pista atascada"
                elif tipo == "TrackEndEvent" and data.get("reason") in ("loadFailed", "cleanup"):
                    return False, f"terminó sola ({data.get('reason')})"

            if not arranco:
                return False, "la pista nunca arrancó"
            return True, "ok"
    except asyncio.TimeoutError:
        return False, "timeout en prueba de reproducción"
    except Exception as e:
        return False, f"prueba de reproducción: {type(e).__name__}"
    finally:
        if session_id:      # borrar el player de prueba para no dejar basura en el nodo
            try:
                async with sesion.delete(f"{base}/v4/sessions/{session_id}/players/{GUILD_PRUEBA}", headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=TIMEOUT_BASICO)):
                    pass
            except Exception:
                pass


async def _probar_nodo(sesion: aiohttp.ClientSession, cfg: dict, m: dict = None) -> tuple:
    """Devuelve (sano: bool, motivo: str). Nunca lanza excepciones. Si se pasa 'm', se llena con métricas
    (latencia, cpu, players, deficit) para ordenar los nodos del mejor al peor."""
    m = m if m is not None else {}
    base = _uri_base(cfg)
    headers = {"Authorization": cfg["password"]}
    try:
        t0 = time.monotonic()
        async with sesion.get(f"{base}/version", headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT_BASICO)) as r:
            if r.status != 200:
                return False, f"/version HTTP {r.status}"
            version = (await r.text()).strip()
        if not version.startswith("4"):
            return False, f"versión no compatible ({version[:20]})"

        async with sesion.get(f"{base}/v4/stats", headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT_BASICO)) as r:
            if r.status == 200:
                st = await r.json(content_type=None)
                carga = (st.get("cpu") or {}).get("systemLoad", 0) or 0
                m["cpu"] = carga
                m["players"] = st.get("players", 0) or 0
                if carga > CARGA_MAXIMA:
                    return False, f"saturado (cpu {carga:.0%})"
                fs = st.get("frameStats") or {}
                m["deficit"] = abs(fs.get("deficit", 0) or 0)
                if abs(fs.get("deficit", 0) or 0) > DEFICIT_MAXIMO:
                    return False, f"audio entrecortado (déficit {fs.get('deficit')})"

        t_busq = time.monotonic()
        async with sesion.get(f"{base}/v4/loadtracks?identifier={quote(QUERY_PRUEBA, safe='')}", headers=headers,
                              timeout=aiohttp.ClientTimeout(total=TIMEOUT_BUSQUEDA)) as r:
            if r.status != 200:
                return False, f"loadtracks HTTP {r.status}"
            data = await r.json(content_type=None)
        m["latencia"] = time.monotonic() - t_busq
        tipo = data.get("loadType")
        if tipo == "error":
            return False, f"no puede cargar YouTube: {(data.get('data') or {}).get('message', '')[:60]}"
        if tipo not in ("search", "track", "playlist") or not data.get("data"):
            return False, f"búsqueda de prueba sin resultados ({tipo})"

        # ── NUEVO: no basta con que BUSQUE; tiene que poder REPRODUCIR unos segundos ──
        d = data["data"]
        if tipo == "search":
            pista = d[0]
        elif tipo == "playlist":
            pista = (d.get("tracks") or [None])[0]
        else:
            pista = d
        encoded = (pista or {}).get("encoded")
        if not encoded:
            return False, "búsqueda de prueba sin pista reproducible"

        ok, motivo = await _probar_reproduccion(sesion, cfg, encoded)
        if not ok:
            return False, motivo

        # ── Segunda prueba: canción que pide inicio de sesión / restringida (la que suele dar problemas) ──
        if PRUEBA_RESTRINGIDA:
            async with sesion.get(f"{base}/v4/loadtracks?identifier={quote(PRUEBA_RESTRINGIDA, safe='')}",
                                  headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT_BUSQUEDA)) as r:
                d2 = (await r.json(content_type=None)) if r.status == 200 else {}
            lista = d2.get("data") if d2.get("loadType") == "search" else None
            if not lista:
                return False, f"canción restringida no cargó ({d2.get('loadType')})"
            ok, motivo = await _probar_reproduccion(sesion, cfg, lista[0]["encoded"])
            if not ok:
                return False, f"restringida: {motivo}"
        return True, f"ok ({time.monotonic() - t0:.1f}s)"
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as e:
        return False, f"{type(e).__name__}"


async def _sincronizar_pool(bot, idx: int, cfg: dict, sano: bool):
    """Conecta/desconecta el nodo del Pool según su estado (sin tocar nodos con players)."""
    ident = f"node{idx}"
    node = wavelink.Pool.nodes.get(ident)

    if sano:
        if node is not None and node.status == wavelink.NodeStatus.CONNECTED:
            return
        if node is not None:
            if node.players:
                return                      # wavelink sigue intentando reconectarlo; no lo piso
            try:
                await node.close(eject=True)
            except Exception:
                pass
        nuevo = wavelink.Node(identifier=ident, uri=_uri_base(cfg), password=cfg["password"], retries=3)
        try:
            await wavelink.Pool.connect(nodes=[nuevo], client=bot)
        except Exception as e:
            logger.debug(f"No se pudo conectar el nodo {ident}: {type(e).__name__}: {e}")
    else:
        if node is not None and not node.players:
            try:
                await node.close(eject=True)
            except Exception as e:
                logger.debug(f"No se pudo cerrar el nodo enfermo {ident}: {e}")


async def revisar_nodos(bot):
    """Una pasada completa: prueba todos los nodos en paralelo, actualiza ESTADO_NODOS y el Pool."""
    global _ultimo_resumen
    async with aiohttp.ClientSession() as sesion:
        metricas = [dict() for _ in N.LAVALINK_NODES_CONFIG]
        resultados = await asyncio.gather(*(_probar_nodo(sesion, c, metricas[i]) for i, c in enumerate(N.LAVALINK_NODES_CONFIG)))

    sanos = set()
    pendientes = False        # algún nodo falló pero aún cuenta como sano: conviene reconfirmar pronto
    for idx, (ok, motivo) in enumerate(resultados):
        if ok:
            _fallas[idx] = 0
        else:
            _fallas[idx] = _fallas.get(idx, 0) + 1
        previo = N.ESTADO_NODOS.get(idx, {}).get("sano")
        # Primera revisión: se confía en el resultado directo. Después: hacen falta varias fallas seguidas
        sano = ok or (previo is True and _fallas[idx] < FALLAS_PARA_DESCARTAR)
        tolerado = (not ok) and sano
        m = metricas[idx]
        # Puntaje: menor = mejor (rápido en responder, poca carga, sin déficit de frames)
        puntaje = m.get("latencia", 5) + m.get("cpu", 0.5) * 3 + m.get("players", 0) * 0.03 + m.get("deficit", 0) / 500
        castigo = N.ESTADO_NODOS.get(idx, {}).get("castigo_hasta", 0)
        if castigo > time.time():            # lo castigaron por lag / fallas reproduciendo: se respeta el castigo
            sano, motivo, puntaje = False, "castigado (lag o fallas reproduciendo)", puntaje + 5
            tolerado = False
            sanos.discard(idx)
        N.ESTADO_NODOS[idx] = {"sano": sano, "motivo": motivo, "revisado": time.time(), "puntaje": puntaje,
                               **({"castigo_hasta": castigo} if castigo > time.time() else {})}
        if sano:
            sanos.add(idx)
        pendientes = pendientes or tolerado

    await asyncio.gather(*(
        _sincronizar_pool(bot, i, c, i in sanos) for i, c in enumerate(N.LAVALINK_NODES_CONFIG)
    ), return_exceptions=True)

    resumen = frozenset(sanos)
    if resumen != _ultimo_resumen:       # solo se loguea cuando cambia algo
        _ultimo_resumen = resumen
        logger.info(f"🎵 Nodos de música sanos: {len(sanos)}/{len(N.LAVALINK_NODES_CONFIG)}")
    for idx in range(len(N.LAVALINK_NODES_CONFIG)):
        e = N.ESTADO_NODOS[idx]
        logger.debug(f"  node{idx} {N.LAVALINK_NODES_CONFIG[idx]['host']}:{N.LAVALINK_NODES_CONFIG[idx]['port']} -> "
                     f"{'OK' if e['sano'] else 'X'} {e['motivo']}")
    return len(sanos), pendientes


def _espera_siguiente(n_sanos: int, pendientes: bool, vacias: int) -> float:
    """Cuánto esperar hasta la próxima revisión según cómo salió esta (pura: se prueba sin red)."""
    if n_sanos == 0:
        return INTERVALOS_SIN_NODOS[min(vacias, len(INTERVALOS_SIN_NODOS) - 1)]
    if pendientes:
        return INTERVALO_CONFIRMAR
    return INTERVALO_REVISION


def forzar_revision() -> bool:
    """Despierta al monitor para que revise YA (p. ej. cuando la radio/música se queda sin nodos). Respeta
    MIN_ENTRE_REVISIONES para no martillar a los nodos. Devuelve True si pidió la revisión."""
    if _despertar is None or time.monotonic() - _ultima_pasada < MIN_ENTRE_REVISIONES:
        return False
    _despertar.set()
    return True


async def _bucle(bot):
    global _despertar, _ultima_pasada
    _despertar = asyncio.Event()
    await bot.wait_until_ready()
    await asyncio.sleep(PRIMERA_REVISION_DELAY)
    vacias = 0                       # pasadas seguidas sin ningún nodo sano
    while True:
        n_sanos, pendientes = 1, False
        try:
            n_sanos, pendientes = await revisar_nodos(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en la revisión de nodos de música")
            n_sanos = 0
        _ultima_pasada = time.monotonic()
        espera = _espera_siguiente(n_sanos, pendientes, vacias)
        vacias = vacias + 1 if n_sanos == 0 else 0
        if n_sanos == 0:
            logger.info(f"🎵 Ningún nodo de música disponible: reviso de nuevo en {espera:.0f}s")
        _despertar.clear()
        try:
            await asyncio.wait_for(_despertar.wait(), timeout=espera)
        except asyncio.TimeoutError:
            pass


def iniciar_monitor(bot):
    """Arranca el monitor en segundo plano (una sola vez)."""
    global _tarea
    if _tarea is None or _tarea.done():
        _tarea = asyncio.create_task(_bucle(bot))
    return _tarea


def detener_monitor():
    global _tarea, _despertar
    _despertar = None
    if _tarea and not _tarea.done():
        _tarea.cancel()
    _tarea = None