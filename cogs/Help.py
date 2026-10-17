import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Select
import unicodedata

# Icono + nombre bonito + alias de búsqueda por cog. Si aparece un cog nuevo que no está
# aquí, se usa el fallback de abajo (📁 + nombre del cog tal cual) para que nunca falte.
CATEGORIAS_INFO = {
    'IA':          {'emoji': '🤖', 'nombre': 'IA / MaxIA',   'desc': 'Todo lo relacionado a hablar con Max, memoria, cumpleaños, apodos y jerga.',
                     'alias': ['ia', 'maxia', 'max', 'ai']},
    'Moderation':  {'emoji': '🛡️', 'nombre': 'Moderación',  'desc': 'Warns, mutes, bans, purge y nuke.',
                     'alias': ['mod', 'moderacion', 'moderation', 'moderación']},
    'Config':      {'emoji': '⚙️', 'nombre': 'Configuración', 'desc': 'Menú de ajustes del server (!config).',
                     'alias': ['config', 'configuracion', 'configuración', 'ajustes', 'settings']},
    'AntiScam':    {'emoji': '🚨', 'nombre': 'Anti-Scam',    'desc': 'Protección contra spam/cuentas hackeadas.',
                     'alias': ['antiscam', 'scam', 'anti-scam']},
    'EightBall':   {'emoji': '🎱', 'nombre': 'Bola 8',       'desc': 'Pregúntale a la bola mágica.',
                     'alias': ['8ball', 'bola8', 'eightball', 'bola']},
    'Markov':      {'emoji': '🧠', 'nombre': 'Cerebro de Frases', 'desc': 'Generador de frases random aprendidas del chat.',
                     'alias': ['markov', 'frases', 'cerebro']},
    'Music':       {'emoji': '🎵', 'nombre': 'Música',       'desc': 'Reproducir música en canal de voz.',
                     'alias': ['musica', 'music', 'música']},
}
FALLBACK_EMOJI = '📁'

# Comandos que no queremos mostrar en el panel aunque existan (además de los marcados hidden=True)
COMANDOS_OCULTOS = {'help'}


def _normalizar(texto: str) -> str:
    """Quita acentos y pasa a minúsculas, para que 'música'/'musica'/'MUSIC' etc. hagan match igual."""
    if not texto:
        return ''
    sin_acentos = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    return sin_acentos.lower().strip()


class HelpMenu(View):
    """Menú desplegable para navegar entre categorías del panel de ayuda"""

    def __init__(self, cog_help: 'Help', ctx, categorias: dict):
        super().__init__(timeout=120)
        self.cog_help = cog_help
        self.ctx = ctx
        self.categorias = categorias
        self.message = None

        options = [
            discord.SelectOption(
                label='Inicio',
                value='__home__',
                emoji='🏠',
                description='Volver al menú principal'
            )
        ]
        for cog_name in categorias:
            info = CATEGORIAS_INFO.get(cog_name, {'emoji': FALLBACK_EMOJI, 'nombre': cog_name})
            options.append(discord.SelectOption(
                label=info['nombre'],
                value=cog_name,
                emoji=info['emoji'],
                description=f"{len(categorias[cog_name])} comando(s)"
            ))

        select = Select(placeholder='📖 Selecciona una categoría...', options=options)

        async def on_select(interaction: discord.Interaction):
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.send_message('❌ Este panel no es tuyo, usa `!max help` para el tuyo.', ephemeral=True)
                return

            valor = select.values[0]
            if valor == '__home__':
                embed = self.cog_help.build_home_embed(self.ctx, self.categorias)
            else:
                embed = self.cog_help.build_category_embed(self.ctx, valor, self.categorias[valor])

            await interaction.response.edit_message(embed=embed, view=self)

        select.callback = on_select
        self.add_item(select)

    async def on_timeout(self):
        if self.message:
            try:
                for item in self.children:
                    item.disabled = True
                await self.message.edit(view=self)
            except Exception:
                pass


class Help(commands.Cog):
    """Panel de ayuda visual, reemplaza el !help feo de la librería"""

    def __init__(self, bot):
        self.bot = bot
        bot.help_command = None  # Se desactiva el !help default de discord.py

    def _comandos_visibles_de(self, cog):
        return [c for c in cog.get_commands() if not c.hidden and c.name not in COMANDOS_OCULTOS]

    async def _agrupar_por_categoria(self, ctx) -> dict:
        """Junta los comandos por cog, solo los que el usuario puede usar (respeta los checks de
        permisos) y que tengan al menos un comando visible."""
        categorias = {}
        for cog_name, cog in self.bot.cogs.items():
            visibles = []
            for cmd in self._comandos_visibles_de(cog):
                try:
                    if await cmd.can_run(ctx):
                        visibles.append(cmd)
                except commands.CommandError:
                    continue          # no cumple permisos/checks: no se le muestra
                except Exception:
                    visibles.append(cmd)
            if visibles:
                categorias[cog_name] = visibles
        return categorias

    def build_home_embed(self, ctx, categorias: dict) -> discord.Embed:
        embed = discord.Embed(
            title='📖 Panel de Ayuda',
            description=(
                f'Wenas {ctx.author.mention}, soy **MaxIA**. Usa el menú de abajo para ver los comandos '
                'por categoría, o tráete `!max help <comando>` para el detalle de uno en específico.'
            ),
            color=discord.Color.blurple()
        )
        for cog_name, comandos in categorias.items():
            info = CATEGORIAS_INFO.get(cog_name, {'emoji': FALLBACK_EMOJI, 'nombre': cog_name, 'desc': ''})
            embed.add_field(
                name=f"{info['emoji']} {info['nombre']}",
                value=f"{info.get('desc', '')}\n`{len(comandos)}` comando(s)".strip(),
                inline=False
            )
        if self.bot.user and self.bot.user.avatar:
            embed.set_thumbnail(url=self.bot.user.avatar.url)
        embed.set_footer(text='Prefijo: !max  •  Ej: !max warn @usuario razón')
        return embed

    def build_category_embed(self, ctx, cog_name: str, comandos: list) -> discord.Embed:
        info = CATEGORIAS_INFO.get(cog_name, {'emoji': FALLBACK_EMOJI, 'nombre': cog_name, 'desc': ''})
        embed = discord.Embed(
            title=f"{info['emoji']} {info['nombre']}",
            description=info.get('desc', ''),
            color=discord.Color.blurple()
        )
        ordenados = sorted(comandos, key=lambda c: c.name)
        if len(ordenados) <= 20:
            for cmd in ordenados:
                firma = f'!max {cmd.name} {cmd.signature}'.strip()
                descripcion = (cmd.help or 'Sin descripción.').split('\n')[0]
                embed.add_field(name=f'`{firma}`'[:256], value=descripcion[:1024], inline=False)
        else:
            # Discord solo permite 25 fields por embed: con muchos comandos (ej. Música) se usa texto corrido
            lineas = []
            for cmd in ordenados:
                firma = f'!max {cmd.name} {cmd.signature}'.strip()
                descripcion = (cmd.help or 'Sin descripción.').split('\n')[0]
                lineas.append(f'`{firma}` — {descripcion}')
            texto = ''
            for n, linea in enumerate(lineas):
                if len(texto) + len(linea) + 1 > 3700:
                    texto += f'\n…y {len(lineas) - n} más.'
                    break
                texto += linea + '\n'
            embed.description = ((info.get('desc', '') + '\n\n') if info.get('desc') else '') + texto
        embed.set_footer(text='Prefijo: !max  •  Usa el menú para cambiar de categoría')
        return embed

    def build_command_embed(self, ctx, cmd: commands.Command) -> discord.Embed:
        firma = f'!max {cmd.name} {cmd.signature}'.strip()
        embed = discord.Embed(
            title=f'📖 Comando: {cmd.name}',
            description=cmd.help or 'Sin descripción.',
            color=discord.Color.blurple()
        )
        embed.add_field(name='Uso', value=f'`{firma}`', inline=False)
        if cmd.aliases:
            embed.add_field(name='Alias', value=', '.join(f'`{a}`' for a in cmd.aliases), inline=False)
        cog_name = cmd.cog_name
        if cog_name:
            info = CATEGORIAS_INFO.get(cog_name, {'emoji': FALLBACK_EMOJI, 'nombre': cog_name})
            embed.add_field(name='Categoría', value=f"{info['emoji']} {info['nombre']}", inline=False)
        return embed

    def _resolver_categoria(self, texto: str, categorias: dict):
        """Busca una categoría por nombre de cog, nombre bonito o alias, sin importar
        mayúsculas/acentos ('musica', 'MUSIC', 'música' -> todas encuentran 'Music')."""
        query = _normalizar(texto)
        if not query:
            return None

        for cog_name in categorias:
            info = CATEGORIAS_INFO.get(cog_name, {})
            candidatos = [cog_name, info.get('nombre', '')] + info.get('alias', [])
            if any(_normalizar(c) == query for c in candidatos):
                return cog_name

        # Si no hubo match exacto, se intenta uno parcial (ej. "mod" ya cubierto arriba,
        # pero por si escriben algo como "moder" sin terminar la palabra)
        for cog_name in categorias:
            info = CATEGORIAS_INFO.get(cog_name, {})
            candidatos = [cog_name, info.get('nombre', '')] + info.get('alias', [])
            if any(query in _normalizar(c) or _normalizar(c) in query for c in candidatos if c):
                return cog_name

        return None

    @commands.hybrid_command(name='help', aliases=['ayuda'])
    @app_commands.describe(comando='Categoría o comando de la ayuda (opcional)')
    async def help_cmd(self, ctx, *, comando: str = None):
        """Muestra este panel de ayuda. Usa !max help <categoría o comando> para ir directo."""
        if comando:
            categorias = await self._agrupar_por_categoria(ctx)

            # Primero se checa si lo que pusieron es una categoría (!max help musica, mod, etc.)
            cat_encontrada = self._resolver_categoria(comando, categorias)
            if cat_encontrada:
                embed = self.build_category_embed(ctx, cat_encontrada, categorias[cat_encontrada])
                view = HelpMenu(self, ctx, categorias)
                view.message = await ctx.send(embed=embed, view=view)
                return

            # Si no es categoría, se busca como comando puntual (!max help ban, etc.)
            cmd = self.bot.get_command(comando)
            if not cmd or cmd.hidden or cmd.name in COMANDOS_OCULTOS:
                await ctx.send(f'❌ No encontré ninguna categoría ni comando llamado `{comando}`.')
                return
            await ctx.send(embed=self.build_command_embed(ctx, cmd))
            return

        categorias = await self._agrupar_por_categoria(ctx)
        if not categorias:
            await ctx.send('❌ No hay comandos disponibles ahorita.')
            return

        embed = self.build_home_embed(ctx, categorias)
        view = HelpMenu(self, ctx, categorias)
        view.message = await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(Help(bot))