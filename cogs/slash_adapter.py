"""
SlashMessage: imita lo mínimo de discord.Message para que el motor de la IA (process_ai_request) pueda
atender un /comando como si fuera un mensaje normal.

Cuando un comando entra por slash no existe un mensaje real al que responder (el "mensaje" que arma discord.py es
sintético y no se puede usar con reply). Este adaptador manda las respuestas con `enviar` (followup de la interacción)
y, si al final la IA no contestó con él (por ejemplo respondió por webhook como un personaje, o solo reaccionó),
`cerrar()` quita el "pensando..." de la interacción para que no se quede colgado.
"""
import discord


class SlashMessage:
    def __init__(self, *, interaction: discord.Interaction, autor, canal, guild, contenido: str, enviar, adjuntos=None):
        self._interaction = interaction
        self._enviar = enviar
        self._respondido = False
        self.id = interaction.id
        self.author = autor
        self.channel = canal
        self.guild = guild
        self.content = contenido or ""
        self.clean_content = self.content
        self.attachments = list(adjuntos or [])
        self.reference = None
        self.mentions = []
        self.role_mentions = []
        self.channel_mentions = []
        self.embeds = []
        self.stickers = []
        self.webhook_id = None
        self.created_at = interaction.created_at

    @classmethod
    def desde_interaction(cls, interaction: discord.Interaction, contenido: str, adjuntos=None):
        async def enviar(content=None, **kw):
            return await interaction.followup.send(content, wait=True, **kw)
        return cls(interaction=interaction, autor=interaction.user, canal=interaction.channel, guild=interaction.guild,
                   contenido=contenido, enviar=enviar, adjuntos=adjuntos)

    @classmethod
    def desde_ctx(cls, ctx, contenido: str, adjuntos=None):
        async def enviar(content=None, **kw):
            return await ctx.send(content, **kw)
        return cls(interaction=ctx.interaction, autor=ctx.author, canal=ctx.channel, guild=ctx.guild,
                   contenido=contenido, enviar=enviar, adjuntos=adjuntos)

    async def reply(self, content=None, **kw):
        kw.pop("mention_author", None)
        kw.pop("reference", None)
        self._respondido = True
        return await self._enviar(content, **kw)

    async def add_reaction(self, emoji):
        return None      # no hay mensaje real al que reaccionar

    async def cerrar(self):
        """Si la IA no contestó con reply(), se quita el 'pensando...' de la interacción."""
        if not self._respondido:
            try:
                await self._interaction.delete_original_response()
            except discord.HTTPException:
                pass
