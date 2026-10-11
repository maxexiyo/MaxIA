import discord
from discord.ext import commands
from database import db
import random
import logging
from datetime import datetime, timedelta
import asyncio
import time
from collections import deque

logger = logging.getLogger(__name__)

class Markov(commands.Cog):
    """Cog de frases aleatorias basadas en palabras sueltas y transiciones de chat"""
    
    def __init__(self, bot):
        self.bot = bot
        # Mantiene un registro en memoria de los últimos 100 mensajes enviados por este módulo
        self.markov_message_ids = {}
    
    def _ensure_table(self, guild_id):
        """Asegura que la tabla de mensajes aprendidos exista en la base de datos del servidor"""
        try:
            conn = db.get_connection(guild_id)
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS learned_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT UNIQUE
                )
            ''')
            conn.commit()
        except Exception as e:
            logger.error(f'Error creando tabla learned_messages: {e}')

    def _register_markov_message(self, guild_id, message_id):
        """Registra el ID del mensaje enviado por Markov para reconocerlo después"""
        if guild_id not in self.markov_message_ids:
            self.markov_message_ids[guild_id] = deque(maxlen=100)
        self.markov_message_ids[guild_id].append(message_id)

    async def _fetch_historical_messages(self, guild, status_msg=None):
        """Función para extraer mensajes de los últimos 7 días actualizando el estado cada segundo"""
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        logger.info(f'Iniciando escaneo de historial de 7 días para el servidor: {guild.name}')
        
        self._ensure_table(guild.id)
        conn = db.get_connection(guild.id)
        cursor = conn.cursor()
        
        count = 0
        last_edit_time = time.time()
        
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
                        try:
                            cursor.execute('INSERT OR IGNORE INTO learned_messages (content) VALUES (?)', (content,))
                            count += 1
                            
                            if status_msg and (time.time() - last_edit_time >= 1.5):
                                last_edit_time = time.time()
                                try:
                                    await status_msg.edit(content=f'🔄 **Escaneando historial...** Mensajes guardados hasta ahora: **{count}**')
                                except Exception:
                                    pass
                        except Exception:
                            pass
                            
                conn.commit()
            except Exception as e:
                logger.error(f'Error leyendo historial en canal {channel.name}: {e}')
                
        if status_msg:
            try:
                await status_msg.edit(content=f'✅ **¡Escaneo completado!** Total de mensajes guardados: **{count}**')
            except Exception:
                pass
                
        logger.info(f'Escaneo histórico de 7 días completado para {guild.name}. Mensajes guardados: {count}')

    async def _generate_phrase(self, guild_id):
        """Separa las palabras, arma un diccionario de conexiones y junta de 1 a 15 palabras con coherencia"""
        try:
            self._ensure_table(guild_id)
            conn = db.get_connection(guild_id)
            cursor = conn.cursor()
            
            cursor.execute('SELECT content FROM learned_messages')
            rows = cursor.fetchall()
            
            if not rows:
                return "estoy escaneando el historial, dame un momento xd"
                
            word_pool = []
            transitions = {}
            
            # Separar cada palabra de cada mensaje guardado
            for row in rows:
                if not row[0]:
                    continue
                words = row[0].split()
                if not words:
                    continue
                
                word_pool.extend(words)
                
                # Registrar qué palabra suele ir después de otra para mantener coherencia
                for i in range(len(words) - 1):
                    w1, w2 = words[i], words[i+1]
                    if w1 not in transitions:
                        transitions[w1] = []
                    transitions[w1].append(w2)
            
            if not word_pool:
                return "todavía no tengo suficientes palabras xd"
                
            # Cantidad aleatoria de palabras entre 1 y 15
            phrase_length = random.randint(1, 15)
            
            # Elegir una palabra inicial al azar de la bolsa revuelta
            current_word = random.choice(word_pool)
            phrase = [current_word]
            
            # Juntar las siguientes palabras intentando mantener el sentido original
            for _ in range(phrase_length - 1):
                if current_word in transitions and transitions[current_word]:
                    current_word = random.choice(transitions[current_word])
                    phrase.append(current_word)
                else:
                    # Si la palabra no tiene registro siguiente, sacamos otra al azar de la bolsa
                    current_word = random.choice(word_pool)
                    phrase.append(current_word)
                    
            return " ".join(phrase)
            
        except Exception as e:
            logger.error(f'Error generando frase combinada: {e}')
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Procesa mensajes en tiempo real y responde solo si le hacen reply a un mensaje generado por Markov"""
        
        if message.author.bot or not message.guild or message.channel.nsfw:
            return
        
        guild_id = message.guild.id
        
        if db.get_config(guild_id, 'markov_enabled', 'false') != 'true':
            return
        
        content = message.content.strip()
        
        # Guardar mensaje en tiempo real si cumple las condiciones
        if content and not content.startswith('!') and not content.startswith('http') and len(content) > 3:
            try:
                self._ensure_table(guild_id)
                conn = db.get_connection(guild_id)
                cursor = conn.cursor()
                cursor.execute('INSERT OR IGNORE INTO learned_messages (content) VALUES (?)', (content,))
                conn.commit()
            except Exception as e:
                logger.error(f'Error guardando mensaje en tiempo real: {e}')

        # Verificar si es un reply a un mensaje del bot
        if message.reference and message.reference.message_id:
            try:
                ref_msg_id = message.reference.message_id
                
                # Validar si el ID del mensaje citado pertenece específicamente a este módulo de Markov
                if guild_id in self.markov_message_ids and ref_msg_id in self.markov_message_ids[guild_id]:
                    phrase = await self._generate_phrase(guild_id)
                    if phrase:
                        sent_msg = await message.reply(phrase)
                        self._register_markov_message(guild_id, sent_msg.id)
            except Exception as e:
                logger.error(f'Error al responder con frase aleatoria: {e}')
    
    @commands.command(name='markov')
    @commands.has_permissions(administrator=True)
    async def markov_cmd(self, ctx, action: str = None):
        """Controlar el sistema de aprendizaje"""
        
        guild_id = ctx.guild.id
        
        if not action:
            await ctx.send('**Comandos de Frases Aleatorias:**\n`!max markov activate` - Activar y ver progreso en tiempo real\n`!max markov status` - Ver cuántos mensajes lleva guardados\n`!max markov deactivate` - Desactivar\n`!max markov clear` - Borrar datos')
            return
        
        action = action.lower()
        
        if action == 'activate':
            db.set_config(guild_id, 'markov_enabled', 'true')
            status_msg = await ctx.send('🚀 **Iniciando escaneo...** Preparando para leer los últimos 7 días.')
            db.add_audit_log(guild_id, 'MARKOV_ENABLED', 0, ctx.author.id)
            asyncio.create_task(self._fetch_historical_messages(ctx.guild, status_msg))
        
        elif action == 'status':
            try:
                self._ensure_table(guild_id)
                conn = db.get_connection(guild_id)
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM learned_messages')
                count = cursor.fetchone()[0]
                await ctx.send(f'📊 Mensajes guardados en la base de datos hasta ahora: **{count}**')
            except Exception as e:
                await ctx.send(f'❌ Error al consultar estado: {e}')
        
        elif action == 'deactivate':
            db.set_config(guild_id, 'markov_enabled', 'false')
            await ctx.send('❌ Sistema desactivado.')
            db.add_audit_log(guild_id, 'MARKOV_DISABLED', 0, ctx.author.id)
        
        elif action == 'clear':
            try:
                self._ensure_table(guild_id)
                conn = db.get_connection(guild_id)
                cursor = conn.cursor()
                cursor.execute('DELETE FROM learned_messages')
                conn.commit()
                await ctx.send('🧹 Datos de frases aleatorias borrados.')
            except Exception as e:
                await ctx.send(f'❌ Error al borrar datos: {e}')
            db.add_audit_log(guild_id, 'MARKOV_CLEARED', 0, ctx.author.id)
        
        else:
            await ctx.send('❌ Acción no reconocida.')
    
    @commands.command(name='msg')
    async def generarmsg(self, ctx):
        """Generar un mensaje random en texto plano"""
        guild_id = ctx.guild.id
        
        phrase = await self._generate_phrase(guild_id)
        if phrase:
            sent_msg = await ctx.send(phrase)
            self._register_markov_message(guild_id, sent_msg.id)
        else:
            await ctx.send('❌ No hay datos suficientes o aún se está procesando el historial.')

async def setup(bot):
    await bot.add_cog(Markov(bot))