"""
MemoriaChatMixin: historial de mensajes de chat por canal/servidor
(guardar, leer, buscar, borrar), apodos de usuarios y mapeo de nombres
de canal a IDs, más el contexto automático reciente del canal.
"""
import sqlite3


class MemoriaChatMixin:
    def save_guild_message(self, guild_id: int, channel_id: int, user_name: str, role: str, content: str, is_ai: bool = False):
        try:
            conn = self.get_db_connection(guild_id)
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT INTO chat_memory (guild_id, channel_id, user_name, role, content, is_ai_generated) 
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (guild_id, channel_id, user_name, role, content, 1 if is_ai else 0)
            )
            conn.commit()
            conn.close()
        except Exception as e: print(f"⚠️ Error guardando mensaje: {e}")

    def get_guild_memory(self, guild_id: int, channel_id: int, limit: int = 30, offset: int = 0):
        try:
            conn = self.get_db_connection(guild_id)
            cursor = conn.cursor()
            # Intentar con columnas nuevas
            try:
                cursor.execute(
                    '''SELECT user_name, role, content FROM chat_memory 
                       WHERE guild_id = ? AND channel_id = ? 
                       ORDER BY id DESC LIMIT ? OFFSET ?''',
                    (guild_id, channel_id, limit, offset)
                )
                messages = cursor.fetchall()
                conn.close()
                return list(reversed(messages))
            except sqlite3.OperationalError:
                # Si falla, la BD es vieja, intentar agregar columnas
                cursor.execute('''ALTER TABLE chat_memory 
                                ADD COLUMN is_ai_generated INTEGER DEFAULT 0''')
                cursor.execute('''ALTER TABLE chat_memory 
                                ADD COLUMN timestamp DATETIME DEFAULT CURRENT_TIMESTAMP''')
                conn.commit()
                
                cursor.execute(
                    '''SELECT user_name, role, content FROM chat_memory 
                       WHERE guild_id = ? AND channel_id = ? 
                       ORDER BY id DESC LIMIT ? OFFSET ?''',
                    (guild_id, channel_id, limit, offset)
                )
                messages = cursor.fetchall()
                conn.close()
                return list(reversed(messages))
        except Exception as e: print(f"⚠️ Error obteniendo memoria: {e}"); return []

    def search_guild_memory(self, guild_id: int, channel_id: int, keyword: str, limit: int = 20):
        try:
            conn = self.get_db_connection(guild_id)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    '''SELECT user_name, role, content FROM chat_memory 
                       WHERE guild_id = ? AND channel_id = ? AND (content LIKE ? OR user_name LIKE ?)
                       ORDER BY id DESC LIMIT ?''',
                    (guild_id, channel_id, f"%{keyword}%", f"%{keyword}%", limit)
                )
                messages = cursor.fetchall()
                conn.close()
                return list(reversed(messages))
            except sqlite3.OperationalError:
                # BD vieja, agregar columnas
                cursor.execute('''ALTER TABLE chat_memory 
                                ADD COLUMN is_ai_generated INTEGER DEFAULT 0''')
                cursor.execute('''ALTER TABLE chat_memory 
                                ADD COLUMN timestamp DATETIME DEFAULT CURRENT_TIMESTAMP''')
                conn.commit()
                
                cursor.execute(
                    '''SELECT user_name, role, content FROM chat_memory 
                       WHERE guild_id = ? AND channel_id = ? AND (content LIKE ? OR user_name LIKE ?)
                       ORDER BY id DESC LIMIT ?''',
                    (guild_id, channel_id, f"%{keyword}%", f"%{keyword}%", limit)
                )
                messages = cursor.fetchall()
                conn.close()
                return list(reversed(messages))
        except Exception as e: print(f"⚠️ Error buscando en memoria: {e}"); return []

    def clear_guild_memory(self, guild_id: int, channel_id: int = None):
        try:
            conn = self.get_db_connection(guild_id)
            cursor = conn.cursor()
            if channel_id:
                cursor.execute('DELETE FROM chat_memory WHERE guild_id = ? AND channel_id = ?', (guild_id, channel_id))
            else:
                cursor.execute('DELETE FROM chat_memory WHERE guild_id = ?', (guild_id,))
            conn.commit()
            conn.close()
        except Exception as e: print(f"⚠️ Error limpiando memoria: {e}")

    def save_user_nickname(self, guild_id: int, user_name: str, nickname: str):
        """Guarda un apodo para un usuario. Se detecctan automáticamente en los mensajes."""
        try:
            conn = self.get_db_connection(guild_id)
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR IGNORE INTO user_nicknames (guild_id, user_name, nickname) 
                   VALUES (?, ?, ?)''',
                (guild_id, user_name, nickname.lower())
            )
            # Si ya existe, incrementar el contador
            cursor.execute(
                '''UPDATE user_nicknames SET times_used = times_used + 1, last_used = CURRENT_TIMESTAMP 
                   WHERE guild_id = ? AND nickname = ?''',
                (guild_id, nickname.lower())
            )
            conn.commit()
            conn.close()
        except Exception as e: print(f"⚠️ Error guardando nickname: {e}")

    def get_user_by_nickname(self, guild_id: int, nickname: str) -> str:
        """Busca qué usuario tiene un apodo específico."""
        try:
            conn = self.get_db_connection(guild_id)
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT user_name FROM user_nicknames 
                   WHERE guild_id = ? AND nickname = ? LIMIT 1''',
                (guild_id, nickname.lower())
            )
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else None
        except Exception as e: print(f"⚠️ Error buscando usuario por nickname: {e}"); return None

    def get_user_nicknames(self, guild_id: int, user_name: str) -> list:
        """Obtiene todos los apodos de un usuario."""
        try:
            conn = self.get_db_connection(guild_id)
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT nickname FROM user_nicknames 
                   WHERE guild_id = ? AND user_name = ? 
                   ORDER BY times_used DESC''',
                (guild_id, user_name)
            )
            results = cursor.fetchall()
            conn.close()
            return [r[0] for r in results] if results else []
        except Exception as e: print(f"⚠️ Error obteniendo nicknames: {e}"); return []

    def save_channel_mapping(self, guild_id: int, channel_id: int, channel_name: str):
        """Guarda el mapeo de nombre de canal a ID."""
        try:
            conn = self.get_db_connection(guild_id)
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR IGNORE INTO channel_mapping (guild_id, channel_id, channel_name) 
                   VALUES (?, ?, ?)''',
                (guild_id, channel_id, channel_name.lower())
            )
            conn.commit()
            conn.close()
        except Exception as e: print(f"⚠️ Error guardando mapeo de canal: {e}")

    def get_channel_id_by_name(self, guild_id: int, channel_name: str) -> int:
        """Obtiene el ID de un canal por su nombre."""
        try:
            conn = self.get_db_connection(guild_id)
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT channel_id FROM channel_mapping 
                   WHERE guild_id = ? AND channel_name = ? LIMIT 1''',
                (guild_id, channel_name.lower())
            )
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else None
        except Exception as e: print(f"⚠️ Error obteniendo canal por nombre: {e}"); return None

    def get_guild_memory_by_channel(self, guild_id: int, channel_id: int, limit: int = 30, offset: int = 0, keyword: str = None):
        """Obtiene mensajes de un canal específico, opcionalmente filtrados por palabra clave."""
        try:
            conn = self.get_db_connection(guild_id)
            cursor = conn.cursor()
            try:
                if keyword:
                    cursor.execute(
                        '''SELECT user_name, role, content FROM chat_memory 
                           WHERE guild_id = ? AND channel_id = ? AND content LIKE ?
                           ORDER BY id DESC LIMIT ? OFFSET ?''',
                        (guild_id, channel_id, f"%{keyword}%", limit, offset)
                    )
                else:
                    cursor.execute(
                        '''SELECT user_name, role, content FROM chat_memory 
                           WHERE guild_id = ? AND channel_id = ? 
                           ORDER BY id DESC LIMIT ? OFFSET ?''',
                        (guild_id, channel_id, limit, offset)
                    )
                messages = cursor.fetchall()
                conn.close()
                # ✅ CONVERTIR A DICCIONARIOS PARA IA
                return [{"role": role, "content": f"[{user_name}]: {content}"} for user_name, role, content in reversed(messages)]
            except sqlite3.OperationalError:
                # BD vieja, agregar columnas
                cursor.execute('''ALTER TABLE chat_memory 
                                ADD COLUMN is_ai_generated INTEGER DEFAULT 0''')
                cursor.execute('''ALTER TABLE chat_memory 
                                ADD COLUMN timestamp DATETIME DEFAULT CURRENT_TIMESTAMP''')
                conn.commit()
                
                if keyword:
                    cursor.execute(
                        '''SELECT user_name, role, content FROM chat_memory 
                           WHERE guild_id = ? AND channel_id = ? AND content LIKE ?
                           ORDER BY id DESC LIMIT ? OFFSET ?''',
                        (guild_id, channel_id, f"%{keyword}%", limit, offset)
                    )
                else:
                    cursor.execute(
                        '''SELECT user_name, role, content FROM chat_memory 
                           WHERE guild_id = ? AND channel_id = ? 
                           ORDER BY id DESC LIMIT ? OFFSET ?''',
                        (guild_id, channel_id, limit, offset)
                    )
                messages = cursor.fetchall()
                conn.close()
                # ✅ CONVERTIR A DICCIONARIOS PARA IA
                return [{"role": role, "content": f"[{user_name}]: {content}"} for user_name, role, content in reversed(messages)]
        except Exception as e: print(f"⚠️ Error obteniendo memoria por canal: {e}"); return []

    def get_auto_context(self, guild_id: int, channel_id: int, min_msgs: int = 5, max_msgs: int = 10):
        """Memoria automática y corta de la plática (sin usar tool, se manda siempre).
        Por defecto trae los últimos `min_msgs` (lo que dijo la persona + el bot). Si en esa
        ventana reciente aparece MÁS DE UNA persona hablando (alguien se metió a la conversación),
        se amplía hasta `max_msgs` para no perder de vista lo que dijeron los demás."""
        try:
            ventana = self.get_guild_memory_by_channel(guild_id, channel_id, limit=max_msgs)
            if not ventana:
                return []
            participantes = {
                m["content"].split("]:", 1)[0].lstrip("[")
                for m in ventana if m.get("role") != "model"
            }
            if len(participantes) <= 1:
                return ventana[-min_msgs:]
            return ventana  # ya se limitó a max_msgs desde la consulta
        except Exception as e:
            print(f"⚠️ Error obteniendo contexto automático: {e}")
            return []

