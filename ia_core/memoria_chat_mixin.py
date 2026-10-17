"""
MemoriaChatMixin: historial de mensajes de chat por canal/servidor
(guardar, leer, buscar, borrar), apodos de usuarios y mapeo de nombres
de canal a IDs, más el contexto automático reciente del canal.

Cambios respecto a la versión anterior:
- Todas las consultas cierran su conexión SIEMPRE (antes, si algo fallaba a medias, la conexión
  quedaba abierta y se acumulaban conexiones colgadas).
- La migración de bases de datos viejas ahora vive en get_db_connection (perfiles_mixin.py); aquí
  ya no hay ALTER TABLE a medias dentro de los except (el que agregaba 'timestamp' fallaba siempre
  en SQLite y dejaba la memoria devolviendo listas vacías).
- El contexto automático ahora lee bastantes más mensajes (ver MEMORIA_CHAT_* en config.py).
"""
from contextlib import contextmanager

from ia_core.config import (
    MEMORIA_CHAT_MIN_MENSAJES,
    MEMORIA_CHAT_MAX_MENSAJES,
    MEMORIA_CHAT_MAX_CHARS_MENSAJE,
    MEMORIA_CHAT_MAX_CHARS_TOTAL,
)


class MemoriaChatMixin:
    @contextmanager
    def _db(self, guild_id: int):
        """Abre la BD del servidor y garantiza que se cierre, pase lo que pase."""
        conn = self.get_db_connection(guild_id)
        try:
            yield conn
        finally:
            conn.close()

    # ───────────────────────── historial del chat ─────────────────────────
    def save_guild_message(self, guild_id: int, channel_id: int, user_name: str, role: str, content: str, is_ai: bool = False):
        try:
            with self._db(guild_id) as conn:
                conn.execute(
                    '''INSERT INTO chat_memory (guild_id, channel_id, user_name, role, content, is_ai_generated)
                       VALUES (?, ?, ?, ?, ?, ?)''',
                    (guild_id, channel_id, user_name, role, content, 1 if is_ai else 0)
                )
                conn.commit()
        except Exception as e: print(f"⚠️ Error guardando mensaje: {e}")

    def get_guild_memory(self, guild_id: int, channel_id: int, limit: int = 30, offset: int = 0):
        try:
            with self._db(guild_id) as conn:
                messages = conn.execute(
                    '''SELECT user_name, role, content FROM chat_memory
                       WHERE guild_id = ? AND channel_id = ?
                       ORDER BY id DESC LIMIT ? OFFSET ?''',
                    (guild_id, channel_id, limit, offset)
                ).fetchall()
            return list(reversed(messages))
        except Exception as e: print(f"⚠️ Error obteniendo memoria: {e}"); return []

    def search_guild_memory(self, guild_id: int, channel_id: int, keyword: str, limit: int = 20):
        try:
            with self._db(guild_id) as conn:
                messages = conn.execute(
                    '''SELECT user_name, role, content FROM chat_memory
                       WHERE guild_id = ? AND channel_id = ? AND (content LIKE ? OR user_name LIKE ?)
                       ORDER BY id DESC LIMIT ?''',
                    (guild_id, channel_id, f"%{keyword}%", f"%{keyword}%", limit)
                ).fetchall()
            return list(reversed(messages))
        except Exception as e: print(f"⚠️ Error buscando en memoria: {e}"); return []

    def clear_guild_memory(self, guild_id: int, channel_id: int = None):
        try:
            with self._db(guild_id) as conn:
                if channel_id:
                    conn.execute('DELETE FROM chat_memory WHERE guild_id = ? AND channel_id = ?', (guild_id, channel_id))
                else:
                    conn.execute('DELETE FROM chat_memory WHERE guild_id = ?', (guild_id,))
                conn.commit()
        except Exception as e: print(f"⚠️ Error limpiando memoria: {e}")

    def get_guild_memory_by_channel(self, guild_id: int, channel_id: int, limit: int = 30, offset: int = 0, keyword: str = None):
        """Obtiene mensajes de un canal específico, opcionalmente filtrados por palabra clave."""
        try:
            with self._db(guild_id) as conn:
                if keyword:
                    messages = conn.execute(
                        '''SELECT user_name, role, content FROM chat_memory
                           WHERE guild_id = ? AND channel_id = ? AND content LIKE ?
                           ORDER BY id DESC LIMIT ? OFFSET ?''',
                        (guild_id, channel_id, f"%{keyword}%", limit, offset)
                    ).fetchall()
                else:
                    messages = conn.execute(
                        '''SELECT user_name, role, content FROM chat_memory
                           WHERE guild_id = ? AND channel_id = ?
                           ORDER BY id DESC LIMIT ? OFFSET ?''',
                        (guild_id, channel_id, limit, offset)
                    ).fetchall()
            # ✅ CONVERTIR A DICCIONARIOS PARA IA
            return [{"role": role, "content": f"[{user_name}]: {content}"} for user_name, role, content in reversed(messages)]
        except Exception as e: print(f"⚠️ Error obteniendo memoria por canal: {e}"); return []

    # ───────────────────────── apodos ─────────────────────────
    def save_user_nickname(self, guild_id: int, user_name: str, nickname: str):
        """Guarda un apodo para un usuario. Se detecctan automáticamente en los mensajes."""
        try:
            with self._db(guild_id) as conn:
                cur = conn.execute(
                    '''INSERT OR IGNORE INTO user_nicknames (guild_id, user_name, nickname)
                       VALUES (?, ?, ?)''',
                    (guild_id, user_name, nickname.lower())
                )
                # Si ya existía (el INSERT no insertó nada), incrementar el contador. Antes se
                # incrementaba también en la primera inserción y el apodo nacía con 2 usos.
                if cur.rowcount == 0:
                    conn.execute(
                        '''UPDATE user_nicknames SET times_used = times_used + 1, last_used = CURRENT_TIMESTAMP
                           WHERE guild_id = ? AND nickname = ?''',
                        (guild_id, nickname.lower())
                    )
                conn.commit()
        except Exception as e: print(f"⚠️ Error guardando nickname: {e}")

    def get_user_by_nickname(self, guild_id: int, nickname: str) -> str:
        """Busca qué usuario tiene un apodo específico."""
        try:
            with self._db(guild_id) as conn:
                result = conn.execute(
                    '''SELECT user_name FROM user_nicknames
                       WHERE guild_id = ? AND nickname = ? LIMIT 1''',
                    (guild_id, nickname.lower())
                ).fetchone()
            return result[0] if result else None
        except Exception as e: print(f"⚠️ Error buscando usuario por nickname: {e}"); return None

    def get_user_nicknames(self, guild_id: int, user_name: str) -> list:
        """Obtiene todos los apodos de un usuario."""
        try:
            with self._db(guild_id) as conn:
                results = conn.execute(
                    '''SELECT nickname FROM user_nicknames
                       WHERE guild_id = ? AND user_name = ?
                       ORDER BY times_used DESC''',
                    (guild_id, user_name)
                ).fetchall()
            return [r[0] for r in results] if results else []
        except Exception as e: print(f"⚠️ Error obteniendo nicknames: {e}"); return []

    # ───────────────────────── mapeo de canales ─────────────────────────
    def save_channel_mapping(self, guild_id: int, channel_id: int, channel_name: str):
        """Guarda el mapeo de nombre de canal a ID.
        Se llama con CADA mensaje, así que se recuerda en memoria lo ya guardado y solo se toca
        la BD cuando el canal es nuevo o lo renombraron (antes era una escritura + commit por mensaje)."""
        nombre = (channel_name or "").lower()
        if not nombre:
            return
        guardados = self.__dict__.setdefault("_canales_guardados", {})
        if guardados.get((guild_id, channel_id)) == nombre:
            return
        try:
            with self._db(guild_id) as conn:
                # Si renombraron el canal, el nombre viejo ya no debe apuntar a él; y si otro canal
                # heredó un nombre que ya existía, ahora apunta al correcto (antes quedaba al viejo).
                conn.execute('DELETE FROM channel_mapping WHERE guild_id = ? AND channel_id = ? AND channel_name != ?',
                             (guild_id, channel_id, nombre))
                conn.execute(
                    '''INSERT OR REPLACE INTO channel_mapping (guild_id, channel_id, channel_name)
                       VALUES (?, ?, ?)''',
                    (guild_id, channel_id, nombre)
                )
                conn.commit()
            guardados[(guild_id, channel_id)] = nombre
        except Exception as e: print(f"⚠️ Error guardando mapeo de canal: {e}")

    def get_channel_id_by_name(self, guild_id: int, channel_name: str) -> int:
        """Obtiene el ID de un canal por su nombre."""
        try:
            with self._db(guild_id) as conn:
                result = conn.execute(
                    '''SELECT channel_id FROM channel_mapping
                       WHERE guild_id = ? AND channel_name = ? LIMIT 1''',
                    (guild_id, channel_name.lower())
                ).fetchone()
            return result[0] if result else None
        except Exception as e: print(f"⚠️ Error obteniendo canal por nombre: {e}"); return None

    # ───────────────────────── contexto automático ─────────────────────────
    @staticmethod
    def _ajustar_a_presupuesto(ventana: list, total_max: int = None) -> list:
        """Recorta cada mensaje muy largo y, si el total pasa el tope, descarta los más VIEJOS.
        Así se puede leer mucho más historial sin que unos pocos mensajes enormes (pegados de texto,
        código) se coman todos los tokens, ni que el respaldo de Groq se pase de su límite por minuto."""
        total_max = total_max or MEMORIA_CHAT_MAX_CHARS_TOTAL
        recortados = []
        for m in ventana:
            c = m.get("content", "")
            if len(c) > MEMORIA_CHAT_MAX_CHARS_MENSAJE:
                c = c[:MEMORIA_CHAT_MAX_CHARS_MENSAJE].rstrip() + "…"
            recortados.append({**m, "content": c})

        conservados, total = [], 0
        for m in reversed(recortados):                 # de lo más reciente hacia atrás
            total += len(m["content"])
            if total > total_max and conservados:      # siempre se conserva al menos el último mensaje
                break
            conservados.append(m)
        return list(reversed(conservados))

    def get_auto_context(self, guild_id: int, channel_id: int, min_msgs: int = None, max_msgs: int = None):
        """Memoria automática de la plática (sin usar tool, se manda siempre).
        Por defecto trae los últimos `min_msgs` (lo que dijo la persona + el bot). Si en esa
        ventana reciente aparece MÁS DE UNA persona hablando (alguien se metió a la conversación),
        se amplía hasta `max_msgs` para no perder de vista lo que dijeron los demás.
        Los valores por defecto vienen de config.py (MEMORIA_CHAT_MIN/MAX_MENSAJES)."""
        min_msgs = min_msgs or MEMORIA_CHAT_MIN_MENSAJES
        max_msgs = max(max_msgs or MEMORIA_CHAT_MAX_MENSAJES, min_msgs)
        try:
            ventana = self.get_guild_memory_by_channel(guild_id, channel_id, limit=max_msgs)
            if not ventana:
                return []
            participantes = {
                m["content"].split("]:", 1)[0].lstrip("[")
                for m in ventana if m.get("role") != "model"
            }
            if len(participantes) > 1:
                elegida = ventana                       # ya se limitó a max_msgs desde la consulta
            else:
                elegida = ventana[-min_msgs:]
            return self._ajustar_a_presupuesto(elegida)
        except Exception as e:
            print(f"⚠️ Error obteniendo contexto automático: {e}")
            return []

    def get_contexto_extendido(self, guild_id: int, channel_id: int, cantidad: int):
        """Contexto de tamaño pedido a propósito (comando !contexto <cantidad>). Tope de 100 mensajes
        y el doble de presupuesto de caracteres que el contexto automático."""
        try:
            cantidad = max(1, min(int(cantidad), 100))
            ventana = self.get_guild_memory_by_channel(guild_id, channel_id, limit=cantidad)
            return self._ajustar_a_presupuesto(ventana, MEMORIA_CHAT_MAX_CHARS_TOTAL * 2)
        except Exception as e:
            print(f"⚠️ Error obteniendo contexto extendido: {e}")
            return []
