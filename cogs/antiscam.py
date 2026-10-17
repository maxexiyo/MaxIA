import discord
from discord import app_commands
from discord.ext import commands
from database import db
from antiscam_db import antiscam_db
import logging
from datetime import timedelta
import random
import time
import re

logger = logging.getLogger(__name__)

class AntiScam(commands.Cog):
    """Sistema anti-scam por servidor - Detecta spam masivo en múltiples canales"""
    
    def __init__(self, bot):
        self.bot = bot
        self.TIMEOUT_DURATION = timedelta(days=7)  # 7 días de mute
        self.DETECTION_WINDOW = 25  # segundos
        self.CHANNEL_THRESHOLD = 3  # mínimo de canales únicos
        
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Monitorea mensajes en tiempo real analizando spam masivo"""
        
        # Manejar verificación por DM
        if isinstance(message.channel, discord.DMChannel):
            if not message.author.bot:
                await self.handle_dm_verification(message)
            return

        # Ignorar bots, sin servidor, o canales NSFW
        if message.author.bot or not message.guild or message.channel.is_nsfw():
            return
        
        guild_id = message.guild.id
        
        # Verificar si antiscam está activado en este servidor
        if db.get_config(guild_id, 'antiscam_enabled', 'false') != 'true':
            return
        
        # Generar firmas del mensaje
        signatures = self._generate_signatures(message)
        if not signatures:
            return
        
        user_id = message.author.id
        channel_id = message.channel.id
        current_time = time.time()
        
        # Si el usuario ya fue marcado, borrar mensajes que repitan firma
        flagged_sigs = antiscam_db.get_flagged_signatures(guild_id, user_id)
        for sig in signatures:
            if sig in flagged_sigs:
                try:
                    await message.delete()
                except Exception:
                    pass
                return
        
        # Evitar alertas duplicadas en el mismo servidor
        if antiscam_db.is_alert_active(guild_id, user_id):
            return

        # Analizar cada firma
        for sig in signatures:
            antiscam_db.add_recent_message(guild_id, user_id, sig, channel_id, current_time)
            
            # Obtener canales únicos en los últimos 25 segundos
            unique_channels = antiscam_db.get_unique_channels(guild_id, user_id, sig, self.DETECTION_WINDOW)
            
            # Si se detectó en 3+ canales diferentes, ejecutar protocolo
            if len(unique_channels) >= self.CHANNEL_THRESHOLD:
                antiscam_db.add_active_alert(guild_id, user_id, current_time)
                await self.trigger_antiscam(message.author, message.guild, sig)
                break

    def _generate_signatures(self, message: discord.Message) -> list:
        """Genera firmas únicas del mensaje para detectar spam"""
        signatures = []
        content = message.content.strip()
        attachments = message.attachments
        
        # 1. Extraer URLs
        url_pattern = re.compile(r'https?://[^\s]+')
        found_urls = url_pattern.findall(content)
        for url in found_urls:
            signatures.append(f"URL_SIG_{url}")
        
        # 2. Guardar contenido de texto (si no es solo URLs)
        if content and len(content) >= 1:
            signatures.append(content)
        
        # 3. Extraer archivos adjuntos
        for att in attachments:
            att_signature = f"FILE_SIG_{att.filename}_{att.size}"
            signatures.append(att_signature)
        
        return signatures

    async def trigger_antiscam(self, member: discord.Member, guild: discord.Guild, signature: str):
        """Ejecuta el protocolo de seguridad - Una sola vez por usuario/servidor"""
        user_id = member.id
        guild_id = guild.id
        try:
            current_time = time.time()
            
            # Marcar esta firma para borrado continuo
            antiscam_db.add_flagged_signature(guild_id, user_id, signature, current_time)

            # Función de verificación para asegurar que solo se borre el spam coincidente
            def is_spam_msg(msg: discord.Message) -> bool:
                if msg.author.id != user_id:
                    return False
                sig_clean = signature.replace("URL_SIG_", "")
                match_text = (msg.content.strip() == signature or sig_clean in msg.content)
                match_att = any(f"FILE_SIG_{att.filename}_{att.size}" == signature for att in msg.attachments)
                return match_text or match_att

            # Borrar mensajes spam recientes coincidentes mediante purge (evita rate-limit)
            for channel in guild.text_channels:
                try:
                    await channel.purge(limit=50, check=is_spam_msg)
                except Exception:
                    pass
            
            # Obtener canal de alertas
            target_channel_id = db.get_config(guild_id, 'antiscam_channel', None)
            alert_channel = None
            
            if target_channel_id and target_channel_id != 'actual':
                try:
                    alert_channel = guild.get_channel(int(target_channel_id))
                except Exception:
                    pass
            
            if not alert_channel:
                alert_channel = member.guild.text_channels[0] if member.guild.text_channels else None
            
            # Enviar alerta al canal
            alert_text = f"🚨 Alerta @here - {member.mention} está siendo hackeado. Envía spam masivo de enlaces/archivos. Ignoren sus mensajes y no hagan clic. ¡Protejan sus cuentas!"
            
            if alert_channel:
                try:
                    await alert_channel.send(alert_text)
                except Exception:
                    pass

            # Verificar jerarquía de permisos
            is_owner = (guild.owner_id == member.id)
            is_higher_role = member.top_role >= guild.me.top_role
            original_nick = member.nick

            # Generar código de verificación
            verify_code = str(random.randint(1000, 9999))
            antiscam_db.add_pending_verification(user_id, guild_id, verify_code, original_nick, current_time)

            if is_owner or is_higher_role:
                # DM adaptado para Administradores / Owner
                dm_msg = (
                    f"⚠️ **¡Alerta de seguridad Staff/Owner en {guild.name}!**\n\n"
                    f"Detecté que tu cuenta de Administrador/Staff está enviando spam masivo.\n\n"
                    f"🔒 **Acciones tomadas:**\n"
                    f"- Se eliminaron los mensajes de spam detectados en el servidor.\n"
                    f"- *Nota:* Por tu rango de Staff/Owner no se aplicó muteo ni cambio de apodo automático.\n\n"
                    f"✅ **Pasos para recuperar y asegurar la cuenta:**\n"
                    f"1. Cambia tu contraseña e implementa autenticación en dos pasos (2FA) INMEDIATAMENTE.\n"
                    f"2. Revisa y revoca aplicaciones autorizadas sospechosas en tu Discord.\n"
                    f"3. Responde a este mensaje con tu código de verificación para limpiar la alerta del sistema y permitirte enviar mensajes.\n\n"
                    f"🔑 **Tu código de verificación es:** `{verify_code}`\n\n"
                    f"¡Cuida tu cuenta y la seguridad del servidor! 🤝"
                )
            else:
                # Sanciones normales para miembros regulares
                try:
                    current_name = member.display_name
                    new_nickname = f"⚠️ {current_name}"[:32]
                    await member.edit(nick=new_nickname, reason="Anti-scam: Cuenta posiblemente hackeada")
                except Exception as e:
                    logger.error(f"Error al cambiar apodo de {member}: {e}")

                try:
                    await member.timeout(self.TIMEOUT_DURATION, reason="Anti-scam: Spam masivo detectado")
                except Exception as e:
                    logger.error(f"Error o falta de permisos al aplicar timeout a {member}: {e}")

                # DM estándar para miembros regulares
                dm_msg = (
                    f"⚠️ **¡Alerta de seguridad en {guild.name}!**\n\n"
                    f"Detecté que tu cuenta está siendo hackeada y utilizada para enviar spam masivo.\n\n"
                    f"🔒 **Acciones tomadas:**\n"
                    f"- Muteo temporal de 7 días (para protegerte)\n"
                    f"- Cambio temporal de apodo\n"
                    f"- Eliminación de mensajes de spam\n\n"
                    f"✅ **Para recuperar tu cuenta:**\n"
                    f"1. Cambia tu contraseña INMEDIATAMENTE\n"
                    f"2. Revisa tu cuenta en otras plataformas\n"
                    f"3. Responde a este mensaje con tu código de verificación\n\n"
                    f"🔑 **Tu código de verificación es:** `{verify_code}`\n\n"
                    f"Una vez lo envíes, se te quitará el mute y se restaurará tu apodo.\n"
                    f"¡Cuídate y mucha suerte! 🤝"
                )

            # Enviar mensaje por DM
            try:
                await member.send(dm_msg)
            except Exception as e:
                logger.error(f"Error al enviar DM a {member}: {e}")

            logger.info(f'✅ Anti-Scam activado para: {member} en {guild.name}')
            
        except Exception as e:
            logger.error(f'Error en trigger_antiscam: {e}')
            antiscam_db.remove_active_alert(guild_id, user_id)

    async def handle_dm_verification(self, message: discord.Message):
        """Maneja la verificación por código enviado por DM"""
        user_id = message.author.id
        
        # Obtener verificación pendiente
        verification = antiscam_db.get_pending_verification(user_id)
        if not verification:
            return
        
        expected_code = str(verification['code'])
        guild_id = verification['guild_id']
        original_nick = verification.get('original_nick')
        
        # Verificar código
        if message.content.strip() == expected_code:
            try:
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    await message.channel.send("❌ No se pudo encontrar el servidor. Contacta a los admins.")
                    return
                
                member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                if not member:
                    await message.channel.send("❌ No se pudo encontrar tu perfil en el servidor.")
                    return
                
                # Quitar timeout si aplica
                try:
                    await member.timeout(None, reason="Verificación completada - Cuenta recuperada")
                except Exception:
                    pass
                
                # Restaurar apodo si aplica
                try:
                    await member.edit(nick=original_nick, reason="Cuenta verificada - Apodo restaurado")
                except Exception:
                    pass
                
                # Limpiar datos de spam
                antiscam_db.remove_pending_verification(user_id)
                antiscam_db.remove_active_alert(guild_id, user_id)
                
                # Confirmar en un solo mensaje
                confirm_msg = (
                    "✅ ¡Código correcto!\n\n"
                    "✔️ Se quitó la alerta de seguridad\n"
                    "✔️ Muteo y apodo restaurados (si aplicaban)\n"
                    "✔️ Mensajes habilitados de nuevo\n\n"
                    "¡Cuídate y protege tu cuenta! 🤝"
                )
                await message.channel.send(confirm_msg)
                
                logger.info(f'✅ Verificación completada: {user_id} en servidor {guild_id}')
                
            except Exception as e:
                logger.error(f'Error en verificación: {e}')
                await message.channel.send("❌ Error técnico. Contacta a un administrador.")
        else:
            await message.channel.send("❌ Código incorrecto. Envía solo el número de 4 dígitos.")

    @commands.hybrid_command(name='antiscam')
    @commands.has_permissions(administrator=True)
    @app_commands.describe(action='Qué hacer con el sistema anti-scam')
    @app_commands.choices(action=[app_commands.Choice(name='activar', value='activate'), app_commands.Choice(name='desactivar', value='deactivate')])
    async def antiscam_cmd(self, ctx, action: str = None):
        """Comando para gestionar antiscam del servidor"""
        guild_id = ctx.guild.id
        
        if not action:
            status = db.get_config(guild_id, 'antiscam_enabled', 'false')
            state_txt = '✅ Activado' if status == 'true' else '❌ Desactivado'
            
            embed = discord.Embed(
                title='🛡️ Sistema Anti-Scam',
                description='Protección contra cuentas hackeadas',
                color=discord.Color.red()
            )
            embed.add_field(name='Estado', value=state_txt, inline=False)
            embed.add_field(
                name='📊 Funcionamiento',
                value='Detecta cuando el mismo mensaje/archivo se envía en 3+ canales en 25 segundos\n'
                      '• Aplica mute de 7 días\n'
                      '• Envía código de verificación por DM\n'
                      '• Restaura cuenta cuando se verifica',
                inline=False
            )
            embed.add_field(name='Comandos', value='`!antiscam activate` - Activar\n`!antiscam deactivate` - Desactivar', inline=False)
            
            await ctx.send(embed=embed)
            return
        
        action = action.lower()
        if action == 'activate':
            db.set_config(guild_id, 'antiscam_enabled', 'true')
            embed = discord.Embed(
                title='✅ Anti-Scam Activado',
                description='La protección está activa en este servidor',
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            db.add_audit_log(guild_id, 'ANTISCAM_ENABLED', 0, ctx.author.id)
            
        elif action == 'deactivate':
            db.set_config(guild_id, 'antiscam_enabled', 'false')
            embed = discord.Embed(
                title='❌ Anti-Scam Desactivado',
                description='La protección está inactiva en este servidor',
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            db.add_audit_log(guild_id, 'ANTISCAM_DISABLED', 0, ctx.author.id)
            
        else:
            await ctx.send('❌ Acción no reconocida. Usa `activate` o `deactivate`')

async def setup(bot):
    await bot.add_cog(AntiScam(bot))