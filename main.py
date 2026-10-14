import subprocess
import sys
import asyncio

# Función para verificar e instalar dependencias automáticamente
def check_and_install(package, import_name=None):
    if import_name is None:
        import_name = package
    try:
        __import__(import_name)
    except ImportError:
        print(f"📦 Dependencia no encontrada: {package}. Instalando...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        print(f"✅ {package} instalado correctamente.")

# Lista de librerías requeridas por tu bot
check_and_install("discord.py", "discord")
check_and_install("python-dotenv", "dotenv")

# Ahora sí, el resto de tus importaciones normales
import discord
from discord.ext import commands
import os
from pathlib import Path
from dotenv import load_dotenv
import io
import logging
import vigilante   # evita que un bloqueo deje todo el bot congelado (ver vigilante.py)

# Configurar logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Cargar variables de entorno
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# Configurar intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

# Función para permitir el prefijo '!max' sin importar mayúsculas o minúsculas (!max, !MAX, !MAx, etc.)
def get_prefix(bot, message):
    content = message.content
    if len(content) >= 4 and content[:4].lower() == '!max':
        prefix = content[:4]
        if len(content) > 4 and content[4] == ' ':
            prefix += ' '
        return commands.when_mentioned_or(prefix)(bot, message)
    return commands.when_mentioned(bot, message)

# Crear bot usando la función de prefijos y manteniendo los comandos case-insensitive
bot = commands.Bot(command_prefix=get_prefix, intents=intents, case_insensitive=True)

# Detectar y crear BD si no existe
db_folder = Path(__file__).parent / 'database'
db_folder.mkdir(exist_ok=True)

# Cargar módulos (cogs)
async def load_cogs():
    """Cargar todos los cogs automáticamente"""
    cogs_path = Path(__file__).parent / 'cogs'
    cogs_path.mkdir(exist_ok=True)
    
    for filename in os.listdir(cogs_path):
        if filename.endswith('.py') and not filename.startswith('_'):
            cog_name = filename[:-3]
            try:
                await bot.load_extension(f'cogs.{cog_name}')
                logger.info(f'✅ Cog cargado: {cog_name}')
            except Exception as e:
                logger.error(f'❌ Error cargando {cog_name}: {e}')

@bot.event
async def on_ready():
    """Evento cuando el bot se conecta"""
    logger.info(f'🤖 Bot conectado como {bot.user}')
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name='!max help'
        )
    )

@bot.event
async def on_command_error(ctx, error):
    """Manejar errores de comandos"""
    if isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send('❌ No tienes permisos para usar este comando.')
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f'❌ Faltan argumentos. Usa `!max help` para más info.')
    else:
        logger.error(f'Error: {error}')
        await ctx.send(f'❌ Error: {error}')

@bot.command(name="diagnostico")
@commands.is_owner()
async def diagnostico(ctx):
    """Qué está esperando cada tarea del bot (solo el dueño). Úsalo cuando algo se queda 'pensando' y no termina."""
    texto = vigilante.volcar_tareas()
    logger.warning(f"[vigilante] diagnóstico pedido por {ctx.author}:\n{texto}")
    await ctx.send(file=discord.File(io.BytesIO(texto.encode("utf-8")), filename="tareas.txt"))

async def main():
    """Función principal"""
    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=64))   # más hilos para DNS y demás
    async with bot:
        await vigilante.iniciar(bot)   # arranca el vigilante (una sola vez, ya dentro del loop)
        await load_cogs()
        await bot.start(TOKEN)

if __name__ == '__main__':
    try:
        import asyncio
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('Bot detenido.')
    except Exception as e:
        logger.error(f'Error fatal: {e}')
        sys.exit(1)