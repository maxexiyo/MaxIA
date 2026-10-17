"""
Configuración central: API keys, directorios, límites de memoria de perfiles,
y el filtro de contenido que NUNCA se guarda en ningún perfil.
Todo lo que antes vivía suelto al inicio de IA.py ahora vive aquí, para que
IA.py y los mixins solo hagan `from config import ...` en vez de repetir esto.
"""
import os
import re

# 1. Obtención de API Keys desde el archivo .env (GROQ y GEMINI)
env_groq_keys = os.getenv("GROQ_API_KEYS", "") or os.getenv("GROQ_API_KEY", "")
GROQ_API_KEYS = [k.strip().strip('"').strip("'") for k in env_groq_keys.split(",") if k.strip()]

env_gemini_keys = os.getenv("GEMINI_API_KEYS", "") or os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
GEMINI_API_KEYS = [k.strip().strip('"').strip("'") for k in env_gemini_keys.split(",") if k.strip()]


def _entero_env(nombre: str, default: int) -> int:
    """Lee un entero del .env; si no está o viene mal escrito usa el default (antes un valor
    no numérico en CREATOR_DISCORD_ID tumbaba la carga completa del cog con un ValueError)."""
    try:
        return int(str(os.getenv(nombre, "")).strip() or default)
    except ValueError:
        print(f"⚠️ {nombre} en el .env no es un número válido, se usa {default}.")
        return default


# ✅ ID real de Discord del creador (maxexiyo), NO su display_name/apodo (eso lo puede poner
# cualquiera igual en otro server). Se usa para verificar de verdad quién es el creador en vez
# de solo confiar en una instrucción de texto que rechaza CUALQUIER afirmación, incluso la real.
CREATOR_DISCORD_ID = _entero_env("CREATOR_DISCORD_ID", 0)
if not CREATOR_DISCORD_ID:
    print("⚠️ CREATOR_DISCORD_ID no configurado en el .env - el bot no podrá reconocer a su creador real por ID.")

if not GEMINI_API_KEYS:
    raise ValueError("❌ No se encontraron API Keys de Gemini en el entorno o archivo .env")

print(f"🔑 Gemini: se detectaron {len(GEMINI_API_KEYS)} key(s) en el .env")
if GROQ_API_KEYS:
    print(f"🔑 Groq: se detectaron {len(GROQ_API_KEYS)} key(s) en el .env (respaldo activo)")
else:
    print("⚠️ Groq: NO se detectó ninguna key (GROQ_API_KEYS / GROQ_API_KEY vacías o no definidas). "
          "El respaldo a Groq NO va a funcionar hasta que configures al menos una.")

# 2. Configuración de Directorios Base
PROFILES_DIR = "server_profiles"

# 🧠 MEMORIA DE LA CHARLA (cuántos mensajes recientes del canal ve el bot en CADA respuesta).
# Antes era 5 (hasta 10 si se metía más gente) y por eso "se le olvidaba" lo que se acababa de hablar.
# Ahora son 20 (hasta 40 si hay varias personas en la plática). Todo se puede cambiar desde el .env sin
# tocar código. Súbelo si aún se le olvida; bájalo si Groq (el respaldo) empieza a quejarse de tokens.
MEMORIA_CHAT_MIN_MENSAJES = _entero_env("MEMORIA_CHAT_MIN_MENSAJES", 20)
MEMORIA_CHAT_MAX_MENSAJES = max(MEMORIA_CHAT_MIN_MENSAJES, _entero_env("MEMORIA_CHAT_MAX_MENSAJES", 40))
# Topes para que más memoria no se convierta en una factura de tokens: cada mensaje se recorta a este
# largo, y si el total pasa el tope se descartan los MÁS VIEJOS (los recientes siempre se conservan).
MEMORIA_CHAT_MAX_CHARS_MENSAJE = _entero_env("MEMORIA_CHAT_MAX_CHARS_MENSAJE", 700)
MEMORIA_CHAT_MAX_CHARS_TOTAL = _entero_env("MEMORIA_CHAT_MAX_CHARS_TOTAL", 9000)

# 🧠 Configuración de memoria de perfiles de usuario:
# - Antes cada dato se cortaba a 150 caracteres (muy poco para listas largas, ej. artistas).
# - Ahora el límite es por PALABRAS (más natural, no corta a media palabra) y lo público se
#   organiza en categorías/bloques, cada una con su propio cupo de datos guardados. Así una
#   lista larga de música no le quita espacio a otras categorías.
MAX_PALABRAS_POR_DATO = 70

# ✅ Si el modelo se niega a aplicar una instrucción de memoria (en vez de fallar sin respuesta,
# como cuando se agota la cuota) responde con una negación en texto normal — eso pasaba antes
# como respuesta válida y se guardaba tal cual como si fuera el dato real (ej. "I'm sorry, but I
# can't help with that." terminó guardado como perfil público). Se detecta y se descarta.
FRASES_RECHAZO_MODELO = [
    "i'm sorry, but i can't", "i'm sorry, but i cannot", "i am sorry, but i can",
    "as an ai", "i cannot assist", "i can't assist", "i cannot help", "i can't help with that",
    "lo siento, pero no puedo", "no puedo ayudarte con eso", "no puedo ayudar con eso",
    "no puedo continuar con esa", "no puedo procesar esa instrucción",
]

def _parece_rechazo_del_modelo(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    return len(t) < 200 and any(frase in t for frase in FRASES_RECHAZO_MODELO)

def _extraer_campo_texto(response_text: str, etiqueta: str, max_palabras: int = MAX_PALABRAS_POR_DATO):
    """Extrae el valor de un campo tipo 'ETIQUETA: valor' de la respuesta estructurada del modelo,
    parando en el siguiente label conocido (TIPO:/CATEGORIA:/PERSONA:/TEXTO_.*:/SIN_CAMBIOS) en vez
    de tragarse todo lo que sigue con un '.+' greedy — antes, si el modelo metía más de un bloque
    en la misma respuesta, el segundo bloque completo terminaba pegado dentro del dato guardado."""
    match = re.search(
        rf'{etiqueta}:\s*(.+?)(?=\n*(?:PERSONA:|TIPO:|CATEGORIA:|TEXTO_NUEVO:|TEXTO_ACTUALIZADO:|SIN_CAMBIOS)|\Z)',
        response_text, re.IGNORECASE | re.DOTALL
    )
    if not match:
        return None
    valor = match.group(1).strip()
    # Cinturón de seguridad extra: si por lo que sea se coló texto larguísimo, se recorta a
    # max_palabras*2 palabras (holgado, el prompt ya pide max_palabras) en vez de guardarlo entero.
    palabras = valor.split()
    if len(palabras) > max_palabras * 2:
        valor = " ".join(palabras[:max_palabras * 2])
    return valor
CATEGORIAS_PUBLICAS = ["musica", "gustos", "odios", "trabajo", "personalidad", "otro"]
MAX_FACTS_POR_CATEGORIA = {
    "musica": 25,
    "gustos": 30,
    "odios": 20,
    "trabajo": 8,
    "personalidad": 8,
    "otro": 10,
}
MAX_FACTS_PRIVADO = 40
ETIQUETAS_CATEGORIAS_PUBLICAS = {
    "musica": "Música",
    "gustos": "Gustos",
    "odios": "Lo que no le gusta / odia",
    "trabajo": "Trabajo",
    "personalidad": "Personalidad",
    "otro": "Otros datos",
}

# 🚫 CONTENIDO NUNCA GUARDABLE EN NINGÚN PERFIL (ni público ni privado), sin excepción, sin
# importar quién lo pidió o cómo esté redactado el dato. Esto corre como filtro EN CÓDIGO
# (no solo como instrucción del prompt) porque el modelo se puede equivocar o el prompt se
# puede ignorar - la última línea de defensa tiene que estar aquí, en el guardado mismo.
# Cubre: sexual/pornografía, salud física/mental (diagnósticos, medicamentos, terapia,
# adicciones), y orientación sexual / identidad de género (los pronombres normales sí se
# guardan aparte en 'genero', esto es sobre etiquetas de orientación/identidad).
PALABRAS_CONTENIDO_BLOQUEADO = [
    # Sexual / pornografía
    "porno", "pornografia", "pornografía", "masturba", "masturbación", "masturbacion",
    "sexo", "sexual", "sexuales", "eyacul", "orgasmo", "excita", "coger", "cogiendo",
    "desnud", "nudes", "onlyfans", "fetiche", "genitales", "pene", "vagina",
    # Salud física / mental
    "diagnostic", "diagnóstic", "enfermedad", "enferma de", "enfermo de", "depresion",
    "depresión", "ansiedad", "trastorno", "terapia", "psiquiatra", "psicólogo", "psicologo",
    "medicamento", "pastillas para", "tratamiento médico", "tratamiento medico",
    "autolesion", "autolesión", "suicid", "adicción", "adiccion", "alcohólic", "alcoholic",
    "drogadic", "rehabilitación", "rehabilitacion", "cáncer", "cancer", "vih", "embarazo",
    "embarazada", "aborto",
    # Orientación sexual / identidad de género (como ETIQUETA sobre la persona - los
    # pronombres normales van aparte en 'genero' y no pasan por este filtro)
    "es gay", "es lesbiana", "es bisexual", "es trans", "es homosexual", "orientación sexual",
    "orientacion sexual", "es heterosexual",
]

# El filtro busca cada palabra pegada al INICIO de una palabra (antes era una búsqueda de texto
# suelta y "escoger"/"recoger" se bloqueaban por contener "coger", "vihuela" por "vih", etc.).
# Las raíces como "suicid" o "masturba" siguen atrapando todas sus variantes (suicida, suicidio...).
# Solo estas dos son demasiado cortas y se piden como palabra completa: "penetrar", "vihuela".
_SOLO_PALABRA_COMPLETA = {"pene", "vih"}
_REGEX_CONTENIDO_BLOQUEADO = re.compile(
    "|".join(
        (rf"(?<!\w){re.escape(p)}(?!\w)" if p in _SOLO_PALABRA_COMPLETA else rf"(?<!\w){re.escape(p)}")
        for p in PALABRAS_CONTENIDO_BLOQUEADO
    ),
    re.IGNORECASE,
)

def _contiene_contenido_bloqueado(texto: str) -> bool:
    """True si el texto toca alguno de los temas que NUNCA se guardan en un perfil
    (sexual, salud, orientación/identidad como etiqueta). Se usa como filtro final antes
    de escribir cualquier dato a disco, sin importar de qué función venga."""
    if not texto:
        return False
    return _REGEX_CONTENIDO_BLOQUEADO.search(texto) is not None


if not os.path.exists(PROFILES_DIR):
    os.makedirs(PROFILES_DIR)

# 🗣️ CONFIG DEL MODO AMBIENTAL "POR LOTES" cuando el server está muy activo:
# en vez de llamar a la IA por cada mensaje, se agrupan varios y se evalúan de un jalón.
AMBIENT_ACTIVITY_WINDOW = 30      # segundos: ventana en la que contamos mensajes para medir actividad
AMBIENT_ACTIVITY_THRESHOLD = 3    # mensajes dentro de esa ventana para considerar el canal "muy activo"
AMBIENT_BATCH_SIZE = 20            # cuántos mensajes se juntan antes de evaluar en modo lote (entre 5 y 10)
AMBIENT_BATCH_IDLE_FLUSH = 12     # segundos sin mensajes nuevos para soltar el lote pendiente aunque no se haya llenado
AMBIENT_COOLDOWN_SECONDS = 180    # mínimo tiempo entre intervenciones ambientales por canal (súbelo/bájalo para que hable menos/más seguido)


# 🛠️ ANTI-AVALANCHA DEL MODO AMBIENTAL (parche):
# Antes el bot evaluaba CADA mensaje con la IA (el filtro random era `<= 1`, o sea siempre pasaba) y el
# único cooldown se activaba solo cuando el bot hablaba/reaccionaba; como casi siempre responde NADA, no había freno.
AMBIENT_EVAL_PROBABILITY = 1.0     # prob. de evaluar un mensaje suelto (1.0 = todos, como estaba; baja a 0.3 para gastar menos)
AMBIENT_MIN_EVAL_INTERVAL = 30     # seg mínimos entre evaluaciones por canal, haya hablado o no (sube a 60+ para gastar menos)


# 🎭 PERSONAJES: cada personalidad se muestra en el server como una "persona" distinta (nombre + foto, vía webhook).
# La personalidad normal (clave "max") habla con la cuenta del propio bot; las demás usan webhook.
# Nombres y fotos se pueden cambiar SIN tocar código con los comandos de admin (!max pjnombre / !max pjfoto) o con
# estas variables del .env. IMPORTANTE para las fotos: usa un link PERMANENTE (imgur, GitHub raw, tu hosting...);
# los links de archivos subidos a Discord caducan y la foto desaparecería a los pocos días.
PERSONAJES_DEFAULT = {
    "max":  {"nombre": "Maxia",       "avatar": os.getenv("AVATAR_MAXIA", "")},
    "neko": {"nombre": "Neko Maxia",  "avatar": os.getenv("AVATAR_NEKO", "")},
    "sexo": {"nombre": "Maxia Fatal", "avatar": os.getenv("AVATAR_SEXO", "")},
    "yuri": {"nombre": "Yuri",        "avatar": os.getenv("AVATAR_YURI", "")},
}
# 1 = los personajes distintos de "max" salen con su nombre y foto (webhook). 0 = todo sale con la cuenta del bot
# (con el nombre del personaje en negritas al inicio).
PERSONAJES_USAR_WEBHOOK = os.getenv("PERSONAJES_USAR_WEBHOOK", "1") != "0"

# 🤪 MODO LOCO (!max loco on): todos los personajes conviven en el canal como si fueran usuarios distintos.
# Frenos para que no se queden respondiéndose para siempre:
LOCO_MAX_CADENA = _entero_env("LOCO_MAX_CADENA", 4)            # máx. mensajes SEGUIDOS de personajes sin que hable una persona real
LOCO_MAX_TURNOS = _entero_env("LOCO_MAX_TURNOS", 2)            # máx. personajes que pueden hablar en una misma ronda
LOCO_PROB_APORTE = float(os.getenv("LOCO_PROB_APORTE", "0.6"))        # prob. de que otro personaje aporte tras una respuesta por comando
LOCO_PROB_CONTINUAR = float(os.getenv("LOCO_PROB_CONTINUAR", "0.7"))  # prob. base de que sigan platicando entre ellos
LOCO_DECAIMIENTO = float(os.getenv("LOCO_DECAIMIENTO", "0.5"))        # cada mensaje seguido de personajes multiplica esa prob. por esto
LOCO_ESPERA_MIN_S = _entero_env("LOCO_ESPERA_MIN_S", 6)               # segundos mínimos entre rondas en un mismo canal

