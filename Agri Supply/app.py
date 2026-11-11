import csv
import io
import math
import os
import re
import secrets
import smtplib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

import click
import mysql.connector
from flask import (
    Flask, Response, abort, flash, g, redirect, render_template, request,
    session, url_for
)
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "database.db"
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "AGRI_SECRET_KEY", "change-this-development-secret-before-deployment"
)
app.config["DATABASE"] = str(DATABASE)
app.config["DB_TYPE"] = os.environ.get(
    "DB_TYPE", "mysql" if os.environ.get("DB_HOST") or os.environ.get("MYSQLHOST") else "sqlite"
).strip().lower()
app.config["MYSQL_CONFIG"] = {
    "host": os.environ.get("DB_HOST") or os.environ.get("MYSQLHOST", ""),
    "port": int(os.environ.get("DB_PORT") or os.environ.get("MYSQLPORT", "3306")),
    "user": os.environ.get("DB_USER") or os.environ.get("MYSQLUSER", ""),
    "password": os.environ.get("DB_PASSWORD") or os.environ.get("MYSQLPASSWORD", ""),
    "database": os.environ.get("DB_NAME") or os.environ.get("MYSQLDATABASE", ""),
}
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER", "").strip()
app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", "587"))
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME", "").strip()
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD", "")
app.config["MAIL_DEFAULT_SENDER"] = os.environ.get("MAIL_DEFAULT_SENDER", "").strip()
app.config["PUBLIC_URL"] = os.environ.get("AGRI_PUBLIC_URL", "").strip().rstrip("/")
app.config["MAIL_USE_TLS"] = os.environ.get("MAIL_USE_TLS", "true").lower() in {
    "1", "true", "yes", "on"
}
app.config["MAIL_USE_SSL"] = os.environ.get("MAIL_USE_SSL", "false").lower() in {
    "1", "true", "yes", "on"
}
app.config["ACCOUNT_TOKEN_MAX_AGE"] = 60 * 60

EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE
)

PRODUCT_IMAGE_FILES = {
    "basmati": "Basmati rice.jpg",
    "bitter melon": "Bitter melon (ampalaya).jpg",
    "ampalaya": "Bitter melon (ampalaya).jpg",
    "cabbage": "Cabbages.jpg",
    "carrot": "Carrot.jpg",
    "eggplant": "Eggplant.jpg",
    "glutinous rice": "Glutinous (sticky) rice, called malagkit in the Philippines.jpg",
    "malagkit": "Glutinous (sticky) rice, called malagkit in the Philippines.jpg",
    "okra": "Okra.jpg",
    "bok choy": "Pechay (bok choy).jpg",
    "pechay": "Pechay (bok choy).jpg",
    "squash": "Squash (kalabasa.jpg",
    "kalabasa": "Squash (kalabasa.jpg",
    "string bean": "String beans (sitaw)jpg.jpg",
    "sitaw": "String beans (sitaw)jpg.jpg",
    "tomato": "Tomato.jpg",
    "water spinach": "Water spinach (kangkong).jpg",
    "kangkong": "Water spinach (kangkong).jpg",
    "black rice": "black rice.jpg",
    "brown rice": "brown.jpg",
    "jasmine rice": "jasmine rice.jpg",
    "red rice": "red.jpg",
}


def product_image_url(product):
    """Return a local product image URL when a matching image is available."""
    filename = product["image_filename"]
    if filename and Path(filename).name == filename:
        image_path = BASE_DIR / "static" / "images" / filename
        if image_path.is_file():
            return url_for("static", filename=f"images/{filename}")

    product_name = product["product_name"].lower()
    for product_keyword, filename in PRODUCT_IMAGE_FILES.items():
        if product_keyword in product_name:
            image_path = BASE_DIR / "static" / "images" / filename
            if image_path.is_file():
                return url_for("static", filename=f"images/{filename}")
    return None


app.jinja_env.globals["product_image_url"] = product_image_url

DATABASE_ERRORS = (sqlite3.Error, mysql.connector.Error)
DATABASE_INTEGRITY_ERRORS = (sqlite3.IntegrityError, mysql.connector.IntegrityError)
VALUE_OR_DATABASE_ERRORS = (ValueError,) + DATABASE_ERRORS
KEY_OR_DATABASE_ERRORS = (KeyError,) + DATABASE_ERRORS


class DatabaseRow(dict):
    """Provide SQLite-compatible name and positional access to MySQL rows."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class MySQLCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self._lastrowid = cursor.lastrowid
        self._rowcount = cursor.rowcount
        if not cursor.with_rows:
            cursor.close()

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self._rowcount

    def fetchone(self):
        try:
            row = self._cursor.fetchone()
        finally:
            self._cursor.close()
        return DatabaseRow(row) if row is not None else None

    def fetchall(self):
        try:
            return [DatabaseRow(row) for row in self._cursor.fetchall()]
        finally:
            self._cursor.close()

    def close(self):
        self._cursor.close()


class MySQLConnection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, args=()):
        if query.strip().upper() == "BEGIN IMMEDIATE":
            query = "START TRANSACTION"
        else:
            query = query.replace("?", "%s")
        cursor = self._connection.cursor(dictionary=True, buffered=True)
        try:
            cursor.execute(query, args)
        except mysql.connector.Error:
            cursor.close()
            raise
        return MySQLCursor(cursor)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def is_valid_email(email):
    """Check email syntax before storing an account address."""
    local_part, separator, _ = email.partition("@")
    return (
        len(email) <= 254
        and separator == "@"
        and len(local_part) <= 64
        and not local_part.startswith(".")
        and not local_part.endswith(".")
        and ".." not in local_part
        and EMAIL_PATTERN.fullmatch(email) is not None
    )


def mail_configuration_error(require_public_url=True):
    if not app.config["MAIL_SERVER"] or not app.config["MAIL_DEFAULT_SENDER"]:
        return "Email delivery is not configured. Set MAIL_SERVER and MAIL_DEFAULT_SENDER."
    public_url = app.config["PUBLIC_URL"]
    if require_public_url and not public_url:
        return "Set AGRI_PUBLIC_URL to the application's public origin before enabling email."
    if public_url:
        try:
            parsed_public_url = urlsplit(public_url)
            parsed_public_url.port
        except ValueError:
            return "AGRI_PUBLIC_URL must be a valid HTTP(S) origin."
        if (
            parsed_public_url.scheme not in {"http", "https"}
            or not parsed_public_url.hostname
            or parsed_public_url.username
            or parsed_public_url.password
            or parsed_public_url.path not in {"", "/"}
            or parsed_public_url.query
            or parsed_public_url.fragment
        ):
            return "AGRI_PUBLIC_URL must be an HTTP(S) origin without a path or credentials."
    if not 1 <= app.config["MAIL_PORT"] <= 65535:
        return "MAIL_PORT must be between 1 and 65535."
    if app.config["MAIL_USE_TLS"] and app.config["MAIL_USE_SSL"]:
        return "Choose either MAIL_USE_TLS or MAIL_USE_SSL, not both."
    if bool(app.config["MAIL_USERNAME"]) != bool(app.config["MAIL_PASSWORD"]):
        return "Set both MAIL_USERNAME and MAIL_PASSWORD, or leave both blank."
    return None


def send_account_email(recipient, subject, body):
    configuration_error = mail_configuration_error(require_public_url=False)
    if configuration_error:
        raise RuntimeError(configuration_error)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = app.config["MAIL_DEFAULT_SENDER"]
    message["To"] = recipient
    message.set_content(body)

    smtp_class = smtplib.SMTP_SSL if app.config["MAIL_USE_SSL"] else smtplib.SMTP
    with smtp_class(
        app.config["MAIL_SERVER"], app.config["MAIL_PORT"], timeout=15
    ) as smtp:
        if app.config["MAIL_USE_TLS"]:
            smtp.starttls()
        if app.config["MAIL_USERNAME"]:
            smtp.login(app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])
        smtp.send_message(message)


def send_order_status_notification(order_reference, order_status):
    user = query_db(
        """SELECT u.full_name, u.email
           FROM transactions t
           JOIN customers c ON c.customer_id = t.customer_id
           JOIN users u ON u.user_id = c.user_id
           WHERE t.order_reference = ? AND u.email_verified = 1 AND u.is_active = 1
           LIMIT 1""",
        (order_reference,),
        one=True
    )
    if user is None:
        return False

    labels = {
        "pending": "Order placed",
        "confirmed": "Order confirmed",
        "processing": "Preparing your order",
        "shipped": "Order shipped",
        "delivered": "Order delivered",
        "cancelled": "Order cancelled",
    }
    status_label = labels[order_status]
    send_account_email(
        user["email"],
        f"Agri Supply order update: {status_label}",
        f"Hello {user['full_name']},\n\n"
        f"Your Agri Supply order {order_reference} is now: {status_label}.\n"
        "Visit your Agri Supply account and open Track Order for the latest "
        "order timeline.\n\n"
        "Thank you for supporting local farmers."
    )
    return True


def create_account_token(db, user_id, purpose):
    token = secrets.token_urlsafe(32)
    token_hash = sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=app.config["ACCOUNT_TOKEN_MAX_AGE"]))
    db.execute(
        "DELETE FROM account_tokens WHERE user_id = ? AND purpose = ?",
        (user_id, purpose)
    )
    db.execute(
        """INSERT INTO account_tokens
           (token_hash, user_id, purpose, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            token_hash,
            user_id,
            purpose,
            expires_at.strftime("%Y-%m-%d %H:%M:%S"),
            now.strftime("%Y-%m-%d %H:%M:%S"),
        )
    )
    return token


def send_account_token(user, token, purpose):
    if purpose == "verify_email":
        endpoint = "verify_email"
        subject = "Verify your Agri Supply email"
        action = "verify your email address"
    elif purpose == "reset_password":
        endpoint = "reset_password"
        subject = "Reset your Agri Supply password"
        action = "reset your password"
    else:
        raise ValueError("Unsupported account token purpose.")
    link_path = url_for(endpoint, token=token)
    link = (
        f"{app.config['PUBLIC_URL']}{link_path}"
        if app.config["PUBLIC_URL"]
        else url_for(endpoint, token=token, _external=True)
    )
    send_account_email(
        user["email"],
        subject,
        f"Hello {user['full_name']},\n\n"
        f"Use the link below to {action}. It expires in one hour and can only "
        "be used once:\n\n"
        f"{link}\n\n"
        "If you did not request this, you can ignore this email."
    )


# -----------------------------
# DATABASE CONNECTION
# -----------------------------
def get_db():
    """Return one database connection for the current request."""
    if "db" not in g:
        if app.config["DB_TYPE"] == "mysql":
            mysql_config = app.config["MYSQL_CONFIG"]
            missing = [
                key for key in ("host", "user", "database")
                if not mysql_config[key]
            ]
            if missing:
                raise RuntimeError(
                    "MySQL is selected but its connection settings are incomplete: "
                    + ", ".join(missing)
                )
            g.db = MySQLConnection(
                mysql.connector.connect(**mysql_config, autocommit=False)
            )
        elif app.config["DB_TYPE"] == "sqlite":
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        else:
            raise RuntimeError("DB_TYPE must be either 'sqlite' or 'mysql'.")
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    """Close the request's database connection."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_db(query, args=(), one=False):
    """Run a SELECT query and return rows."""
    cursor = get_db().execute(query, args)
    rows = cursor.fetchall()
    cursor.close()
    return (rows[0] if rows else None) if one else rows


@app.before_request
def migrate_runtime_schema():
    """Apply additive upgrades required by current routes to existing databases."""
    if app.config["DB_TYPE"] == "mysql":
        migrated_databases = app.extensions.setdefault("agri_supply_migrations", set())
        if "mysql" in migrated_databases:
            return
        try:
            get_db().execute("SELECT user_id FROM users LIMIT 0").close()
        except mysql.connector.Error as error:
            raise RuntimeError(
                "The MySQL schema is not initialized. Run 'flask init-db' before "
                "starting the application."
            ) from error
        migrated_databases.add("mysql")
        return

    database_path = app.config["DATABASE"]
    migrated_databases = app.extensions.setdefault("agri_supply_migrations", set())
    if database_path in migrated_databases:
        return

    db = get_db()
    user_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
    }
    if "email_verified" not in user_columns:
        db.execute(
            "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 1 "
            "CHECK (email_verified IN (0, 1))"
        )
    db.execute(
        """CREATE TABLE IF NOT EXISTS account_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            purpose TEXT NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS idx_account_tokens_user_purpose
           ON account_tokens(user_id, purpose)"""
    )
    supplier_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(suppliers)").fetchall()
    }
    if "is_active" not in supplier_columns:
        db.execute(
            "ALTER TABLE suppliers ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 "
            "CHECK (is_active IN (0, 1))"
        )

    transaction_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(transactions)").fetchall()
    }
    if "delivery_address" not in transaction_columns:
        db.execute(
            "ALTER TABLE transactions ADD COLUMN delivery_address TEXT NOT NULL DEFAULT ''"
        )
    if "payment_received_at" not in transaction_columns:
        db.execute(
            "ALTER TABLE transactions ADD COLUMN payment_received_at TEXT"
        )
    db.execute(
        """CREATE TABLE IF NOT EXISTS order_status_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_reference TEXT NOT NULL,
            order_status TEXT NOT NULL
                CHECK (order_status IN ('pending', 'confirmed', 'processing',
                                        'shipped', 'delivered', 'cancelled')),
            changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS idx_order_status_history_reference
           ON order_status_history(order_reference, changed_at)"""
    )
    db.execute(
        """INSERT INTO order_status_history (order_reference, order_status, changed_at)
           SELECT order_reference, 'pending', MIN(order_date)
           FROM transactions
           GROUP BY order_reference
           HAVING MIN(order_status) = 'pending'
              AND MAX(order_status) = 'pending'
              AND NOT EXISTS (
                  SELECT 1 FROM order_status_history AS history
                  WHERE history.order_reference = transactions.order_reference
              )"""
    )
    db.commit()
    migrated_databases.add(database_path)


@app.cli.command("init-db")
def init_db_command():
    """Create the database schema for the configured database backend."""
    if app.config["DB_TYPE"] == "mysql":
        schema_path = BASE_DIR / "database" / "schema.mysql.sql"
        statements = schema_path.read_text(encoding="utf-8").split(";")
        db = get_db()
        try:
            for statement in statements:
                if statement.strip():
                    db.execute(statement).close()
            db.commit()
        except mysql.connector.Error as error:
            db.rollback()
            raise click.ClickException(f"Could not initialize MySQL schema: {error}") from error
        click.echo("MySQL schema initialized.")
        return

    db = sqlite3.connect(app.config["DATABASE"])
    try:
        schema_path = BASE_DIR / "database" / "schema.sql"
        db.executescript(schema_path.read_text(encoding="utf-8"))
    except sqlite3.Error as error:
        raise click.ClickException(f"Could not initialize SQLite schema: {error}") from error
    finally:
        db.close()
    click.echo("SQLite schema initialized.")


@app.cli.command("migrate-sqlite-to-mysql")
@click.option(
    "--source",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=DATABASE,
    show_default=True,
    help="SQLite database file to copy into the configured MySQL database."
)
def migrate_sqlite_to_mysql_command(source):
    """Copy all application data from SQLite to an initialized MySQL database."""
    if app.config["DB_TYPE"] != "mysql":
        raise click.ClickException("Set DB_TYPE=mysql and configure MySQL before migrating.")

    table_order = (
        "users", "suppliers", "categories", "customers", "products", "inventory",
        "transactions", "account_tokens", "order_status_history",
    )
    target_db = get_db()
    source_db = sqlite3.connect(source)
    source_db.row_factory = sqlite3.Row
    copied = {}
    try:
        for table in table_order:
            exists = source_db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,)
            ).fetchone()
            if exists is None:
                copied[table] = 0
                continue
            rows = source_db.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                copied[table] = 0
                continue
            columns = rows[0].keys()
            column_sql = ", ".join(f"`{column}`" for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            insert_sql = (
                f"INSERT INTO `{table}` ({column_sql}) VALUES ({placeholders})"
            )
            for row in rows:
                target_db.execute(insert_sql, tuple(row[column] for column in columns)).close()
            copied[table] = len(rows)
        target_db.commit()
    except (sqlite3.Error, mysql.connector.Error) as error:
        target_db.rollback()
        raise click.ClickException(
            f"SQLite data migration failed; MySQL changes were rolled back: {error}"
        ) from error
    finally:
        source_db.close()

    for table, count in copied.items():
        click.echo(f"{table}: {count} rows")
    click.echo("SQLite data copied to MySQL.")


ALLOWED_IMAGE_SIGNATURES = {
    ".jpg": lambda header: header.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda header: header.startswith(b"\xff\xd8\xff"),
    ".png": lambda header: header.startswith(b"\x89PNG\r\n\x1a\n"),
    ".webp": lambda header: (
        len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    ),
}


def save_product_image(upload):
    """Validate and save an uploaded image under a generated local filename."""
    if upload is None or not upload.filename:
        return None

    extension = Path(upload.filename).suffix.lower()
    signature_check = ALLOWED_IMAGE_SIGNATURES.get(extension)
    if signature_check is None:
        raise ValueError("Choose a JPG, PNG, or WEBP product image.")

    header = upload.stream.read(12)
    upload.stream.seek(0)
    if not signature_check(header):
        raise ValueError("The uploaded file does not appear to be a valid image.")

    filename = f"uploaded_{uuid.uuid4().hex}{extension}"
    upload.save(BASE_DIR / "static" / "images" / filename)
    return filename


def remove_uploaded_product_image(filename):
    """Remove only app-generated uploads, never the bundled catalog images."""
    if (
        not filename
        or Path(filename).name != filename
        or not filename.startswith("uploaded_")
    ):
        return
    image_path = BASE_DIR / "static" / "images" / filename
    if image_path.is_file():
        image_path.unlink()


@app.cli.command("create-admin")
@click.option("--email", prompt=True, help="Email address for the administrator.")
@click.option("--name", prompt="Administrator name", help="Display name.")
def create_admin_command(email, name):
    """Create an administrator account using a hidden password prompt."""
    email = email.strip().lower()
    name = name.strip()
    if not is_valid_email(email):
        raise click.ClickException("Enter a valid email address.")
    if not name:
        raise click.ClickException("Administrator name is required.")

    password = click.prompt("Password (at least 8 characters)", hide_input=True)
    if len(password) < 8:
        raise click.ClickException("Password must contain at least 8 characters.")

    db = get_db()
    try:
        db.execute(
            """INSERT INTO users (full_name, email, password_hash, role)
               VALUES (?, ?, ?, 'admin')""",
            (name, email, generate_password_hash(password))
        )
        db.commit()
    except DATABASE_INTEGRITY_ERRORS as error:
        db.rollback()
        raise click.ClickException("That email address is already registered.") from error
    click.echo(f"Administrator account created for {email}.")


@app.cli.command("test-email")
@click.option("--email", prompt=True, help="Address that should receive a test email.")
def test_email_command(email):
    """Send a non-sensitive test message to verify SMTP delivery."""
    email = email.strip().lower()
    if not is_valid_email(email):
        raise click.ClickException("Enter a valid recipient email address.")
    try:
        send_account_email(
            email,
            "Agri Supply email delivery test",
            "This is a test message confirming that Agri Supply email delivery is "
            "configured. No account or order details are included."
        )
    except (OSError, RuntimeError, smtplib.SMTPException) as error:
        raise click.ClickException(f"Could not send test email: {error}") from error
    click.echo(f"Test email sent to {email}.")


# -----------------------------
# LOGIN / ROLE PROTECTION
# -----------------------------
@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.user = query_db(
        "SELECT user_id, full_name, email, role FROM users "
        "WHERE user_id = ? AND is_active = 1",
        (user_id,),
        one=True
    ) if user_id else None


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped_view(*args, **kwargs):
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped_view


# -----------------------------
# PUBLIC PAGES
# -----------------------------
@app.route("/")
def index():
    featured_products = query_db(
        """SELECT p.*, c.category_name, i.quantity
           FROM products p
           JOIN categories c ON c.category_id = p.category_id
           LEFT JOIN suppliers s ON s.supplier_id = p.supplier_id
           LEFT JOIN inventory i ON i.product_id = p.product_id
           WHERE p.is_available = 1 AND (p.supplier_id IS NULL OR s.is_active = 1)
           ORDER BY p.created_at DESC
           LIMIT 8"""
    )
    return render_template("index.html", products=featured_products)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/contact")
def contact():
    return render_template("contact.html")


# -----------------------------
# AUTHENTICATION
# -----------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        phone = request.form.get("phone", "").strip()
        address = request.form.get("delivery_address", "").strip()

        if not full_name or not email or not password:
            flash("Name, email, and password are required.", "danger")
            return render_template("auth/register.html")

        if not is_valid_email(email):
            flash("Enter a valid email address, such as you@gmail.com.", "danger")
            return render_template("auth/register.html")

        if len(password) < 8:
            flash("Password must contain at least 8 characters.", "danger")
            return render_template("auth/register.html")

        configuration_error = mail_configuration_error()
        if configuration_error:
            flash(configuration_error, "danger")
            return render_template("auth/register.html")

        db = get_db()
        try:
            cursor = db.execute(
                """INSERT INTO users
                   (full_name, email, password_hash, role, is_active, email_verified)
                   VALUES (?, ?, ?, 'customer', 0, 0)""",
                (full_name, email, generate_password_hash(password))
            )
            user_id = cursor.lastrowid
            db.execute(
                """INSERT INTO customers (user_id, phone, delivery_address)
                   VALUES (?, ?, ?)""",
                (user_id, phone, address)
            )
            token = create_account_token(db, user_id, "verify_email")
            db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            db.rollback()
            flash("That email address is already registered.", "danger")
            return render_template("auth/register.html")

        user = query_db(
            "SELECT user_id, full_name, email FROM users WHERE user_id = ?",
            (user_id,),
            one=True
        )
        try:
            send_account_token(user, token, "verify_email")
        except (OSError, RuntimeError, smtplib.SMTPException):
            app.logger.exception("Could not send account verification email.")
            flash(
                "Your account was created, but the verification email could not be "
                "sent. Check the email settings or try resending it.",
                "danger"
            )
            return redirect(url_for("resend_verification"))

        flash("Check your email for a link to verify your account.", "success")
        return redirect(url_for("login"))

    return render_template("auth/register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = query_db(
            "SELECT * FROM users WHERE email = ?",
            (email,),
            one=True
        )

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.", "danger")
            return render_template("auth/login.html")

        if not user["email_verified"] or not user["is_active"]:
            flash("Verify your email before logging in.", "danger")
            return redirect(url_for("resend_verification"))

        session.clear()
        session["user_id"] = user["user_id"]
        flash("Welcome back!", "success")
        if user["role"] == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("customer_dashboard"))

    return render_template("auth/login.html")


@app.route("/verify-email/<token>", methods=["GET", "POST"])
def verify_email(token):
    token_hash = sha256(token.encode("utf-8")).hexdigest()
    token_row = query_db(
        """SELECT token_hash, user_id, expires_at FROM account_tokens
           WHERE token_hash = ? AND purpose = 'verify_email'""",
        (token_hash,),
        one=True
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if token_row is None or token_row["expires_at"] <= now:
        if token_row is not None:
            db = get_db()
            db.execute("DELETE FROM account_tokens WHERE token_hash = ?", (token_hash,))
            db.commit()
        flash("This verification link is invalid or has expired.", "danger")
        return redirect(url_for("resend_verification"))

    if request.method == "POST":
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        current_token = query_db(
            """SELECT user_id FROM account_tokens
               WHERE token_hash = ? AND purpose = 'verify_email' AND expires_at > ?""",
            (token_hash, now),
            one=True
        )
        if current_token is None:
            db.rollback()
            flash("This verification link is invalid or has expired.", "danger")
            return redirect(url_for("resend_verification"))
        db.execute(
            "UPDATE users SET email_verified = 1, is_active = 1 WHERE user_id = ?",
            (current_token["user_id"],)
        )
        db.execute("DELETE FROM account_tokens WHERE token_hash = ?", (token_hash,))
        db.commit()
        flash("Your email is verified. You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("auth/verify_email.html")


@app.route("/resend-verification", methods=["GET", "POST"])
def resend_verification():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        configuration_error = mail_configuration_error()
        if configuration_error:
            flash(configuration_error, "danger")
            return render_template("auth/resend_verification.html")
        if is_valid_email(email):
            user = query_db(
                """SELECT user_id, full_name, email FROM users
                   WHERE email = ? AND email_verified = 0 AND role = 'customer'""",
                (email,),
                one=True
            )
            if user is not None:
                db = get_db()
                token = create_account_token(db, user["user_id"], "verify_email")
                db.commit()
                try:
                    send_account_token(user, token, "verify_email")
                except (OSError, RuntimeError, smtplib.SMTPException):
                    app.logger.exception("Could not resend account verification email.")
        flash(
            "If the address belongs to an unverified account, a verification link "
            "will be sent.",
            "success"
        )
        return redirect(url_for("login"))
    return render_template("auth/resend_verification.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        configuration_error = mail_configuration_error()
        if configuration_error:
            flash(configuration_error, "danger")
            return render_template("auth/forgot_password.html")
        if is_valid_email(email):
            user = query_db(
                """SELECT user_id, full_name, email FROM users
                   WHERE email = ? AND email_verified = 1 AND is_active = 1""",
                (email,),
                one=True
            )
            if user is not None:
                db = get_db()
                token = create_account_token(db, user["user_id"], "reset_password")
                db.commit()
                try:
                    send_account_token(user, token, "reset_password")
                except (OSError, RuntimeError, smtplib.SMTPException):
                    app.logger.exception("Could not send password reset email.")
        flash(
            "If an active account uses that email, a password reset link will be sent.",
            "success"
        )
        return redirect(url_for("login"))
    return render_template("auth/forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = sha256(token.encode("utf-8")).hexdigest()
    token_row = query_db(
        """SELECT token_hash, user_id, expires_at FROM account_tokens
           WHERE token_hash = ? AND purpose = 'reset_password'""",
        (token_hash,),
        one=True
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if token_row is None or token_row["expires_at"] <= now:
        if token_row is not None:
            db = get_db()
            db.execute("DELETE FROM account_tokens WHERE token_hash = ?", (token_hash,))
            db.commit()
        flash("This password reset link is invalid or has expired.", "danger")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(password) < 8:
            flash("Password must contain at least 8 characters.", "danger")
            return render_template("auth/reset_password.html")
        if password != confirm_password:
            flash("Password and confirmation do not match.", "danger")
            return render_template("auth/reset_password.html")

        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        current_token = query_db(
            """SELECT user_id FROM account_tokens
               WHERE token_hash = ? AND purpose = 'reset_password' AND expires_at > ?""",
            (token_hash, now),
            one=True
        )
        if current_token is None:
            db.rollback()
            flash("This password reset link is invalid or has expired.", "danger")
            return redirect(url_for("forgot_password"))
        db.execute(
            "UPDATE users SET password_hash = ? WHERE user_id = ?",
            (generate_password_hash(password), current_token["user_id"])
        )
        db.execute(
            "DELETE FROM account_tokens WHERE user_id = ? AND purpose = 'reset_password'",
            (current_token["user_id"],)
        )
        db.commit()
        session.clear()
        flash("Your password has been reset. Log in with your new password.", "success")
        return redirect(url_for("login"))
    return render_template("auth/reset_password.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


# -----------------------------
# CUSTOMER PAGES
# -----------------------------
@app.route("/customer/dashboard")
@login_required
def customer_dashboard():
    if g.user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    return render_template("customer/dashboard.html")


@app.route("/customer/profile", methods=["GET", "POST"])
@login_required
def customer_profile():
    if g.user["role"] != "customer":
        abort(403)

    profile = query_db(
        """SELECT u.user_id, u.full_name, u.email, u.email_verified,
                  c.phone, c.delivery_address
           FROM users u
           LEFT JOIN customers c ON c.user_id = u.user_id
           WHERE u.user_id = ?""",
        (g.user["user_id"],),
        one=True
    )
    if profile is None:
        abort(404)

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("delivery_address", "").strip()
        email_changed = email.casefold() != profile["email"].casefold()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not full_name:
            flash("Your name is required.", "danger")
        elif not is_valid_email(email):
            flash("Enter a valid email address.", "danger")
        elif email_changed and mail_configuration_error():
            flash(mail_configuration_error(), "danger")
        elif new_password and not check_password_hash(
            query_db(
                "SELECT password_hash FROM users WHERE user_id = ?",
                (g.user["user_id"],),
                one=True
            )["password_hash"],
            current_password
        ):
            flash("Enter your current password to change it.", "danger")
        elif new_password and len(new_password) < 8:
            flash("New password must contain at least 8 characters.", "danger")
        elif new_password != confirm_password:
            flash("New password and confirmation do not match.", "danger")
        else:
            db = get_db()
            try:
                if email_changed:
                    db.execute(
                        """UPDATE users SET full_name = ?, email = ?,
                                  email_verified = 0, is_active = 0
                           WHERE user_id = ?""",
                        (full_name, email, g.user["user_id"])
                    )
                else:
                    db.execute(
                        "UPDATE users SET full_name = ?, email = ? WHERE user_id = ?",
                        (full_name, email, g.user["user_id"])
                    )
                if new_password:
                    db.execute(
                        "UPDATE users SET password_hash = ? WHERE user_id = ?",
                        (generate_password_hash(new_password), g.user["user_id"])
                    )
                if profile["user_id"]:
                    db.execute(
                        """UPDATE customers SET phone = ?, delivery_address = ?
                           WHERE user_id = ?""",
                        (phone, address, g.user["user_id"])
                    )
                token = None
                if email_changed:
                    token = create_account_token(db, g.user["user_id"], "verify_email")
                db.commit()
                if email_changed:
                    updated_user = query_db(
                        "SELECT user_id, full_name, email FROM users WHERE user_id = ?",
                        (g.user["user_id"],),
                        one=True
                    )
                    try:
                        send_account_token(updated_user, token, "verify_email")
                    except (OSError, RuntimeError, smtplib.SMTPException):
                        app.logger.exception(
                            "Could not send changed-email verification message."
                        )
                        session.clear()
                        flash(
                            "Your email was changed, but the verification message could "
                            "not be sent. Check the email settings or request another link.",
                            "danger"
                        )
                        return redirect(url_for("resend_verification"))
                    session.clear()
                    flash(
                        "Your email was changed. Verify the new address before logging in.",
                        "success"
                    )
                    return redirect(url_for("login"))
                flash("Your profile has been updated.", "success")
                return redirect(url_for("customer_profile"))
            except DATABASE_INTEGRITY_ERRORS:
                db.rollback()
                flash("That email address is already in use.", "danger")
            except DATABASE_ERRORS:
                db.rollback()
                flash("Could not update your profile. Please try again.", "danger")

        profile = {
            "user_id": profile["user_id"],
            "full_name": full_name,
            "email": email,
            "email_verified": profile["email_verified"],
            "phone": phone,
            "delivery_address": address,
        }

    return render_template("customer/profile.html", profile=profile)


@app.route("/customer/products")
def customer_products():
    category = request.args.get("category", "").strip()
    search = request.args.get("q", "").strip()
    sql = """SELECT p.*, c.category_name, COALESCE(i.quantity, 0) AS quantity
             FROM products p
             JOIN categories c ON c.category_id = p.category_id
             LEFT JOIN suppliers s ON s.supplier_id = p.supplier_id
             LEFT JOIN inventory i ON i.product_id = p.product_id
             WHERE p.is_available = 1
               AND (p.supplier_id IS NULL OR s.is_active = 1)"""
    args = []
    if category:
        sql += " AND c.category_name = ?"
        args.append(category)
    if search:
        sql += " AND (p.product_name LIKE ? OR p.description LIKE ?)"
        args.extend([f"%{search}%", f"%{search}%"])
    sql += " ORDER BY p.product_name"
    products = query_db(sql, tuple(args))
    categories = query_db("SELECT * FROM categories ORDER BY category_name")
    return render_template(
        "customer/products.html",
        products=products,
        categories=categories,
        selected_category=category,
        search=search
    )


@app.route("/customer/products/<int:product_id>")
def product_details(product_id):
    product = query_db(
        """SELECT p.*, c.category_name, COALESCE(i.quantity, 0) AS quantity
           FROM products p
           JOIN categories c ON c.category_id = p.category_id
           LEFT JOIN suppliers s ON s.supplier_id = p.supplier_id
           LEFT JOIN inventory i ON i.product_id = p.product_id
           WHERE p.product_id = ? AND p.is_available = 1
             AND (p.supplier_id IS NULL OR s.is_active = 1)""",
        (product_id,),
        one=True
    )
    if product is None:
        abort(404)
    return render_template("customer/product_details.html", product=product)


@app.route("/customer/cart")
@login_required
def cart():
    cart_items = session.get("cart", {})
    products = []
    grand_total = 0
    for product_id, quantity in cart_items.items():
        product = query_db(
            """SELECT p.*, COALESCE(i.quantity, 0) AS stock
               FROM products p
               LEFT JOIN suppliers s ON s.supplier_id = p.supplier_id
               LEFT JOIN inventory i ON i.product_id = p.product_id
               WHERE p.product_id = ? AND p.is_available = 1
                 AND (p.supplier_id IS NULL OR s.is_active = 1)""",
            (product_id,),
            one=True
        )
        if product:
            subtotal = product["price"] * int(quantity)
            grand_total += subtotal
            products.append({
                "product": product,
                "quantity": int(quantity),
                "subtotal": subtotal
            })
    return render_template(
        "customer/cart.html",
        cart_items=products,
        grand_total=grand_total
    )


@app.route("/customer/cart/add/<int:product_id>", methods=["POST"])
@login_required
def add_to_cart(product_id):
    product = query_db(
        """SELECT p.product_id, COALESCE(i.quantity, 0) AS stock
           FROM products p
           LEFT JOIN suppliers s ON s.supplier_id = p.supplier_id
           LEFT JOIN inventory i ON i.product_id = p.product_id
           WHERE p.product_id = ? AND p.is_available = 1
             AND (p.supplier_id IS NULL OR s.is_active = 1)""",
        (product_id,),
        one=True
    )
    if product is None:
        abort(404)
    try:
        quantity = max(1, int(request.form.get("quantity", 1)))
    except ValueError:
        quantity = 1
    cart_items = session.get("cart", {})
    current = int(cart_items.get(str(product_id), 0))
    if current + quantity > product["stock"]:
        flash("The requested quantity is greater than the available stock.", "danger")
    else:
        cart_items[str(product_id)] = current + quantity
        session["cart"] = cart_items
        flash("Product added to your cart.", "success")
    return redirect(url_for("cart"))


@app.route("/customer/cart/update/<int:product_id>", methods=["POST"])
@login_required
def update_cart(product_id):
    cart_items = session.get("cart", {})
    if str(product_id) not in cart_items:
        return redirect(url_for("cart"))
    try:
        quantity = int(request.form.get("quantity", 1))
    except ValueError:
        quantity = 1
    product = query_db(
        "SELECT quantity FROM inventory WHERE product_id = ?",
        (product_id,),
        one=True
    )
    stock = product["quantity"] if product else 0
    if quantity <= 0:
        cart_items.pop(str(product_id), None)
    elif quantity <= stock:
        cart_items[str(product_id)] = quantity
    else:
        flash("The requested quantity is greater than available stock.", "danger")
    session["cart"] = cart_items
    return redirect(url_for("cart"))


@app.route("/customer/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    if g.user["role"] != "customer":
        abort(403)
    cart_items = session.get("cart", {})
    if not cart_items:
        flash("Your cart is empty.", "warning")
        return redirect(url_for("customer_products"))

    customer = query_db(
        "SELECT customer_id, phone, delivery_address FROM customers WHERE user_id = ?",
        (g.user["user_id"],),
        one=True
    )
    if customer is None:
        abort(400)

    if request.method == "POST":
        db = get_db()
        delivery_address = request.form.get("delivery_address", "").strip()
        phone = request.form.get("phone", "").strip()
        payment_method = request.form.get("payment_method", "")
        if not delivery_address:
            flash("A delivery address is required.", "danger")
            return render_template(
                "customer/checkout.html", customer=customer,
                delivery_address=delivery_address, phone=phone
            )
        if payment_method != "cash_on_delivery":
            flash("Cash on Delivery is the only available payment method.", "danger")
            return render_template(
                "customer/checkout.html", customer=customer,
                delivery_address=delivery_address, phone=phone
            )

        order_reference = f"AGR-{g.user['user_id']}-{os.urandom(4).hex().upper()}"
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE customers SET phone = ?, delivery_address = ? WHERE customer_id = ?",
                (phone, delivery_address, customer["customer_id"])
            )
            for product_id, quantity in cart_items.items():
                quantity = int(quantity)
                lock_clause = (
                    " FOR UPDATE" if app.config["DB_TYPE"] == "mysql" else ""
                )
                product = db.execute(
                    """SELECT p.product_id, p.price, i.quantity
                       FROM products p
                       JOIN inventory i ON i.product_id = p.product_id
                       LEFT JOIN suppliers s ON s.supplier_id = p.supplier_id
                       WHERE p.product_id = ? AND p.is_available = 1
                         AND (p.supplier_id IS NULL OR s.is_active = 1)"""
                    + lock_clause,
                    (int(product_id),)
                ).fetchone()
                if product is None or product["quantity"] < quantity:
                    raise ValueError("One or more products no longer have enough stock.")

                total = product["price"] * quantity
                db.execute(
                    """INSERT INTO transactions
                       (order_reference, customer_id, product_id, quantity,
                        unit_price, total_amount, payment_method, delivery_address)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        order_reference, customer["customer_id"], int(product_id),
                        quantity, product["price"], total,
                        payment_method, delivery_address
                    )
                )
                db.execute(
                    """UPDATE inventory
                       SET quantity = quantity - ?, last_updated = CURRENT_TIMESTAMP
                       WHERE product_id = ?""",
                    (quantity, int(product_id))
                )
            db.execute(
                """INSERT INTO order_status_history (order_reference, order_status)
                   VALUES (?, 'pending')""",
                (order_reference,)
            )
            db.commit()
        except VALUE_OR_DATABASE_ERRORS as error:
            db.rollback()
            flash(str(error) or "Could not place your order. Please try again.", "danger")
            return redirect(url_for("cart"))

        session.pop("cart", None)
        flash(f"Order placed successfully. Reference: {order_reference}", "success")
        return redirect(url_for("orders"))

    return render_template(
        "customer/checkout.html",
        customer=customer,
        delivery_address=customer["delivery_address"] or "",
        phone=customer["phone"] or ""
    )


@app.route("/customer/orders")
@login_required
def orders():
    customer = query_db(
        "SELECT customer_id FROM customers WHERE user_id = ?",
        (g.user["user_id"],),
        one=True
    )
    if customer is None:
        return render_template("customer/orders.html", orders=[])
    order_rows = query_db(
        """SELECT order_reference, MIN(order_date) AS order_date,
                  SUM(total_amount) AS total_amount,
                  MIN(order_status) AS order_status,
                  MIN(payment_status) AS payment_status,
                  MIN(payment_received_at) AS payment_received_at,
                  MIN(delivery_address) AS delivery_address
           FROM transactions
           WHERE customer_id = ?
           GROUP BY order_reference
           ORDER BY MIN(order_date) DESC""",
        (customer["customer_id"],)
    )
    return render_template("customer/orders.html", orders=order_rows)


@app.route("/customer/track-order", methods=["GET", "POST"])
@login_required
def track_order():
    order = None
    reference = (
        request.form.get("order_reference", "").strip()
        if request.method == "POST"
        else request.args.get("order_reference", "").strip()
    )
    customer = query_db(
        "SELECT customer_id FROM customers WHERE user_id = ?",
        (g.user["user_id"],),
        one=True
    )
    if reference and customer:
        order = query_db(
            """SELECT order_reference, MIN(order_date) AS order_date,
                      SUM(total_amount) AS total_amount,
                      MIN(order_status) AS order_status,
                      MIN(payment_status) AS payment_status,
                      MIN(payment_received_at) AS payment_received_at,
                      MIN(delivery_address) AS delivery_address
               FROM transactions
               WHERE order_reference = ? AND customer_id = ?
               GROUP BY order_reference""",
            (reference, customer["customer_id"]),
            one=True
        )
    if reference and order is None:
        flash("We could not find that order reference.", "warning")
    tracking_events = []
    if order:
        history = query_db(
            """SELECT order_status, changed_at FROM order_status_history
               WHERE order_reference = ? ORDER BY history_id""",
            (reference,)
        )
        status_labels = {
            "pending": "Order placed",
            "confirmed": "Order confirmed",
            "processing": "Preparing your order",
            "shipped": "Shipped / departed from seller",
            "delivered": "Delivered",
            "cancelled": "Order cancelled",
        }
        tracking_events = [
            {
                "status": event["order_status"],
                "label": status_labels[event["order_status"]],
                "changed_at": event["changed_at"],
            }
            for event in history
        ]
        if not tracking_events:
            tracking_events = [{
                "status": order["order_status"],
                "label": status_labels[order["order_status"]],
                "changed_at": None,
            }]
    return render_template(
        "customer/track_order.html",
        order=order,
        order_reference=reference,
        tracking_events=tracking_events
    )


# -----------------------------
# ADMIN PAGES
# -----------------------------
@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    stats = {
        "products": query_db("SELECT COUNT(*) AS total FROM products", one=True)["total"],
        "suppliers": query_db("SELECT COUNT(*) AS total FROM suppliers", one=True)["total"],
        "customers": query_db("SELECT COUNT(*) AS total FROM customers", one=True)["total"],
        "open_orders": query_db(
            """SELECT COUNT(DISTINCT order_reference) AS total FROM transactions
               WHERE order_status IN ('pending', 'confirmed', 'processing', 'shipped')""",
            one=True
        )["total"],
        "today_orders": query_db(
            """SELECT COUNT(DISTINCT order_reference) AS total FROM transactions
               WHERE DATE(order_date) = CURRENT_DATE""",
            one=True
        )["total"],
        "low_stock": query_db(
            """SELECT COUNT(*) AS total FROM products p
               LEFT JOIN inventory i ON i.product_id = p.product_id
               WHERE p.is_available = 1
                 AND COALESCE(i.quantity, 0) <= COALESCE(i.reorder_level, 5)""",
            one=True
        )["total"],
        "sales": query_db(
            "SELECT COALESCE(SUM(total_amount), 0) AS total FROM transactions "
            "WHERE payment_status = 'paid'",
            one=True
        )["total"],
    }
    recent_orders = query_db(
        """SELECT t.order_reference, MIN(t.order_date) AS order_date,
                  SUM(t.total_amount) AS total_amount,
                  MIN(t.order_status) AS order_status,
                  u.full_name
           FROM transactions t
           JOIN customers c ON c.customer_id = t.customer_id
           LEFT JOIN users u ON u.user_id = c.user_id
           GROUP BY t.order_reference, u.full_name
           ORDER BY MIN(t.order_date) DESC
           LIMIT 5"""
    )
    low_stock_products = query_db(
        """SELECT p.product_name, COALESCE(i.quantity, 0) AS quantity,
                  COALESCE(i.reorder_level, 5) AS reorder_level, p.unit
           FROM products p
           LEFT JOIN inventory i ON i.product_id = p.product_id
           WHERE p.is_available = 1
             AND COALESCE(i.quantity, 0) <= COALESCE(i.reorder_level, 5)
           ORDER BY COALESCE(i.quantity, 0), p.product_name
           LIMIT 5"""
    )
    return render_template(
        "admin/dashboard.html",
        stats=stats,
        recent_orders=recent_orders,
        low_stock_products=low_stock_products
    )


@app.route("/admin/suppliers", methods=["GET", "POST"])
@admin_required
def admin_suppliers():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("supplier_name", "").strip()
        if not name:
            flash("Supplier name is required.", "danger")
        else:
            try:
                db.execute(
                    """INSERT INTO suppliers
                       (supplier_name, contact_person, phone, email, address)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        name,
                        request.form.get("contact_person", "").strip(),
                        request.form.get("phone", "").strip(),
                        request.form.get("email", "").strip(),
                        request.form.get("address", "").strip()
                    )
                )
                db.commit()
                flash("Supplier added.", "success")
            except DATABASE_ERRORS:
                db.rollback()
                flash("Could not add the supplier. Please try again.", "danger")
        return redirect(url_for("admin_suppliers"))
    suppliers = query_db(
        "SELECT * FROM suppliers ORDER BY is_active DESC, supplier_name"
    )
    return render_template("admin/suppliers.html", suppliers=suppliers)


@app.route("/admin/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_supplier(supplier_id):
    supplier = query_db(
        "SELECT * FROM suppliers WHERE supplier_id = ?", (supplier_id,), one=True
    )
    if supplier is None:
        abort(404)
    if request.method == "POST":
        name = request.form.get("supplier_name", "").strip()
        if not name:
            flash("Supplier name is required.", "danger")
            return render_template("admin/supplier_edit.html", supplier=supplier)
        try:
            db = get_db()
            db.execute(
                """UPDATE suppliers
                   SET supplier_name = ?, contact_person = ?, phone = ?, email = ?, address = ?
                   WHERE supplier_id = ?""",
                (
                    name,
                    request.form.get("contact_person", "").strip(),
                    request.form.get("phone", "").strip(),
                    request.form.get("email", "").strip(),
                    request.form.get("address", "").strip(),
                    supplier_id
                )
            )
            db.commit()
            flash("Supplier updated.", "success")
            return redirect(url_for("admin_suppliers"))
        except DATABASE_ERRORS:
            get_db().rollback()
            flash("Could not update the supplier. Please try again.", "danger")
    return render_template("admin/supplier_edit.html", supplier=supplier)


@app.route("/admin/suppliers/<int:supplier_id>/toggle", methods=["POST"])
@admin_required
def toggle_supplier(supplier_id):
    db = get_db()
    supplier = query_db(
        "SELECT is_active FROM suppliers WHERE supplier_id = ?",
        (supplier_id,),
        one=True
    )
    if supplier is None:
        abort(404)
    db.execute(
        "UPDATE suppliers SET is_active = ? WHERE supplier_id = ?",
        (0 if supplier["is_active"] else 1, supplier_id)
    )
    db.commit()
    flash(
        "Supplier disabled." if supplier["is_active"] else "Supplier enabled.",
        "success"
    )
    return redirect(url_for("admin_suppliers"))


@app.route("/admin/products", methods=["GET", "POST"])
@admin_required
def admin_products():
    db = get_db()
    if request.method == "POST":
        saved_image = None
        try:
            name = request.form["product_name"].strip()
            price = float(request.form["price"])
            quantity = int(request.form.get("quantity", 0))
            supplier_id = request.form.get("supplier_id") or None
            reorder_level = int(request.form.get("reorder_level", 5))
            if (
                not name or not math.isfinite(price) or price < 0
                or quantity < 0 or reorder_level < 0
            ):
                raise ValueError
            if supplier_id and query_db(
                "SELECT supplier_id FROM suppliers WHERE supplier_id = ? AND is_active = 1",
                (supplier_id,),
                one=True
            ) is None:
                raise ValueError("Choose an active supplier.")
            saved_image = save_product_image(request.files.get("image"))
            cursor = db.execute(
                """INSERT INTO products
                   (supplier_id, category_id, product_name, description, unit,
                    price, image_filename)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    supplier_id,
                    int(request.form["category_id"]),
                    name,
                    request.form.get("description", "").strip(),
                    request.form.get("unit", "kg").strip(),
                    price,
                    saved_image
                )
            )
            new_product_id = cursor.lastrowid
            db.execute(
                """INSERT INTO inventory (product_id, quantity, reorder_level)
                   VALUES (?, ?, ?)""",
                (new_product_id, quantity, reorder_level)
            )
            db.commit()
            flash("Product added.", "success")
        except ValueError as error:
            db.rollback()
            remove_uploaded_product_image(saved_image)
            flash(str(error) or "Please check the product details and try again.", "danger")
        except KEY_OR_DATABASE_ERRORS:
            db.rollback()
            remove_uploaded_product_image(saved_image)
            flash("Please check the product details and try again.", "danger")
        return redirect(url_for("admin_products"))

    products = query_db(
        """SELECT p.*, c.category_name, s.supplier_name,
                  COALESCE(i.quantity, 0) AS quantity
           FROM products p
           JOIN categories c ON c.category_id = p.category_id
           LEFT JOIN suppliers s ON s.supplier_id = p.supplier_id
           LEFT JOIN inventory i ON i.product_id = p.product_id
           ORDER BY p.product_name"""
    )
    categories = query_db("SELECT * FROM categories ORDER BY category_name")
    suppliers = query_db(
        "SELECT * FROM suppliers WHERE is_active = 1 ORDER BY supplier_name"
    )
    return render_template(
        "admin/products.html",
        products=products,
        categories=categories,
        suppliers=suppliers
    )


@app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_product(product_id):
    db = get_db()
    if request.method == "POST":
        product = query_db(
            "SELECT image_filename, supplier_id FROM products WHERE product_id = ?",
            (product_id,),
            one=True
        )
        if product is None:
            abort(404)
        saved_image = None
        try:
            name = request.form["product_name"].strip()
            price = float(request.form["price"])
            if not name or not math.isfinite(price) or price < 0:
                raise ValueError("Enter a product name and a valid non-negative price.")
            supplier_id = request.form.get("supplier_id") or None
            if supplier_id and query_db(
                """SELECT supplier_id FROM suppliers
                   WHERE supplier_id = ? AND (is_active = 1 OR supplier_id = ?)""",
                (supplier_id, product["supplier_id"]),
                one=True
            ) is None:
                raise ValueError("Choose an active supplier.")
            saved_image = save_product_image(request.files.get("image"))
            image_filename = saved_image or product["image_filename"]
            db.execute(
                """UPDATE products
                   SET supplier_id = ?, category_id = ?, product_name = ?,
                       description = ?, unit = ?, price = ?, image_filename = ?
                   WHERE product_id = ?""",
                (
                    supplier_id,
                    int(request.form["category_id"]),
                    name,
                    request.form.get("description", "").strip(),
                    request.form.get("unit", "kg").strip(),
                    price,
                    image_filename,
                    product_id
                )
            )
            db.commit()
            if saved_image:
                remove_uploaded_product_image(product["image_filename"])
            flash("Product updated.", "success")
            return redirect(url_for("admin_products"))
        except ValueError as error:
            db.rollback()
            remove_uploaded_product_image(saved_image)
            flash(str(error) or "Please enter valid product details.", "danger")
        except KEY_OR_DATABASE_ERRORS:
            db.rollback()
            remove_uploaded_product_image(saved_image)
            flash("Could not update the product. Check the details and try again.", "danger")

    product = query_db(
        """SELECT p.*, c.category_name, COALESCE(i.quantity, 0) AS quantity,
                  COALESCE(i.reorder_level, 5) AS reorder_level
           FROM products p
           JOIN categories c ON c.category_id = p.category_id
           LEFT JOIN inventory i ON i.product_id = p.product_id
           WHERE p.product_id = ?""",
        (product_id,),
        one=True
    )
    if product is None:
        abort(404)
    categories = query_db("SELECT * FROM categories ORDER BY category_name")
    suppliers = query_db(
        """SELECT * FROM suppliers WHERE is_active = 1 OR supplier_id = ?
           ORDER BY supplier_name""",
        (product["supplier_id"],)
    )
    return render_template(
        "admin/product_edit.html",
        product=product,
        categories=categories,
        suppliers=suppliers
    )


@app.route("/admin/products/<int:product_id>/toggle", methods=["POST"])
@admin_required
def toggle_product(product_id):
    db = get_db()
    product = query_db(
        "SELECT is_available FROM products WHERE product_id = ?",
        (product_id,),
        one=True
    )
    if product is None:
        abort(404)
    db.execute(
        "UPDATE products SET is_available = ? WHERE product_id = ?",
        (0 if product["is_available"] else 1, product_id)
    )
    db.commit()
    flash(
        "Product disabled." if product["is_available"] else "Product enabled.",
        "success"
    )
    return redirect(url_for("admin_products"))


@app.route("/admin/inventory")
@admin_required
def admin_inventory():
    inventory = query_db(
        """SELECT i.*, p.product_name, p.unit
           FROM inventory i JOIN products p ON p.product_id = i.product_id
           ORDER BY p.product_name"""
    )
    return render_template("admin/inventory.html", inventory=inventory)


@app.route("/admin/inventory/<int:product_id>", methods=["POST"])
@admin_required
def update_inventory(product_id):
    db = get_db()
    try:
        quantity = int(request.form["quantity"])
        reorder_level = int(request.form["reorder_level"])
        if quantity < 0 or reorder_level < 0:
            raise ValueError
        product = query_db(
            "SELECT product_id FROM products WHERE product_id = ?",
            (product_id,),
            one=True
        )
        if product is None:
            abort(404)
        inventory = query_db(
            "SELECT inventory_id FROM inventory WHERE product_id = ?",
            (product_id,),
            one=True
        )
        if inventory is None:
            db.execute(
                "INSERT INTO inventory (product_id, quantity, reorder_level) VALUES (?, ?, ?)",
                (product_id, quantity, reorder_level)
            )
        else:
            db.execute(
                """UPDATE inventory
                   SET quantity = ?, reorder_level = ?, last_updated = CURRENT_TIMESTAMP
                   WHERE product_id = ?""",
                (quantity, reorder_level, product_id)
            )
        db.commit()
        flash("Inventory updated.", "success")
    except (KeyError, ValueError):
        db.rollback()
        flash("Enter valid non-negative stock and reorder values.", "danger")
    except DATABASE_ERRORS:
        db.rollback()
        flash("Could not update inventory. Please try again.", "danger")
    return redirect(url_for("admin_inventory"))


@app.route("/admin/customers")
@admin_required
def admin_customers():
    customers = query_db(
        """SELECT c.*, u.full_name, u.email
           FROM customers c LEFT JOIN users u ON u.user_id = c.user_id
           ORDER BY c.customer_id DESC"""
    )
    return render_template("admin/customers.html", customers=customers)


@app.route("/admin/transactions")
@admin_required
def admin_transactions():
    products_expression = (
        "GROUP_CONCAT(CONCAT(p.product_name, ' × ', t.quantity) SEPARATOR ', ')"
        if app.config["DB_TYPE"] == "mysql"
        else "GROUP_CONCAT(p.product_name || ' × ' || t.quantity, ', ')"
    )
    transactions = query_db(
        """SELECT t.order_reference, MIN(t.order_date) AS order_date,
                  SUM(t.quantity) AS quantity, SUM(t.total_amount) AS total_amount,
                  MIN(t.payment_method) AS payment_method,
                  MIN(t.payment_status) AS payment_status,
                  MIN(t.payment_received_at) AS payment_received_at,
                  MIN(t.order_status) AS order_status,
                  MIN(t.delivery_address) AS delivery_address,
                  u.full_name,
                  {products_expression} AS products
           FROM transactions t
           JOIN customers c ON c.customer_id = t.customer_id
           LEFT JOIN users u ON u.user_id = c.user_id
           JOIN products p ON p.product_id = t.product_id
           GROUP BY t.order_reference, c.customer_id, u.full_name
           ORDER BY order_date DESC""".format(
            products_expression=products_expression
        )
    )
    return render_template("admin/transactions.html", transactions=transactions)


@app.route("/admin/orders/<order_reference>/status", methods=["POST"])
@admin_required
def update_order_status(order_reference):
    order_status = request.form.get("order_status", "")
    valid_order_statuses = {
        "pending", "confirmed", "processing", "shipped", "delivered", "cancelled"
    }
    db = get_db()
    lines = db.execute(
        """SELECT product_id, quantity, order_status, payment_method, payment_status
           FROM transactions WHERE order_reference = ?""",
        (order_reference,)
    ).fetchall()
    if not lines:
        abort(404)
    if order_status not in valid_order_statuses:
        flash("Choose a valid order status.", "danger")
        return redirect(url_for("admin_transactions"))
    previous_statuses = {line["order_status"] for line in lines}
    if (
        len(previous_statuses) != 1
        or len({line["payment_method"] for line in lines}) != 1
        or len({line["payment_status"] for line in lines}) != 1
    ):
        flash("This order has inconsistent statuses and was not changed.", "danger")
        return redirect(url_for("admin_transactions"))
    previous_status = previous_statuses.pop()
    status_changed = order_status != previous_status
    try:
        db.execute("BEGIN IMMEDIATE")
        if order_status == "cancelled" and previous_status != "cancelled":
            for line in lines:
                cursor = db.execute(
                    "UPDATE inventory SET quantity = quantity + ?, last_updated = CURRENT_TIMESTAMP "
                    "WHERE product_id = ?",
                    (line["quantity"], line["product_id"])
                )
                if cursor.rowcount == 0:
                    db.execute(
                        "INSERT INTO inventory (product_id, quantity) VALUES (?, ?)",
                        (line["product_id"], line["quantity"])
                    )
        elif previous_status == "cancelled" and order_status != "cancelled":
            for line in lines:
                cursor = db.execute(
                    """UPDATE inventory SET quantity = quantity - ?,
                           last_updated = CURRENT_TIMESTAMP
                       WHERE product_id = ? AND quantity >= ?""",
                    (line["quantity"], line["product_id"], line["quantity"])
                )
                if cursor.rowcount == 0:
                    raise ValueError("There is not enough stock to reopen this order.")
        db.execute(
            "UPDATE transactions SET order_status = ? WHERE order_reference = ?",
            (order_status, order_reference)
        )
        if status_changed:
            db.execute(
                """INSERT INTO order_status_history (order_reference, order_status)
                   VALUES (?, ?)""",
                (order_reference, order_status)
            )
        db.commit()
    except VALUE_OR_DATABASE_ERRORS as error:
        db.rollback()
        flash(str(error) or "Could not update the order. Please try again.", "danger")
        return redirect(url_for("admin_transactions"))
    flash(f"Order {order_reference} updated.", "success")
    if status_changed:
        try:
            if not send_order_status_notification(order_reference, order_status):
                flash(
                    "No verified customer email is linked to this order; no update email was sent.",
                    "warning"
                )
        except (OSError, RuntimeError, smtplib.SMTPException):
            app.logger.exception(
                "Could not email the customer about order %s status change.",
                order_reference
            )
            flash(
                "The order status was saved, but the customer update email could not "
                "be sent. Check SMTP settings and logs.",
                "warning"
            )
    return redirect(url_for("admin_transactions"))


@app.route("/admin/orders/<order_reference>/collect-cash", methods=["POST"])
@admin_required
def mark_cash_collected(order_reference):
    db = get_db()
    order = query_db(
        """SELECT MIN(payment_method) AS payment_method,
                  MIN(payment_status) AS payment_status,
                  MIN(order_status) AS order_status,
                  COUNT(DISTINCT payment_method) AS payment_method_count,
                  COUNT(DISTINCT payment_status) AS payment_status_count,
                  COUNT(DISTINCT order_status) AS order_status_count,
                  MIN(payment_received_at) AS payment_received_at
           FROM transactions WHERE order_reference = ?""",
        (order_reference,),
        one=True
    )
    if order is None or order["payment_method"] is None:
        abort(404)
    if (
        order["payment_method_count"] != 1
        or order["payment_status_count"] != 1
        or order["order_status_count"] != 1
    ):
        flash("This order has inconsistent payment details and was not changed.", "danger")
        return redirect(url_for("admin_transactions"))
    if order["payment_method"] != "cash_on_delivery":
        flash("Cash collection can only be recorded for cash-on-delivery orders.", "danger")
        return redirect(url_for("admin_transactions"))
    if order["order_status"] != "delivered":
        flash("Mark the order as delivered before confirming cash collection.", "warning")
        return redirect(url_for("admin_transactions"))
    if order["payment_status"] != "pending":
        flash("This order does not have a pending cash payment.", "warning")
        return redirect(url_for("admin_transactions"))

    cursor = db.execute(
        """UPDATE transactions
           SET payment_status = 'paid', payment_received_at = CURRENT_TIMESTAMP
           WHERE order_reference = ? AND payment_method = 'cash_on_delivery'
             AND payment_status = 'pending' AND order_status = 'delivered'""",
        (order_reference,)
    )
    if cursor.rowcount == 0:
        db.rollback()
        flash("Cash payment could not be confirmed. Refresh and check the order.", "danger")
        return redirect(url_for("admin_transactions"))
    db.commit()
    flash(f"Cash payment for order {order_reference} recorded as paid.", "success")
    return redirect(url_for("admin_transactions"))


@app.route("/admin/reports")
@admin_required
def admin_reports():
    sales = query_db(
        """SELECT DATE(order_date) AS sales_day,
                  SUM(total_amount) AS total_sales,
                  SUM(quantity) AS items_sold
           FROM transactions
           WHERE payment_status = 'paid'
           GROUP BY DATE(order_date)
           ORDER BY sales_day DESC"""
    )
    return render_template("admin/reports.html", sales=sales)


@app.route("/admin/reports/export.csv")
@admin_required
def export_sales_report():
    sales = query_db(
        """SELECT DATE(order_date) AS sales_day,
                  SUM(total_amount) AS total_sales,
                  SUM(quantity) AS items_sold
           FROM transactions
           WHERE payment_status = 'paid'
           GROUP BY DATE(order_date)
           ORDER BY sales_day DESC"""
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("Sales date", "Items sold", "Paid sales (PHP)"))
    for row in sales:
        writer.writerow((
            row["sales_day"],
            row["items_sold"],
            f'{row["total_sales"]:.2f}',
        ))

    return Response(
        output.getvalue(),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="agri-supply-sales.csv"'}
    )


@app.errorhandler(403)
def forbidden(error):
    return "Access denied: you do not have permission to view this page.", 403


@app.errorhandler(404)
def not_found(error):
    return "Page not found.", 404


if __name__ == "__main__":
    # Run with: python app.py
    app.run(debug=True)
