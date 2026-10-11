import discord
from discord.ext import commands
from discord.ui import View, Select, Button
from database import db
from utils import check_similarity, format_variables, parse_variables_help
import logging
import asyncio
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

SANCTION_DM_VARS_HELP = (
    'Variables disponibles: [usuario], [nombredelserver], [sancion], [motivo], [tiempo]\n'
    '"durante [tiempo]" se quita solo en baneos (no tienen duración).'
)

class ConfigMenu(View):
    """Menú de configuración interactivo"""
    
    def __init__(self, ctx, category: str = None):
        super().__init__()
        self.ctx = ctx
        self.category = category
        self.message = None
    
    @discord.ui.select(
        placeholder='Selecciona qué configurar...',
        options=[
            discord.SelectOption(label='TTS (Text-to-Speech)', value='tts', emoji='🔊'),
            discord.SelectOption(label='Cerebro de Frases (Markov)', value='markov', emoji='🧠'),
            discord.SelectOption(label='Bienvenidas/Despedidas', value='welcome', emoji='👋'),
            discord.SelectOption(label='Warns y Sanciones', value='warns', emoji='⚠️'),
            discord.SelectOption(label='Protección Anti-Scam', value='antiscam', emoji='🛡️'),
            discord.SelectOption(label='Canales de Avisos', value='channels', emoji='📢'),
            discord.SelectOption(label='Roles Admin/Mod', value='roles', emoji='🛡️'),
            discord.SelectOption(label='Autorizados para Nuke', value='nuke', emoji='💥'),
        ]
    )
    async def config_select(self, interaction: discord.Interaction, select: Select):
        """Seleccionar categoría de configuración"""
        await interaction.response.defer()
        
        choice = select.values[0]
        
        if choice == 'tts':
            await self.config_tts(interaction)
        elif choice == 'markov':
            await self.config_markov(interaction)
        elif choice == 'welcome':
            await self.config_welcome(interaction)
        elif choice == 'warns':
            await self.config_warns(interaction)
        elif choice == 'antiscam':
            await self.config_antiscam(interaction)
        elif choice == 'channels':
            await self.config_channels(interaction)
        elif choice == 'roles':
            await self.config_roles(interaction)
        elif choice == 'nuke':
            await self.config_nuke(interaction)
    
    async def config_tts(self, interaction: discord.Interaction):
        """Configurar TTS"""
        guild_id = interaction.guild.id
        
        embed = discord.Embed(
            title='⚙️ Configuración TTS',
            description='Configura la velocidad de lectura y voces',
            color=discord.Color.blue()
        )
        
        tts_enabled = db.get_config(guild_id, 'tts_enabled', 'false') == 'true'
        tts_speed = float(db.get_config(guild_id, 'tts_speed', 1.0))
        tts_voice = db.get_config(guild_id, 'tts_voice', 'default')
        
        embed.add_field(name='Estado', value='✅ Activado' if tts_enabled else '❌ Desactivado', inline=False)
        embed.add_field(name='Velocidad', value=f'{tts_speed}x', inline=True)
        embed.add_field(name='Voz', value=tts_voice, inline=True)
        
        class TTSView(View):
            async def on_timeout(self):
                pass
            
            @discord.ui.button(label='Activar/Desactivar', style=discord.ButtonStyle.primary)
            async def toggle_tts(self, interaction: discord.Interaction, button: discord.ui.Button):
                current = db.get_config(guild_id, 'tts_enabled', 'false') == 'true'
                db.set_config(guild_id, 'tts_enabled', 'false' if current else 'true')
                await interaction.response.send_message('✅ Estado de TTS actualizado', ephemeral=True)
            
            @discord.ui.button(label='Cambiar Velocidad', style=discord.ButtonStyle.primary)
            async def change_speed(self, interaction: discord.Interaction, button: discord.ui.Button):
                await interaction.response.send_modal(SpeedModal(guild_id))
            
            @discord.ui.button(label='Cambiar Voz', style=discord.ButtonStyle.primary)
            async def change_voice(self, interaction: discord.Interaction, button: discord.ui.Button):
                await interaction.response.defer()
                voices = ['default', 'deep', 'high', 'natural', 'robotic', 'es-MX-DaliaNeural']
                options = [discord.SelectOption(label=v, value=v) for v in voices]
                
                class VoiceSelect(View):
                    @discord.ui.select(options=options)
                    async def select_voice(self, i: discord.Interaction, select: Select):
                        db.set_config(guild_id, 'tts_voice', select.values[0])
                        await i.response.send_message(f'✅ Voz guardada: {select.values[0]}', ephemeral=True)
                
                await interaction.followup.send('Selecciona una voz:', view=VoiceSelect(), ephemeral=True)
        
        await interaction.followup.send(embed=embed, view=TTSView(), ephemeral=True)
    
    async def config_markov(self, interaction: discord.Interaction):
        """Configurar cerebro de frases aleatorias con actualización de estadísticas en vivo"""
        guild_id = interaction.guild.id
        
        embed = discord.Embed(
            title='🧠 Configuración Cerebro de Frases',
            description='Controla la generación automática de frases aleatorias del chat',
            color=discord.Color.purple()
        )
        
        markov_enabled = db.get_config(guild_id, 'markov_enabled', 'false') == 'true'
        
        try:
            conn = db.get_connection(guild_id)
            cursor = conn.cursor()
            cursor.execute('CREATE TABLE IF NOT EXISTS learned_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT UNIQUE)')
            conn.commit()
            cursor.execute('SELECT COUNT(*) FROM learned_messages')
            total_messages = cursor.fetchone()[0] or 0
        except Exception:
            total_messages = 0
        
        embed.add_field(name='Estado', value='✅ Activo' if markov_enabled else '❌ Inactivo', inline=False)
        embed.add_field(name='Histórico de lectura', value='Últimos 7 días (Todos los canales)', inline=False)
        embed.add_field(name='📚 Frases únicas guardadas', value=f'`{total_messages}` frases', inline=True)
        
        outer_self = self
        
        class MarkovView(View):
            def __init__(self):
                super().__init__()
            
            @discord.ui.button(label='Activar/Desactivar', style=discord.ButtonStyle.primary)
            async def toggle_markov(self, interaction: discord.Interaction, button: discord.ui.Button):
                current = db.get_config(guild_id, 'markov_enabled', 'false') == 'true'
                new_state = 'false' if current else 'true'
                db.set_config(guild_id, 'markov_enabled', new_state)
                
                markov_enabled = new_state == 'true'
                embed.clear_fields()
                embed.add_field(name='Estado', value='✅ Activo' if markov_enabled else '❌ Inactivo', inline=False)
                embed.add_field(name='Histórico de lectura', value='Últimos 7 días (Todos los canales)', inline=False)
                
                try:
                    conn = db.get_connection(guild_id)
                    cursor = conn.cursor()
                    cursor.execute('SELECT COUNT(*) FROM learned_messages')
                    current_count = cursor.fetchone()[0] or 0
                except Exception:
                    current_count = 0
                embed.add_field(name='📚 Frases únicas guardadas', value=f'`{current_count}` frases', inline=True)
                
                await interaction.response.edit_message(embed=embed, view=self)
            
            @discord.ui.button(label='📖 Leer historial de chat', style=discord.ButtonStyle.success)
            async def fetch_history(self, interaction: discord.Interaction, button: discord.ui.Button):
                """Inicia el escaneo y actualiza el embed en vivo con contadores detallados"""
                await interaction.response.defer()
                asyncio.create_task(outer_self._run_scan_and_update_embed(interaction, embed, self, guild_id))
            
            @discord.ui.button(label='Borrar Datos', style=discord.ButtonStyle.danger)
            async def clear_markov(self, interaction: discord.Interaction, button: discord.ui.Button):
                try:
                    conn = db.get_connection(guild_id)
                    cursor = conn.cursor()
                    cursor.execute('DELETE FROM learned_messages')
                    conn.commit()
                    
                    embed.clear_fields()
                    markov_enabled = db.get_config(guild_id, 'markov_enabled', 'false') == 'true'
                    embed.add_field(name='Estado', value='✅ Activo' if markov_enabled else '❌ Inactivo', inline=False)
                    embed.add_field(name='Histórico de lectura', value='Últimos 7 días (Todos los canales)', inline=False)
                    embed.add_field(name='📚 Frases únicas guardadas', value='`0` frases', inline=True)
                    
                    await interaction.response.edit_message(embed=embed, view=self)
                except Exception as e:
                    await interaction.followup.send(f'❌ Error al borrar datos: {e}', ephemeral=True)

        await interaction.followup.send(embed=embed, view=MarkovView(), ephemeral=True)

    async def _run_scan_and_update_embed(self, interaction, embed, view, guild_id):
        """Función en segundo plano que actualiza la interfaz embed en tiempo real con contadores detallados"""
        guild = interaction.guild
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        conn = db.get_connection(guild_id)
        cursor = conn.cursor()
        
        last_edit_time = time.time()
        messages_read = 0

        for channel in guild.text_channels:
            if channel.nsfw:
                continue
            
            permissions = channel.permissions_for(guild.me)
            if not permissions.read_message_history or not permissions.view_channel:
                continue
                
            try:
                async for message in channel.history(after=seven_days_ago, limit=None):
                    if message.author.bot or not message.content:
                        continue
                        
                    content = message.content.strip()
                    if content.startswith('!') or content.startswith('http'):
                        continue
                        
                    if len(content) > 3:
                        messages_read += 1
                        try:
                            cursor.execute('INSERT OR IGNORE INTO learned_messages (content) VALUES (?)', (content,))
                            
                            if time.time() - last_edit_time >= 1.5:
                                last_edit_time = time.time()
                                cursor.execute('SELECT COUNT(*) FROM learned_messages')
                                current_count = cursor.fetchone()[0] or 0
                                
                                embed.clear_fields()
                                markov_enabled = db.get_config(guild_id, 'markov_enabled', 'false') == 'true'
                                embed.add_field(name='Estado', value='✅ Activo' if markov_enabled else '❌ Inactivo', inline=False)
                                embed.add_field(name='Histórico de lectura', value='Últimos 7 días (Todos los canales)', inline=False)
                                embed.add_field(name='🔄 Estado del proceso', value='`Actualizando datos...`', inline=False)
                                embed.add_field(name='📩 Mensajes leídos', value=f'`{messages_read}` mensajes', inline=True)
                                embed.add_field(name='📚 Frases únicas guardadas', value=f'`{current_count}` frases', inline=True)
                                
                                try:
                                    await interaction.edit_original_response(embed=embed, view=view)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                            
                conn.commit()
            except Exception as e:
                logger.error(f'Error leyendo historial en canal {channel.name}: {e}')
                
        cursor.execute('SELECT COUNT(*) FROM learned_messages')
        final_count = cursor.fetchone()[0] or 0
        
        embed.clear_fields()
        markov_enabled = db.get_config(guild_id, 'markov_enabled', 'false') == 'true'
        embed.add_field(name='Estado', value='✅ Activo' if markov_enabled else '❌ Inactivo', inline=False)
        embed.add_field(name='Histórico de lectura', value='Últimos 7 días (Todos los canales)', inline=False)
        embed.add_field(name='✅ Estado', value='`Escaneo completado`', inline=False)
        embed.add_field(name='📩 Mensajes leídos totales', value=f'`{messages_read}` mensajes', inline=True)
        embed.add_field(name='📚 Frases únicas guardadas', value=f'`{final_count}` frases', inline=True)
        
        try:
            await interaction.edit_original_response(embed=embed, view=view)
        except Exception:
            pass

    def _build_welcome_embed(self, guild_id):
        welcome_enabled = db.get_config(guild_id, 'welcome_enabled', 'true') == 'true'
        farewell_enabled = db.get_config(guild_id, 'farewell_enabled', 'true') == 'true'
        ban_farewell_enabled = db.get_config(guild_id, 'ban_farewell_enabled', 'true') == 'true'
        welcome_channel_id = db.get_config(guild_id, 'welcome_channel', '')
        farewell_channel_id = db.get_config(guild_id, 'farewell_channel', '')

        embed = discord.Embed(
            title='👋 Configuración de Bienvenidas y Despedidas',
            description='Personaliza los mensajes y selecciona los canales donde se enviarán.',
            color=discord.Color.green()
        )

        embed.add_field(name='Bienvenida', value='✅ Activada' if welcome_enabled else '❌ Desactivada', inline=True)
        embed.add_field(name='Despedida', value='✅ Activada' if farewell_enabled else '❌ Desactivada', inline=True)
        embed.add_field(name='Despedida (Ban)', value='✅ Activada' if ban_farewell_enabled else '❌ Desactivada', inline=True)

        ch_welcome_display = f'<#{welcome_channel_id}>' if welcome_channel_id else '🔍 Automático (Busca `#bienvenidas` o usa el primer canal)'
        embed.add_field(name='📢 Canal de Bienvenidas', value=ch_welcome_display, inline=False)

        ch_farewell_display = f'<#{farewell_channel_id}>' if farewell_channel_id else '🔍 Automático (Usa el mismo canal de Bienvenidas si no se configura aparte)'
        embed.add_field(name='📢 Canal de Despedidas (normal y por ban)', value=ch_farewell_display, inline=False)

        embed.add_field(
            name='📝 Variables Disponibles para Mensajes',
            value='Puedes incluir estas etiquetas en tus textos personalizados:\n'
                  '• `[usuario]` — Mención al miembro (`@Usuario`)\n'
                  '• `[nombredelserver]` — Nombre del servidor\n'
                  '• `[totalmiembros]` — Total de miembros (sin contar bots)\n'
                  '• `[allmembers]` — Total de miembros (incluyendo bots)',
            inline=False
        )
        return embed

    async def config_welcome(self, interaction: discord.Interaction):
        """Configurar bienvenidas y despedidas con canales separados, botones de activar/desactivar y guía de variables"""
        guild_id = interaction.guild.id
        embed = self._build_welcome_embed(guild_id)
        outer_self = self

        class WelcomeView(View):
            def __init__(self):
                super().__init__()

            async def _refresh(self, interaction: discord.Interaction):
                nuevo_embed = outer_self._build_welcome_embed(guild_id)
                await interaction.response.edit_message(embed=nuevo_embed, view=self)

            @discord.ui.select(
                cls=discord.ui.ChannelSelect,
                channel_types=[discord.ChannelType.text],
                placeholder='Selecciona el canal para BIENVENIDAS...',
                min_values=1,
                max_values=1,
                row=0
            )
            async def select_welcome_channel(self, i: discord.Interaction, select: discord.ui.ChannelSelect):
                selected_channel = select.values[0]
                db.set_config(guild_id, 'welcome_channel', str(selected_channel.id))
                await self._refresh(i)

            @discord.ui.select(
                cls=discord.ui.ChannelSelect,
                channel_types=[discord.ChannelType.text],
                placeholder='Selecciona el canal para DESPEDIDAS...',
                min_values=1,
                max_values=1,
                row=1
            )
            async def select_farewell_channel(self, i: discord.Interaction, select: discord.ui.ChannelSelect):
                selected_channel = select.values[0]
                db.set_config(guild_id, 'farewell_channel', str(selected_channel.id))
                await self._refresh(i)

            @discord.ui.button(label='Activar/Desactivar Bienvenida', style=discord.ButtonStyle.secondary, row=2)
            async def toggle_welcome(self, i: discord.Interaction, button: discord.ui.Button):
                current = db.get_config(guild_id, 'welcome_enabled', 'true') == 'true'
                db.set_config(guild_id, 'welcome_enabled', 'false' if current else 'true')
                await self._refresh(i)

            @discord.ui.button(label='Activar/Desactivar Despedida', style=discord.ButtonStyle.secondary, row=2)
            async def toggle_farewell(self, i: discord.Interaction, button: discord.ui.Button):
                current = db.get_config(guild_id, 'farewell_enabled', 'true') == 'true'
                db.set_config(guild_id, 'farewell_enabled', 'false' if current else 'true')
                await self._refresh(i)

            @discord.ui.button(label='Activar/Desactivar Despedida (Ban)', style=discord.ButtonStyle.secondary, row=2)
            async def toggle_ban_farewell(self, i: discord.Interaction, button: discord.ui.Button):
                current = db.get_config(guild_id, 'ban_farewell_enabled', 'true') == 'true'
                db.set_config(guild_id, 'ban_farewell_enabled', 'false' if current else 'true')
                await self._refresh(i)

            @discord.ui.button(label='Personalizar Bienvenida', style=discord.ButtonStyle.primary, row=3)
            async def edit_welcome(self, i: discord.Interaction, button: discord.ui.Button):
                await i.response.send_modal(WelcomeModal(guild_id, 'welcome'))
            
            @discord.ui.button(label='Personalizar Despedida', style=discord.ButtonStyle.primary, row=3)
            async def edit_farewell(self, i: discord.Interaction, button: discord.ui.Button):
                await i.response.send_modal(WelcomeModal(guild_id, 'farewell'))
            
            @discord.ui.button(label='Personalizar Ban', style=discord.ButtonStyle.danger, row=3)
            async def edit_ban_farewell(self, i: discord.Interaction, button: discord.ui.Button):
                await i.response.send_modal(WelcomeModal(guild_id, 'ban_farewell'))
        
        await interaction.followup.send(embed=embed, view=WelcomeView(), ephemeral=True)

    async def config_warns(self, interaction: discord.Interaction):
        """Configurar warns y sanciones"""
        guild_id = interaction.guild.id
        
        embed = discord.Embed(
            title='⚠️ Configuración de Warns',
            description='Establece límites y sanciones.\n'
                        '📢 *¿Quieres configurar en qué canal avisan los warns/bans/suspensiones? '
                        'Ve a **Canales de Avisos y Sanciones**.*',
            color=discord.Color.orange()
        )
        
        warn_limit = db.get_config(guild_id, 'warn_limit', '3')
        sanction_type = db.get_config(guild_id, 'sanction_type', 'mute')
        suspension_max_days = db.get_config(guild_id, 'suspension_max_days', '7')

        embed.add_field(name='Límite de Warns', value=warn_limit, inline=True)
        embed.add_field(name='Sanción', value='🔇 Suspensión' if sanction_type == 'mute' else '🚫 Ban', inline=True)
        embed.add_field(name='⏱️ Máximo de suspensión', value=f'{suspension_max_days} días', inline=True)
        embed.add_field(
            name='✉️ Mensaje de MD al sancionar',
            value='Se le manda al usuario por privado cuando se le suspende o banea.\n'
                  f'{SANCTION_DM_VARS_HELP}',
            inline=False
        )
        
        class WarnsView(View):
            @discord.ui.button(label='Cambiar Límite', style=discord.ButtonStyle.primary)
            async def change_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
                await interaction.response.send_modal(WarnLimitModal(guild_id))
            
            @discord.ui.button(label='Tipo de Sanción', style=discord.ButtonStyle.primary)
            async def change_sanction(self, interaction: discord.Interaction, button: discord.ui.Button):
                options = [
                    discord.SelectOption(label='Suspensión (Mute)', value='mute'),
                    discord.SelectOption(label='Ban', value='ban')
                ]
                
                class SanctionSelect(View):
                    @discord.ui.select(options=options)
                    async def select_sanction(self, i: discord.Interaction, select: Select):
                        db.set_config(guild_id, 'sanction_type', select.values[0])
                        await i.response.send_message(f'✅ Sanción actualizada a: {select.values[0]}', ephemeral=True)
                
                await interaction.response.send_message('Selecciona tipo de sanción:', view=SanctionSelect(), ephemeral=True)

            @discord.ui.button(label='Máximo de Suspensión', style=discord.ButtonStyle.secondary)
            async def change_max_suspension(self, interaction: discord.Interaction, button: discord.ui.Button):
                await interaction.response.send_modal(SuspensionMaxModal(guild_id))

            @discord.ui.button(label='Personalizar Mensaje DM', style=discord.ButtonStyle.success)
            async def change_dm_message(self, interaction: discord.Interaction, button: discord.ui.Button):
                await interaction.response.send_modal(SanctionDMModal(guild_id))
        
        await interaction.followup.send(embed=embed, view=WarnsView(), ephemeral=True)

    async def config_antiscam(self, interaction: discord.Interaction):
        """Configurar el sistema Anti-Scam por servidor"""
        guild_id = interaction.guild.id
        
        embed = discord.Embed(
            title='🛡️ Configuración Anti-Scam',
            description='Protección contra cuentas hackeadas detectando spam masivo',
            color=discord.Color.red()
        )
        
        antiscam_enabled = db.get_config(guild_id, 'antiscam_enabled', 'false') == 'true'
        antiscam_channel = db.get_config(guild_id, 'antiscam_channel', '')
        
        embed.add_field(
            name='Estado',
            value='✅ Activado' if antiscam_enabled else '❌ Desactivado',
            inline=False
        )
        
        embed.add_field(
            name='📊 Cómo Funciona',
            value='**Detecta spam masivo en 3+ canales en 25 segundos:**\n'
                  '✓ Mensajes idénticos\n'
                  '✓ URLs maliciosas\n'
                  '✓ Archivos adjuntos\n\n'
                  '**Protecciones:**\n'
                  '🔒 Mute de 7 días\n'
                  '📝 Código de verificación por DM\n'
                  '🗑️ Eliminación automática de spam\n'
                  '⚠️ Apodo marcado temporalmente',
            inline=False
        )
        
        if antiscam_channel and antiscam_channel != '':
            embed.add_field(
                name='Canal de Alertas',
                value=f'<#{antiscam_channel}>',
                inline=False
            )
        else:
            embed.add_field(
                name='Canal de Alertas',
                value='Automático (Primer canal de texto)',
                inline=False
            )
        
        class AntiScamView(View):
            def __init__(self):
                super().__init__()
            
            @discord.ui.button(label='Activar/Desactivar', style=discord.ButtonStyle.primary)
            async def toggle_antiscam(self, i: discord.Interaction, button: discord.ui.Button):
                current = db.get_config(guild_id, 'antiscam_enabled', 'false') == 'true'
                new_state = 'false' if current else 'true'
                db.set_config(guild_id, 'antiscam_enabled', new_state)
                
                status_text = '✅ Activado' if new_state == 'true' else '❌ Desactivado'
                embed_updated = discord.Embed(
                    title='🛡️ Configuración Anti-Scam',
                    description='Protección contra cuentas hackeadas detectando spam masivo',
                    color=discord.Color.green() if new_state == 'true' else discord.Color.red()
                )
                embed_updated.add_field(name='Estado', value=status_text, inline=False)
                embed_updated.add_field(
                    name='📊 Cómo Funciona',
                    value='**Detecta spam masivo en 3+ canales en 25 segundos:**\n'
                          '✓ Mensajes idénticos\n'
                          '✓ URLs maliciosas\n'
                          '✓ Archivos adjuntos\n\n'
                          '**Protecciones:**\n'
                          '🔒 Mute de 7 días\n'
                          '📝 Código de verificación por DM\n'
                          '🗑️ Eliminación automática de spam',
                    inline=False
                )
                
                await i.response.edit_message(embed=embed_updated, view=self)
            
            @discord.ui.select(
                cls=discord.ui.ChannelSelect,
                channel_types=[discord.ChannelType.text],
                placeholder='Selecciona el canal para Alertas Anti-Scam...',
                min_values=1,
                max_values=1
            )
            async def select_alert_channel(self, i: discord.Interaction, select: discord.ui.ChannelSelect):
                selected_channel = select.values[0]
                db.set_config(guild_id, 'antiscam_channel', str(selected_channel.id))
                await i.response.send_message(f'✅ Canal de Alertas Anti-Scam guardado en <#{selected_channel.id}>', ephemeral=True)

            @discord.ui.button(label='🔄 Actualizar', style=discord.ButtonStyle.secondary)
            async def refresh_antiscam(self, i: discord.Interaction, button: discord.ui.Button):
                await i.response.edit_message(embed=embed, view=self)
        
        await interaction.followup.send(embed=embed, view=AntiScamView(), ephemeral=True)

    def _build_channels_embed(self, guild_id):
        warn_channel = db.get_config(guild_id, 'warn_channel', '')
        ban_channel = db.get_config(guild_id, 'ban_channel', '')
        mute_channel = db.get_config(guild_id, 'mute_channel', '')

        embed = discord.Embed(
            title='📢 Canales de Avisos y Sanciones',
            description='Selecciona por separado los canales donde el bot avisa cada tipo de acción.',
            color=discord.Color.blue()
        )

        embed.add_field(
            name='⚠️ Canal para Warns',
            value=f'<#{warn_channel}>' if warn_channel not in ['', 'actual'] else '📌 Canal actual (donde se ejecuta el comando)',
            inline=False
        )
        embed.add_field(
            name='🚫 Canal para Bans',
            value=f'<#{ban_channel}>' if ban_channel not in ['', 'actual'] else '📌 Canal actual (donde se ejecuta el comando)',
            inline=False
        )
        embed.add_field(
            name='🔇 Canal para Suspensiones (Mutes)',
            value=f'<#{mute_channel}>' if mute_channel not in ['', 'actual'] else '📌 Canal actual (donde se ejecuta el comando)',
            inline=False
        )
        return embed

    async def config_channels(self, interaction: discord.Interaction):
        """Configurar canales para avisos de Warns, Bans y Suspensiones, por separado"""
        guild_id = interaction.guild.id
        embed = self._build_channels_embed(guild_id)
        outer_self = self

        class ChannelView(View):
            def __init__(self):
                super().__init__()

            async def _refresh(self, i: discord.Interaction):
                nuevo_embed = outer_self._build_channels_embed(guild_id)
                await i.response.edit_message(embed=nuevo_embed, view=self)

            @discord.ui.select(
                cls=discord.ui.ChannelSelect,
                channel_types=[discord.ChannelType.text],
                placeholder='Selecciona el canal para WARNS...',
                min_values=1,
                max_values=1,
                row=0
            )
            async def select_warn_channel(self, i: discord.Interaction, select: discord.ui.ChannelSelect):
                db.set_config(guild_id, 'warn_channel', str(select.values[0].id))
                await self._refresh(i)
            
            @discord.ui.select(
                cls=discord.ui.ChannelSelect,
                channel_types=[discord.ChannelType.text],
                placeholder='Selecciona el canal para BANS...',
                min_values=1,
                max_values=1,
                row=1
            )
            async def select_ban_channel(self, i: discord.Interaction, select: discord.ui.ChannelSelect):
                db.set_config(guild_id, 'ban_channel', str(select.values[0].id))
                await self._refresh(i)

            @discord.ui.select(
                cls=discord.ui.ChannelSelect,
                channel_types=[discord.ChannelType.text],
                placeholder='Selecciona el canal para SUSPENSIONES (Mutes)...',
                min_values=1,
                max_values=1,
                row=2
            )
            async def select_mute_channel(self, i: discord.Interaction, select: discord.ui.ChannelSelect):
                db.set_config(guild_id, 'mute_channel', str(select.values[0].id))
                await self._refresh(i)

            @discord.ui.button(label='Restablecer a canal actual', style=discord.ButtonStyle.secondary, row=3)
            async def reset_channels(self, i: discord.Interaction, button: discord.ui.Button):
                db.set_config(guild_id, 'warn_channel', 'actual')
                db.set_config(guild_id, 'ban_channel', 'actual')
                db.set_config(guild_id, 'mute_channel', 'actual')
                await self._refresh(i)
        
        await interaction.followup.send(embed=embed, view=ChannelView(), ephemeral=True)
    
    async def config_roles(self, interaction: discord.Interaction):
        """Configurar roles admin/mod"""
        guild_id = interaction.guild.id
        
        embed = discord.Embed(
            title='🛡️ Roles Admin/Mod',
            description=(
                'Aquí se guardan roles extra que **deberían** poder sancionar (además de los permisos '
                'normales de Discord como "Moderar miembros" o "Banear miembros").\n\n'
                '⚠️ **Nota:** por ahora esta sección solo GUARDA los datos — los comandos de moderación '
                '(`!warn`, `!mute`, `!ban`, etc.) todavía usan nada más los permisos nativos de Discord '
                '(`has_permissions`), no revisan esta lista. Si quieres que un rol pueda moderar, dale el '
                'permiso de Discord correspondiente directamente en Ajustes del Servidor por ahora.'
            ),
            color=discord.Color.red()
        )
        
        admin_roles = db.get_config(guild_id, 'admin_roles', '')
        mod_roles = db.get_config(guild_id, 'mod_roles', '')
        
        embed.add_field(name='Roles Admin (guardados)', value=admin_roles or 'Ninguno configurado', inline=False)
        embed.add_field(name='Roles Mod (guardados)', value=mod_roles or 'Ninguno configurado', inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    def _build_nuke_embed(self, guild_id):
        extra_users = [u for u in db.get_config(guild_id, 'nuke_extra_users', '').split(',') if u]
        extra_roles = [r for r in db.get_config(guild_id, 'nuke_extra_roles', '').split(',') if r]

        embed = discord.Embed(
            title='💥 Autorizados para usar Nuke',
            description=(
                'El comando `!nuke` (borra y vuelve a crear el canal desde cero) SIEMPRE lo puede usar '
                'el owner del servidor. Aquí puedes autorizar usuarios o roles extra.'
            ),
            color=discord.Color.dark_red()
        )
        embed.add_field(
            name='Usuarios autorizados',
            value=', '.join(f'<@{u}>' for u in extra_users) if extra_users else 'Ninguno',
            inline=False
        )
        embed.add_field(
            name='Roles autorizados',
            value=', '.join(f'<@&{r}>' for r in extra_roles) if extra_roles else 'Ninguno',
            inline=False
        )
        return embed

    async def config_nuke(self, interaction: discord.Interaction):
        """Configurar qué usuarios/roles extra (además del owner) pueden usar !nuke"""
        guild_id = interaction.guild.id
        embed = self._build_nuke_embed(guild_id)
        outer_self = self

        class NukeView(View):
            def __init__(self):
                super().__init__()

            async def _refresh(self, i: discord.Interaction):
                nuevo_embed = outer_self._build_nuke_embed(guild_id)
                await i.response.edit_message(embed=nuevo_embed, view=self)

            @discord.ui.select(
                cls=discord.ui.UserSelect,
                placeholder='Agregar un usuario autorizado...',
                min_values=1,
                max_values=1,
                row=0
            )
            async def add_user(self, i: discord.Interaction, select: discord.ui.UserSelect):
                actuales = {u for u in db.get_config(guild_id, 'nuke_extra_users', '').split(',') if u}
                actuales.add(str(select.values[0].id))
                db.set_config(guild_id, 'nuke_extra_users', ','.join(actuales))
                await self._refresh(i)

            @discord.ui.select(
                cls=discord.ui.RoleSelect,
                placeholder='Agregar un rol autorizado...',
                min_values=1,
                max_values=1,
                row=1
            )
            async def add_role(self, i: discord.Interaction, select: discord.ui.RoleSelect):
                actuales = {r for r in db.get_config(guild_id, 'nuke_extra_roles', '').split(',') if r}
                actuales.add(str(select.values[0].id))
                db.set_config(guild_id, 'nuke_extra_roles', ','.join(actuales))
                await self._refresh(i)

            @discord.ui.button(label='Quitar todos los usuarios', style=discord.ButtonStyle.danger, row=2)
            async def clear_users(self, i: discord.Interaction, button: discord.ui.Button):
                db.set_config(guild_id, 'nuke_extra_users', '')
                await self._refresh(i)

            @discord.ui.button(label='Quitar todos los roles', style=discord.ButtonStyle.danger, row=2)
            async def clear_roles(self, i: discord.Interaction, button: discord.ui.Button):
                db.set_config(guild_id, 'nuke_extra_roles', '')
                await self._refresh(i)

        await interaction.followup.send(embed=embed, view=NukeView(), ephemeral=True)


class SpeedModal(discord.ui.Modal):
    speed = discord.ui.TextInput(label='Velocidad (0.5-2.0)', placeholder='1.0')
    
    def __init__(self, guild_id: int):
        super().__init__(title='Cambiar Velocidad TTS')
        self.guild_id = guild_id
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            speed = float(self.speed.value)
            if 0.5 <= speed <= 2.0:
                db.set_config(self.guild_id, 'tts_speed', speed)
                await interaction.response.send_message(f'✅ Velocidad establecida a {speed}x', ephemeral=True)
            else:
                await interaction.response.send_message('❌ Rango permitido: 0.5 - 2.0', ephemeral=True)
        except ValueError:
            await interaction.response.send_message('❌ Valor numérico inválido', ephemeral=True)


class WelcomeModal(discord.ui.Modal):
    message = discord.ui.TextInput(label='Mensaje', style=discord.TextStyle.long, max_length=1000)
    image_url = discord.ui.TextInput(label='URL de imagen (opcional)', required=False)
    
    def __init__(self, guild_id: int, msg_type: str):
        super().__init__(title=f'Personalizar Mensaje ({msg_type})')
        self.guild_id = guild_id
        self.msg_type = msg_type
    
    async def on_submit(self, interaction: discord.Interaction):
        db.set_config(self.guild_id, f'{self.msg_type}_message', self.message.value)
        if self.image_url.value:
            db.set_config(self.guild_id, f'{self.msg_type}_image', self.image_url.value)
        
        embed = discord.Embed(
            title='✅ Mensaje Guardado',
            description=self.message.value[:500],
            color=discord.Color.green()
        )
        embed.set_footer(text=parse_variables_help())
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


class WarnLimitModal(discord.ui.Modal):
    limit = discord.ui.TextInput(label='Número de warns', placeholder='3')
    
    def __init__(self, guild_id: int):
        super().__init__(title='Establecer Límite de Warns')
        self.guild_id = guild_id
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            limit = int(self.limit.value)
            if limit > 0:
                db.set_config(self.guild_id, 'warn_limit', limit)
                await interaction.response.send_message(f'✅ Límite establecido a {limit} warns', ephemeral=True)
            else:
                await interaction.response.send_message('❌ Debe ser un número positivo', ephemeral=True)
        except ValueError:
            await interaction.response.send_message('❌ Valor inválido', ephemeral=True)


class SuspensionMaxModal(discord.ui.Modal):
    dias = discord.ui.TextInput(label='Máximo de días de suspensión', placeholder='7')

    def __init__(self, guild_id: int):
        super().__init__(title='Máximo de Suspensión')
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            dias = int(self.dias.value)
            if 1 <= dias <= 28:  # 28 días es el límite que impone Discord para timeouts
                db.set_config(self.guild_id, 'suspension_max_days', dias)
                await interaction.response.send_message(f'✅ Máximo de suspensión establecido a {dias} días', ephemeral=True)
            else:
                await interaction.response.send_message('❌ Rango permitido: 1 - 28 días (límite de Discord)', ephemeral=True)
        except ValueError:
            await interaction.response.send_message('❌ Valor inválido', ephemeral=True)


class SanctionDMModal(discord.ui.Modal):
    message = discord.ui.TextInput(
        label='Mensaje de MD',
        style=discord.TextStyle.long,
        max_length=1000,
        default='Wenas [usuario] fuiste [sancion] de [nombredelserver] por [motivo] durante [tiempo]'
    )

    def __init__(self, guild_id: int):
        super().__init__(title='Mensaje de MD al Sancionar')
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        db.set_config(self.guild_id, 'sanction_dm_message', self.message.value)

        embed = discord.Embed(
            title='✅ Mensaje de MD Guardado',
            description=self.message.value[:500],
            color=discord.Color.green()
        )
        embed.set_footer(text=SANCTION_DM_VARS_HELP)

        await interaction.response.send_message(embed=embed, ephemeral=True)


class Config(commands.Cog):
    """Cog de configuración interactiva"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(name='config')
    @commands.has_permissions(administrator=True)
    async def config(self, ctx):
        """Abre el menú de configuración"""
        view = ConfigMenu(ctx)
        await ctx.send('⚙️ **Menú de Configuración General**', view=view)

async def setup(bot):
    await bot.add_cog(Config(bot))