import re
import random
from typing import List, Tuple

def normalize_command(text: str) -> str:
    """Normalizar comando: remover espacios extra, minúsculas"""
    return re.sub(r'\s+', '', text.lower().strip())

def get_normalized_command(content: str) -> Tuple[str, str]:
    """
    Extraer comando normalizado y argumentos
    Retorna: (comando_normalizado, argumentos_originales)
    """
    # Remover prefijo '!max '
    if content.lower().startswith('!max '):
        content = content[5:].strip()
    
    # Separar comando de argumentos
    parts = content.split(None, 1)
    if not parts:
        return '', ''
    
    normalized_cmd = normalize_command(parts[0])
    args = parts[1] if len(parts) > 1 else ''
    
    return normalized_cmd, args

def check_similarity(cmd1: str, cmd2: str) -> bool:
    """Comparar comandos ignorando mayúsculas y espacios"""
    return normalize_command(cmd1) == normalize_command(cmd2)

def format_variables(text: str, usuario: str = '', nombredelserver: str = '', 
                    totalmiembros: int = 0, allmembers: int = 0) -> str:
    """Reemplazar variables en mensajes personalizados"""
    text = text.replace('[usuario]', usuario)
    text = text.replace('[nombredelserver]', nombredelserver)
    text = text.replace('[totalmiembros]', str(totalmiembros))
    text = text.replace('[allmembers]', str(allmembers))
    return text

def generate_markov_message(first_words: List[str], markov_chains: dict, max_length: int = 30) -> str:
    """Generar mensaje usando cadena de Markov"""
    if not first_words:
        return ''
    
    message = [random.choice(first_words)]
    
    for _ in range(max_length - 1):
        current_word = message[-1]
        
        if current_word not in markov_chains or not markov_chains[current_word]:
            break
        
        # Seleccionar siguiente palabra ponderada por frecuencia
        next_words = markov_chains[current_word]
        total_count = sum(count for _, count in next_words)
        
        rand = random.uniform(0, total_count)
        cumulative = 0
        
        for word, count in next_words:
            cumulative += count
            if rand <= cumulative:
                message.append(word)
                break
        else:
            message.append(next_words[0][0])
    
    return ' '.join(message)

def parse_variables_help() -> str:
    """Obtener texto de ayuda para variables personalizables"""
    return (
        '📝 **Variables disponibles:**\n'
        '`[usuario]` - Mencionar al miembro\n'
        '`[nombredelserver]` - Nombre del servidor\n'
        '`[totalmiembros]` - Cantidad de usuarios (sin bots)\n'
        '`[allmembers]` - Cantidad de usuarios (con bots)\n'
        '🎨 También puedes usar emojis personalizados y mencionar roles'
    )

def is_admin_or_mod(member, admin_roles: List[str] = None) -> bool:
    """Verificar si miembro es admin o mod"""
    if member.guild_permissions.administrator:
        return True
    
    if admin_roles:
        return any(role.name in admin_roles for role in member.roles)
    
    return False

def extract_user_mention(text: str):
    """Extraer ID de usuario de mención"""
    match = re.search(r'<@!?(\d+)>', text)
    return int(match.group(1)) if match else None

def extract_role_mention(text: str):
    """Extraer ID de rol de mención"""
    match = re.search(r'<@&(\d+)>', text)
    return int(match.group(1)) if match else None