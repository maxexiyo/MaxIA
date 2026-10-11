import discord


def get_user_display_name(member: discord.Member) -> str:
    """Obtiene el nombre grande del perfil (global_name) o el nombre de usuario por defecto"""
    if not isinstance(member, (discord.Member, discord.User)):
        return "Desconocido"
    return member.global_name or member.name
