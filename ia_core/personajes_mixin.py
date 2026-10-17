"""
PersonajesMixin: las personalidades de la IA como "personas distintas" en el server (webhooks) y el MODO LOCO.

· Cada personalidad ("max", "neko", "sexo", "yuri") tiene un nombre y una foto. La normal ("max") habla con la
  cuenta del propio bot; las demás hablan por webhook, así que en el chat se ven como otro usuario más.
  Si el bot no tiene permiso de "Gestionar webhooks" (o el canal no lo permite), el mensaje sale con la cuenta del
  bot y el nombre del personaje en negritas: nunca se queda sin contestar.
· Todos los personajes comparten LA MISMA memoria (chat_memory, perfiles, glosario...): en el historial cada
  mensaje queda guardado con el nombre de quien lo dijo.
· MODO LOCO (!max loco on, requiere el modo ambiental activo para las intervenciones espontáneas): los personajes
  conviven en el canal como si fueran usuarios normales. La IA decide, en una sola "mesa", quién (si alguien) habla
  y a quién le contesta (a una persona o a otro personaje). Frenos anti-bucle (ver LOCO_* en config.py):
  tope de mensajes seguidos entre personajes, probabilidad que baja con cada mensaje seguido, espera mínima entre
  rondas, una sola ronda a la vez por canal, y todo se reinicia en cuanto habla una persona real.
"""
import asyncio
import json
import os
import random
import re
import time

import discord

from ia_core.config import (
    PERSONAJES_DEFAULT, PERSONAJES_USAR_WEBHOOK,
    LOCO_MAX_CADENA, LOCO_MAX_TURNOS, LOCO_PROB_APORTE, LOCO_PROB_CONTINUAR, LOCO_DECAIMIENTO, LOCO_ESPERA_MIN_S,
)

NOMBRE_WEBHOOK = "MaxIA Personajes"
NOMBRE_HISTORIAL_MAX = "MaxIA"      # nombre con el que SIEMPRE se guardó la personalidad normal en el historial
ORDEN_PERSONAJES = ["max", "neko", "yuri", "sexo"]


class PersonajesMixin:
    # ───────────────────────── nombres y fotos (por servidor) ─────────────────────────
    def _pj_archivo(self, guild_id: int) -> str:
        return os.path.join(self.get_server_dir(guild_id), "personajes.json")

    def _pj_overrides(self, guild_id: int) -> dict:
        datos = self._leer_json_seguro(self._pj_archivo(guild_id), {}, "personajes")
        return datos if isinstance(datos, dict) else {}

    def pj_info(self, guild_id: int, clave: str) -> dict:
        """{'nombre':..., 'avatar':...} del personaje: lo configurado en este server o, si no, los valores por defecto."""
        info = dict(PERSONAJES_DEFAULT.get(clave, {"nombre": clave.title(), "avatar": ""}))
        if guild_id:
            for k, v in (self._pj_overrides(guild_id).get(clave) or {}).items():
                if v:
                    info[k] = v
        return info

    def pj_set(self, guild_id: int, clave: str, campo: str, valor: str):
        datos = self._pj_overrides(guild_id)
        datos.setdefault(clave, {})[campo] = valor
        self._escribir_json_atomico(self._pj_archivo(guild_id), datos)

    def nombre_historial(self, guild_id: int, clave: str) -> str:
        """Con qué nombre queda guardado en el historial lo que dice este personaje."""
        return NOMBRE_HISTORIAL_MAX if clave == "max" else self.pj_info(guild_id, clave)["nombre"]

    # ───────────────────────── modo loco (interruptor por servidor) ─────────────────────────
    def get_loco_mode(self, guild_id: int) -> bool:
        if not guild_id or guild_id < 0:
            return False
        try:
            with open(os.path.join(self.get_server_dir(guild_id), "loco_mode.txt"), "r", encoding="utf-8") as f:
                return f.read().strip().lower() == "on"
        except Exception:
            return False

    def set_loco_mode(self, guild_id: int, enabled: bool):
        try:
            with open(os.path.join(self.get_server_dir(guild_id), "loco_mode.txt"), "w", encoding="utf-8") as f:
                f.write("on" if enabled else "off")
        except Exception as e:
            print(f"⚠️ Error guardando el modo loco: {e}")

    def loco_activo(self, guild_id: int) -> bool:
        return self.get_loco_mode(guild_id)

    def personajes_disponibles(self, canal) -> list:
        """Claves de los personajes que pueden hablar en este canal (todos, en cualquier canal; la versión explícita
        de la personalidad +18 solo existe en canales NSFW, ver get_personality)."""
        return [c for c in ORDEN_PERSONAJES if c in self.PERSONALIDADES]

    # ───────────────────────── webhooks ─────────────────────────
    async def _pj_webhook(self, canal):
        """(webhook, hilo) para mandar como personaje en `canal`, o (None, None) si no se puede."""
        if not PERSONAJES_USAR_WEBHOOK or not isinstance(canal, (discord.TextChannel, discord.Thread)) or canal.guild is None:
            return None, None
        hilo = canal if isinstance(canal, discord.Thread) else None
        base = canal.parent if hilo is not None else canal
        if base is None:
            return None, None
        cache = self.__dict__.setdefault("_pj_webhooks", {})
        malos = self.__dict__.setdefault("_pj_webhook_malos", {})
        if malos.get(base.id, 0) > time.monotonic():
            return None, None
        wh = cache.get(base.id)
        if wh is None:
            if not base.permissions_for(base.guild.me).manage_webhooks:
                malos[base.id] = time.monotonic() + 600       # sin permiso: no se insiste durante 10 min
                return None, None
            try:
                for w in await base.webhooks():
                    if w.user and self.bot.user and w.user.id == self.bot.user.id and w.name == NOMBRE_WEBHOOK and w.token:
                        wh = w
                        break
                if wh is None:
                    wh = await base.create_webhook(name=NOMBRE_WEBHOOK, reason="Personajes de MaxIA")
            except discord.HTTPException as e:
                print(f"⚠️ [Personajes] No pude preparar el webhook en #{getattr(base, 'name', '?')}: {e}")
                malos[base.id] = time.monotonic() + 600
                return None, None
            cache[base.id] = wh
            self.__dict__.setdefault("_pj_wh_ids", set()).add(wh.id)
        return wh, hilo

    def es_mensaje_de_personaje(self, message: discord.Message) -> bool:
        """True si el mensaje salió de un webhook creado por ESTE bot (un personaje)."""
        if not getattr(message, "webhook_id", None):
            return False
        if message.webhook_id in self.__dict__.get("_pj_wh_ids", ()):
            return True
        app_id = getattr(message, "application_id", None)
        return bool(app_id and self.bot.application_id and app_id == self.bot.application_id)

    def personaje_de_mensaje(self, message_id: int):
        """Clave del personaje que mandó ese mensaje (si se sabe), para contestarle con ese mismo personaje."""
        return self.__dict__.get("_pj_msg", {}).get(message_id)

    # ───────────────────────── enviar como personaje ─────────────────────────
    async def enviar_personaje(self, canal, texto: str, clave: str, *, responder_a=None, guardar: bool = True,
                               guild_id: int = None) -> list:
        """Manda `texto` como el personaje `clave` y lo registra (historial, 'mensajes de la IA', quién lo dijo).
        Devuelve la lista de mensajes enviados. `responder_a` = mensaje al que se contesta (la personalidad normal
        usa reply nativo; un webhook no puede, así que pone una línea chica de contexto sin hacer ping)."""
        guild = getattr(canal, "guild", None)
        gid = guild.id if guild else guild_id
        info = self.pj_info(gid or 0, clave)
        enviados = []
        largo = len(texto) > 2000
        trozos = [texto[i:i + 1900] for i in range(0, len(texto), 1900)] if largo else [texto]

        wh, hilo = (await self._pj_webhook(canal)) if (clave != "max" and guild is not None) else (None, None)
        for i, trozo in enumerate(trozos):
            sent = None
            if wh is not None:
                cuerpo = trozo
                if i == 0 and responder_a is not None:
                    cuerpo = f"-# ↪️ {responder_a.author.display_name}\n{trozo}"
                kw = dict(content=cuerpo[:2000], username=info["nombre"][:80], wait=True,
                          allowed_mentions=self.SIN_PINGS_MASIVOS)
                if info.get("avatar"):
                    kw["avatar_url"] = info["avatar"]
                if hilo is not None:
                    kw["thread"] = hilo
                try:
                    sent = await wh.send(**kw)
                except (discord.NotFound, discord.Forbidden):         # webhook borrado a mano: se vuelve a crear la próxima vez
                    base = canal.parent if hilo is not None else canal
                    self.__dict__.get("_pj_webhooks", {}).pop(base.id, None)
                    wh = None
                except discord.HTTPException as e:
                    print(f"⚠️ [Personajes] El webhook falló ({e}); se manda con la cuenta del bot")
                    wh = None
            if sent is None:
                if clave == "max" and responder_a is not None and not largo and i == 0:
                    sent = await responder_a.reply(trozo, mention_author=False, allowed_mentions=self.SIN_PINGS_MASIVOS)
                else:
                    prefijo = f"**{info['nombre']}:** " if (clave != "max" and i == 0) else ""
                    sent = await canal.send(prefijo + trozo, allowed_mentions=self.SIN_PINGS_MASIVOS)
            enviados.append(sent)

        registro = self.__dict__.setdefault("_pj_msg", {})
        for s in enviados:
            self.ai_generated_messages.add(s.id)
            registro[s.id] = clave
        while len(registro) > 4000:
            registro.pop(next(iter(registro)))
        if guardar and gid:
            self.save_guild_message(gid, canal.id, self.nombre_historial(gid, clave), "model", texto, is_ai=True)
        return enviados

    # ───────────────────────── memoria compartida ─────────────────────────
    def vista_para_personaje(self, historial: list, guild_id: int, clave: str) -> list:
        """Con el modo loco ENCENDIDO, lo que dijeron los OTROS personajes se le presenta a este como si fuera de
        otra persona (rol 'user'). Con el modo loco apagado todos los personajes son la misma IA: no se toca."""
        if not self.loco_activo(guild_id):
            return historial
        propio = self.nombre_historial(guild_id, clave)
        salida = []
        for m in historial:
            if m.get("role") == "model":
                nombre = m.get("content", "").split("]:", 1)[0].lstrip("[")
                if nombre != propio:
                    m = {**m, "role": "user"}
            salida.append(m)
        return salida

    # ───────────────────────── modo loco ─────────────────────────
    def _loco_st(self, channel_id: int) -> dict:
        return self.__dict__.setdefault("_loco_estado", {}).setdefault(
            channel_id, {"cadena": 0, "ocupado": False, "ultima": 0.0})

    def loco_humano_hablo(self, channel_id: int):
        """Habló una persona real: se reinicia la cuenta de mensajes seguidos entre personajes."""
        self._loco_st(channel_id)["cadena"] = 0

    def loco_tras_respuesta(self, canal, guild_id: int, clave: str):
        """Un personaje acaba de contestar (comando, mención, intervención ambiental). Si el modo loco está
        encendido, puede que otro personaje quiera aportar algo (como el modo ambiental, pero entre personajes)."""
        if not self.loco_activo(guild_id):
            return
        st = self._loco_st(canal.id)
        st["cadena"] += 1
        st["ultima"] = time.monotonic()
        if random.random() > LOCO_PROB_APORTE:
            return
        self._loco_lanzar(canal, guild_id, desde=clave)

    def _loco_lanzar(self, canal, guild_id: int, desde: str):
        t = asyncio.get_running_loop().create_task(self.loco_evaluar(canal, guild_id, desde=desde, excluir={desde}))
        tareas = self.__dict__.setdefault("_loco_tareas", set())
        tareas.add(t)
        t.add_done_callback(tareas.discard)

    async def loco_on_humano(self, message: discord.Message, guild_id: int, channel_id: int):
        """Mensaje de una persona en un canal con modo loco (y modo ambiental) activos."""
        if not self._ambient_reservar_evaluacion(channel_id, "loco"):
            return
        await self.loco_evaluar(message.channel, guild_id, desde="humano")

    async def loco_evaluar(self, canal, guild_id: int, *, desde: str, excluir=frozenset()):
        """Una ronda de la mesa: la IA decide si habla algún personaje (máx. LOCO_MAX_TURNOS) y qué dice."""
        st = self._loco_st(canal.id)
        if st["ocupado"]:
            return
        if desde != "humano" and st["cadena"] >= LOCO_MAX_CADENA:
            return
        if desde != "humano" and random.random() > LOCO_PROB_CONTINUAR * (LOCO_DECAIMIENTO ** max(0, st["cadena"] - 1)):
            return
        if time.monotonic() - st["ultima"] < LOCO_ESPERA_MIN_S and desde != "humano":
            await asyncio.sleep(LOCO_ESPERA_MIN_S)
        st["ocupado"] = True
        ultimo, hablo = None, False
        try:
            await asyncio.sleep(random.uniform(1.5, 3.5))                 # una pausa para que se sienta natural
            if self.loco_activo(guild_id) is False:
                return
            turnos = await self._loco_pedir_turnos(canal, guild_id, desde, set(excluir), st["cadena"])
            for clave, texto in turnos[:LOCO_MAX_TURNOS]:
                if st["cadena"] >= LOCO_MAX_CADENA + (1 if desde == "humano" else 0):
                    break
                try:
                    async with canal.typing():
                        await asyncio.sleep(min(4.0, 0.8 + len(texto) / 70))
                    await self.enviar_personaje(canal, texto, clave, guild_id=guild_id)
                except Exception as e:
                    print(f"⚠️ [Loco] No pude mandar el mensaje de {clave}: {type(e).__name__}: {e}")
                    break
                st["cadena"] += 1
                st["ultima"] = time.monotonic()
                ultimo, hablo = clave, True
        except Exception as e:
            print(f"⚠️ [Loco] Error en la ronda: {type(e).__name__}: {e}")
        finally:
            st["ocupado"] = False
        if hablo and st["cadena"] < LOCO_MAX_CADENA:
            self._loco_lanzar(canal, guild_id, desde=ultimo)               # la plática puede seguir (con probabilidad que baja)

    async def _loco_pedir_turnos(self, canal, guild_id: int, desde: str, excluir: set, cadena: int) -> list:
        participantes = [c for c in self.personajes_disponibles(canal) if c not in excluir]
        if not participantes:
            return []
        lineas_pj = []
        for c in self.personajes_disponibles(canal):
            nombre = self.nombre_historial(guild_id, c)
            p = self.get_personality(guild_id, canal, clave=c)       # la versión que corresponde a ESTE canal
            estado = "" if c in participantes else " (acaba de hablar: ahora NO puede hablar)"
            lineas_pj.append(f"- id «{c}»: {nombre}{estado}. {p['corta']}")
        recientes = self.get_guild_memory(guild_id, canal.id, 15)
        historial = "\n".join(f"[{u}]: {c}" for u, r, c in recientes) or "(sin historial)"
        canal_nombre = getattr(canal, "name", "canal")

        sistema = (
            "Eres la 'mesa' de un chat de Discord donde conviven varios PERSONAJES, cada uno con su personalidad y su nombre. "
            "Para el resto del server cada personaje es una persona distinta (se conocen entre sí por su nombre) y todos "
            "comparten memoria de lo que se ha hablado. Decides si alguno quiere meterse a la plática ahora y qué diría. "
            "Se portan como usuarios normales del server: pueden contestarle a una persona o a otro personaje, bromear, "
            "darle la razón, llevarle la contraria con cariño o ignorar todo. No es obligatorio que hable nadie: lo más "
            "normal es que no. Nada de repetir lo que ya se dijo ni de soltar datos random. Mensajes cortos (1 o 2 frases) "
            "y fieles a la forma de ser de cada uno. Nunca hables como asistente ni expliques reglas. "
            f"{self.format_server_rules_context(guild_id)}"
        )
        origen = ("Acaba de escribir una PERSONA (la última línea del historial)." if desde == "humano" else
                  f"Acaba de hablar el personaje «{desde}» (la última línea del historial).")
        prompt = (
            f"Canal #{canal_nombre}. Historial reciente (cada línea es [quien lo dijo]: mensaje):\n{historial}\n\n"
            f"{origen} Mensajes seguidos de personajes sin que hable una persona: {cadena}. "
            f"Mientras más seguidos, más natural es que ya nadie hable.\n\n"
            "Personajes:\n" + "\n".join(lineas_pj) + "\n\n"
            f"Pueden hablar ahora SOLO: {', '.join(participantes)}. Máximo {LOCO_MAX_TURNOS} mensajes en total y cada personaje "
            "una sola vez; el orden es el orden en que hablan.\n"
            'Responde ÚNICAMENTE con un JSON: {"turnos":[{"personaje":"<id>","mensaje":"<lo que dice>"}]}. '
            'Si nadie tiene algo que valga la pena decir, responde {"turnos":[]}.'
        )
        respuesta, _ = await self.request_gemini(sistema, [], prompt, [], guild_id=guild_id, channel_id=canal.id,
                                                 usar_tools=False, prioridad="baja")
        if not respuesta:
            respuesta, _ = await self.request_groq(sistema, [], prompt, guild_id, canal.id, [], prioridad="baja")
        return self._loco_parsear(respuesta, participantes, canal)

    def _loco_parsear(self, respuesta, participantes: list, canal) -> list:
        if not respuesta:
            return []
        txt = re.sub(r"```(?:json)?", "", respuesta)
        i, j = txt.find("{"), txt.rfind("}")
        if i == -1 or j <= i:
            return []
        try:
            data = json.loads(txt[i:j + 1])
        except Exception:
            return []
        out, vistos = [], set()
        for t in (data.get("turnos") if isinstance(data, dict) else None) or []:
            if not isinstance(t, dict):
                continue
            clave, msg = str(t.get("personaje", "")).strip().lower(), t.get("mensaje")
            if clave not in participantes or clave in vistos or not isinstance(msg, str):
                continue
            msg = self.clean_ai_response(msg)
            msg = self.format_custom_emojis(msg, getattr(canal, "guild", None)).strip()
            if not msg or msg.upper() == "NADA" or self.es_respuesta_meta_ambiental(msg):
                continue
            vistos.add(clave)
            out.append((clave, msg[:1500]))
        return out
