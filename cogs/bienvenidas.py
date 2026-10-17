import discord
from discord.ext import commands
from database import db
from utils import format_variables
import asyncio
import logging

logger = logging.getLogger(__name__)

class Welcome(commands.Cog):
    """Cog de bienvenidas y despedidas"""
    
    def __init__(self, bot):
        self.bot = bot
        # Mensajes por defecto
        self.default_welcome = 'Wenas [usuario] Bienvenido a [nombredelserver]'
        self.default_farewell = 'Pipipi se nos fue [usuario] hasta pronto :c'
        self.default_ban_farewell = 'Hasta nunca [usuario] fuiste mal integrante aqui >:c'
    
    async def get_configured_channel(self, guild: discord.Guild, config_key: str) -> discord.TextChannel:
        """Obtiene un canal guardado en la BD para esa clave (ej. 'welcome_channel' o
        'farewell_channel'), validando que el bot tenga permiso de mandar ahí."""
        channel_id = db.get_config(guild.id, config_key, '')
        if channel_id:
            try:
                ch = guild.get_channel(int(channel_id))
                if ch and ch.permissions_for(guild.me).send_messages:
                    return ch
            except ValueError:
                pass
        return None

    async def get_welcome_channel(self, guild: discord.Guild) -> discord.TextChannel:
        """Canal de BIENVENIDAS (Prioridad: BD > Nombre > Primer canal disponible)"""
        guild_id = guild.id

        configurado = await self.get_configured_channel(guild, 'welcome_channel')
        if configurado:
            return configurado

        # Buscar canal por palabra clave "bienvenida" o "welcome"
        for channel in guild.channels:
            if isinstance(channel, discord.TextChannel):
                if 'bienvenida' in channel.name.lower() or 'welcome' in channel.name.lower():
                    if channel.permissions_for(guild.me).send_messages:
                        return channel

        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                return channel

        return None

    async def get_farewell_channel(self, guild: discord.Guild) -> discord.TextChannel:
        """Canal de DESPEDIDAS (Prioridad: BD propio > canal de bienvenidas > primer canal disponible).
        Se usa tanto para despedidas normales como para despedidas por ban."""
        guild_id = guild.id

        configurado = await self.get_configured_channel(guild, 'farewell_channel')
        if configurado:
            return configurado

        # Si no configuraron uno específico para despedidas, cae al mismo de bienvenidas (compatibilidad)
        return await self.get_welcome_channel(guild)
    
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Evento cuando un usuario se une al servidor"""
        guild_id = member.guild.id
        
        # Verificar si la función está activada
        if db.get_config(guild_id, 'welcome_enabled', 'true') != 'true':
            return
        
        # Obtener mensaje personalizado guardado o usar el predeterminado
        welcome_msg = db.get_config(
            guild_id, 'welcome_message',
            self.default_welcome
        )
        
        # Contar miembros (sin bots y total)
        total_miembros = sum(1 for m in member.guild.members if not m.bot)
        all_members = len(member.guild.members)
        
        # Reemplazar las variables personalizadas
        welcome_msg = format_variables(
            welcome_msg,
            usuario=member.mention,
            nombredelserver=member.guild.name,
            totalmiembros=total_miembros,
            allmembers=all_members
        )
        
        # Obtener URL de imagen si existe
        welcome_image = db.get_config(guild_id, 'welcome_image', None)
        
        # Crear embed de bienvenida
        embed = discord.Embed(
            description=welcome_msg,
            color=discord.Color.green()
        )
        
        if welcome_image and str(welcome_image).startswith(('http://', 'https://')):
            embed.set_image(url=welcome_image)
        
        # Obtener canal y enviar
        channel = await self.get_welcome_channel(member.guild)
        if channel:
            try:
                await channel.send(embed=embed)
                logger.info(f'Bienvenido {member.name} en {member.guild.name}')
            except Exception as e:
                logger.error(f'Error enviando bienvenida: {e}')
    
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Evento cuando un usuario sale o es expulsado/baneado"""
        guild_id = member.guild.id
        
        # Verificar si las despedidas están activadas
        if db.get_config(guild_id, 'farewell_enabled', 'true') != 'true':
            return
        
        # Verificar si la salida fue por un ban. El evento de salida llega a veces ANTES de que el ban
        # quede registrado, así que se espera un momento; y sin permiso "Banear miembros" no truena.
        await asyncio.sleep(1.5)
        is_banned = False
        try:
            await member.guild.fetch_ban(member)
            is_banned = True
        except discord.NotFound:
            is_banned = False
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.warning(f'No pude verificar si {member.name} fue baneado: {e}')
        
        # Seleccionar mensaje correspondiente
        if is_banned:
            if db.get_config(guild_id, 'ban_farewell_enabled', 'true') != 'true':
                return
            farewell_msg = db.get_config(
                guild_id, 'ban_farewell_message',
                self.default_ban_farewell
            )
        else:
            farewell_msg = db.get_config(
                guild_id, 'farewell_message',
                self.default_farewell
            )
        
        # Contar miembros actualizados
        total_miembros = sum(1 for m in member.guild.members if not m.bot)
        all_members = len(member.guild.members)
        
        # Reemplazar las variables personalizadas
        farewell_msg = format_variables(
            farewell_msg,
            usuario=member.mention,
            nombredelserver=member.guild.name,
            totalmiembros=total_miembros,
            allmembers=all_members
        )
        
        # Obtener URL de imagen si existe
        farewell_image = db.get_config(
            guild_id,
            'ban_farewell_image' if is_banned else 'farewell_image',
            None
        )
        
        # Crear embed de despedida
        embed = discord.Embed(
            description=farewell_msg,
            color=discord.Color.red() if is_banned else discord.Color.gold()
        )
        
        if farewell_image and str(farewell_image).startswith(('http://', 'https://')):
            embed.set_image(url=farewell_image)
        
        # Obtener canal (despedidas, separado del de bienvenidas) y enviar
        channel = await self.get_farewell_channel(member.guild)
        if channel:
            try:
                await channel.send(embed=embed)
                logger.info(f'Despedida {member.name} en {member.guild.name}')
            except Exception as e:
                logger.error(f'Error enviando despedida: {e}')

async def setup(bot):
    await bot.add_cog(Welcome(bot))