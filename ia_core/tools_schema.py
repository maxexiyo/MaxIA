"""
Definición de las herramientas (function calling) que puede usar la IA,
en formato Groq/OpenAI (TOOLS_DEFINITIONS) y su conversión automática al
formato que espera Gemini (GEMINI_FUNCTION_DECLARATIONS). También la ruleta
de modelos de Gemini. Esto era antes un bloque suelto de ~250 líneas en medio
de IA.py, ahora vive solo aquí.
"""

# --- DEFINICIÓN DE HERRAMIENTAS PARA GROQ ---
TOOLS_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "buscar_historial",
            "description": "Busca mensajes pasados en la base de datos de este canal usando una palabra clave o tema.",
            "parameters": {
                "type": "object",
                "properties": {
                    "busqueda": {
                        "type": "string",
                        "description": "La palabra clave o tema a buscar en los mensajes (ej. 'tarea', 'link', 'juego')"
                    }
                },
                "required": ["busqueda"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "obtener_ultimos_mensajes",
            "description": "Obtiene los últimos N mensajes del historial del canal para entender el contexto o de qué estaban hablando.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cantidad": {
                        "type": "integer",
                        "description": "Número de mensajes a consultar (máximo 50)"
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Mensajes a saltar (para no repetir los ya vistos). Default: 0"
                    }
                },
                "required": ["cantidad"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_en_internet",
            "description": "Busca información actualizada en la web (noticias, eventos recientes, política, términos modernos o datos que desconozcas). ÚSALA cuando la pregunta trate de algo que pudo haber pasado o cambiado recientemente — no confíes en tu memoria interna para eso, aunque te parezca poco creíble o una teoría de conspiración: verifica primero.",
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {
                        "type": "string",
                        "description": "El término o pregunta exacta para buscar en la web"
                    }
                },
                "required": ["consulta"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_en_canal_especifico",
            "description": "Busca mensajes en un canal específico del servidor. Útil cuando el usuario menciona un canal concreto (ej: 'lo que dijiste en #general').",
            "parameters": {
                "type": "object",
                "properties": {
                    "canal": {
                        "type": "string",
                        "description": "Nombre del canal (sin #, ej: 'general', 'dibujos-likos', 'musica')"
                    },
                    "busqueda": {
                        "type": "string",
                        "description": "Palabra clave o tema a buscar en ese canal específico (opcional)"
                    },
                    "cantidad": {
                        "type": "integer",
                        "description": "Número de mensajes a obtener (máximo 50). Default: 30"
                    }
                },
                "required": ["canal"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "actualizar_nombre_preferido",
            "description": "Úsala cuando alguien te pida explícitamente que le digas de cierta forma (ej: 'llámame Fulanito', 'dime X', 'llámame así aquí'). Si dice 'aquí' o se refiere solo a este servidor, usa alcance='aqui'. Si no especifica nada, usa alcance='global' (aplica en cualquier servidor donde hables con él/ella).",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre_nuevo": {"type": "string", "description": "El nombre exacto que la persona quiere que uses con ella"},
                    "alcance": {"type": "string", "enum": ["global", "aqui"], "description": "'global' si aplica en todos lados, 'aqui' si solo es válido en este servidor"}
                },
                "required": ["nombre_nuevo", "alcance"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "actualizar_memoria_usuario",
            "description": "Úsala cuando alguien te pida (directa o indirectamente) agregar, corregir o borrar algo que sabes de él/ella o de otra persona mencionada (ej: 'ya no me gusta X, ahora me gusta Y', 'olvida que...', 'ya no juego eso', 'quita ese dato de mí'). Aplica el cambio directo en tu memoria de esa persona.",
            "parameters": {
                "type": "object",
                "properties": {
                    "persona": {"type": "string", "description": "Nombre de la persona de quien se habla, o 'yo' si es quien te escribe"},
                    "tipo": {"type": "string", "enum": ["publico", "privado"], "description": "Si el dato a cambiar es información pública o privada/sensible"},
                    "categoria": {"type": "string", "enum": ["musica", "gustos", "odios", "trabajo", "personalidad", "otro"], "description": "Solo si tipo es 'publico': a qué categoría pertenece el dato que se está cambiando. 'odios' es para lo que le choca/odia/no le gusta"},
                    "instruccion": {"type": "string", "description": "Qué cambiar exactamente, en tus palabras (ej: 'ya no le gusta el anime, ahora le gusta el k-pop', 'olvida que se le cayó algo de su casa')"}
                },
                "required": ["persona", "tipo", "instruccion"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "aprender_significado",
            "description": "Busca el significado de una palabra, jerga, modismo o término que NO conozcas (ej: albures, siglas nuevas, slang de internet). Se guarda en tu memoria para siempre, así que la próxima vez que lo veas ya lo sabrás sin buscar de nuevo. NO la uses para cosas que ya conoces de tu propio conocimiento (ej: si ya sabes que BTS es una banda de k-pop, no la busques) — solo para lo que genuinamente no sepas qué es.",
            "parameters": {
                "type": "object",
                "properties": {
                    "termino": {"type": "string", "description": "La palabra, jerga o término exacto que no conoces"}
                },
                "required": ["termino"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "guardar_cumpleanos",
            "description": "Guarda el día y mes de cumpleaños de alguien, junto con si quiere o no que lo felicites ese día. IMPORTANTE: NUNCA uses esta herramienta solo porque alguien mencionó su fecha de cumpleaños — SIEMPRE pregúntale primero, en texto normal, si quiere que lo felicites ese día (no falta gente a la que no le gusta que se lo recuerden). Usa esta herramienta SOLO después de que la persona te conteste esa pregunta. También úsala si alguien pide CAMBIAR su fecha ya guardada (sobrescribe la anterior).",
            "parameters": {
                "type": "object",
                "properties": {
                    "dia": {"type": "integer", "description": "Día del cumpleaños (1-31)"},
                    "mes": {"type": "integer", "description": "Mes del cumpleaños (1-12)"},
                    "felicitar": {"type": "boolean", "description": "true si la persona SÍ quiere que la felicites ese día, false si dijo que no"}
                },
                "required": ["dia", "mes", "felicitar"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "eliminar_cumpleanos",
            "description": "Borra por completo la fecha de cumpleaños guardada de alguien (deja de saberla y deja de felicitar). Úsala cuando alguien te pida específicamente que la olvides o la borres.",
            "parameters": {
                "type": "object",
                "properties": {
                    "persona": {"type": "string", "description": "Nombre de la persona, o 'yo' si es quien te escribe"}
                },
                "required": ["persona"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "obtener_hora_actual",
            "description": "Obtiene la hora actual exacta (y fecha). Úsala si alguien te pregunta qué hora es o cuánto falta/pasó desde cierta hora.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agregar_regla_servidor",
            "description": "Úsala si piden una regla de comportamiento PERMANENTE para todo el server (ej: 'no digas @everyone', 'prohibido hablar de política'). NO para peticiones de una sola vez ni datos de una persona (usa 'actualizar_memoria_usuario' para eso). Solo se aplica si quien lo pide es administrador.",
            "parameters": {
                "type": "object",
                "properties": {
                    "regla": {"type": "string", "description": "La regla en pocas palabras, como instrucción para ti misma"}
                },
                "required": ["regla"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "quitar_regla_servidor",
            "description": "Úsala si un admin pide borrar/desactivar una regla fija del server. Solo se aplica si quien lo pide es administrador.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identificador": {"type": "string", "description": "Número de la regla, o parte de su texto/tema"}
                },
                "required": ["identificador"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reaccionar_mensaje",
            "description": "Reacciona con un emoji al mensaje que te acaban de mandar, en vez de (o además de) escribir texto. Úsala si te piden reaccionar directamente, o si de plano basta un emoji.",
            "parameters": {
                "type": "object",
                "properties": {
                    "emoji": {"type": "string", "description": "Emoji unicode a usar (ej: 🐒). Si piden uno específico ('el del mono'), tradúcelo al emoji real. Si piden un emoji PERSONALIZADO del server (de la lista de EMOJIS PERSONALIZADOS DISPONIBLES), manda su nombre tal cual entre dos puntos, ej: ':nombre_emoji:'."}
                },
                "required": ["emoji"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agregar_canciones_musica",
            "description": "ÚNICA forma de poner música de verdad. Úsala cuando alguien te pida que pongas una canción, varias, una playlist o 'algo de [artista/género]'. Busca cada canción en YouTube Music, las mete a la fila y arranca la música (la persona debe estar en un canal de voz). Para una playlist, tú eliges las canciones y mandas la lista completa en 'canciones'. NUNCA digas que ya pusiste música o hiciste una playlist si no llamaste esta herramienta: sin ella NO suena ni se agrega NADA. Después de usarla, cuenta qué pasó según su resultado (si dice que algo falló, dilo tal cual).",
            "parameters": {
                "type": "object",
                "properties": {
                    "canciones": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de canciones a buscar, cada una como 'Artista - Título' (ej: ['Gorillaz - Feel Good Inc', 'Daft Punk - One More Time']). También acepta links de YouTube/playlist. Máximo 20."
                    }
                },
                "required": ["canciones"]
            }
        }
    }
]

def _convertir_schema_a_gemini(schema: dict) -> dict:
    """Convierte un JSON Schema estilo OpenAI/Groq (usado en TOOLS_DEFINITIONS) al formato que
    espera la API de Gemini (tipos en MAYÚSCULAS: OBJECT, STRING, INTEGER, BOOLEAN, ARRAY)."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out["type"] = v.upper()
        elif k == "properties" and isinstance(v, dict):
            out["properties"] = {pk: _convertir_schema_a_gemini(pv) for pk, pv in v.items()}
        elif k == "items":
            out["items"] = _convertir_schema_a_gemini(v)
        else:
            out[k] = v
    return out

# ✅ Mismas tools que usa Groq, pero convertidas al formato de Gemini, para que
# los 3 modelos puedan usar EXACTAMENTE las mismas funciones (memoria, historial, etc.) en vez
# de que Gemini solo pueda responder texto y termine "alucinando" un function call como texto plano.
GEMINI_FUNCTION_DECLARATIONS = [
    {
        "name": t["function"]["name"],
        "description": t["function"].get("description", ""),
        "parameters": _convertir_schema_a_gemini(t["function"].get("parameters", {"type": "object", "properties": {}})),
    }
    for t in TOOLS_DEFINITIONS
]

# ✅ Ruleta de MODELOS de Gemini (independiente de la ruleta de API keys). Los 3 son
# gratuitos, soportan visión + function calling (las mismas TOOLS_DEFINITIONS), y bajo
# consumo de tokens. Se prueban en este orden; solo se salta al siguiente modelo si el
# actual da 404 (no existe/deprecado) o 503/"overloaded"/"unavailable" (demanda alta) -
# los demás errores (ej. 429 por key) siguen rotando keys dentro del MISMO modelo.
GEMINI_MODELS_FALLBACK = [
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]
