import discord
from discord.ext import commands
import logging
import asyncio
import tempfile
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ✅ Emoji con el que el bot reacciona automático a cualquier audio detectado. Si alguien
# reacciona con este MISMO emoji, dispara la transcripción.
TRANSCRIBE_EMOJI = "📝"

AUDIO_EXTENSIONS = {'.mp3', '.ogg', '.wav', '.m4a', '.flac', '.opus', '.webm', '.aac'}

# ✅ Mismas keys que ya usa IA.py — mismo patrón: listas separadas por coma en
# GROQ_API_KEYS/GEMINI_API_KEYS, con fallback al nombre en singular. Aquí solo se usa la
# primera key de cada lista (no hace falta rotación para transcripción, es poco volumen).
env_groq_keys = os.getenv("GROQ_API_KEYS", "") or os.getenv("GROQ_API_KEY", "")
_groq_keys = [k.strip().strip('"').strip("'") for k in env_groq_keys.split(",") if k.strip()]
GROQ_API_KEY = _groq_keys[0] if _groq_keys else None

env_gemini_keys = os.getenv("GEMINI_API_KEYS", "") or os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
_gemini_keys = [k.strip().strip('"').strip("'") for k in env_gemini_keys.split(",") if k.strip()]
GEMINI_API_KEY = _gemini_keys[0] if _gemini_keys else None

GROQ_MODEL = "whisper-large-v3-turbo"
GEMINI_MODEL = "gemini-3.5-flash-lite"


class Transcribir(commands.Cog):
    """Cog para transcribir notas de voz y archivos de audio usando APIs en la nube
    (Groq Whisper como principal, Gemini como fallback) — nada corre localmente en el
    teléfono, así que no le pega en batería/rendimiento."""

    def __init__(self, bot):
        self.bot = bot
        self.groq_client = None
        self.gemini_model = None

        if GROQ_API_KEY:
            try:
                from groq import AsyncGroq
                self.groq_client = AsyncGroq(api_key=GROQ_API_KEY)
                logger.info(f"✅ Transcripción vía Groq lista ({GROQ_MODEL})")
            except ImportError:
                logger.warning("⚠️ Falta instalar 'groq' (pip install groq) para transcripción vía Groq.")
        else:
            logger.warning("⚠️ No hay GROQ_API_KEY configurada — se saltará Groq y se usará Gemini directo.")

        if GEMINI_API_KEY:
            try:
                import google.generativeai as genai
                genai.configure(api_key=GEMINI_API_KEY)
                self.gemini_model = genai.GenerativeModel(GEMINI_MODEL)
                logger.info(f"✅ Fallback de transcripción vía Gemini listo ({GEMINI_MODEL})")
            except ImportError:
                logger.warning("⚠️ Falta instalar 'google-generativeai' para el fallback de transcripción.")
        else:
            logger.warning("⚠️ No hay GEMINI_API_KEY configurada — no habrá fallback si Groq falla.")

        if not self.groq_client and not self.gemini_model:
            logger.error("❌ Transcripción sin backend disponible: faltan ambas keys (Groq y Gemini).")

    def _es_audio(self, attachment: discord.Attachment) -> bool:
        if getattr(attachment, "is_voice_message", None) and attachment.is_voice_message():
            return True
        if attachment.content_type and attachment.content_type.startswith("audio/"):
            return True
        return Path(attachment.filename).suffix.lower() in AUDIO_EXTENSIONS

    def _primer_audio(self, mensajes):
        """Devuelve (mensaje, adjunto) del primer mensaje de la lista que traiga un audio,
        o (None, None) si ninguno lo trae."""
        for m in mensajes:
            adjunto = next((a for a in m.attachments if self._es_audio(a)), None)
            if adjunto:
                return m, adjunto
        return None, None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Reacciona automáticamente con 📝 a cualquier mensaje que traiga un audio adjunto."""
        if message.author.bot:
            return
        for attachment in message.attachments:
            if self._es_audio(attachment):
                try:
                    await message.add_reaction(TRANSCRIBE_EMOJI)
                except discord.HTTPException as e:
                    logger.warning(f"No se pudo reaccionar al audio: {e}")
                break

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Si alguien reacciona con 📝 a un mensaje con audio, transcribe y responde con embed."""
        if payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != TRANSCRIBE_EMOJI:
            return

        channel = self.bot.get_channel(payload.channel_id) or await self.bot.fetch_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        audio_attachment = next((a for a in message.attachments if self._es_audio(a)), None)
        if not audio_attachment:
            return

        guild = getattr(channel, "guild", None)
        member = payload.member or (guild.get_member(payload.user_id) if guild else None)
        requester_name = member.display_name if member else "alguien"
        requester_avatar = member.display_avatar.url if member else None

        # ✅ Borra la reacción 📝 (de todos) en cuanto se dispara — evita que se reactive si
        # alguien más reacciona mientras se procesa. Necesita permiso "Manage Messages".
        try:
            await message.clear_reaction(TRANSCRIBE_EMOJI)
        except discord.HTTPException as e:
            logger.warning(f"No se pudo borrar la reacción {TRANSCRIBE_EMOJI}: {e}")

        await self._responder_transcripcion(message, audio_attachment, requester_name, requester_avatar)

    @commands.command(name="transcribir", aliases=["transcribe", "transcripcion", "transcripción"])
    async def transcribir_cmd(self, ctx: commands.Context):
        """Transcribe un audio. Respóndele al mensaje con el audio y usa este comando (también
        sirve si el audio va adjunto en el mismo mensaje del comando; y si no respondes a nada,
        busca el audio más reciente entre los 10 mensajes anteriores del canal).
        La IA también lo usa sola cuando alguien le pide transcribir un audio."""
        candidatos = []
        ref = ctx.message.reference
        respondio_a_algo = bool(ref and ref.message_id)
        if respondio_a_algo:
            ref_msg = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            if ref_msg is None:
                try:
                    ref_msg = await ctx.channel.fetch_message(ref.message_id)
                except discord.HTTPException:
                    ref_msg = None
            if ref_msg:
                candidatos.append(ref_msg)
        candidatos.append(ctx.message)

        mensaje_audio, adjunto = self._primer_audio(candidatos)

        # Sin respuesta ni adjunto: se busca el audio más reciente del canal
        if not adjunto and not respondio_a_algo:
            async for m in ctx.channel.history(limit=10, before=ctx.message):
                mensaje_audio, adjunto = self._primer_audio([m])
                if adjunto:
                    break

        if not adjunto:
            await ctx.send(f"❌ No encontré ningún audio. Respóndele al mensaje con el audio usando `{ctx.clean_prefix}transcribir`.")
            return

        # Igual que con la reacción: quita el 📝 para que no se dispare dos veces
        try:
            await mensaje_audio.clear_reaction(TRANSCRIBE_EMOJI)
        except discord.HTTPException as e:
            logger.warning(f"No se pudo borrar la reacción {TRANSCRIBE_EMOJI}: {e}")

        await self._responder_transcripcion(mensaje_audio, adjunto, ctx.author.display_name, ctx.author.display_avatar.url)

    async def _responder_transcripcion(self, message: discord.Message, audio_attachment: discord.Attachment,
                                       requester_name: str, requester_avatar):
        """Transcribe el audio y responde al mensaje con el embed. Lo comparten la reacción 📝
        y el comando 'transcribir' (que también usa la IA)."""
        if not self.groq_client and not self.gemini_model:
            embed = discord.Embed(
                description="❌ La transcripción no está disponible ahorita (faltan las API keys de Groq/Gemini).",
                color=discord.Color.red()
            )
            await message.reply(embed=embed, mention_author=False)
            return

        # ✅ "MaxIA está escribiendo..." mientras dura todo el proceso (descarga + conversión +
        # llamada a la API) — puede tardar algunos segundos según el largo del audio.
        async with message.channel.typing():
            try:
                texto = await self._transcribir_audio(audio_attachment)
            except Exception as e:
                logger.error(f"Error transcribiendo audio: {type(e).__name__}: {e}", exc_info=True)
                texto = None

        embed = discord.Embed(
            title="📝 Transcripción de audio",
            description=texto if texto else "❌ No se detectó voz clara en el audio.",
            color=discord.Color.blurple() if texto else discord.Color.red()
        )
        embed.set_footer(text=f"Transcripción pedida por {requester_name}", icon_url=requester_avatar)

        await message.reply(embed=embed, mention_author=False)

    async def _transcribir_audio(self, attachment: discord.Attachment):
        """Descarga el audio, lo convierte a WAV mono 16kHz con ffmpeg (formato liviano y
        universal para ambas APIs), e intenta transcribir primero con Groq y si falla o no
        está disponible, cae a Gemini."""
        logger.info(f"🎙️ Iniciando transcripción de: {attachment.filename}")
        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = Path(tmpdir) / attachment.filename
            wav_path = Path(tmpdir) / "audio.wav"

            await attachment.save(original_path)

            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(original_path),
                "-ar", "16000", "-ac", "1", "-f", "wav", str(wav_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, ffmpeg_stderr = await proc.communicate()

            if not wav_path.exists():
                logger.error(f"❌ ffmpeg no generó el WAV. stderr: {ffmpeg_stderr.decode('utf-8', errors='ignore')[-1000:]}")
                return None

            # 1) Intento principal: Groq (whisper-large-v3-turbo)
            if self.groq_client:
                texto = await self._transcribir_groq(wav_path)
                if texto:
                    return texto
                logger.warning("⚠️ Groq no devolvió texto (o falló) — probando fallback con Gemini.")

            # 2) Fallback: Gemini
            if self.gemini_model:
                return await self._transcribir_gemini(wav_path)

            return None

    async def _transcribir_groq(self, wav_path: Path):
        logger.info(f"🎙️ Transcribiendo con Groq ({GROQ_MODEL})...")
        try:
            with open(wav_path, "rb") as f:
                audio_bytes = f.read()

            transcripcion = await self.groq_client.audio.transcriptions.create(
                file=(wav_path.name, audio_bytes),
                model=GROQ_MODEL,
                language="es",
                response_format="text",
            )
            texto = (transcripcion or "").strip() if isinstance(transcripcion, str) else (getattr(transcripcion, "text", "") or "").strip()
            logger.info(f"🎙️ Groq respondió: {texto[:200]}")
            return texto if texto else None
        except Exception as e:
            logger.error(f"❌ Error con Groq ({type(e).__name__}): {e}")
            return None

    async def _transcribir_gemini(self, wav_path: Path):
        logger.info(f"🎙️ Transcribiendo con Gemini ({GEMINI_MODEL})...")
        try:
            import google.generativeai as genai

            # La subida y la generación son llamadas bloqueantes del SDK de Gemini —
            # se corren en un hilo aparte para no trabar el event loop del bot.
            audio_file = await asyncio.to_thread(genai.upload_file, str(wav_path))
            response = await self.gemini_model.generate_content_async([
                "Transcribe este audio en español. Responde ÚNICAMENTE con el texto "
                "transcrito, sin comentarios ni explicaciones adicionales. Si no hay "
                "voz clara o el audio está vacío, responde con la palabra: NADA",
                audio_file,
            ])
            texto = (response.text or "").strip()
            logger.info(f"🎙️ Gemini respondió: {texto[:200]}")

            if not texto or texto.strip().upper() == "NADA":
                return None
            return texto
        except Exception as e:
            logger.error(f"❌ Error con Gemini ({type(e).__name__}): {e}")
            return None


async def setup(bot):
    await bot.add_cog(Transcribir(bot))
