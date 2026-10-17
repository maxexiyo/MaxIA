import discord
from discord.ext import commands, tasks
import asyncio
import os
import sqlite3
import aiohttp
import json
import base64
import re
import random
import io
from PIL import Image
from datetime import datetime
from collections import deque


from ia_core.config import PROFILES_DIR, CREATOR_DISCORD_ID, GROQ_API_KEYS, GEMINI_API_KEYS
from ia_core.tools_schema import GEMINI_MODELS_FALLBACK  # (se usa dentro de motor_ia_mixin, pero se deja
                                                  # disponible aquí también por si algo del cog lo referencia)

from ia_core.perfiles_mixin import PerfilesMixin
from ia_core.personajes_mixin import PersonajesMixin
from ia_core.memoria_chat_mixin import MemoriaChatMixin
from ia_core.busqueda_web_mixin import BusquedaWebMixin
from ia_core.motor_ia_mixin import MotorIAMixin
from ia_core.eventos_mixin import EventosMixin
from ia_core.comandos_mixin import ComandosMixin
from ia_core.acciones_mixin import AccionesMixin

try:
    from zoneinfo import ZoneInfo
    ZONA_BOT = ZoneInfo(os.getenv("BOT_TIMEZONE", "America/Mexico_City"))
except Exception:
    ZONA_BOT = None  # sin tzdata: se usa la hora del servidor


class BoundedSet(set):
    """set que olvida lo más viejo al pasar de `maxlen` (antes ai_generated_messages crecía sin límite)."""
    def __init__(self, maxlen: int = 5000):
        super().__init__()
        self._orden = deque()
        self._maxlen = maxlen

    def add(self, item):
        if item in self:
            return
        super().add(item)
        self._orden.append(item)
        while len(self._orden) > self._maxlen:
            super().discard(self._orden.popleft())


class AIModule(commands.Cog, PerfilesMixin, PersonajesMixin, MemoriaChatMixin, BusquedaWebMixin, MotorIAMixin, EventosMixin, ComandosMixin, AccionesMixin):
    def __init__(self, bot):
        self.bot = bot
        self.ai_generated_messages = BoundedSet(5000)  # IDs de mensajes generados por la IA (acotado)
        # 🤖 Peticiones de la IA esperando a que la persona entre a voz: {(guild_id, user_id): {...}}
        self.acciones_pendientes = {}
        # ✅ Rotación "pegajosa": se queda en la key que funciona, solo rota si falla
        self.groq_active_idx = 0
        self.gemini_active_idx = 0
        self.gemini_active_model_idx = 0  # ✅ rotación pegajosa de MODELO (independiente de la key)
        # ✅ Cooldown por canal para el modo ambiental (para no ser pesado)
        self.last_ambient_time = {}
        # ✅ Cooldown por canal para cuando lo mencionan por nombre sin @ (para no evaluar cada rato)
        self.last_name_mention_time = {}
        # 📦 Modo ambiental "por lotes": estado para agrupar mensajes cuando hay mucha actividad
        self.ambient_message_times = {}   # channel_id -> deque[datetime] (para medir qué tan activo está)
        self.ambient_pending_batch = {}   # channel_id -> lista de mensajes juntados esperando evaluarse
        self.ambient_flush_timer = {}     # channel_id -> tarea que suelta el lote si dejan de escribir
        # ✅ Control de cumpleaños ya felicitados hoy (se limpia cada día)
        self.cumpleanos_felicitados_hoy = set()
        self.ultimo_dia_revisado_cumple = None
        self.check_birthdays_loop.start()

    async def cog_unload(self):
        self.check_birthdays_loop.cancel()
        # Cancelar cualquier temporizador de "soltar lote pendiente" que siga corriendo
        for task in self.ambient_flush_timer.values():
            if task and not task.done():
                task.cancel()
        # 🛠️ Cierra la sesión HTTP compartida del motor de IA (evita "Unclosed client session" de este cog)
        try:
            await self.cerrar_http_ia()
        except Exception as e:
            print(f"⚠️ Error cerrando la sesión HTTP de la IA: {type(e).__name__}: {e!r}")

    @tasks.loop(hours=1)
    async def check_birthdays_loop(self):
        """Revisa cada hora si hoy es cumpleaños de alguien que pidió ser felicitado, y lo saluda.
        Solo felicita entre las 7am y las 8pm (hora del servidor donde corre el bot) - fuera de
        ese rango sigue revisando cada hora pero no manda nada, para no despertar a nadie con un
        mensaje a las 3am. En cuanto la hora vuelve a caer dentro del rango, felicita normal."""
        try:
            await self.bot.wait_until_ready()
            hoy = datetime.now(ZONA_BOT)
            if self.ultimo_dia_revisado_cumple != hoy.date():
                self.cumpleanos_felicitados_hoy = set()
                self.ultimo_dia_revisado_cumple = hoy.date()

            # ⏰ Fuera del horario activo (7am-8pm): no felicita todavía, pero SÍ deja que la
            # revisión de "ya es su cumpleaños" siga corriendo normal la próxima hora dentro del rango.
            if not (7 <= hoy.hour < 20):
                return

            profiles = self.load_user_profiles()
            for user_id_str, data in profiles.items():
                if not isinstance(data, dict): continue
                if user_id_str in self.cumpleanos_felicitados_hoy: continue

                cumple = data.get("cumpleanos", {})
                if not cumple or not cumple.get("felicitar"): continue
                if cumple.get("dia") != hoy.day or cumple.get("mes") != hoy.month: continue

                nombre = data.get("nombre_global", "esa persona")
                mensaje = f"🎉🎂 ¡Feliz cumpleaños, {nombre}! Espero que la pases increíble hoy :v"

                for guild in self.bot.guilds:
                    try:
                        member = guild.get_member(int(user_id_str))
                        if not member: continue
                        canal = guild.system_channel
                        if not canal or not canal.permissions_for(guild.me).send_messages:
                            canal = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
                        if canal:
                            sent = await canal.send(mensaje, allowed_mentions=self.SIN_PINGS_MASIVOS)
                            self.ai_generated_messages.add(sent.id)
                            self.save_guild_message(guild.id, canal.id, "MaxIA", "model", mensaje, is_ai=True)
                            print(f"🎂 [Cumpleaños] Felicitado {nombre} en `{guild.name}` #{canal.name}")
                    except Exception as e:
                        print(f"⚠️ Error felicitando cumpleaños en server {guild.id}: {e}")

                self.cumpleanos_felicitados_hoy.add(user_id_str)
        except Exception as e:
            print(f"⚠️ Error en check_birthdays_loop: {e}")

    def clean_ai_response(self, text: str) -> str:
        if not text: return ""
        
        cleaned = re.sub(r'<[^>]*think[^>]*>.*?</[^>]*think[^>]*>', '', text, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r'<thought>.*?</thought>', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r'.*?</think>\s*', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r'.*?</thought>\s*', '', cleaned, flags=re.IGNORECASE | re.DOTALL)

        if "<think>" in cleaned.lower():
            cleaned = re.sub(r'<think>.*?\n\n', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
            cleaned = re.sub(r'<think>.*', '', cleaned, flags=re.IGNORECASE | re.DOTALL)

        patterns_to_remove = [
            r"(?i)^(Here is a thinking process|Thinking Process|Pensamiento|Análisis):.*?\n",
            r"(?i)\*Thinking\*:?.*?\n"
        ]
        for pat in patterns_to_remove:
            cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE | re.DOTALL)

        # ✅ Quita el prefijo "[MaxIA]:" / "[Nombre]:" que el modelo a veces copia
        # del formato de historial ("[usuario]: mensaje") al inicio de su propia respuesta.
        cleaned = re.sub(r'^\s*\[[^\]\n]{1,30}\]\s*:\s*', '', cleaned)

        return cleaned.strip()

    # --- REACCIONES (en vez de escribir texto, a veces solo reacciona con un emoji) ---
    REACT_PATTERN = re.compile(r'^\s*REACT:\s*(\S+)\s*$', re.IGNORECASE)

    # ✅ Sentinela interna: cuando la IA usó la tool 'reaccionar_mensaje' y ESA fue la única
    # tool que pidió, request_groq/request_gemini devuelven esto en vez de pedirle un texto
    # extra al modelo. Así el que llama sabe que ya se reaccionó y NO debe mandar nada más.
    SOLO_REACCION = "\x00__SOLO_REACCION__\x00"

    async def _intentar_reaccionar(self, message: discord.Message, texto: str) -> bool:
        """Si la IA decidió 'REACT:<emoji>' en vez de escribir un mensaje normal, reacciona con
        ese emoji al mensaje correspondiente y devuelve True (para que el que llama NO mande
        también una respuesta de texto). Si el texto no tiene ese formato, devuelve False y no
        hace nada, para que el flujo normal siga su curso."""
        if not texto:
            return False
        if texto == self.SOLO_REACCION:
            # Ya se reaccionó desde ejecutar_tool_call (tool 'reaccionar_mensaje') - aquí
            # solo hace falta avisar que NO se debe mandar texto.
            return True
        match = self.REACT_PATTERN.match(texto.strip())
        if not match:
            return False
        emoji = match.group(1)
        emoji_resuelto = self.resolver_emoji_para_reaccion(emoji, message.guild)
        try:
            await message.add_reaction(emoji_resuelto)
            print(f"👍 [Reacción] Reaccionó con {emoji_resuelto} a un mensaje de {message.author.display_name}")
        except Exception as e:
            print(f"⚠️ No se pudo reaccionar con '{emoji}' (¿emoji inválido?): {e}")
        return True  # Se decidió "solo reaccionar" - aunque el emoji falle, no hay que mandar texto

    def es_respuesta_meta_ambiental(self, texto: str) -> bool:
        """Detecta cuando, en modo ambiental, la IA en vez de decidir (NADA o el mensaje) se pone
        a EXPLICAR la regla de cuándo debería meterse (ej. 'Solo si tengo algo valioso que aportar,
        de lo contrario mejor no interrumpir'). Eso no es una respuesta válida para el chat, así que
        se trata como si hubiera dicho NADA."""
        if not texto:
            return False
        t = texto.lower()
        frases_meta = [
            "algo valioso que aportar", "mejor no interrumpir", "de lo contrario",
            "solo si tengo algo", "solo si aporto", "solo si realmente tengo",
            "cuando tengo algo que decir", "prefiero no interrumpir", "no debería interrumpir",
        ]
        return any(f in t for f in frases_meta)

    # ✅ Ningún mensaje generado por la IA puede pingear @everyone, @here, ni roles - por más que el
    # texto lo incluya literal (ya sea porque el modelo lo escribió solo, o porque alguien lo engañó
    # para que lo repitiera). Los mentions a usuarios normales (@usuario) se dejan pasar, ya que
    # sirven para responderle a alguien puntualmente y no hacen spam masivo.
    SIN_PINGS_MASIVOS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)

    def format_custom_emojis(self, text: str, guild: discord.Guild = None) -> str:
        """Convierte los ':nombre_emoji:' que escribe la IA en texto plano al formato real que
        Discord necesita para que se rendericen como emoji: <:nombre:id> o <a:nombre:id> si es animado.
        Sin esto, Discord solo muestra el texto literal ':nombre:' en vez del emoji de a de veras."""
        if not text:
            return text
        try:
            # ✅ A veces la IA "alucina" y escribe ella misma el tag completo de Discord
            # (<:nombre:idfalso>) copiando el formato que ve en el historial, con un id
            # inventado que no es real. Si no se limpia esto primero, el reemplazo de abajo
            # solo cambia el ":nombre:" de adentro y deja el "<" y el id falso rotos y sueltos
            # alrededor (el emoji roto con números raros que se ve en Discord).
            # Lo bajamos a texto plano ':nombre:' para que el reemplazo normal lo arme bien.
            text = re.sub(r'<a?:([a-zA-Z0-9_]{2,32}):\d+>', r':\1:', text)

            disponibles = {}
            if guild:
                for e in guild.emojis:
                    disponibles[e.name.lower()] = e
            # Fallback: emojis de otros servers donde esté el bot (por si el server no tiene ese emoji propio)
            for e in self.bot.emojis:
                disponibles.setdefault(e.name.lower(), e)

            def _reemplazar(match):
                nombre = match.group(1)
                emoji_obj = disponibles.get(nombre.lower())
                return str(emoji_obj) if emoji_obj else match.group(0)

            return re.sub(r':([a-zA-Z0-9_]{2,32}):', _reemplazar, text)
        except Exception as e:
            print(f"⚠️ Error formateando emojis personalizados: {e}")
            return text


async def setup(bot):
    await bot.add_cog(AIModule(bot))