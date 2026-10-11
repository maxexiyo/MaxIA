"""
MotorIAMixin: el "motor" que habla con los modelos. Resuelve emojis para
reacciones, ejecuta las tool calls, hace los requests a Groq y a Gemini
(con rotación pegajosa de keys/modelo), procesa/comprime imágenes adjuntas,
extrae emojis personalizados, aprende datos de usuario automáticamente y
detecta apodos mencionados al vuelo.
"""
import os
import re
import json
import base64
import io
import discord
import aiohttp
from datetime import datetime
from PIL import Image

from config import MAX_PALABRAS_POR_DATO, _parece_rechazo_del_modelo, _extraer_campo_texto
from tools_schema import (
    TOOLS_DEFINITIONS,
    GEMINI_FUNCTION_DECLARATIONS,
    GEMINI_MODELS_FALLBACK,
)

# GROQ_API_KEYS / GEMINI_API_KEYS se leen desde config, pero como aquí se
# rota sobre ellas en vivo, se importan directo también:
from config import GROQ_API_KEYS, GEMINI_API_KEYS  # noqa: F401 (usadas dentro de los métodos)


class MotorIAMixin:
    # --- REQUEST A GROQ (con rotación "pegajosa" de keys) ---
    def resolver_emoji_para_reaccion(self, emoji_texto: str, guild: discord.Guild = None):
        """Convierte lo que mandó la IA en algo que discord.Message.add_reaction pueda usar de verdad.
        Si es un emoji unicode normal (ej: 🐒) lo regresa tal cual. Si es un emoji PERSONALIZADO del
        server (':nombre:', o el tag completo '<:nombre:id>' que a veces la IA alucina con un id
        inventado), busca el emoji real por nombre entre los del server (y de respaldo, entre los de
        cualquier otro server donde esté el bot) y regresa el objeto discord.Emoji real - sin esto,
        add_reaction con un id inventado truena."""
        if not emoji_texto:
            return emoji_texto
        texto = emoji_texto.strip()

        # Si vino como tag completo '<a:nombre:id>' o '<:nombre:id>', nos quedamos solo con el nombre
        # (el id que trae puede ser inventado por la IA, así que no confiamos en él).
        match_tag = re.match(r'^<a?:([a-zA-Z0-9_]{2,32}):\d+>$', texto)
        nombre = match_tag.group(1) if match_tag else None

        # Si vino como ':nombre:' plano
        if nombre is None:
            match_colones = re.match(r'^:([a-zA-Z0-9_]{2,32}):$', texto)
            nombre = match_colones.group(1) if match_colones else None

        if nombre is None:
            return texto  # No parece emoji personalizado - se asume unicode normal, tal cual

        disponibles = {}
        if guild:
            for e in guild.emojis:
                disponibles[e.name.lower()] = e
        for e in self.bot.emojis:
            disponibles.setdefault(e.name.lower(), e)

        emoji_obj = disponibles.get(nombre.lower())
        return emoji_obj if emoji_obj else texto  # Si no lo encontramos, se intenta tal cual (probablemente falle)

    async def ejecutar_tool_call(self, tool_name: str, tool_input: dict, guild_id: int, channel_id: int,
                                  author_id: int = 0, author_name: str = "", message: discord.Message = None) -> str:
        """Ejecuta una herramienta pedida por el modelo (Groq o Gemini, las 2 usan las mismas)
        y devuelve el resultado en texto plano. Centralizado aquí para no repetir esta lógica en
        cada función que llama a un modelo con soporte de tools.
        'message' es el mensaje de Discord que se está procesando ahorita (si lo hay) - lo necesitan
        tools como 'reaccionar_mensaje' para saber a qué mensaje ponerle el emoji."""
        tool_result = None
        if tool_name == "obtener_hora_actual":
            ahora_tool = datetime.now()
            dias_es_tool = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
            tool_result = f"Son las {ahora_tool.strftime('%H:%M')} del {dias_es_tool[ahora_tool.weekday()]} {ahora_tool.day}/{ahora_tool.month}/{ahora_tool.year}"
        elif tool_name == "obtener_ultimos_mensajes":
            cantidad = min(tool_input.get("cantidad", 30), 50)
            offset = tool_input.get("offset", 0)
            msgs = self.get_guild_memory(guild_id, channel_id, cantidad, offset)
            tool_result = "\n".join([f"{u}: {c}" for u, r, c in msgs]) if msgs else "No hay mensajes disponibles"
        elif tool_name == "buscar_historial":
            keyword = tool_input.get("busqueda", "")
            msgs = self.search_guild_memory(guild_id, channel_id, keyword)
            tool_result = "\n".join([f"{u}: {c}" for u, r, c in msgs]) if msgs else "No se encontraron resultados"
        elif tool_name == "buscar_en_internet":
            query = tool_input.get("consulta", "")
            tool_result = await self.search_duckduckgo(query)
            print(f"🌐 [Búsqueda Web] Consulta: '{query}' -> Resultado: {tool_result[:300]}")
        elif tool_name == "buscar_en_canal_especifico":
            canal_name = tool_input.get("canal", "").lower()
            keyword = tool_input.get("busqueda", "")
            cantidad = min(tool_input.get("cantidad", 30), 50)

            # Intentar encontrar el ID del canal
            target_channel_id = self.get_channel_id_by_name(guild_id, canal_name)

            if not target_channel_id:
                # Buscar en todos los canales del servidor
                try:
                    for gc in self.bot.get_guild(guild_id).channels:
                        if gc.name.lower() == canal_name:
                            target_channel_id = gc.id
                            self.save_channel_mapping(guild_id, gc.id, gc.name)
                            break
                except: pass

            if target_channel_id:
                msgs = self.get_guild_memory_by_channel(guild_id, target_channel_id, cantidad, 0, keyword if keyword else None)
                tool_result = "\n".join([f"{u}: {c}" for u, r, c in msgs]) if msgs else f"No hay mensajes en #{canal_name}"
            else:
                tool_result = f"No encontré el canal #{canal_name} en este servidor"
        elif tool_name == "actualizar_nombre_preferido":
            nombre_nuevo = tool_input.get("nombre_nuevo", "").strip()
            alcance = tool_input.get("alcance", "global")
            if author_id and nombre_nuevo:
                self.save_preferred_name(author_id, author_name, nombre_nuevo, guild_id if alcance == "aqui" else 0)
                tool_result = f"Listo, ahora le digo {nombre_nuevo} a {author_name} ({'solo en este servidor' if alcance == 'aqui' else 'en cualquier lado'})."
                print(f"✏️ [Nombre] {author_name} (ID {author_id}) pidió que le digan '{nombre_nuevo}' ({alcance})")
            else:
                tool_result = "No pude identificar a quién actualizar el nombre."
        elif tool_name == "actualizar_memoria_usuario":
            persona = tool_input.get("persona", "").strip().lower()
            tipo_mem = tool_input.get("tipo", "publico")
            categoria_mem = tool_input.get("categoria")
            instruccion = tool_input.get("instruccion", "")

            target_id, target_name = None, None
            if persona in ["yo", "mismo", "misma", "mi", "me", ""] and author_id:
                target_id, target_name = author_id, author_name
            else:
                profiles_all = self.load_user_profiles()
                for pid, pdata in profiles_all.items():
                    if not isinstance(pdata, dict): continue
                    nombres_posibles = [pdata.get("nombre_global", ""), pdata.get("nombre_preferido", "")] + list(pdata.get("nombres_por_servidor", {}).values())
                    if any(persona and persona in (n or "").lower() for n in nombres_posibles):
                        target_id, target_name = int(pid), pdata.get("nombre_global", persona)
                        break

            if target_id and instruccion:
                tool_result = await self.actualizar_memoria_con_instruccion(target_id, target_name, tipo_mem, instruccion, categoria=categoria_mem)
                print(f"✏️ [Memoria Editada] {target_name} (ID {target_id}) [{tipo_mem}]: {instruccion}")
            else:
                tool_result = f"No encontré a '{persona}' en mi memoria para editar sus datos."
        elif tool_name == "aprender_significado":
            termino = tool_input.get("termino", "").strip()
            clave = termino.lower()
            glosario_actual = self.load_glosario()
            if clave in glosario_actual:
                tool_result = f"Ya lo sabes: {glosario_actual[clave]}"
            else:
                resultado_busqueda = await self.search_duckduckgo(f"que significa {termino}")
                resumen = resultado_busqueda[:400]
                self.save_glosario_termino(clave, resumen)
                tool_result = resumen
                print(f"📖 [Glosario] Término nuevo aprendido: '{termino}' -> {resumen[:150]}")
        elif tool_name == "guardar_cumpleanos":
            dia = tool_input.get("dia", 0)
            mes = tool_input.get("mes", 0)
            felicitar = tool_input.get("felicitar", False)
            if author_id and 1 <= dia <= 31 and 1 <= mes <= 12:
                self.save_user_birthday(author_id, author_name, mes, dia, felicitar)
                tool_result = f"Guardado: cumpleaños {dia}/{mes}. ¿Felicitar ese día? {'Sí' if felicitar else 'No'}."
                print(f"🎂 [Cumpleaños Guardado] {author_name} (ID {author_id}): {dia}/{mes} - felicitar: {felicitar}")
            else:
                tool_result = "No pude guardar la fecha, datos inválidos."
        elif tool_name == "eliminar_cumpleanos":
            persona = tool_input.get("persona", "").strip().lower()
            target_id, target_name = None, None
            if persona in ["yo", "mismo", "misma", "mi", "me", ""] and author_id:
                target_id, target_name = author_id, author_name
            else:
                profiles_all = self.load_user_profiles()
                for pid, pdata in profiles_all.items():
                    if not isinstance(pdata, dict): continue
                    nombres_posibles = [pdata.get("nombre_global", ""), pdata.get("nombre_preferido", "")] + list(pdata.get("nombres_por_servidor", {}).values())
                    if any(persona and persona in (n or "").lower() for n in nombres_posibles):
                        target_id, target_name = int(pid), pdata.get("nombre_global", persona)
                        break
            if target_id:
                self.delete_user_birthday(target_id, target_name)
                tool_result = f"Listo, borré la fecha de cumpleaños de {target_name}."
                print(f"🗑️ [Cumpleaños Eliminado] {target_name} (ID {target_id})")
            else:
                tool_result = f"No encontré a '{persona}' en mi memoria para borrar su cumpleaños."
        elif tool_name == "agregar_regla_servidor":
            regla_texto = tool_input.get("regla", "").strip()
            if not guild_id:
                tool_result = "Las reglas fijas solo aplican dentro de un servidor, no en DM."
            elif not regla_texto:
                tool_result = "No me quedó claro cuál sería la regla exacta, pide que la repitan más claro."
            elif not self._es_admin_del_server(guild_id, author_id):
                tool_result = "No pude agregar esa regla: solo un administrador del servidor puede fijar reglas nuevas."
                print(f"🚫 [Regla Rechazada] {author_name} (ID {author_id}) intentó agregar una regla sin ser admin: {regla_texto}")
            else:
                rid = self.add_server_rule(guild_id, regla_texto, author_name)
                tool_result = f"Listo, regla #{rid} guardada: \"{regla_texto}\". La voy a respetar siempre en este servidor."
                print(f"📏 [Regla Agregada por IA] Guild {guild_id} - {author_name}: {regla_texto}")
        elif tool_name == "quitar_regla_servidor":
            identificador = tool_input.get("identificador", "").strip()
            if not guild_id:
                tool_result = "Las reglas fijas solo aplican dentro de un servidor, no en DM."
            elif not self._es_admin_del_server(guild_id, author_id):
                tool_result = "No pude quitar esa regla: solo un administrador del servidor puede hacerlo."
                print(f"🚫 [Regla Rechazada] {author_name} (ID {author_id}) intentó quitar una regla sin ser admin: {identificador}")
            else:
                quitada = self.remove_server_rule_fuzzy(guild_id, identificador)
                if quitada:
                    tool_result = f"Listo, quité la regla: \"{quitada}\"."
                    print(f"🗑️ [Regla Quitada por IA] Guild {guild_id} - {author_name} quitó: {quitada}")
                else:
                    tool_result = f"No encontré ninguna regla que coincida con '{identificador}'."
        elif tool_name == "reaccionar_mensaje":
            emoji = tool_input.get("emoji", "").strip()
            if not emoji:
                tool_result = "No me quedó claro qué emoji usar."
            elif not message:
                tool_result = "No tengo a qué mensaje reaccionar ahorita."
            else:
                emoji_resuelto = self.resolver_emoji_para_reaccion(emoji, message.guild)
                try:
                    await message.add_reaction(emoji_resuelto)
                    tool_result = f"Listo, reaccioné con {emoji_resuelto}."
                    print(f"👍 [Reacción - Tool] Reaccionó con {emoji_resuelto} al mensaje de {author_name}")
                except Exception as e:
                    tool_result = f"No pude reaccionar con ese emoji."
                    print(f"⚠️ Error reaccionando con '{emoji}' (resuelto: {emoji_resuelto}): {e}")
        elif tool_name == "agregar_canciones_musica":
            canciones = tool_input.get("canciones") or []
            if isinstance(canciones, str):
                canciones = [canciones]
            # El cog de música (cogs/musica.py) es el que sabe buscar, meter a la fila y reproducir
            cog_musica = next((c for c in self.bot.cogs.values() if hasattr(c, "agregar_desde_ia")), None)
            guild_obj = self.bot.get_guild(guild_id) if guild_id else None
            miembro = guild_obj.get_member(author_id) if guild_obj and author_id else None
            if cog_musica is None:
                tool_result = "El módulo de música no está cargado ahorita, no pude poner nada."
            elif not guild_obj or not miembro:
                tool_result = "La música solo funciona dentro de un servidor, no en DM."
            else:
                try:
                    tool_result = await cog_musica.agregar_desde_ia(miembro, message, canciones)
                except Exception as e:
                    tool_result = f"No pude poner la música por un error ({type(e).__name__})."
                    print(f"⚠️ Error en tool agregar_canciones_musica: {e!r}")
            print(f"🎵 [Música - Tool] {author_name}: {canciones} -> {tool_result[:200]}")

        return tool_result or "Error"

    async def request_groq(self, system_instruction: str, history: list, prompt: str, guild_id: int, channel_id: int,
                            images: list = None, author_id: int = 0, author_name: str = "", usar_tools: bool = True,
                            target_message: discord.Message = None) -> tuple:
        if not images: images = []

        num_keys = len(GROQ_API_KEYS)
        # ✅ Rotación pegajosa: empieza en la última key que funcionó, no siempre en la #1.
        # Si falla, prueba la siguiente en orden hasta encontrar una que sirva, y se queda ahí.
        orden_intentos = [(self.groq_active_idx + i) % num_keys for i in range(num_keys)]

        # 🚧 Si el modelo de VISIÓN está saturado ("over capacity"), es un problema del modelo
        # completo en Groq, no de una key en particular - reintentar con las otras 5 keys no
        # sirve de nada y solo hace perder tiempo. Se corta después de 2 intentos como mucho.
        intentos_saturacion = 0

        for pos in orden_intentos:
            groq_idx = pos + 1
            groq_key = GROQ_API_KEYS[pos]
            try:
                url = "https://api.groq.com/openai/v1/chat/completions"
                headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
                
                messages = [{"role": "system", "content": system_instruction}]
                # ✅ ARREGLADO: history ya viene como diccionarios desde get_guild_memory_by_channel
                for msg in history:
                    if isinstance(msg, dict):
                        msg_role = msg.get("role", "user")
                        if msg_role == "model":
                            msg_role = "assistant"
                        messages.append({"role": msg_role, "content": msg.get("content", "")})
                    else:
                        # Por si acaso viene como tupla (user_name, role, content)
                        user_name, role, content = msg
                        # ✅ El rol se guarda como "model" (formato Gemini); Groq/OpenAI exigen "assistant"
                        role = "assistant" if role == "model" else role
                        messages.append({"role": role, "content": f"[{user_name}]: {content}"})
                
                content_parts = [{"type": "text", "text": prompt}]
                for mime, b64 in images:
                    content_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                
                messages.append({"role": "user", "content": content_parts if len(content_parts) > 1 else prompt})
                
                # ✅ Modelo dinámico: texto -> GPT-OSS 120B, imagen -> Qwen 3.6 (soporta visión)
                # (llama-3.3-70b-versatile fue deprecado por Groq el 17/jun/2026)
                usa_imagen = len(images) > 0
                modelo = "qwen/qwen3.6-27b" if usa_imagen else "openai/gpt-oss-120b"
                
                payload = {
                    "model": modelo,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 1500,
                    # ✅ FIX: tanto gpt-oss-120b (texto) como Qwen (imagen) son modelos "reasoning"
                    # que por default piensan en voz alta (chain-of-thought) antes de responder o de
                    # decidir un tool call, comiéndose el max_tokens en puro razonamiento y dejando
                    # sin espacio para la respuesta real (sobre todo en la 2da llamada, tras usar una
                    # tool como buscar_en_internet) -> caía en el fallback de "saturado de cuota" o
                    # de plano no respondía.
                    # ⚠️ IMPORTANTE: cada modelo acepta valores DISTINTOS para reasoning_effort y
                    # Groq tira 400 si mandas uno que ese modelo no soporta:
                    #   - openai/gpt-oss-120b (texto): solo low/medium/high (NO acepta "none")
                    #   - qwen/qwen3.6-27b (imagen): solo none/default (NO acepta low/medium/high)
                    "reasoning_effort": "none" if usa_imagen else "low"
                }
                # ✅ Las llamadas INTERNAS (clasificar/reescribir un dato de memoria, detectar
                # info de terceros, etc.) no deben poder invocar herramientas: si el modelo
                # decidiera llamar una tool aquí, se entraría en la rama de "2da llamada tras
                # tools", y si esa 2da llamada se queda sin cuota, el fallback textual ("ando
                # bien saturado de cuota ahorita...") se devolvía como si fuera la respuesta real
                # y terminaba GUARDADO como dato de perfil. Sin tools, el modelo solo puede
                # responder texto plano, así que ese camino de corrupción queda cerrado de raíz.
                if usar_tools:
                    payload["tools"] = TOOLS_DEFINITIONS
                    payload["tool_choice"] = "auto"
                
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers=headers, timeout=30) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            response_message = data['choices'][0]['message']
                            
                            if response_message.get('tool_calls'):
                                messages.append(response_message)
                                tool_summaries = []
                                tool_names = []
                                for tool_call in response_message['tool_calls']:
                                    tool_name = tool_call['function']['name']
                                    tool_input = json.loads(tool_call['function']['arguments'])
                                    tool_names.append(tool_name)
                                    print(f"🔧 [Tool Call] El modelo pidió usar: {tool_name} con args: {tool_input}")

                                    tool_result = await self.ejecutar_tool_call(tool_name, tool_input, guild_id, channel_id, author_id, author_name, target_message)

                                    # ✅ Igual que Meta AI en WhatsApp, puede reaccionar y quedarse ahí, o reaccionar
                                    # Y ADEMÁS comentar algo con texto - se lo dejamos decidir a él mismo en la
                                    # respuesta de texto que viene después (si de plano no quiere agregar nada,
                                    # que responda ÚNICAMENTE "NADA" y no se manda ningún mensaje extra).
                                    if tool_name == "reaccionar_mensaje":
                                        tool_result = (
                                            f"{tool_result} Ya reaccionaste. Ahora decide: si con la reacción "
                                            f"basta y no quieres agregar nada más, responde ÚNICAMENTE la palabra "
                                            f"NADA. Si además quieres comentar algo (breve, en personaje), escríbelo normal."
                                        )

                                    messages.append({"role": "tool", "tool_call_id": tool_call['id'], "content": tool_result or "Error"})
                                    tool_summaries.append(tool_result or "Error")

                                # ✅ FIX: antes, si esta 2da llamada fallaba (ej. esta misma key se quedó sin cuota
                                # justo después de ejecutar la herramienta), no se reintentaba con otra key ni se
                                # devolvía nada — la acción ya se había aplicado (ej. cumpleaños guardado) pero el
                                # usuario se quedaba sin respuesta. Ahora se reintenta la 2da llamada con TODAS las
                                # keys de Groq (ya no se vuelve a ejecutar la herramienta, eso ya quedó hecho arriba),
                                # y si de plano ninguna responde, se manda una confirmación directa en vez de nada.
                                final_response = None
                                final_groq_idx = groq_idx
                                for pos2 in orden_intentos:
                                    key2 = GROQ_API_KEYS[pos2]
                                    headers2 = {"Authorization": f"Bearer {key2}", "Content-Type": "application/json"}
                                    try:
                                        async with session.post(url, json={**payload, "messages": messages}, headers=headers2, timeout=30) as resp2:
                                            if resp2.status == 200:
                                                data2 = await resp2.json()
                                                final_response = data2['choices'][0]['message']['content']
                                                self.groq_active_idx = pos2  # ✅ se queda en esta key mientras funcione
                                                final_groq_idx = pos2 + 1
                                                break
                                            else:
                                                error_body2 = await resp2.text()
                                                print(f"⚠️ Groq Key #{pos2 + 1} - (2da llamada, tras tools) Status {resp2.status} - Detalle: {error_body2[:300]}")
                                    except Exception as e2:
                                        print(f"⚠️ Error Groq Key #{pos2 + 1} en 2da llamada (tras tools): {e2}")

                                if final_response:
                                    texto_final = self.clean_ai_response(final_response)
                                    if "reaccionar_mensaje" in tool_names and (not texto_final or texto_final.strip().upper() == "NADA"):
                                        print(f"👀 [Solo Reacción] El modelo decidió que la reacción bastaba, sin texto adicional.")
                                        return self.SOLO_REACCION, final_groq_idx
                                    return texto_final, final_groq_idx
                                else:
                                    # Las herramientas SÍ se ejecutaron (ej. se guardó el cumpleaños) pero ninguna
                                    # key pudo generar la respuesta natural. Mandamos una confirmación directa para
                                    # que el usuario nunca se quede sin respuesta cuando la acción sí se aplicó.
                                    if "reaccionar_mensaje" in tool_names and all(n == "reaccionar_mensaje" for n in tool_names):
                                        # Ya reaccionó y no hay nada más que confirmar por texto - mejor no mandar
                                        # el mensaje feo de "saturado de cuota" solo por no poder redactar un extra.
                                        return self.SOLO_REACCION, groq_idx
                                    resumen = " | ".join(tool_summaries) if tool_summaries else "Listo."
                                    print(f"⚠️ [Fallback Tools] Se agotó la cuota para generar respuesta natural tras usar herramientas. Confirmación directa: {resumen}")
                                    return f"ando bien saturado de cuota ahorita pero sí se aplicó: {resumen} :v", groq_idx
                            else:
                                print(f"ℹ️ [Sin Tool] El modelo respondió directo, sin usar ninguna herramienta.")
                                self.groq_active_idx = pos  # ✅ se queda en esta key mientras funcione
                                return self.clean_ai_response(response_message['content']), groq_idx
                        elif resp.status == 429:
                            print(f"⚠️ Groq Key #{groq_idx} - Límite de tasa alcanzado. Rotando a la siguiente...")
                            continue
                        else:
                            error_body = await resp.text()
                            print(f"⚠️ Groq Key #{groq_idx} - Status {resp.status} - Detalle: {error_body}")
                            if usa_imagen and resp.status == 503 and "over capacity" in error_body.lower():
                                intentos_saturacion += 1
                                if intentos_saturacion >= 2:
                                    print(f"🚧 [Visión Saturada] '{modelo}' lleva {intentos_saturacion} intentos con 'over capacity' - dejo de rotar keys, paso a Gemini como respaldo.")
                                    break
                            continue
            except Exception as e:
                print(f"⚠️ Error Groq Key #{groq_idx}: {e}")
                continue

        return None, None

    # --- REQUEST A GEMINI (RESPALDO, con rotación "pegajosa" de MODELO y de key) ---
    async def request_gemini(self, system_instruction: str, history: list, prompt: str, images: list = None,
                              guild_id: int = 0, channel_id: int = 0, author_id: int = 0, author_name: str = "",
                              usar_tools: bool = False, target_message: discord.Message = None) -> tuple:
        """Igual que antes, pero ahora hay DOS ruletas: la de MODELO (GEMINI_MODELS_FALLBACK) por
        fuera, y la de API KEY (GEMINI_API_KEYS) por dentro - ambas 'pegajosas' (se quedan en lo
        último que funcionó). Solo se salta al siguiente MODELO cuando el problema es del modelo
        en sí (404 no existe/deprecado, o 503/"overloaded"/"unavailable" por demanda alta) - un
        429 normal de cuota sigue rotando keys dentro del mismo modelo, igual que antes."""
        if not GEMINI_API_KEYS: return None, None
        if not images: images = []

        num_keys = len(GEMINI_API_KEYS)
        num_modelos = len(GEMINI_MODELS_FALLBACK)
        orden_keys = [(self.gemini_active_idx + i) % num_keys for i in range(num_keys)]
        orden_modelos = [(self.gemini_active_model_idx + i) % num_modelos for i in range(num_modelos)]

        contents = [
            {"role": "user", "parts": [{"text": system_instruction}]},
            {"role": "model", "parts": [{"text": "Entendido, estoy listo para actuar según las instrucciones."}]}
        ]

        # ✅ ARREGLADO: history ya viene como diccionarios
        for msg in history:
            if isinstance(msg, dict):
                h_role = msg.get("role", "user")
                h_content = msg.get("content", "")
            else:
                # Por si acaso viene como tupla
                user_name, h_role, h_content = msg
            contents.append({"role": "user" if h_role == "user" else "model", "parts": [{"text": h_content}]})

        prompt_parts = [{"text": prompt}]
        for mime, b64 in images:
            prompt_parts.append({"inlineData": {"mimeType": mime, "data": b64}})

        contents.append({"role": "user", "parts": prompt_parts})

        payload = {"contents": contents, "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1000}}
        if usar_tools:
            payload["tools"] = [{"functionDeclarations": GEMINI_FUNCTION_DECLARATIONS}]

        headers = {"Content-Type": "application/json"}

        for mpos in orden_modelos:
            modelo_actual = GEMINI_MODELS_FALLBACK[mpos]
            saltar_modelo = False

            for pos in orden_keys:
                gemini_idx = pos + 1
                gemini_key = GEMINI_API_KEYS[pos]
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo_actual}:generateContent?key={gemini_key}"

                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, json=payload, headers=headers, timeout=30) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if 'candidates' not in data or not data['candidates']:
                                    motivo = data.get('promptFeedback', data)
                                    print(f"⚠️ Gemini Key #{gemini_idx} [{modelo_actual}] - Sin candidates. Motivo: {str(motivo)[:300]}")
                                    continue

                                parts = data['candidates'][0].get('content', {}).get('parts', [])
                                function_call_part = next((p for p in parts if 'functionCall' in p), None)

                                if function_call_part and usar_tools:
                                    fc = function_call_part['functionCall']
                                    tool_name = fc.get('name')
                                    tool_input = fc.get('args') or {}
                                    print(f"🔧 [Tool Call - Gemini/{modelo_actual}] El modelo pidió usar: {tool_name} con args: {tool_input}")

                                    tool_result = await self.ejecutar_tool_call(tool_name, tool_input, guild_id, channel_id, author_id, author_name, target_message)

                                    # ✅ Igual que Meta AI en WhatsApp: tras reaccionar puede quedarse solo con eso, o
                                    # además comentar algo con texto - se lo dejamos decidir a él en la 2da llamada.
                                    es_reaccion = tool_name == "reaccionar_mensaje"
                                    if es_reaccion:
                                        tool_result = (
                                            f"{tool_result} Ya reaccionaste. Ahora decide: si con la reacción basta y "
                                            f"no quieres agregar nada más, responde ÚNICAMENTE la palabra NADA. Si además "
                                            f"quieres comentar algo (breve, en personaje), escríbelo normal."
                                        )

                                    # ✅ Gemini 3.x exige que se preserve el thought_signature que viene pegado
                                    # al part de functionCall original — si se reconstruye el part a mano (solo
                                    # con 'functionCall') se pierde y la 2da llamada truena con 400. Por eso se
                                    # reusa function_call_part completo, tal cual lo mandó la API, tenga o no
                                    # thoughtSignature.
                                    contents.append({"role": "model", "parts": [function_call_part]})
                                    contents.append({"role": "user", "parts": [{"functionResponse": {"name": tool_name, "response": {"result": tool_result or "Error"}}}]})
                                    payload["contents"] = contents

                                    # Segunda llamada para que redacte la respuesta final ya con el resultado de la
                                    # tool - se queda en el MISMO modelo, reintentando con todas las keys por si
                                    # esta se queda sin cuota justo después.
                                    final_text = None
                                    final_gemini_idx = gemini_idx
                                    for pos2 in orden_keys:
                                        key2 = GEMINI_API_KEYS[pos2]
                                        url2 = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo_actual}:generateContent?key={key2}"
                                        try:
                                            async with session.post(url2, json=payload, headers=headers, timeout=30) as resp2:
                                                if resp2.status == 200:
                                                    data2 = await resp2.json()
                                                    cands2 = data2.get('candidates')
                                                    if cands2:
                                                        parts2 = cands2[0].get('content', {}).get('parts', [])
                                                        texto2 = "".join(p.get('text', '') for p in parts2 if 'text' in p)
                                                        if texto2:
                                                            self.gemini_active_idx = pos2
                                                            self.gemini_active_model_idx = mpos
                                                            final_gemini_idx = pos2 + 1
                                                            final_text = texto2
                                                            break
                                                else:
                                                    error_body2 = await resp2.text()
                                                    print(f"⚠️ Gemini Key #{pos2 + 1} [{modelo_actual}] - (2da llamada, tras tools) Status {resp2.status} - Detalle: {error_body2[:300]}")
                                        except Exception as e2:
                                            print(f"⚠️ Error Gemini Key #{pos2 + 1} [{modelo_actual}] en 2da llamada (tras tools): {e2}")

                                    if final_text:
                                        texto_final = self.clean_ai_response(final_text)
                                        if es_reaccion and (not texto_final or texto_final.strip().upper() == "NADA"):
                                            print(f"👀 [Solo Reacción - Gemini] El modelo decidió que la reacción bastaba, sin texto adicional.")
                                            return self.SOLO_REACCION, final_gemini_idx
                                        return texto_final, final_gemini_idx
                                    else:
                                        self.gemini_active_idx = pos
                                        self.gemini_active_model_idx = mpos
                                        if es_reaccion:
                                            return self.SOLO_REACCION, gemini_idx
                                        return f"ando bien saturado de cuota ahorita pero sí se aplicó: {tool_result} :v", gemini_idx

                                response_text = "".join(p.get('text', '') for p in parts if 'text' in p)
                                if not response_text:
                                    print(f"⚠️ Gemini Key #{gemini_idx} [{modelo_actual}] - candidates sin texto ni functionCall utilizable.")
                                    continue
                                self.gemini_active_idx = pos
                                self.gemini_active_model_idx = mpos
                                return self.clean_ai_response(response_text), gemini_idx
                            else:
                                error_body = await resp.text()
                                print(f"⚠️ Gemini Key #{gemini_idx} [{modelo_actual}] - Status {resp.status} - Detalle: {error_body[:300]}")
                                cuerpo_low = error_body.lower()
                                # Problema del MODELO (no de la key): no existe/deprecado, o saturado por demanda alta.
                                if resp.status == 404 or resp.status == 503 or "overloaded" in cuerpo_low or "unavailable" in cuerpo_low or "not found" in cuerpo_low:
                                    saltar_modelo = True
                                    break
                                continue
                except Exception as e:
                    print(f"⚠️ Error Gemini Key #{gemini_idx} [{modelo_actual}]: {e}")
                    continue

            if saltar_modelo:
                print(f"🔄 [Gemini] Modelo '{modelo_actual}' con problemas (no disponible/demanda alta) - probando siguiente modelo de respaldo...")
                continue

        return None, None

    def comprimir_imagen(self, img_bytes: bytes, mime: str) -> tuple:
        """Reduce el tamaño de la imagen antes de mandarla en base64 a Groq/Gemini.
        Sin esto, una foto de Discord (a veces varios MB) se come toda la cuota de
        tokens por minuto (8000 TPM) de un solo mensaje. Bajamos resolución máxima
        y comprimimos a JPEG/WebP con buena calidad mientras se mantiene legible."""
        try:
            img = Image.open(io.BytesIO(img_bytes))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            max_lado = 1280  # Aumentado para mejor calidad en detalles
            if max(img.size) > max_lado:
                img.thumbnail((max_lado, max_lado), Image.LANCZOS)
            buffer = io.BytesIO()
            # Calidad más alta (85) para mantener detalles importantes
            # optimize=True reduce tamaño sin perder mucho
            img.save(buffer, format="JPEG", quality=85, optimize=True)
            compressed = buffer.getvalue()
            return "image/jpeg", compressed
        except Exception as e:
            print(f"⚠️ No se pudo comprimir la imagen, se manda tal cual: {e}")
            return mime, img_bytes

    def get_attachment_mime(self, attachment) -> str:
        content_type = attachment.content_type or ""
        if content_type.startswith("image/"): return content_type
        if content_type.startswith("application/pdf"): return "application/pdf"
        return None

    @staticmethod
    def limpiar_emojis_texto(text: str) -> str:
        """Convierte los tags crudos de Discord para emojis personalizados (<:nombre:123456789>,
        <a:nombre:123456789>) a algo legible (:nombre:) antes de que el texto llegue al modelo o se
        guarde en el historial. Sin esto, la IA ve el ID numérico crudo y a veces lo confunde con un
        dato real, un enlace o hasta piensa que se le mandó una imagen."""
        if not text: return text
        return re.sub(r'<a?:(\w+):\d+>', r':\1:', text)

    async def get_message_images(self, message: discord.Message) -> list:
        """Saca y comprime las imágenes adjuntas de un mensaje (mismo procesamiento que usa el
        modo normal), para reusarla también en el modo ambiental. Devuelve lista de (mime, base64)."""
        imagenes = []
        if not message.attachments:
            return imagenes
        for attachment in message.attachments:
            mime = self.get_attachment_mime(attachment)
            if mime and mime != "application/pdf":
                try:
                    img_bytes = await attachment.read()
                    mime, img_bytes = self.comprimir_imagen(img_bytes, mime)
                    b64_str = base64.b64encode(img_bytes).decode('utf-8')
                    imagenes.append((mime, b64_str))
                except Exception as e:
                    print(f"⚠️ Error leyendo imagen adjunta en modo ambiental: {e}")
        return imagenes

    async def extract_custom_emojis(self, text: str) -> list:
        """Saca los emojis personalizados de Discord (<:nombre:id> / <a:nombre:id>) del texto, incluyendo
        su imagen (para que la IA de verdad "vea" qué emoji es) y su nombre. Devuelve lista de
        (mime, img_bytes, b64_str, emoji_name)."""
        if not text: return []
        matches = re.findall(r'<a?:(\w+):(\d+)>', text)
        results = []
        for emoji_name, emoji_id in matches:
            try:
                emoji_url = f"https://cdn.discordapp.com/emojis/{emoji_id}.png"
                async with aiohttp.ClientSession() as session:
                    async with session.get(emoji_url, timeout=5) as resp:
                        if resp.status == 200:
                            img_bytes = await resp.read()
                            mime_c, img_bytes_c = self.comprimir_imagen(img_bytes, "image/png")
                            b64_str = base64.b64encode(img_bytes_c).decode('utf-8')
                            results.append((mime_c, img_bytes_c, b64_str, emoji_name))
            except: pass
        return results

    async def auto_learn_user_info(self, user_id: int, user_name: str, user_message: str, guild_id: int = 0, contexto_previo: str = ""):
        """Aprende automáticamente información del usuario. Es GLOBAL por user_id:
        no importa en qué servidor esté, el bot recuerda lo mismo de esa persona.
        Clasifica cada nota como PUBLICA o PRIVADA para no exponer cosas delicadas después.
        'contexto_previo' (opcional) es la plática/mensaje al que el usuario está reaccionando,
        para no confundir un chiste, una respuesta sarcástica o algo que dijo OTRO (incluyendo el bot)
        con un dato real y permanente sobre el usuario."""
        try:
            # ✅ Filtro ANTES de gastar una llamada a Groq:
            # - Mensajes muy cortos ("jaja", "ok", ":v", emoji) = casi nunca traen datos reales
            # - Mensajes que empiezan con comillas (dialogo/broma) = probablemente sarcasmo
            if len(user_message.strip()) < 8:
                return
            
            # Detectar bromas obvias: "Me encanta...", "Adoro...", comillas, etc.
            broma_patterns = [
                r'^["\']',  # Empieza con comilla (diálogo)
                r'xd\s*$|:v\s*$|jaja|jeje|lol',  # Terminadores de broma
                r'^(me encanta|adoro|amo|le encanta|le gusta|odio|detesto).*["\']',  # Estructura de broma
            ]
            if any(re.search(pattern, user_message.lower()) for pattern in broma_patterns):
                return

            existing_profiles = self.load_user_profiles()
            existing_entry = existing_profiles.get(str(user_id), {})
            existing_publico = (
                self._construir_texto_publico(existing_entry.get("perfil_publico_bloques", {}))
                or existing_entry.get("perfil_publico", existing_entry.get("perfil", ""))
            ) if isinstance(existing_entry, dict) else ""
            existing_privado = existing_entry.get("perfil_privado", "") if isinstance(existing_entry, dict) else ""

            contexto_bloque = f"Contexto de la plática justo antes de este mensaje (para que sepas a qué está reaccionando, y no confundas algo dicho por otra persona o por ti mismo (MaxIA) con un dato del usuario):\n{contexto_previo}\n\n" if contexto_previo else ""

            prompt_for_learning = (
                f"{contexto_bloque}"
                f"Mensaje de {user_name}: '{user_message}'\n"
                f"Público actual: {existing_publico if existing_publico else 'Nada'}\n"
                f"Privado actual: {existing_privado if existing_privado else 'Nada'}\n\n"
                f"REGLAS ANTES DE GUARDAR:\n"
                f"1. ¿Es broma/sarcasmo/chiste? → NO GUARDES, responde SIN_CAMBIOS\n"
                f"2. ¿Es algo que dijo otra persona (no {user_name})? → NO GUARDES\n"
                f"2.5. ¿Es algo dicho EN 2DA PERSONA sobre TI MISMO (MaxIA/el bot) - ej. 'eres "
                f"tsundere', 'tú eres bien creído', 'pareces tonto', 'ya te crees mucho'? Eso es "
                f"una opinión SOBRE EL BOT, NO un dato sobre {user_name} → NO GUARDES\n"
                f"3. ¿Es un sentimiento temporal ('estoy triste hoy')? → NO GUARDES, necesita ser duradero\n"
                f"4. ¿Ya está guardado? → NO REPITAS, responde SIN_CAMBIOS\n"
                f"5. ¿Es sexual, de salud física/mental (diagnósticos, terapia, medicamentos, adicciones) u orientación "
                f"sexual/identidad de género como etiqueta? → NO GUARDES NUNCA, responde SIN_CAMBIOS, sin excepción\n\n"
                f"SOLO SI pasa todas las reglas arriba:\n"
                f"CATEGORÍAS: GENERO (pronombres solo) | PRIVADO (dinero/familia/relaciones - NUNCA salud ni sexual) | "
                f"PUBLICO (hobbies/gustos/profesión)\n"
                f"Si es PUBLICO, di también la subcategoría: musica, gustos, odios (lo que no le gusta/odia), trabajo, personalidad u otro\n"
                f"Responde EXACTAMENTE:\nTIPO: PUBLICO, PRIVADO o GENERO\nCATEGORIA: musica/gustos/odios/trabajo/personalidad/otro (solo si TIPO es PUBLICO; si no, pon 'na')\n"
                f"TEXTO_ACTUALIZADO: solo el dato (máx {MAX_PALABRAS_POR_DATO} palabras)\nO: SIN_CAMBIOS"
            )
            
            system_for_learning = (
                "ESTRICTO: No guardes bromas, sarcasmo, emociones temporales, datos duplicados o cosas que dijo otro. "
                "Tampoco guardes como dato del usuario algo dicho EN 2DA PERSONA sobre ti mismo (MaxIA/el bot), como "
                "'eres tsundere' o 'tú eres bien creído' - eso es una opinión sobre el bot, no un dato de quien lo escribió. "
                "JAMÁS guardes nada sexual, de salud física/mental, ni orientación sexual/identidad de género como "
                "etiqueta - ni siquiera en PRIVADO, esas categorías no existen para este bot. Solo datos reales y "
                "duraderos como gustos musicales, peliculas, cosas favoritas, etc."
            )
            
            response_text, _ = await self.request_gemini(system_for_learning, [], prompt_for_learning)
            if not response_text:
                response_text, _ = await self.request_groq(system_for_learning, [], prompt_for_learning, guild_id, 0, [], usar_tools=False)

            if response_text and "sin_cambios" not in response_text.lower() and not _parece_rechazo_del_modelo(response_text):
                tipo_match = re.search(r'TIPO:\s*(PUBLICO|PRIVADO|GENERO)', response_text, re.IGNORECASE)
                categoria_match = re.search(r'CATEGORIA:\s*(musica|gustos|odios|trabajo|personalidad|otro)', response_text, re.IGNORECASE)
                texto_match = re.search(r'TEXTO_ACTUALIZADO:\s*(.+?)(?=\n*(?:TIPO:|CATEGORIA:|TEXTO_ACTUALIZADO:|SIN_CAMBIOS)|\Z)', response_text, re.IGNORECASE | re.DOTALL)
                
                if texto_match:
                    tipo_raw = tipo_match.group(1).upper() if tipo_match else "PUBLICO"
                    nuevo_texto = _extraer_campo_texto(response_text, "TEXTO_ACTUALIZADO") or texto_match.group(1).strip()
                    if tipo_raw == "GENERO":
                        self.save_user_gender(user_id, user_name, nuevo_texto)
                        print(f"🧑‍🤝‍🧑 [Género] Actualizado automáticamente para {user_name} (ID {user_id}): {nuevo_texto}")
                    else:
                        tipo = "privado" if tipo_raw == "PRIVADO" else "publico"
                        categoria = categoria_match.group(1).lower() if categoria_match else "otro"
                        # ✅ Se AGREGA el dato nuevo a la lista (antes 'set_profile_text' reemplazaba
                        # TODO el campo con solo este dato nuevo, borrando lo que ya sabía de la persona).
                        texto_guardado = self.append_profile_fact(user_id, user_name, tipo, nuevo_texto, categoria=categoria)
                        etiqueta = "🔒 [Privado]" if tipo == "privado" else f"📚 [Público/{categoria}]"
                        print(f"{etiqueta} Perfil actualizado para {user_name} (ID {user_id}): {texto_guardado}")
        except Exception as e:
            print(f"⚠️ Error en auto_learn: {e}")

    def detect_and_save_nicknames(self, guild_id: int, message_content: str):
        """Detecta apodos comunes en el mensaje (palabras cortas, gritadas o entre comillas) y los guarda."""
        try:
            # Patrones para detectar posibles apodos
            # 1. Palabras entre comillas: "tuki", "liko", "max"
            quoted_pattern = r'"([a-zA-Z0-9]{2,15})"'
            quoted_nicknames = re.findall(quoted_pattern, message_content)
            
            # 2. Palabras en MAYÚSCULAS (apodos): "LIKO", "TUKI"
            caps_pattern = r'\b([A-Z]{2,10})\b'
            caps_words = re.findall(caps_pattern, message_content)
            
            # Filtrar palabras comunes que no son apodos (como articles)
            common_words = {
    # Inglés
    'THE', 'AND', 'BUT', 'FOR', 'ARE', 'YOU', 'YOUR', 'IS', 
    'TO', 'OF', 'IN', 'THAT', 'IT', 'WITH', 'AS', 'ON', 
    'AT', 'BY', 'AN', 'BE', 'THIS', 'FROM', 'OR',
    
    # Español
    'EL', 'LA', 'LOS', 'LAS', 'UN', 'UNA', 'UNOS', 'UNAS', 
    'Y', 'O', 'PERO', 'SINO', 'POR', 'PARA', 'DE', 'DEL', 
    'EN', 'CON', 'SIN', 'QUE', 'SU', 'SUS', 'TU', 'TUS', 
    'ES', 'SE'
}
            caps_words = [w for w in caps_words if w not in common_words and len(w) > 2]
            
            all_nicknames = quoted_nicknames + caps_words
            
            # Guardar cada apodo (deduplicado)
            for nickname in set(all_nicknames):
                # No guardamos si es muy similar al nombre del usuario
                if nickname.lower() not in message_content.lower() or len(nickname) >= 4:
                    pass  # Los nicknames se guardarán solo si el usuario los usa de forma explícita
        except Exception as e:
            print(f"⚠️ Error detectando nicknames: {e}")

    def necesita_historial(self, prompt: str) -> bool:
        """(Ya no se usa para decidir automáticamente — ahora la IA misma decide con la
        herramienta 'obtener_ultimos_mensajes'. Se deja el método por si se quiere usar
        como filtro extra en el futuro.)"""
        if not prompt:
            return True
        texto = prompt.lower().strip()
        if len(texto.split()) <= 4:
            return True
        referencias = [
            "eso", "esa", "ese", "eso mismo", "lo que dij", "lo anterior", "antes dij",
            "recuerdas", "te acuerdas", "de lo que hablamos", "también", "tambien",
            "igual que", "como dij", "la otra vez", "lo de hace rato", "el chiste de",
            "eso de", "por lo de", "ahorita que dij", "lo que dijiste", "dijiste que"
        ]
        return any(ref in texto for ref in referencias)

