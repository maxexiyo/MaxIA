import asyncio
import logging
import re
import time
import wavelink

from musica_core.player import MusicPlayer

logger = logging.getLogger(__name__)

# ✅ Nodos públicos Lavalink v4 (revisados en lavalink.darrennathanael.com, AjieDev/lavalink-list,
# triniumhost y heavencloud). Se quitaron los que ya no aparecen en ninguna lista (topjav, paniek,
# lexnet, freelavalink.ga) y se corrigieron hosts/passwords que habían cambiado. No importa que alguno
# esté caído: el bot los intenta todos en paralelo y usa los que respondan.
LAVALINK_NODES_CONFIG = [
    # ── Con SSL ──
    # {"host": "lavalink.nazha.online", "port": 443, "password": "nazhafreelava", "secure": True},  # DESCARTADO: era el que se atoraba/lagueaba
    {"host": "lavalink-v4.triniumhost.com", "port": 443, "password": "free", "secure": True},
    {"host": "lavalink-v4.triniumhost.indevs.in", "port": 443, "password": "free", "secure": True},
    {"host": "lavalinkv4.serenetia.com", "port": 443, "password": "https://seretia.link/discord", "secure": True},
    # {"host": "lavalink.jirayu.net", "port": 443, "password": "youshallnotpass", "secure": True},  # certificado SSL inválido: se rechaza
    # ── Sin SSL ──
    {"host": "lavalink.triniumhost.com", "port": 4333, "password": "free", "secure": False},
    {"host": "lavalink.triniumhost.com", "port": 2333, "password": "kirito", "secure": False},
    {"host": "lavalink.triniumhost.indevs.in", "port": 4333, "password": "free", "secure": False},
    {"host": "lavalinkv4.serenetia.com", "port": 80, "password": "https://seretia.link/discord", "secure": False},
    {"host": "lavalink.jirayu.net", "port": 13592, "password": "youshallnotpass", "secure": False},
    {"host": "lava2.kasawa.pro", "port": 2334, "password": "youshallnotpass", "secure": False},     # nuevo
    # ── HeavenCloud (India, USA, Singapur, Europa) ──
    {"host": "us.lavalink.heavencloud.in", "port": 443, "password": "heavencloud", "secure": True},
    {"host": "eu.lavalink.heavencloud.in", "port": 443, "password": "heavencloud", "secure": True},
    {"host": "sg.lavalink.heavencloud.in", "port": 443, "password": "heavencloud", "secure": True},
    {"host": "lavalink.heavencloud.in", "port": 443, "password": "heavencloud", "secure": True},    # India (Mumbai)
    {"host": "lavalink.heavencloud.in", "port": 2333, "password": "heavencloud", "secure": False},  # India, sin SSL
    {"host": "free-lava.heavencloud.in", "port": 4000, "password": "heavencloud.in", "secure": False},  # nuevo (ojo: contraseña distinta)

        # ── Mi servidor propio (Termux) ──
    {"host": "127.0.0.1", "port": 2333, "password": "tumadreteniaunapolla", "secure": False},
]

# ── Nodelink (otro servidor que habla el protocolo de Lavalink v4). Se pueden usar, pero son menos probados
# con wavelink; están apagados por defecto. Pon USAR_NODELINK = True para añadirlos (el monitor de salud
# igual descarta los que no respondan). ──
USAR_NODELINK = False
NODELINK_NODES_CONFIG = [
    {"host": "nodelink.triniumhost.com", "port": 443, "password": "free", "secure": True},
    {"host": "nodelink-02.triniumhost.com", "port": 443, "password": "trinium", "secure": True},
    {"host": "nodelink.gdjkhp.com", "port": 443, "password": "youshallnotpass", "secure": True},
    {"host": "nodelink.gdjkhp.com", "port": 3000, "password": "youshallnotpass", "secure": False},
    {"host": "sg1-nodelink.nyxbot.app", "port": 3000, "password": "nyxbot.app/support", "secure": False},
    {"host": "sg2-nodelink.nyxbot.app", "port": 3000, "password": "nyxbot.app/support", "secure": False},
    {"host": "lavalink.triniumhost.com", "port": 9008, "password": "free", "secure": False},
    {"host": "lavalink.triniumhost.com", "port": 6000, "password": "trinium", "secure": False},
]
if USAR_NODELINK:
    LAVALINK_NODES_CONFIG += NODELINK_NODES_CONFIG


def _clave_nodo(cfg: dict) -> str:
    """Hay hosts repetidos con distinto puerto, así que el bloqueo se guarda por host:puerto."""
    return f"{cfg['host']}:{cfg['port']}"

# Ya NO hay pruebas de salud: un nodo sirve si wavelink lo reporta CONNECTED y nada más.
# ESTADO_NODOS se deja vacío solo para que los imports viejos (p. ej. salud_nodos.py) no truenen; ya no se consulta.
ESTADO_NODOS = {}


def marcar_nodo_enfermo(node, motivo: str = "falló al reproducir"):
    """Compatibilidad: antes castigaba al nodo 15 min. Ahora no hace nada (solo cuenta si está CONNECTED o no)."""
    logger.debug(f"marcar_nodo_enfermo ignorado para {getattr(node, 'identifier', '?')}: {motivo}")


# Nodos que se prefieren SIEMPRE que estén conectados, EN ORDEN DE PREFERENCIA (parte del host, sin importar
# mayúsculas): primero kasawa; si kasawa falla, tu servidor propio (Termux, 127.0.0.1); si ese también falla, los públicos.
NODOS_PRIORITARIOS = ["kasawa", "127.0.0.1"]


def _prioridad_nodo(idx: int) -> int:
    """0 = primer nodo de NODOS_PRIORITARIOS (kasawa), 1 = el segundo (local), ... y len(NODOS_PRIORITARIOS) = los demás.
    Menor = se usa antes."""
    try:
        host = LAVALINK_NODES_CONFIG[idx]["host"].lower()
    except Exception:
        return len(NODOS_PRIORITARIOS)
    for i, p in enumerate(NODOS_PRIORITARIOS):
        if p.lower() in host:
            return i
    return len(NODOS_PRIORITARIOS)


PRIO_ENFRIAMIENTO_S = 120      # tras un fallo REAL en el nodo prioritario, se usan los demás este tiempo
_prio_fallo = {}               # identifier -> time.time() del último fallo real


def nivel_prioridad(node) -> int:
    """0 = kasawa, 1 = nodo local, ... len(NODOS_PRIORITARIOS) = cualquier otro (público de respaldo)."""
    ident = getattr(node, "identifier", "")
    try:
        return _prioridad_nodo(int(str(ident).replace("node", "")))
    except Exception:
        return len(NODOS_PRIORITARIOS)


def es_nodo_prioritario(node) -> bool:
    """True para cualquiera de los nodos de NODOS_PRIORITARIOS (kasawa o el local)."""
    return nivel_prioridad(node) < len(NODOS_PRIORITARIOS)


def prioritario_enfriando(node) -> bool:
    return time.time() - _prio_fallo.get(getattr(node, "identifier", ""), 0) < PRIO_ENFRIAMIENTO_S


def marcar_fallo_prioritario(node):
    if es_nodo_prioritario(node):
        _prio_fallo[node.identifier] = time.time()
        logger.warning(f"⚠️ Nodo prioritario {node.identifier} falló al reproducir: se usan los de respaldo {PRIO_ENFRIAMIENTO_S}s")


def limpiar_fallo_prioritario(node):
    _prio_fallo.pop(getattr(node, "identifier", ""), None)


def _nodo_usable(node) -> bool:
    """Conectado (y si es prioritario, que no esté enfriándose tras un fallo real). Solo el nodo de nivel 0 (kasawa)
    se usa AUNQUE el código lo vea desconectado; el nodo local solo cuenta si está CONNECTED de verdad (si el
    Termux está apagado, no tiene caso intentarlo)."""
    if node is None:
        return False
    if node.status == wavelink.NodeStatus.CONNECTED:
        return not (es_nodo_prioritario(node) and prioritario_enfriando(node))
    return nivel_prioridad(node) == 0 and not prioritario_enfriando(node)


def _puntaje_nodo(idx: int) -> float:
    """Compatibilidad: ya no se puntúan los nodos (todos valen igual)."""
    return 0.0


def _idx_permitidos() -> list:
    """Todos los nodos configurados: ya no se filtra por pruebas de salud. Que esté prendido o no lo decide
    `node.status == CONNECTED` en cada función de abajo."""
    return list(range(len(LAVALINK_NODES_CONFIG)))


# ✅ Variable para trackear nodos bloqueados y no perder tiempo con ellos
_nodos_bloqueados = {}
_lavalink_active_idx = 0

# ⏱️ Tiempos máximos: un nodo público "colgado" ya no puede congelar el bot por minutos
BUSQUEDA_TIMEOUT_NODO = 8        # seg. máximos por nodo en cada búsqueda
BUSQUEDA_PRESUPUESTO = 40        # seg. máximos TOTALES por búsqueda (sumando todos los nodos)
PLAY_TIMEOUT = 20                # seg. máximos para que Lavalink acepte un play()
BLOQUEO_NODO_SEG = 300           # cuánto tiempo se considera "baneado" a un nodo
WATCHDOG_ESPERA = 6              # seg. que se espera el TrackEnd normal antes de avanzar por watchdog
PROGRESS_UPDATE_INTERVAL = 1.0   # seg. entre ediciones del embed de progreso (Discord limita ~5 ediciones/5s)


def _error_bloquea_nodo(err_str: str) -> bool:
    """True SOLO si el error indica que el NODO (su IP) está baneado/limitado por YouTube.
    Errores del VIDEO (edad, región, no disponible) NO deben bloquear el nodo: antes un solo video
    restringido bloqueaba los 10 nodos por 5 min y entonces ninguna canción cargaba. Además el
    chequeo viejo `"age" in err_str` coincidía con palabras como 'message' o 'language'."""
    low = err_str.lower()
    return bool(re.search(r"sign in to confirm|not a bot|\b403\b|\b429\b|too many requests|forbidden", low))


async def _buscar_en_nodos(query: str, preferred_node=None):
    """Busca rotando entre nodos CONECTADOS. Devuelve (resultado, nodo_que_resolvio) o (None, None).
    - Cada nodo tiene timeout (antes un nodo colgado podía bloquear la búsqueda ~5 min).
    - Si hay preferred_node (el nodo del player) se intenta primero, para que el track se resuelva en
      el mismo nodo donde se va a reproducir.
    - Los nodos marcados como bloqueados se dejan para el final (último recurso), no se descartan."""
    global _lavalink_active_idx
    num_nodes = len(LAVALINK_NODES_CONFIG)

    # Limpiamos bloqueos viejos
    ahora = time.time()
    for host in list(_nodos_bloqueados.keys()):
        if ahora - _nodos_bloqueados[host] > BLOQUEO_NODO_SEG:
            del _nodos_bloqueados[host]

    candidatos = []
    permitidos = set(_idx_permitidos())
    for i in range(num_nodes):
        idx = (_lavalink_active_idx + i) % num_nodes
        if idx not in permitidos:
            continue
        node = wavelink.Pool.nodes.get(f"node{idx}")
        if node and _nodo_usable(node):
            candidatos.append((idx, node))

    candidatos.sort(key=lambda c: _prioridad_nodo(c[0]))                   # prioritarios primero (sort estable)
    if preferred_node is not None:
        candidatos.sort(key=lambda c: 0 if c[1] == preferred_node else 1)  # sort estable

    libres = [c for c in candidatos if _clave_nodo(LAVALINK_NODES_CONFIG[c[0]]) not in _nodos_bloqueados]
    bloqueados = [c for c in candidatos if _clave_nodo(LAVALINK_NODES_CONFIG[c[0]]) in _nodos_bloqueados]

    inicio = time.monotonic()
    fallos_nodos = {}  # idx -> error, para loguear al final

    for idx, node in libres + bloqueados:
        restante = BUSQUEDA_PRESUPUESTO - (time.monotonic() - inicio)
        if restante <= 0:
            logger.warning(f"⏱️ Se agotó el tiempo máximo de búsqueda ({BUSQUEDA_PRESUPUESTO}s) para: {query}")
            break

        cfg = LAVALINK_NODES_CONFIG[idx]
        try:
            resultado = await asyncio.wait_for(
                wavelink.Pool.fetch_tracks(query, node=node),
                timeout=min(BUSQUEDA_TIMEOUT_NODO, restante)
            )
            if resultado:  # ✅ Éxito
                _lavalink_active_idx = idx
                if es_nodo_prioritario(node):
                    limpiar_fallo_prioritario(node)
                return resultado, node
            # Si resultado es None/vacío, sigue con el siguiente nodo
            fallos_nodos[idx] = "sin resultados"
            continue
        except asyncio.TimeoutError:
            marcar_fallo_prioritario(node)
            msg = f"timeout ({BUSQUEDA_TIMEOUT_NODO}s)"
            fallos_nodos[idx] = msg
            logger.debug(f"⏱️ Nodo {cfg['host']} no respondió a tiempo.")
            continue
        except Exception as e:
            err_str = str(e)
            # Un video no disponible / restringido / borrado NO es culpa del nodo: antes cualquier error mandaba al
            # nodo prioritario a "enfriamiento" 2 min y toda la música caía a los nodos públicos por una sola canción mala.
            es_error_de_video = isinstance(e, getattr(wavelink, "LavalinkLoadException", ())) and not _error_bloquea_nodo(err_str)
            if not es_error_de_video:
                marcar_fallo_prioritario(node)
            if _error_bloquea_nodo(err_str):
                logger.debug(f"⚠️ Nodo {cfg['host']} reportó bloqueo de YouTube: {e}")
                _nodos_bloqueados[_clave_nodo(cfg)] = time.time()
                fallos_nodos[idx] = "bloqueado por YouTube"
            else:
                fallos_nodos[idx] = str(e)[:60]
                logger.debug(f"⚠️ Nodo {cfg['host']} falló: {e}")
            continue

    # Ningún nodo devolvió resultado
    if fallos_nodos:
        logger.warning(f"❌ Ningún nodo pudo cargar '{query}'. Fallos: {fallos_nodos}")
    return None, None


async def buscar_tracks_con_fallback(query: str):
    """Busca tracks rotando entre nodos (ver _buscar_en_nodos). Devuelve el resultado o None."""
    resultado, _ = await _buscar_en_nodos(query)
    return resultado


def get_nodo_conectado():
    """Devuelve un wavelink.Node que esté realmente CONNECTED ahorita mismo (empezando por el
    nodo 'activo' que buscar_tracks_con_fallback ya validó, y si no, cualquier otro conectado).
    Se usa al unirse a un canal de voz para no dejar que wavelink elija automáticamente un nodo
    que solo APARENTA estar disponible pero no completó el handshake (bug que causaba
    ChannelTimeoutException al reproducir). Devuelve None si ninguno está conectado."""
    num_nodes = len(LAVALINK_NODES_CONFIG)
    if num_nodes == 0:
        return None

    permitidos = set(_idx_permitidos())
    orden_intentos = [(_lavalink_active_idx + i) % num_nodes for i in range(num_nodes)]
    orden_intentos.sort(key=_prioridad_nodo)          # los prioritarios primero
    for idx in orden_intentos:
        if idx not in permitidos:
            continue
        node = wavelink.Pool.nodes.get(f"node{idx}")
        if node and _nodo_usable(node):
            return node
    return None


def get_nodos_conectados():
    """Lista de TODOS los nodos realmente CONNECTED (empezando por el 'activo')."""
    num_nodes = len(LAVALINK_NODES_CONFIG)
    nodos = []
    permitidos = set(_idx_permitidos())
    orden = [(_lavalink_active_idx + i) % num_nodes for i in range(num_nodes)]
    orden.sort(key=_prioridad_nodo)                   # los prioritarios primero
    for idx in orden:
        if idx not in permitidos:
            continue
        node = wavelink.Pool.nodes.get(f"node{idx}")
        if node and _nodo_usable(node):
            nodos.append(node)
    return nodos


def mejor_nodo_disponible(actual):
    """Un nodo usable con MEJOR prioridad que `actual` (kasawa si estás en el local o en un público; el local si estás
    en un público), o None. Sirve para volver al nodo preferido cuando se recupera."""
    nodos = get_nodos_conectados()                    # ya vienen ordenados: el mejor nivel primero
    if nodos and nivel_prioridad(nodos[0]) < nivel_prioridad(actual):
        return nodos[0]
    return None


async def limpiar_voz(guild):
    """Borra TODO rastro de una conexión de voz muerta/zombi de este servidor (player de wavelink,
    voice client de discord.py y estado de voz en Discord). Sin esto, después de un timeout o de que
    Discord/Lavalink tiraran la conexión, el voice_client viejo se quedaba registrado y cada intento
    nuevo de conectar fallaba hasta reiniciar el bot."""
    vc = guild.voice_client
    if vc is not None:
        try:
            await asyncio.wait_for(vc.disconnect(force=True), timeout=8)
        except Exception:
            pass
    try:
        guild._state._remove_voice_client(guild.id)
    except Exception:
        pass
    for node in list(wavelink.Pool.nodes.values()):
        try:
            node._players.pop(guild.id, None)
        except Exception:
            pass
    try:
        await guild.change_voice_state(channel=None)
    except Exception:
        pass


async def conectar_player(channel):
    """Conecta un MusicPlayer al canal y lo asegura en un nodo realmente CONNECTED.
    Si la conexión se cuelga (ChannelTimeoutException), limpia el estado zombi y reintenta una vez."""
    guild = channel.guild
    vc_viejo = guild.voice_client
    if vc_viejo is not None and not getattr(vc_viejo, 'connected', False):
        await limpiar_voz(guild)

    ultimo_error = None
    vc = None
    for intento in range(2):
        try:
            vc = await channel.connect(cls=MusicPlayer, timeout=15.0)
            break
        except Exception as e:
            ultimo_error = e
            logger.debug(f"Intento {intento + 1} de conectar a voz falló: {e}")
            await limpiar_voz(guild)
            await asyncio.sleep(1.5)
    if vc is None:
        raise ultimo_error

    nodo = get_nodo_conectado()
    if nodo and vc.node != nodo:
        try:
            await vc.switch_node(nodo)
        except Exception as e:
            logger.debug(f"No se pudo cambiar al nodo {nodo.identifier}: {e}")
    return vc


async def _conectar_nodo_lavalink(bot, nodo: "wavelink.Node", resultados: list):
    """Conecta UN nodo de Lavalink en su propia tarea, corriendo en paralelo con los demás."""
    try:
        await wavelink.Pool.connect(nodes=[nodo], client=bot)
        logger.debug(f"✅ Nodo Lavalink conectado: {nodo.uri}")
        resultados.append(True)
    except Exception as e:
        logger.warning(f"❌ No se pudo conectar el nodo Lavalink {nodo.uri}: {type(e).__name__}: {e}")
        resultados.append(False)