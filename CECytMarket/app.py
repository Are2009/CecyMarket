from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
import os
from datetime import datetime, timezone
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import PyMongoError

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cecytmarket_clave_2026")

# El proyecto usa dos bases de datos:
#   SQLite  -> usuarios y ventas (datos con estructura fija, contraseñas y dinero)
#   MongoDB -> donaciones y notificaciones (documentos flexibles que crecen rápido)
DATABASE = os.path.join(app.root_path, "donaciones.db")

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "cecytmarket")

cliente_mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
mongo = cliente_mongo[MONGO_DB]


# BASE DE DATOS (SQLite)

def conectar():
    conexion = sqlite3.connect(DATABASE)
    conexion.row_factory = sqlite3.Row
    return conexion


def agregar_columna(conexion, tabla, columna, definicion):
    columnas = [
        fila["name"]
        for fila in conexion.execute(f"PRAGMA table_info({tabla})")
    ]

    if columna not in columnas:
        conexion.execute(
            f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}"
        )


def crear_tabla():
    conexion = conectar()

    conexion.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    conexion.execute("""
        CREATE TABLE IF NOT EXISTS ventas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prenda TEXT NOT NULL,
            talla TEXT,
            estado TEXT NOT NULL,
            precio REAL NOT NULL
        )
    """)

    agregar_columna(conexion, "ventas", "usuario_id", "INTEGER")
    agregar_columna(conexion, "ventas", "reservado_por", "INTEGER")
    agregar_columna(conexion, "ventas", "punto_encuentro", "TEXT")

    conexion.commit()
    conexion.close()


# BASE DE DATOS (MongoDB)

def preparar_mongo():
    try:
        cliente_mongo.admin.command("ping")
    except PyMongoError as error:
        raise RuntimeError(
            f"No se pudo conectar a MongoDB en {MONGO_URI}. "
            "Verifica que MongoDB esté encendido o define la variable "
            "de entorno MONGO_URI con la dirección correcta."
        ) from error

    mongo.donaciones.create_index("id", unique=True)
    mongo.notificaciones.create_index("id", unique=True)
    mongo.notificaciones.create_index(
        [("usuario_id", ASCENDING), ("id", DESCENDING)]
    )


def siguiente_id(coleccion):
    # Genera ids numéricos (1, 2, 3...) igual que AUTOINCREMENT en SQLite,
    # así las rutas como /editar/<int:id> siguen funcionando igual.
    contador = mongo.contadores.find_one_and_update(
        {"_id": coleccion},
        {"$inc": {"valor": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    return contador["valor"]


def texto_a_fecha(valor):
    if not valor:
        return datetime.now(timezone.utc)

    try:
        return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return valor


def fecha_a_texto(valor):
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d %H:%M:%S")

    return valor


def migrar_sqlite_a_mongo():
    # Copia UNA sola vez a MongoDB las donaciones y notificaciones que ya
    # estaban guardadas en SQLite. Las tablas viejas no se borran.
    if mongo.migraciones.find_one({"_id": "sqlite_a_mongo"}):
        return

    conexion = conectar()

    try:
        for tabla in ("donaciones", "notificaciones"):
            existe = conexion.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (tabla,)
            ).fetchone()

            if not existe:
                continue

            ultimo_id = 0

            for fila in conexion.execute(f"SELECT * FROM {tabla} ORDER BY id"):
                documento = dict(fila)

                if tabla == "donaciones":
                    documento.setdefault("usuario_id", None)
                    documento.setdefault("reservado_por", None)
                    documento.setdefault("punto_encuentro", None)
                else:
                    documento["fecha"] = texto_a_fecha(documento.get("fecha"))

                mongo[tabla].replace_one(
                    {"id": documento["id"]}, documento, upsert=True
                )
                ultimo_id = max(ultimo_id, documento["id"])

            secuencia = conexion.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?",
                (tabla,)
            ).fetchone()

            if secuencia:
                ultimo_id = max(ultimo_id, secuencia["seq"])

            mongo.contadores.update_one(
                {"_id": tabla},
                {"$max": {"valor": ultimo_id}},
                upsert=True
            )

    finally:
        conexion.close()

    mongo.migraciones.update_one(
        {"_id": "sqlite_a_mongo"},
        {"$set": {"fecha": datetime.now(timezone.utc)}},
        upsert=True
    )


def nombres_de_usuarios(conexion, ids):
    ids = [i for i in set(ids) if i is not None]

    if not ids:
        return {}

    marcadores = ", ".join("?" for _ in ids)
    filas = conexion.execute(
        f"SELECT id, usuario FROM usuarios WHERE id IN ({marcadores})",
        ids
    ).fetchall()

    return {fila["id"]: fila["usuario"] for fila in filas}


def crear_notificacion(usuario_id, mensaje):
    mongo.notificaciones.insert_one({
        "id": siguiente_id("notificaciones"),
        "usuario_id": usuario_id,
        "mensaje": mensaje,
        "leida": 0,
        "fecha": datetime.now(timezone.utc)
    })


# PUBLICACIONES (donaciones en MongoDB, ventas en SQLite)

def obtener_publicacion(tipo, id):
    if tipo == "donacion":
        return mongo.donaciones.find_one({"id": id}, {"_id": 0})

    conexion = conectar()

    try:
        return conexion.execute(
            """SELECT id, usuario_id, reservado_por
               FROM ventas
               WHERE id = ?""",
            (id,)
        ).fetchone()
    finally:
        conexion.close()


def reservar_publicacion(tipo, id, usuario_id, punto):
    # Solo aparta si nadie la ha apartado todavía. Devuelve True si se apartó.
    if tipo == "donacion":
        resultado = mongo.donaciones.update_one(
            {"id": id, "reservado_por": None},
            {"$set": {"reservado_por": usuario_id, "punto_encuentro": punto}}
        )
        return resultado.matched_count == 1

    conexion = conectar()

    try:
        cursor = conexion.execute(
            """UPDATE ventas
               SET reservado_por = ?, punto_encuentro = ?
               WHERE id = ? AND reservado_por IS NULL""",
            (usuario_id, punto, id)
        )
        conexion.commit()
        return cursor.rowcount == 1
    finally:
        conexion.close()


def liberar_publicacion(tipo, id):
    if tipo == "donacion":
        mongo.donaciones.update_one(
            {"id": id},
            {"$set": {"reservado_por": None, "punto_encuentro": None}}
        )
        return

    conexion = conectar()

    try:
        conexion.execute(
            """UPDATE ventas
               SET reservado_por = NULL, punto_encuentro = NULL
               WHERE id = ?""",
            (id,)
        )
        conexion.commit()
    finally:
        conexion.close()


def obtener_usuario_actual():
    nombre = session.get("usuario")

    if not nombre:
        return None

    conexion = conectar()
    usuario = conexion.execute(
        "SELECT id, usuario FROM usuarios WHERE usuario = ?",
        (nombre,)
    ).fetchone()
    conexion.close()

    return usuario


def login_requerido(funcion):
    @wraps(funcion)
    def decorador(*args, **kwargs):
        if "usuario" not in session:
            flash("Debes iniciar sesión para continuar.", "error")
            return redirect(url_for("login"))

        return funcion(*args, **kwargs)

    return decorador


def tipo_valido(tipo):
    return tipo in ("donacion", "venta")


# INICIO

@app.route("/")
def index():
    return render_template("index.html")


# REGISTRO

@app.route("/registro", methods=["GET", "POST"])
def registro():
    if "usuario" in session:
        return redirect(url_for("listado"))

    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        password = request.form.get("password", "")
        confirmar = request.form.get("confirmar", "")

        if not usuario or not password or not confirmar:
            flash("Completa todos los campos.", "error")
            return redirect(url_for("registro"))

        if len(usuario) > 50:
            flash("El usuario es demasiado largo.", "error")
            return redirect(url_for("registro"))

        if len(password) < 8:
            flash("La contraseña debe tener mínimo 8 caracteres.", "error")
            return redirect(url_for("registro"))

        if password != confirmar:
            flash("Las contraseñas no coinciden.", "error")
            return redirect(url_for("registro"))

        conexion = conectar()

        try:
            conexion.execute("""
                INSERT INTO usuarios (usuario, password)
                VALUES (?, ?)
            """, (usuario, generate_password_hash(password)))

            conexion.commit()
            flash("Cuenta creada correctamente. Inicia sesión.", "success")
            return redirect(url_for("login"))

        except sqlite3.IntegrityError:
            flash("Ese usuario ya existe.", "error")
            return redirect(url_for("registro"))

        finally:
            conexion.close()

    return render_template("registro.html")


# INICIAR SESIÓN

@app.route("/login", methods=["GET", "POST"])
def login():
    if "usuario" in session:
        return redirect(url_for("listado"))

    error = None

    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        password = request.form.get("password", "")

        conexion = conectar()

        try:
            cuenta = conexion.execute(
                "SELECT * FROM usuarios WHERE usuario = ?",
                (usuario,)
            ).fetchone()
        finally:
            conexion.close()

        if cuenta and check_password_hash(cuenta["password"], password):
            session.clear()
            session["usuario"] = cuenta["usuario"]
            flash("Bienvenido a CECyMarket.", "success")
            return redirect(url_for("listado"))

        error = "Usuario o contraseña incorrectos."

    return render_template("login.html", error=error)


# CERRAR SESIÓN

@app.route("/logout")
@login_requerido
def logout():
    session.clear()
    flash("Sesión cerrada correctamente.", "success")
    return redirect(url_for("index"))


# DONAR (se guarda en MongoDB)

@app.route("/donar", methods=["GET", "POST"])
@login_requerido
def donar():
    usuario = obtener_usuario_actual()

    if request.method == "POST":
        prenda = request.form.get("prenda", "").strip()
        talla = request.form.get("talla", "").strip()
        estado = request.form.get("estado", "").strip()

        if not prenda or estado not in ["Nuevo", "Usado"]:
            flash("Completa correctamente los campos.", "error")
            return redirect(url_for("donar"))

        mongo.donaciones.insert_one({
            "id": siguiente_id("donaciones"),
            "prenda": prenda,
            "talla": talla,
            "estado": estado,
            "usuario_id": usuario["id"],
            "reservado_por": None,
            "punto_encuentro": None
        })

        flash("Donación registrada correctamente.", "success")
        return redirect(url_for("listado"))

    return render_template("donar.html")


# VENDER (se guarda en SQLite)

@app.route("/vender", methods=["GET", "POST"])
@login_requerido
def vender():
    usuario = obtener_usuario_actual()

    if request.method == "POST":
        prenda = request.form.get("prenda", "").strip()
        talla = request.form.get("talla", "").strip()
        estado = request.form.get("estado", "").strip()
        precio = request.form.get("precio", "").strip()

        if not prenda or estado not in ["Nuevo", "Usado"]:
            flash("Completa correctamente los campos.", "error")
            return redirect(url_for("vender"))

        try:
            precio = float(precio)
            if precio <= 0:
                raise ValueError
        except ValueError:
            flash("Ingresa un precio válido.", "error")
            return redirect(url_for("vender"))

        conexion = conectar()

        try:
            conexion.execute("""
                INSERT INTO ventas
                (prenda, talla, estado, precio, usuario_id)
                VALUES (?, ?, ?, ?, ?)
            """, (prenda, talla, estado, precio, usuario["id"]))

            conexion.commit()
        finally:
            conexion.close()

        flash("Artículo publicado correctamente.", "success")
        return redirect(url_for("listado"))

    return render_template("vender.html")


# LISTADO

@app.route("/listado")
@login_requerido
def listado():
    usuario = obtener_usuario_actual()
    conexion = conectar()

    try:
        donaciones = list(
            mongo.donaciones.find({}, {"_id": 0}).sort("id", DESCENDING)
        )

        nombres = nombres_de_usuarios(
            conexion,
            [d["usuario_id"] for d in donaciones]
            + [d["reservado_por"] for d in donaciones]
        )

        for d in donaciones:
            d["nombre_dueno"] = nombres.get(d["usuario_id"])
            d["nombre_reservador"] = nombres.get(d["reservado_por"])

        ventas = conexion.execute("""
            SELECT v.*,
                   du.usuario AS nombre_dueno,
                   ru.usuario AS nombre_reservador
            FROM ventas v
            LEFT JOIN usuarios du ON du.id = v.usuario_id
            LEFT JOIN usuarios ru ON ru.id = v.reservado_por
            ORDER BY v.id DESC
        """).fetchall()

    finally:
        conexion.close()

    notificaciones = [
        {"mensaje": n["mensaje"], "fecha": fecha_a_texto(n.get("fecha"))}
        for n in mongo.notificaciones.find(
            {"usuario_id": usuario["id"]}
        ).sort("id", DESCENDING)
    ]

    return render_template(
        "listado.html",
        donaciones=donaciones,
        ventas=ventas,
        usuario_actual=usuario["id"],
        notificaciones=notificaciones
    )


# APARTAR PUBLICACIÓN

@app.route("/apartar/<tipo>/<int:id>", methods=["POST"])
@login_requerido
def apartar(tipo, id):
    if not tipo_valido(tipo):
        flash("Tipo de publicación no válido.", "error")
        return redirect(url_for("listado"))

    usuario = obtener_usuario_actual()
    punto = request.form.get("punto", "").strip()

    if not punto:
        flash("Escribe un punto de encuentro.", "error")
        return redirect(url_for("listado"))

    publicacion = obtener_publicacion(tipo, id)

    if publicacion is None:
        flash("La publicación no existe.", "error")

    elif publicacion["usuario_id"] == usuario["id"]:
        flash("No puedes apartar tu propia publicación.", "error")

    elif publicacion["reservado_por"] is not None:
        flash("Esta publicación ya fue apartada.", "error")

    elif reservar_publicacion(tipo, id, usuario["id"], punto):
        tipo_texto = "donación" if tipo == "donacion" else "producto"
        mensaje = (
            f"Tu {tipo_texto} fue apartado por {usuario['usuario']}. "
            f"Punto de encuentro: {punto}"
        )

        crear_notificacion(publicacion["usuario_id"], mensaje)
        flash("Publicación apartada correctamente.", "success")

    else:
        flash("Esta publicación ya fue apartada.", "error")

    return redirect(url_for("listado"))


# CANCELAR APARTADO

@app.route("/cancelar_apartado/<tipo>/<int:id>", methods=["POST"])
@login_requerido
def cancelar_apartado(tipo, id):
    if not tipo_valido(tipo):
        flash("Tipo de publicación no válido.", "error")
        return redirect(url_for("listado"))

    usuario = obtener_usuario_actual()
    publicacion = obtener_publicacion(tipo, id)

    if publicacion is None:
        flash("La publicación no existe.", "error")

    elif usuario["id"] not in (
        publicacion["usuario_id"], publicacion["reservado_por"]
    ):
        flash("No tienes permiso para cancelar este apartado.", "error")

    elif publicacion["reservado_por"] is None:
        flash("Esta publicación no está apartada.", "error")

    else:
        liberar_publicacion(tipo, id)
        flash("Apartado cancelado correctamente.", "success")

    return redirect(url_for("listado"))


# EDITAR DONACIÓN (MongoDB)

@app.route("/editar/<int:id>", methods=["GET", "POST"])
@login_requerido
def editar(id):
    usuario = obtener_usuario_actual()
    donacion = mongo.donaciones.find_one({"id": id}, {"_id": 0})

    if donacion is None:
        flash("La donación no existe.", "error")
        return redirect(url_for("listado"))

    if donacion["usuario_id"] != usuario["id"]:
        flash("Solo quien publicó esta donación puede editarla.", "error")
        return redirect(url_for("listado"))

    if request.method == "POST":
        prenda = request.form.get("prenda", "").strip()
        talla = request.form.get("talla", "").strip()
        estado = request.form.get("estado", "").strip()

        if not prenda or estado not in ["Nuevo", "Usado"]:
            flash("Completa correctamente los campos.", "error")
            return redirect(url_for("editar", id=id))

        mongo.donaciones.update_one(
            {"id": id, "usuario_id": usuario["id"]},
            {"$set": {"prenda": prenda, "talla": talla, "estado": estado}}
        )

        flash("Donación actualizada correctamente.", "success")
        return redirect(url_for("listado"))

    return render_template("editar.html", donacion=donacion)


# ELIMINAR DONACIÓN (MongoDB)

@app.route("/eliminar/<int:id>", methods=["POST"])
@login_requerido
def eliminar(id):
    usuario = obtener_usuario_actual()

    resultado = mongo.donaciones.delete_one(
        {"id": id, "usuario_id": usuario["id"]}
    )

    if resultado.deleted_count:
        flash("Donación eliminada correctamente.", "success")
    else:
        flash("No existe la donación o no tienes permiso.", "error")

    return redirect(url_for("listado"))


# EDITAR VENTA (SQLite)

@app.route("/editar_venta/<int:id>", methods=["GET", "POST"])
@login_requerido
def editar_venta(id):
    usuario = obtener_usuario_actual()
    conexion = conectar()

    try:
        venta = conexion.execute(
            "SELECT * FROM ventas WHERE id = ?",
            (id,)
        ).fetchone()

        if venta is None:
            flash("El producto no existe.", "error")
            return redirect(url_for("listado"))

        if venta["usuario_id"] != usuario["id"]:
            flash("Solo quien publicó este producto puede editarlo.", "error")
            return redirect(url_for("listado"))

        if request.method == "POST":
            prenda = request.form.get("prenda", "").strip()
            talla = request.form.get("talla", "").strip()
            estado = request.form.get("estado", "").strip()
            precio = request.form.get("precio", "").strip()

            if not prenda or estado not in ["Nuevo", "Usado"]:
                flash("Completa correctamente los campos.", "error")
                return redirect(url_for("editar_venta", id=id))

            try:
                precio = float(precio)
                if precio <= 0:
                    raise ValueError
            except ValueError:
                flash("Ingresa un precio válido.", "error")
                return redirect(url_for("editar_venta", id=id))

            conexion.execute("""
                UPDATE ventas
                SET prenda = ?, talla = ?, estado = ?, precio = ?
                WHERE id = ? AND usuario_id = ?
            """, (prenda, talla, estado, precio, id, usuario["id"]))

            conexion.commit()
            flash("Producto actualizado correctamente.", "success")
            return redirect(url_for("listado"))

        return render_template("editar_venta.html", venta=venta)

    finally:
        conexion.close()


# ELIMINAR VENTA (SQLite)

@app.route("/eliminar_venta/<int:id>", methods=["POST"])
@login_requerido
def eliminar_venta(id):
    usuario = obtener_usuario_actual()
    conexion = conectar()

    try:
        cursor = conexion.execute("""
            DELETE FROM ventas
            WHERE id = ? AND usuario_id = ?
        """, (id, usuario["id"]))

        conexion.commit()

        if cursor.rowcount:
            flash("Producto eliminado correctamente.", "success")
        else:
            flash("El producto no existe o no tienes permiso.", "error")

    finally:
        conexion.close()

    return redirect(url_for("listado"))


crear_tabla()
preparar_mongo()
migrar_sqlite_a_mongo()

if __name__ == "__main__":
    app.run(debug=True)
