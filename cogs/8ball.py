import discord
from discord import app_commands
from discord.ext import commands
import random
import logging

logger = logging.getLogger(__name__)

class EightBall(commands.Cog):
    """Cog para el comando 8ball con respuestas aleatorias en Embed"""
    
    def __init__(self, bot):
        self.bot = bot
        self.responses = [
            "Si",
            "sacaste premio",
            "premio o castigo?",
            "no",
            "no se",
            "pregunta mas al rato",
            "puede que si",
            "puede que no",
            "chi",
            "ño",
            "ñose",
            "posiblemente si",
            "posiblemente no",
            "ok mañana",
            "hoy no fio, mañana si",
            "no se, pero si quieres te puedo decir un chiste",
            "ni de pedo respondere eso",
            "no lo sé, preguntale a otro"
        ]
            

    @commands.hybrid_command(name='8ball', aliases=['8b'])
    @app_commands.describe(pregunta='Tu pregunta para la bola 8')
    async def eight_ball(self, ctx, *, pregunta: str = None):
        """Hazle una pregunta a la bola 8 mágica"""
        
        if not pregunta:
            await ctx.send('❌ Tienes que escribir una pregunta. Ejemplo: `!max 8ball ¿hoy es viernes?`')
            return
        
        answer = random.choice(self.responses)
        
        # Crear un embed bien estilizado
        embed = discord.Embed(
            title="🎱 Bola 8 Mágica",
            color=discord.Color.from_rgb(114, 137, 218) # Color estilo Discord blurple
        )
        embed.add_field(name="Pregunta:", value=pregunta[:1000], inline=False)
        embed.add_field(name="Respuesta:", value=f"**{answer}**", inline=False)
        
        # Pie de página con el usuario que preguntó
        avatar_url = ctx.author.display_avatar.url
        embed.set_footer(text=f"{ctx.author.display_name}", icon_url=avatar_url)
        
        try:
            await ctx.send(embed=embed)
            logger.info(f'8ball pregunto {ctx.author.name} con respuesta: {answer}')
        except Exception as e:
            logger.error(f'Error al enviar respuesta de 8ball: {e}')

async def setup(bot):
    await bot.add_cog(EightBall(bot))