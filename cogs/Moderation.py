import discord
from discord import app_commands
from discord.ext import commands
from database import db
from utils import extract_user_mention, is_admin_or_mod, format_variables
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

# Mensaje por defecto que le llega al usuario por MD cuando se le sanciona.
# La parte "durante [tiempo]" se quita sola si la sanción no tiene duración (ej. baneos).
DEFAULT_SANCTION_DM = 'Wenas [usuario] fuiste [sancion] de [nombredelserver] por [motivo] durante [tiempo]'


class Moderation(commands.Cog):
    """Cog de moderación"""

    def __init__(self, bot):
        self.bot = bot

    def _jerarquia_ok(self, ctx, member: discord.Member):
        """Devuelve None si se puede sancionar a `member`, o el texto del motivo si no."""
        if member.id == ctx.guild.owner_id:
            return '❌ No se puede sancionar al dueño del servidor.'
        if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
            return '❌ Ese usuario tiene un rol igual o superior al tuyo.'
        if member.top_role >= ctx.guild.me.top_role:
            return '❌ Ese usuario tiene un rol igual o superior al mío, no puedo sancionarlo.'
        return None

    async def get_channel_for(self, ctx, tipo: str):
        """Obtiene el canal configurado para avisar 'warn', 'ban' o 'mute' (suspensión).
        Si no hay uno configurado específico para ese tipo, usa el canal general de
        sanciones (compatibilidad con configuraciones viejas) y si tampoco hay, el canal actual."""
        guild_id = ctx.guild.id
        clave = {'warn': 'warn_channel', 'ban': 'ban_channel', 'mute': 'mute_channel'}[tipo]
        channel_id = db.get_config(guild_id, clave, '')

        if not channel_id or channel_id == 'actual':
            # Compatibilidad: si no se configuró el canal específico, usa el viejo 'sanction_channel'
            channel_id = db.get_config(guild_id, 'sanction_channel', 'actual') if tipo != 'warn' else 'actual'

        if channel_id == 'actual' or not channel_id:
            return ctx.channel
        try:
            canal = ctx.guild.get_channel(int(channel_id))
            return canal or ctx.channel
        except Exception:
            return ctx.channel

    # Se mantiene por compatibilidad con el resto del código que aún llame a esto
    async def get_sanction_channel(self, ctx):
        return await self.get_channel_for(ctx, 'ban')

    def _formatear_tiempo(self, dias: int) -> str:
        if dias == 1:
            return '1 día'
        return f'{dias} días'

    async def enviar_dm_sancion(self, member: discord.Member, guild: discord.Guild, sancion: str, motivo: str, dias: int = None):
        """Manda por MD el aviso de sanción al usuario, usando el mensaje personalizado del
        server (o el default). Si `dias` es None, la parte 'durante [tiempo]' se quita sola."""
        guild_id = guild.id
        template = db.get_config(guild_id, 'sanction_dm_message', DEFAULT_SANCTION_DM)

        if dias is not None:
            tiempo_txt = self._formatear_tiempo(dias)
        else:
            # Sin duración (ej. ban) -> se quita la frase "durante [tiempo]" completa
            template = template.replace(' durante [tiempo]', '').replace('durante [tiempo]', '')
            tiempo_txt = ''

        mensaje = format_variables(
            template,
            usuario=member.display_name,
            nombredelserver=guild.name,
            sancion=sancion,
            motivo=motivo,
            tiempo=tiempo_txt
        )

        try:
            await member.send(mensaje)
        except Exception as e:
            logger.info(f'No se pudo mandar MD de sanción a {member.name}: {e}')

    @commands.hybrid_command(name='warn')
    @commands.has_permissions(moderate_members=True)
    @app_commands.describe(member='Usuario a advertir', reason='Motivo')
    async def warn(self, ctx, member: discord.Member, *, reason: str = 'Sin razón'):
        """Dar warn a un usuario"""

        # Verificar que no sea el mismo bot ni que intente advertirse a sí mismo
        if member.bot or member == ctx.author:
            await ctx.send('❌ No puedes dar warn a ese usuario.')
            return

        guild_id = ctx.guild.id

        # Añadir warn
        db.add_warn(guild_id, member.id, ctx.author.id, reason)
        warn_count = db.get_warn_count(guild_id, member.id)
        try:
            warn_limit = int(db.get_config(guild_id, 'warn_limit', '3'))
        except (TypeError, ValueError):
            warn_limit = 3

        # Crear mensaje
        embed = discord.Embed(
            title='⚠️ Warn Aplicado',
            color=discord.Color.orange()
        )
        embed.add_field(name='Usuario', value=member.mention, inline=True)
        embed.add_field(name='Razón', value=reason, inline=True)
        embed.add_field(name='Warns', value=f'{warn_count}/{warn_limit}', inline=True)
        embed.add_field(name='Moderador', value=ctx.author.mention, inline=True)

        # Notificar
        warn_channel = await self.get_channel_for(ctx, 'warn')
        await warn_channel.send(embed=embed)

        # Registrar en auditoría
        db.add_audit_log(
            guild_id, 'WARN', member.id, ctx.author.id,
            f'Razón: {reason}'
        )

        # Verificar si se debe aplicar sanción
        if warn_count >= warn_limit:
            sanction_type = db.get_config(guild_id, 'sanction_type', 'mute')

            problema = self._jerarquia_ok(ctx, member)
            if problema:
                await ctx.send(f'⚠️ {member.mention} llegó al límite de warns pero no pude sancionarlo. {problema}')
                return

            razon_auto = f'Warns excedidos ({warn_count}/{warn_limit})'
            if sanction_type == 'ban':
                aplicada = await self.ban_user(ctx, member, razon_auto)
            else:
                aplicada = await self.suspend_user(ctx, member, razon_auto)

            # Solo se limpian los warns (y se avisa) si la sanción SÍ se aplicó
            if aplicada:
                db.delete_warns(guild_id, member.id)
                await ctx.send(f'🚫 Sanción automática aplicada a {member.mention}')

    async def suspend_user(self, ctx, member: discord.Member, reason: str = 'Sin razón', dias: int = None):
        """Suspende (timeout nativo de Discord) a un usuario en vez del viejo sistema de rol Muted.
        Si no se especifica `dias`, se usa el máximo configurado por el server (7 días por default)."""
        guild_id = ctx.guild.id

        max_dias = int(db.get_config(guild_id, 'suspension_max_days', '7'))
        if dias is None:
            dias = max_dias
        else:
            dias = max(1, min(dias, max_dias))

        try:
            await member.timeout(timedelta(days=dias), reason=reason)
        except Exception as e:
            await ctx.send(f'❌ No pude suspender a {member.mention}: {e}')
            return False

        # Registrar en BD
        cursor = db.get_connection(guild_id).cursor()
        cursor.execute(
            'INSERT INTO mutes (user_id, moderator_id, reason) VALUES (?, ?, ?)',
            (member.id, ctx.author.id, reason)
        )
        db.get_connection(guild_id).commit()

        # Aviso por MD al usuario (con el mensaje personalizable del server)
        await self.enviar_dm_sancion(member, ctx.guild, 'suspendido', reason, dias=dias)

        # Mensaje en el canal de mutes/suspensiones
        embed = discord.Embed(
            title='🔇 Usuario Suspendido',
            color=discord.Color.red()
        )
        embed.add_field(name='Usuario', value=member.mention, inline=True)
        embed.add_field(name='Duración', value=self._formatear_tiempo(dias), inline=True)
        embed.add_field(name='Razón', value=reason, inline=True)
        embed.add_field(name='Moderador', value=ctx.author.mention, inline=True)

        mute_channel = await self.get_channel_for(ctx, 'mute')
        await mute_channel.send(embed=embed)

        # Auditoría
        db.add_audit_log(guild_id, 'MUTE', member.id, ctx.author.id, f'{reason} ({dias} días)')
        return True

    # Alias por si algo más del código todavía llama a mute_user
    mute_user = suspend_user

    @commands.hybrid_command(name='unmute')
    @commands.has_permissions(moderate_members=True)
    @app_commands.describe(member='Usuario')
    async def unmute(self, ctx, member: discord.Member):
        """Quitar la suspensión (timeout) a un usuario"""
        if member.is_timed_out():
            try:
                await member.timeout(None, reason=f'Desuspendido por {ctx.author}')
            except Exception as e:
                await ctx.send(f'❌ No pude quitar la suspensión: {e}')
                return

            embed = discord.Embed(
                title='🔊 Usuario Desuspendido',
                color=discord.Color.green()
            )
            embed.add_field(name='Usuario', value=member.mention)

            await ctx.send(embed=embed)
            db.add_audit_log(ctx.guild.id, 'UNMUTE', member.id, ctx.author.id)
        else:
            await ctx.send('❌ El usuario no está suspendido.')

    async def ban_user(self, ctx, member: discord.Member, reason: str = 'Sin razón') -> bool:
        """Banear usuario. Devuelve True si el ban se aplicó."""
        guild_id = ctx.guild.id

        # Aviso por MD ANTES de banear (después de banear ya no le podemos escribir)
        await self.enviar_dm_sancion(member, ctx.guild, 'baneado', reason, dias=None)

        # Banear (si falla, no se registra nada ni se avisa como si hubiera funcionado)
        try:
            await ctx.guild.ban(member, reason=reason)
        except discord.Forbidden:
            await ctx.send(f'❌ No tengo permisos para banear a {member.mention}.')
            return False
        except discord.HTTPException as e:
            await ctx.send(f'❌ No pude banear a {member.mention}: {e}')
            return False

        # Crear registro en BD
        cursor = db.get_connection(guild_id).cursor()
        cursor.execute(
            'INSERT INTO bans (user_id, moderator_id, reason) VALUES (?, ?, ?)',
            (member.id, ctx.author.id, reason)
        )
        db.get_connection(guild_id).commit()

        # Mensaje
        embed = discord.Embed(
            title='🚫 Usuario Baneado',
            color=discord.Color.dark_red()
        )
        embed.add_field(name='Usuario', value=member.mention, inline=True)
        embed.add_field(name='Razón', value=reason, inline=True)
        embed.add_field(name='Moderador', value=ctx.author.mention, inline=True)

        ban_channel = await self.get_channel_for(ctx, 'ban')
        await ban_channel.send(embed=embed)

        # Auditoría
        db.add_audit_log(guild_id, 'BAN', member.id, ctx.author.id, reason)
        return True

    @commands.hybrid_command(name='mute')
    @commands.has_permissions(moderate_members=True)
    @app_commands.describe(member='Usuario a suspender', dias='Días de suspensión (opcional)', reason='Motivo')
    async def mute_cmd(self, ctx, member: discord.Member, dias: int = None, *, reason: str = 'Sin razón'):
        """Suspender a un usuario. Si no pones días, se usa el máximo configurado (7 por default)."""
        if member == ctx.author or member.bot:
            await ctx.send('❌ No puedes suspender a ese usuario.')
            return

        problema = self._jerarquia_ok(ctx, member)
        if problema:
            await ctx.send(problema)
            return

        if await self.suspend_user(ctx, member, reason, dias=dias):
            await ctx.send(f'🔇 {member.mention} ha sido suspendido.')

    @commands.hybrid_command(name='ban')
    @commands.has_permissions(ban_members=True)
    @app_commands.describe(member='Usuario a banear', reason='Motivo')
    async def ban_cmd(self, ctx, member: discord.Member, *, reason: str = 'Sin razón'):
        """Banear a un usuario"""
        if member == ctx.author or member.bot:
            await ctx.send('❌ No puedes banear a ese usuario.')
            return

        problema = self._jerarquia_ok(ctx, member)
        if problema:
            await ctx.send(problema)
            return

        if await self.ban_user(ctx, member, reason):
            await ctx.send(f'🚫 {member.mention} ha sido baneado.')

    @commands.hybrid_command(name='warns')
    @app_commands.describe(member='Usuario')
    async def warns(self, ctx, member: discord.Member = None):
        """Ver warns de un usuario"""
        member = member or ctx.author
        guild_id = ctx.guild.id

        warns = db.get_warns(guild_id, member.id)

        if not warns:
            await ctx.send(f'{member.mention} no tiene warns.')
            return

        embed = discord.Embed(
            title=f'⚠️ Warns - {member.name}',
            color=discord.Color.orange()
        )

        warns = list(warns)
        total_warns = len(warns)
        warns = warns[-10:]  # un embed admite máximo 25 fields; se muestran los 10 más recientes
        for i, warn in enumerate(warns, total_warns - len(warns) + 1):
            embed.add_field(
                name=f'Warn #{i}',
                value=f'**Razón:** {warn[3]}\n**Fecha:** {warn[4]}',
                inline=False
            )

        await ctx.send(embed=embed)

    @commands.hybrid_command(name='clearwarns')
    @commands.has_permissions(administrator=True)
    @app_commands.describe(member='Usuario')
    async def clearwarns(self, ctx, member: discord.Member):
        """Limpiar warns de un usuario"""
        db.delete_warns(ctx.guild.id, member.id)
        await ctx.send(f'✅ Warns de {member.mention} borrados.')
        db.add_audit_log(ctx.guild.id, 'CLEAR_WARNS', member.id, ctx.author.id)

    # --- PURGE (borrado masivo) ---
    MAX_PURGE = 500

    @commands.hybrid_command(name='purge')
    @commands.has_permissions(administrator=True)
    @app_commands.describe(cantidad='Cuántos mensajes borrar')
    async def purge(self, ctx, cantidad: int = None):
        """Borra de golpe hasta 500 mensajes del canal donde se ejecuta. Uso: !purge <cantidad>"""
        if cantidad is None or cantidad <= 0:
            await ctx.send(f'❌ Uso: `!purge <cantidad>` (máximo {self.MAX_PURGE})')
            return

        cantidad = min(cantidad, self.MAX_PURGE)

        try:
            # +1 para que también se lleve el mensaje del propio comando (en slash no existe ese mensaje)
            extra = 1 if ctx.interaction is None else 0
            borrados = await ctx.channel.purge(limit=cantidad + extra)
        except discord.Forbidden:
            await ctx.send('❌ No tengo permisos para borrar mensajes en este canal.')
            return
        except Exception as e:
            await ctx.send(f'❌ No pude completar la purga: {e}')
            return

        total = max(0, len(borrados) - extra)
        db.add_audit_log(
            ctx.guild.id, 'PURGE', ctx.author.id, ctx.author.id,
            f'{total} mensajes borrados en #{ctx.channel.name}'
        )
        await ctx.send(f'🧹 {total} mensajes borrados.', delete_after=5)


    # --- NUKE (resetea el canal desde cero) ---
    async def is_nuke_allowed(self, ctx) -> bool:
        """Solo el owner del server puede usar nuke por default. Además de él, se puede
        autorizar a usuarios y/o roles específicos desde !config (o guardándolos directo
        en 'nuke_extra_users'/'nuke_extra_roles', listas de IDs separadas por coma)."""
        guild = ctx.guild
        if ctx.author.id == guild.owner_id:
            return True

        extra_users = db.get_config(guild.id, 'nuke_extra_users', '')
        if extra_users:
            ids_usuarios = {u.strip() for u in extra_users.split(',') if u.strip()}
            if str(ctx.author.id) in ids_usuarios:
                return True

        extra_roles = db.get_config(guild.id, 'nuke_extra_roles', '')
        if extra_roles:
            ids_roles = {r.strip() for r in extra_roles.split(',') if r.strip()}
            if any(str(role.id) in ids_roles for role in ctx.author.roles):
                return True

        return False

    @commands.hybrid_command(name='nuke')
    async def nuke(self, ctx):
        """Borra el canal actual y lo vuelve a crear igualito (mismo nombre, categoría,
        permisos y posición). Solo el owner del server (o quien se haya autorizado en
        !config) puede usarlo."""
        if not await self.is_nuke_allowed(ctx):
            await ctx.send('❌ Solo el owner del servidor (o alguien autorizado para nuke) puede usar esto.')
            return

        canal = ctx.channel
        posicion = canal.position
        nombre_original = canal.name
        razon = f'Nuke pedido por {ctx.author} ({ctx.author.id})'

        nuevo_canal = None
        try:
            nuevo_canal = await canal.clone(reason=razon)
            await nuevo_canal.edit(position=posicion)
            await canal.delete(reason=razon)
        except Exception as e:
            # Si el clon ya se creó pero el original no se pudo borrar, se quita el clon para no dejar canales duplicados
            if nuevo_canal is not None:
                try:
                    await nuevo_canal.delete(reason='Nuke fallido: se revierte el clon')
                except Exception:
                    pass
            if isinstance(e, discord.Forbidden):
                await ctx.send('❌ No tengo permisos para borrar/crear canales aquí.')
            else:
                await ctx.send(f'❌ No pude completar el nuke: {e}')
            return

        db.add_audit_log(ctx.guild.id, 'NUKE', ctx.author.id, ctx.author.id, f'Canal #{nombre_original} reseteado')

        embed = discord.Embed(
            title='💥 Canal reiniciado',
            description=f'{ctx.author.mention} le metió nuke a este canal.',
            color=discord.Color.dark_red()
        )
        await nuevo_canal.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Moderation(bot))