# =============================================================
#  OLFATO — Servidor v5: CHAT EN TIEMPO REAL (SSE)
#  (reemplaza al servidor.py anterior)
#  Corre con: python servidor.py
#
#  Lo nuevo:
#   - Chat entre adoptante y refugio post-aprobación
#   - Server-Sent Events (SSE): mensajes instantáneos sin recargar
#   - Mensaje de bienvenida automático al aprobar una solicitud
#   - mensajes.json: nueva base de datos de conversaciones
#
#  El chat solo se habilita cuando el refugio aprueba la solicitud
# =============================================================

import json
import os
import hashlib
import secrets
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

PUERTO = 8000
ARCHIVO_PERROS = "perros.json"
ARCHIVO_SOLICITUDES = "solicitudes.json"
ARCHIVO_USUARIOS = "usuarios.json"
ARCHIVO_MENSAJES = "mensajes.json"

SESIONES = {}

# SSE: lista de clientes conectados esperando mensajes
# { conv_id: [queue1, queue2, ...] }
SSE_CLIENTES = {}
SSE_LOCK = threading.Lock()


# ----------------------------------------------------------
# Utilidades de archivos
# ----------------------------------------------------------
def cargar(archivo, si_no_existe):
    if os.path.exists(archivo):
        with open(archivo, "r", encoding="utf-8") as f:
            return json.load(f)
    guardar(archivo, si_no_existe)
    return si_no_existe


def guardar(archivo, datos):
    with open(archivo, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)


def hashear_clave(clave, sal):
    return hashlib.sha256((sal + clave).encode("utf-8")).hexdigest()


def crear_usuario(usuarios, nombre, email, clave, rol, datos_refugio=None):
    sal = secrets.token_hex(8)
    usuario = {
        "id": max((u["id"] for u in usuarios), default=0) + 1,
        "nombre": nombre, "email": email.lower().strip(),
        "sal": sal, "clave_hash": hashear_clave(clave, sal),
        "rol": rol,
        "estado": "pendiente" if rol == "refugio" else "activo",
        "datos_refugio": datos_refugio or {},
    }
    usuarios.append(usuario)
    return usuario


def usuario_publico(u):
    return {"id": u["id"], "nombre": u["nombre"], "email": u["email"],
            "rol": u["rol"], "estado": u["estado"],
            "datos_refugio": u.get("datos_refugio", {}),
            "perfil": u.get("perfil")}


def asegurar_admin(usuarios):
    if not any(u["rol"] == "admin" for u in usuarios):
        crear_usuario(usuarios, "Admin Olfato", "admin@olfato.com", "olfato123", "admin")
        guardar(ARCHIVO_USUARIOS, usuarios)
        print("  🔑 Cuenta admin creada -> admin@olfato.com / olfato123")


# ----------------------------------------------------------
# SSE: empujar mensajes a clientes conectados
# ----------------------------------------------------------
def sse_push(conv_id, mensaje):
    """Empuja un mensaje a todos los clientes SSE de esa conversación."""
    with SSE_LOCK:
        clientes = SSE_CLIENTES.get(str(conv_id), [])
        data = json.dumps(mensaje, ensure_ascii=False)
        for queue in clientes:
            queue.append(data)


def id_conversacion(solicitud_id):
    return f"conv_{solicitud_id}"


# ----------------------------------------------------------
# Crear mensaje de bienvenida automático al aprobar
# ----------------------------------------------------------
def crear_bienvenida(solicitud, refugio_nombre, acepta_visita):
    nombre_adoptante = solicitud["adoptante"]
    nombre_perro = solicitud["perro_nombre"]

    if acepta_visita:
        texto = (f"¡Hola {nombre_adoptante}! 🐾 Somos {refugio_nombre} "
                 f"y nos alegra tu interés en {nombre_perro}. "
                 f"¿Cuándo podés venir a conocerlo al refugio?")
    else:
        texto = (f"¡Hola {nombre_adoptante}! 🐾 Somos {refugio_nombre} "
                 f"y nos alegra tu interés en {nombre_perro}. "
                 f"¿Cómo preferís que sigamos el proceso de adopción?")

    return {
        "id": int(time.time() * 1000),
        "conv_id": id_conversacion(solicitud["id"]),
        "solicitud_id": solicitud["id"],
        "autor": refugio_nombre,
        "rol": "refugio",
        "texto": texto,
        "timestamp": time.time(),
        "automatico": True,
    }


class ServidorOlfato(BaseHTTPRequestHandler):

    def responder_json(self, datos, codigo=200):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(cuerpo)

    def responder_archivo(self, ruta, tipo):
        if not os.path.exists(ruta):
            self.responder_json({"error": f"No encuentro {ruta}"}, 404)
            return
        with open(ruta, "rb") as f:
            cuerpo = f.read()
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def leer_cuerpo(self):
        largo = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(largo).decode("utf-8"))

    def quien_soy(self):
        token = self.headers.get("X-Token", "")
        id_usuario = SESIONES.get(token)
        if id_usuario is None:
            return None
        usuarios = cargar(ARCHIVO_USUARIOS, [])
        return next((u for u in usuarios if u["id"] == id_usuario), None)

    def mis_perros_ids(self, quien):
        perros = cargar(ARCHIVO_PERROS, [])
        if quien["rol"] == "admin":
            return {p["id"] for p in perros}
        return {p["id"] for p in perros if p.get("refugio_id") == quien["id"]}

    # ----------------------------------------------------------
    # GET
    # ----------------------------------------------------------
    def do_GET(self):
        if self.path == "/":
            self.responder_archivo("portada.html", "text/html; charset=utf-8")
        elif self.path in ("/adoptar", "/app.html"):
            self.responder_archivo("app.html", "text/html; charset=utf-8")
        elif self.path == "/refugio":
            self.responder_archivo("refugio.html", "text/html; charset=utf-8")
        elif self.path == "/admin":
            self.responder_archivo("admin.html", "text/html; charset=utf-8")

        elif self.path == "/api/perros":
            perros = cargar(ARCHIVO_PERROS, [])
            self.responder_json([p for p in perros if p["estado"] == "disponible"])

        elif self.path == "/api/perros/todos":
            quien = self.quien_soy()
            if not quien or quien["rol"] not in ("refugio", "admin"):
                self.responder_json({"error": "Sesión inválida"}, 403); return
            perros = cargar(ARCHIVO_PERROS, [])
            if quien["rol"] != "admin":
                perros = [p for p in perros if p.get("refugio_id") == quien["id"]]
            self.responder_json(perros)

        elif self.path == "/api/solicitudes":
            quien = self.quien_soy()
            if not quien or quien["rol"] not in ("refugio", "admin"):
                self.responder_json({"error": "Sesión inválida"}, 403); return
            solicitudes = cargar(ARCHIVO_SOLICITUDES, [])
            mios = self.mis_perros_ids(quien)
            self.responder_json([s for s in solicitudes if s["perro_id"] in mios])

        elif self.path == "/api/mis-solicitudes":
            quien = self.quien_soy()
            if not quien:
                self.responder_json({"error": "Sesión inválida"}, 403); return
            solicitudes = cargar(ARCHIVO_SOLICITUDES, [])
            self.responder_json([s for s in solicitudes if s["adoptante"] == quien["nombre"]])

        # Mensajes de una conversación
        elif self.path.startswith("/api/mensajes/"):
            quien = self.quien_soy()
            if not quien:
                self.responder_json({"error": "Sesión inválida"}, 403); return
            sol_id = int(self.path.split("/")[-1])
            mensajes = cargar(ARCHIVO_MENSAJES, [])
            conv = [m for m in mensajes if m["solicitud_id"] == sol_id]
            self.responder_json(conv)

        # SSE: conexión en tiempo real para una conversación
        elif self.path.startswith("/api/sse/"):
            quien = self.quien_soy()
            if not quien:
                self.send_response(403); self.end_headers(); return
            sol_id = self.path.split("/")[-1]
            conv_id = f"conv_{sol_id}"

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            # Registrar este cliente
            queue = []
            with SSE_LOCK:
                if conv_id not in SSE_CLIENTES:
                    SSE_CLIENTES[conv_id] = []
                SSE_CLIENTES[conv_id].append(queue)

            try:
                # Keepalive: mandamos un comentario cada 15s para mantener viva la conexión
                last_ping = time.time()
                while True:
                    if queue:
                        data = queue.pop(0)
                        msg = f"data: {data}\n\n"
                        self.wfile.write(msg.encode("utf-8"))
                        self.wfile.flush()
                    elif time.time() - last_ping > 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_ping = time.time()
                    else:
                        time.sleep(0.1)
            except Exception:
                pass
            finally:
                with SSE_LOCK:
                    if conv_id in SSE_CLIENTES:
                        try:
                            SSE_CLIENTES[conv_id].remove(queue)
                        except ValueError:
                            pass

        elif self.path.startswith("/api/admin/refugios-pendientes"):
            quien = self.quien_soy()
            if not quien or quien["rol"] != "admin":
                self.responder_json({"error": "Solo el admin"}, 403); return
            usuarios = cargar(ARCHIVO_USUARIOS, [])
            self.responder_json([usuario_publico(u) for u in usuarios
                                 if u["rol"] == "refugio" and u["estado"] == "pendiente"])
            guardar(ARCHIVO_MENSAJES, mensajes)
            self.responder_json({"ok": True})

        else:
            self.responder_json({"error": "Ruta desconocida"}, 404)

    # ----------------------------------------------------------
    # POST
    # ----------------------------------------------------------
    def do_POST(self):
        if self.path == "/api/registro":
            datos = self.leer_cuerpo()
            usuarios = cargar(ARCHIVO_USUARIOS, [])
            email = datos["email"].lower().strip()
            if any(u["email"] == email for u in usuarios):
                self.responder_json({"error": "Ya existe una cuenta con ese email"}, 400); return
            if datos["rol"] not in ("adoptante", "refugio"):
                self.responder_json({"error": "Rol inválido"}, 400); return
            usuario = crear_usuario(usuarios, datos["nombre"], email, datos["clave"],
                                    datos["rol"], datos.get("datos_refugio"))
            guardar(ARCHIVO_USUARIOS, usuarios)
            if usuario["estado"] == "pendiente":
                self.responder_json({"ok": True, "estado": "pendiente"})
            else:
                token = secrets.token_hex(16)
                SESIONES[token] = usuario["id"]
                self.responder_json({"ok": True, "token": token,
                                     "usuario": usuario_publico(usuario)})

        elif self.path == "/api/login":
            datos = self.leer_cuerpo()
            usuarios = cargar(ARCHIVO_USUARIOS, [])
            email = datos["email"].lower().strip()
            usuario = next((u for u in usuarios if u["email"] == email), None)
            if not usuario or usuario["clave_hash"] != hashear_clave(datos["clave"], usuario["sal"]):
                self.responder_json({"error": "Email o clave incorrectos"}, 401); return
            if usuario["rol"] == "refugio" and usuario["estado"] == "pendiente":
                self.responder_json({"error": "pendiente",
                                     "mensaje": "Tu refugio todavía está en revisión. Te avisamos cuando esté aprobado 🐾"}, 403); return
            if usuario["estado"] == "rechazado":
                self.responder_json({"error": "rechazado",
                                     "mensaje": "Tu solicitud de refugio no fue aprobada."}, 403); return
            token = secrets.token_hex(16)
            SESIONES[token] = usuario["id"]
            self.responder_json({"ok": True, "token": token,
                                 "usuario": usuario_publico(usuario)})

        elif self.path == "/api/admin/decidir":
            datos = self.leer_cuerpo()
            quien = self.quien_soy()
            if not quien or quien["rol"] != "admin":
                self.responder_json({"error": "Solo el admin"}, 403); return
            usuarios = cargar(ARCHIVO_USUARIOS, [])
            for u in usuarios:
                if u["id"] == datos["usuario_id"] and u["rol"] == "refugio":
                    u["estado"] = "activo" if datos["decision"] == "aprobar" else "rechazado"
            guardar(ARCHIVO_USUARIOS, usuarios)
            self.responder_json({"ok": True})

        elif self.path == "/api/perfil":
            datos = self.leer_cuerpo()
            quien = self.quien_soy()
            if not quien:
                self.responder_json({"error": "Sesión inválida"}, 403); return
            usuarios = cargar(ARCHIVO_USUARIOS, [])
            for u in usuarios:
                if u["id"] == quien["id"]:
                    u["perfil"] = datos["perfil"]
            guardar(ARCHIVO_USUARIOS, usuarios)
            self.responder_json({"ok": True})

        elif self.path == "/api/solicitudes":
            datos = self.leer_cuerpo()
            solicitudes = cargar(ARCHIVO_SOLICITUDES, [])
            perros = cargar(ARCHIVO_PERROS, [])
            nuevo_id = max((s["id"] for s in solicitudes), default=0) + 1
            solicitudes.append({
                "id": nuevo_id, "perro_id": datos["perro_id"],
                "perro_nombre": datos["perro_nombre"],
                "adoptante": datos["adoptante"], "estado": "pendiente",
                "motivo_adoptante": datos.get("motivo", ""),
                "informe": datos["informe"],
            })
            for p in perros:
                if p["id"] == datos["perro_id"]:
                    p["likes"] += 1
            guardar(ARCHIVO_SOLICITUDES, solicitudes)
            guardar(ARCHIVO_PERROS, perros)
            self.responder_json({"ok": True, "id": nuevo_id})

        # Aprobar/rechazar solicitud + crear conversación y bienvenida
        elif self.path == "/api/solicitudes/decidir":
            datos = self.leer_cuerpo()
            quien = self.quien_soy()
            if not quien or quien["rol"] not in ("refugio", "admin"):
                self.responder_json({"error": "Sesión inválida"}, 403); return
            solicitudes = cargar(ARCHIVO_SOLICITUDES, [])
            perros = cargar(ARCHIVO_PERROS, [])
            mios = self.mis_perros_ids(quien)
            usuarios = cargar(ARCHIVO_USUARIOS, [])

            for s in solicitudes:
                if s["id"] == datos["id"]:
                    if s["perro_id"] not in mios:
                        self.responder_json({"error": "Ese perro no es de tu refugio"}, 403); return
                    s["estado"] = datos["decision"]
                    if datos["decision"] == "rechazada":
                        s["motivo_rechazo"] = datos.get("motivo", "")
                    if datos["decision"] == "aprobada":
                        for p in perros:
                            if p["id"] == s["perro_id"]:
                                p["estado"] = "en proceso de adopción"
                        # Crear mensaje de bienvenida automático
                        refugio_nombre = quien.get("datos_refugio", {}).get("organizacion", quien["nombre"])
                        informe = s.get("informe", {})
                        alertas = informe.get("alertas", [])
                        acepta_visita = not any("visita" in a.lower() for a in alertas)
                        bienvenida = crear_bienvenida(s, refugio_nombre, acepta_visita)
                        mensajes = cargar(ARCHIVO_MENSAJES, [])
                        mensajes.append(bienvenida)
                        guardar(ARCHIVO_MENSAJES, mensajes)
                        # Empujar por SSE a quien esté conectado
                        sse_push(s["id"], bienvenida)

            guardar(ARCHIVO_SOLICITUDES, solicitudes)
            guardar(ARCHIVO_PERROS, perros)
            self.responder_json({"ok": True})

        # Enviar mensaje de chat
        elif self.path == "/api/mensajes":
            datos = self.leer_cuerpo()
            quien = self.quien_soy()
            if not quien:
                self.responder_json({"error": "Sesión inválida"}, 403); return
            mensajes = cargar(ARCHIVO_MENSAJES, [])
            nuevo = {
                "id": int(time.time() * 1000),
                "conv_id": id_conversacion(datos["solicitud_id"]),
                "solicitud_id": datos["solicitud_id"],
                "autor": quien["nombre"],
                "rol": quien["rol"],
                "texto": datos["texto"].strip(),
                "timestamp": time.time(),
                "automatico": False,
            }
            mensajes.append(nuevo)
            guardar(ARCHIVO_MENSAJES, mensajes)
            sse_push(datos["solicitud_id"], nuevo)
            self.responder_json({"ok": True, "mensaje": nuevo})

        elif self.path == "/api/perros":
            datos = self.leer_cuerpo()
            quien = self.quien_soy()
            if not quien or quien["rol"] not in ("refugio", "admin"):
                self.responder_json({"error": "Solo refugios verificados pueden publicar"}, 403); return
            perros = cargar(ARCHIVO_PERROS, [])
            nuevo_id = max((p["id"] for p in perros), default=0) + 1
            perros.append({
                "id": nuevo_id, "nombre": datos["nombre"],
                "edad_anios": datos["edad_anios"], "tamanio": datos["tamanio"],
                "energia": datos["energia"],
                "bueno_con_chicos": datos["bueno_con_chicos"],
                "bueno_con_otros": datos["bueno_con_otros"],
                "likes": 0, "meses_espera": datos["meses_espera"],
                "estado": "disponible",
                "foto_url": datos.get("foto_url"),
                "descripcion": datos.get("descripcion"),
                "refugio_id": quien["id"],
                "refugio_nombre": quien.get("datos_refugio", {}).get("organizacion", quien["nombre"]),
            })
            guardar(ARCHIVO_PERROS, perros)
            self.responder_json({"ok": True, "id": nuevo_id})

        elif self.path == "/api/perros/adoptado":
            datos = self.leer_cuerpo()
            quien = self.quien_soy()
            if not quien or quien["rol"] not in ("refugio", "admin"):
                self.responder_json({"error": "Sesión inválida"}, 403); return
            perros = cargar(ARCHIVO_PERROS, [])
            solicitudes = cargar(ARCHIVO_SOLICITUDES, [])
            nuevo_estado = datos.get("estado", "adoptado")
            for p in perros:
                if p["id"] == datos["perro_id"]:
                    p["estado"] = nuevo_estado
            if nuevo_estado == "adoptado":
                for s in solicitudes:
                    if s["perro_id"] == datos["perro_id"] and s["estado"] == "pendiente":
                        s["estado"] = "cerrada"
            guardar(ARCHIVO_PERROS, perros)
            guardar(ARCHIVO_SOLICITUDES, solicitudes)
            self.responder_json({"ok": True})

        elif self.path == "/api/mensajes/borrar":
            datos = self.leer_cuerpo()
            quien = self.quien_soy()
            if not quien:
                self.responder_json({"error": "Sesión inválida"}, 403); return
            mensajes = cargar(ARCHIVO_MENSAJES, [])
            mensajes = [m for m in mensajes if m["solicitud_id"] != datos["solicitud_id"]]
            guardar(ARCHIVO_MENSAJES, mensajes)
            self.responder_json({"ok": True})

        else:
            self.responder_json({"error": "Ruta desconocida"}, 404)

    def log_message(self, formato, *args):
        print(f"  📡 {self.command} {self.path}")


if __name__ == "__main__":
    usuarios = cargar(ARCHIVO_USUARIOS, [])
    print("=" * 52)
    print("  🐾 OLFATO — Servidor v5 con chat en tiempo real")
    print(f"  Abrí en tu navegador: http://localhost:{PUERTO}")
    asegurar_admin(usuarios)
    print("  Para frenarlo: Ctrl + C")
    print("=" * 52)
    try:
        servidor = HTTPServer(("0.0.0.0", PUERTO), ServidorOlfato)
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\n🐾 Servidor frenado. ¡Hasta luego!")
