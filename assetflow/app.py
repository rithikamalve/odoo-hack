"""
AssetFlow - Enterprise Asset & Resource Management System
Hackathon build - single-file Flask app with SQLite.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000

Seeded accounts (all passwords shown below):
    admin@assetflow.com      / admin123      (Admin)
    manager@assetflow.com    / manager123    (Asset Manager)
    alice@assetflow.com      / password123   (Employee)
    bob@assetflow.com        / password123   (Employee)
    carol@assetflow.com      / password123   (Employee)
"""

import os
import sqlite3
from datetime import datetime
from functools import wraps

import qrcode
from flask import (Flask, g, redirect, render_template, request,
                    session, url_for, flash)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "assetflow.db")
QR_DIR = os.path.join(BASE_DIR, "static", "qr")
os.makedirs(QR_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = "hackathon-secret-key-change-me"

DATE_FMT = "%Y-%m-%dT%H:%M"  # matches <input type="datetime-local">


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    fresh = not os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'Employee'  -- Admin / AssetManager / Employee
        );

        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Available',  -- Available/Allocated/Under Maintenance/Retired/Lost
            holder_id INTEGER,
            bookable INTEGER NOT NULL DEFAULT 0,
            acquisition_cost REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (holder_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            employee_id INTEGER NOT NULL,
            allocated_at TEXT NOT NULL,
            expected_return TEXT,
            returned_at TEXT,
            FOREIGN KEY (asset_id) REFERENCES assets(id),
            FOREIGN KEY (employee_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            employee_id INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Upcoming',  -- Upcoming/Ongoing/Completed/Cancelled
            FOREIGN KEY (asset_id) REFERENCES assets(id),
            FOREIGN KEY (employee_id) REFERENCES users(id)
        );
        """
    )
    conn.commit()

    if fresh:
        seed(conn)
    conn.close()


def seed(conn):
    def add_user(name, email, pw, role):
        conn.execute(
            "INSERT INTO users (name, email, password, role) VALUES (?,?,?,?)",
            (name, email, generate_password_hash(pw), role),
        )

    add_user("Admin User", "admin@assetflow.com", "admin123", "Admin")
    add_user("Asset Manager", "manager@assetflow.com", "manager123", "AssetManager")
    add_user("Alice", "alice@assetflow.com", "password123", "Employee")
    add_user("Bob", "bob@assetflow.com", "password123", "Employee")
    add_user("Carol", "carol@assetflow.com", "password123", "Employee")
    conn.commit()

    now = datetime.utcnow().strftime(DATE_FMT)

    def add_asset(tag, name, category, cost, bookable=0):
        conn.execute(
            "INSERT INTO assets (tag,name,category,status,bookable,acquisition_cost,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (tag, name, category, "Available", bookable, cost, now),
        )

    add_asset("AF-0001", "Dell Laptop 14\"", "Electronics", 65000)
    add_asset("AF-0002", "Ergonomic Office Chair", "Furniture", 8000)
    add_asset("AF-0003", "Projector - Epson X200", "Electronics", 32000)
    add_asset("AF-0004", "Conference Room B2", "Room", 0, bookable=1)
    add_asset("AF-0005", "Company Vehicle - Van 03", "Vehicle", 900000, bookable=1)
    add_asset("AF-0006", "Meeting Pod - Alpha", "Room", 0, bookable=1)
    conn.commit()

    # Give Alice a laptop that's already been returned late once (for trust score demo)
    alice_id = conn.execute("SELECT id FROM users WHERE email='alice@assetflow.com'").fetchone()["id"]
    laptop_id = conn.execute("SELECT id FROM assets WHERE tag='AF-0001'").fetchone()["id"]
    conn.execute(
        "INSERT INTO allocations (asset_id, employee_id, allocated_at, expected_return, returned_at) "
        "VALUES (?,?,?,?,?)",
        (laptop_id, alice_id, "2026-06-01T09:00", "2026-06-10T09:00", "2026-06-15T09:00"),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------
def current_user():
    if "user_id" not in session:
        return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Please log in first.", "warning")
                return redirect(url_for("login"))
            if user["role"] not in roles:
                flash("You don't have permission to do that.", "danger")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


# --------------------------------------------------------------------------
# Business logic helpers
# --------------------------------------------------------------------------
def parse_dt(s):
    return datetime.strptime(s, DATE_FMT) if s else None


def compute_trust_score(conn, employee_id):
    """
    Unique feature #1: Asset Trust Score.
    Heuristic reputation score (0-100) based on allocation history:
      - late returns: -15 each
      - currently holding an asset past its expected return date: -20 each
    New employees with no history default to 100 (neutral/trusted).
    """
    rows = conn.execute(
        "SELECT expected_return, returned_at FROM allocations WHERE employee_id=?",
        (employee_id,),
    ).fetchall()
    if not rows:
        return 100
    now = datetime.utcnow()
    penalty = 0
    for r in rows:
        exp = parse_dt(r["expected_return"])
        ret = parse_dt(r["returned_at"])
        if exp is None:
            continue
        if ret:
            if ret > exp:
                penalty += 15
        else:
            if now > exp:
                penalty += 20
    return max(0, 100 - penalty)


def refresh_booking_statuses(conn):
    """Auto-flip Upcoming -> Ongoing -> Completed based on current time."""
    now = datetime.utcnow()
    rows = conn.execute(
        "SELECT id, start_time, end_time, status FROM bookings WHERE status IN ('Upcoming','Ongoing')"
    ).fetchall()
    for r in rows:
        start, end = parse_dt(r["start_time"]), parse_dt(r["end_time"])
        new_status = r["status"]
        if now > end:
            new_status = "Completed"
        elif start <= now <= end:
            new_status = "Ongoing"
        if new_status != r["status"]:
            conn.execute("UPDATE bookings SET status=? WHERE id=?", (new_status, r["id"]))
    conn.commit()


def get_qr_path(tag):
    filename = f"{tag}.png"
    filepath = os.path.join(QR_DIR, filename)
    if not os.path.exists(filepath):
        url = url_for("lookup", tag=tag, _external=True)
        img = qrcode.make(url)
        img.save(filepath)
    return f"qr/{filename}"


def next_asset_tag(conn):
    row = conn.execute("SELECT COUNT(*) AS c FROM assets").fetchone()
    return f"AF-{row['c'] + 1:04d}"


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("dashboard") if current_user() else url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        pw = request.form["password"]
        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            flash("An account with that email already exists.", "danger")
            return redirect(url_for("signup"))
        # Signup ALWAYS creates an Employee account - no role selection at signup.
        db.execute(
            "INSERT INTO users (name, email, password, role) VALUES (?,?,?,?)",
            (name, email, generate_password_hash(pw), "Employee"),
        )
        db.commit()
        flash("Account created! Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        pw = request.form["password"]
        user = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password"], pw):
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    refresh_booking_statuses(db)
    now = datetime.utcnow().strftime(DATE_FMT)

    kpis = {
        "available": db.execute("SELECT COUNT(*) c FROM assets WHERE status='Available'").fetchone()["c"],
        "allocated": db.execute("SELECT COUNT(*) c FROM assets WHERE status='Allocated'").fetchone()["c"],
        "active_bookings": db.execute(
            "SELECT COUNT(*) c FROM bookings WHERE status IN ('Upcoming','Ongoing')"
        ).fetchone()["c"],
        "overdue": db.execute(
            "SELECT COUNT(*) c FROM allocations WHERE returned_at IS NULL AND expected_return IS NOT NULL AND expected_return < ?",
            (now,),
        ).fetchone()["c"],
    }

    overdue_rows = db.execute(
        """SELECT a.tag, a.name, u.name AS holder, al.expected_return
           FROM allocations al
           JOIN assets a ON a.id = al.asset_id
           JOIN users u ON u.id = al.employee_id
           WHERE al.returned_at IS NULL AND al.expected_return IS NOT NULL AND al.expected_return < ?
           ORDER BY al.expected_return ASC""",
        (now,),
    ).fetchall()

    upcoming_bookings = db.execute(
        """SELECT b.*, a.name AS asset_name, u.name AS employee_name
           FROM bookings b JOIN assets a ON a.id=b.asset_id JOIN users u ON u.id=b.employee_id
           WHERE b.status IN ('Upcoming','Ongoing') ORDER BY b.start_time ASC LIMIT 6"""
    ).fetchall()

    return render_template(
        "dashboard.html", kpis=kpis, overdue_rows=overdue_rows, upcoming_bookings=upcoming_bookings
    )


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------
@app.route("/assets", methods=["GET", "POST"])
@login_required
def assets():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        if user["role"] not in ("Admin", "AssetManager"):
            flash("Only Admins/Asset Managers can register assets.", "danger")
            return redirect(url_for("assets"))
        name = request.form["name"].strip()
        category = request.form["category"].strip()
        cost = float(request.form.get("acquisition_cost") or 0)
        bookable = 1 if request.form.get("bookable") == "on" else 0
        tag = next_asset_tag(db)
        db.execute(
            "INSERT INTO assets (tag,name,category,status,bookable,acquisition_cost,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (tag, name, category, "Available", bookable, cost, datetime.utcnow().strftime(DATE_FMT)),
        )
        db.commit()
        flash(f"Asset {tag} registered.", "success")
        return redirect(url_for("assets"))

    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")
    query = "SELECT a.*, u.name AS holder_name FROM assets a LEFT JOIN users u ON u.id=a.holder_id WHERE 1=1"
    params = []
    if q:
        query += " AND (a.tag LIKE ? OR a.name LIKE ? OR a.category LIKE ?)"
        params += [f"%{q}%"] * 3
    if status_filter:
        query += " AND a.status = ?"
        params.append(status_filter)
    query += " ORDER BY a.id DESC"
    rows = db.execute(query, params).fetchall()

    return render_template("assets.html", assets=rows, q=q, status_filter=status_filter)


@app.route("/assets/<int:asset_id>")
@login_required
def asset_detail(asset_id):
    db = get_db()
    asset = db.execute(
        "SELECT a.*, u.name AS holder_name FROM assets a LEFT JOIN users u ON u.id=a.holder_id WHERE a.id=?",
        (asset_id,),
    ).fetchone()
    if not asset:
        flash("Asset not found.", "danger")
        return redirect(url_for("assets"))

    history = db.execute(
        """SELECT al.*, u.name AS employee_name FROM allocations al
           JOIN users u ON u.id = al.employee_id WHERE al.asset_id=? ORDER BY al.allocated_at DESC""",
        (asset_id,),
    ).fetchall()

    qr_rel_path = get_qr_path(asset["tag"])
    return render_template("asset_detail.html", asset=asset, history=history, qr_rel_path=qr_rel_path)


@app.route("/assets/<int:asset_id>/return", methods=["POST"])
@roles_required("Admin", "AssetManager")
def return_asset(asset_id):
    db = get_db()
    asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not asset or asset["status"] != "Allocated":
        flash("Asset is not currently allocated.", "warning")
        return redirect(url_for("assets"))

    now = datetime.utcnow().strftime(DATE_FMT)
    db.execute(
        "UPDATE allocations SET returned_at=? WHERE asset_id=? AND returned_at IS NULL",
        (now, asset_id),
    )
    db.execute("UPDATE assets SET status='Available', holder_id=NULL WHERE id=?", (asset_id,))
    db.commit()
    flash("Asset marked as returned.", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


# --------------------------------------------------------------------------
# Allocation (with Trust Score feature)
# --------------------------------------------------------------------------
@app.route("/allocate", methods=["GET", "POST"])
@roles_required("Admin", "AssetManager")
def allocate():
    db = get_db()

    if request.method == "POST":
        asset_id = int(request.form["asset_id"])
        employee_id = int(request.form["employee_id"])
        expected_return = request.form.get("expected_return") or None

        asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset:
            flash("Asset not found.", "danger")
            return redirect(url_for("allocate"))

        if asset["status"] != "Available":
            holder = db.execute("SELECT name FROM users WHERE id=?", (asset["holder_id"],)).fetchone()
            holder_name = holder["name"] if holder else "someone"
            flash(
                f"Conflict: '{asset['name']}' ({asset['tag']}) is currently held by {holder_name}. "
                f"Ask them to return it before reallocating.",
                "danger",
            )
            return redirect(url_for("allocate"))

        now = datetime.utcnow().strftime(DATE_FMT)
        db.execute(
            "INSERT INTO allocations (asset_id, employee_id, allocated_at, expected_return) VALUES (?,?,?,?)",
            (asset_id, employee_id, now, expected_return),
        )
        db.execute(
            "UPDATE assets SET status='Allocated', holder_id=? WHERE id=?", (employee_id, asset_id)
        )
        db.commit()
        flash("Asset allocated successfully.", "success")
        return redirect(url_for("assets"))

    available_assets = db.execute("SELECT * FROM assets WHERE status='Available' ORDER BY name").fetchall()
    employees = db.execute("SELECT * FROM users WHERE role='Employee' ORDER BY name").fetchall()
    employees_with_scores = [
        {"user": e, "score": compute_trust_score(db, e["id"])} for e in employees
    ]
    return render_template(
        "allocate.html", available_assets=available_assets, employees=employees_with_scores
    )


# --------------------------------------------------------------------------
# Bookings (shared/bookable assets, overlap validation)
# --------------------------------------------------------------------------
@app.route("/bookings", methods=["GET", "POST"])
@login_required
def bookings():
    db = get_db()
    user = current_user()
    refresh_booking_statuses(db)

    if request.method == "POST":
        asset_id = int(request.form["asset_id"])
        start_time = request.form["start_time"]
        end_time = request.form["end_time"]

        start_dt, end_dt = parse_dt(start_time), parse_dt(end_time)
        if not start_dt or not end_dt or end_dt <= start_dt:
            flash("Invalid time range.", "danger")
            return redirect(url_for("bookings"))

        overlap = db.execute(
            """SELECT * FROM bookings WHERE asset_id=? AND status IN ('Upcoming','Ongoing')
               AND start_time < ? AND end_time > ?""",
            (asset_id, end_time, start_time),
        ).fetchone()
        if overlap:
            flash("That time slot overlaps with an existing booking. Please choose another slot.", "danger")
            return redirect(url_for("bookings"))

        db.execute(
            "INSERT INTO bookings (asset_id, employee_id, start_time, end_time, status) VALUES (?,?,?,?,?)",
            (asset_id, user["id"], start_time, end_time, "Upcoming"),
        )
        db.commit()
        flash("Resource booked successfully.", "success")
        return redirect(url_for("bookings"))

    bookable_assets = db.execute("SELECT * FROM assets WHERE bookable=1 ORDER BY name").fetchall()
    all_bookings = db.execute(
        """SELECT b.*, a.name AS asset_name, a.tag AS asset_tag, u.name AS employee_name
           FROM bookings b JOIN assets a ON a.id=b.asset_id JOIN users u ON u.id=b.employee_id
           ORDER BY b.start_time DESC"""
    ).fetchall()
    return render_template("bookings.html", bookable_assets=bookable_assets, bookings=all_bookings)


@app.route("/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
def cancel_booking(booking_id):
    db = get_db()
    user = current_user()
    booking = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not booking:
        flash("Booking not found.", "danger")
        return redirect(url_for("bookings"))
    if booking["employee_id"] != user["id"] and user["role"] not in ("Admin", "AssetManager"):
        flash("You can only cancel your own bookings.", "danger")
        return redirect(url_for("bookings"))
    db.execute("UPDATE bookings SET status='Cancelled' WHERE id=?", (booking_id,))
    db.commit()
    flash("Booking cancelled.", "success")
    return redirect(url_for("bookings"))


# --------------------------------------------------------------------------
# Unique feature #2: QR-code self-service public lookup (no login required)
# --------------------------------------------------------------------------
@app.route("/lookup/<tag>")
def lookup(tag):
    db = get_db()
    asset = db.execute(
        "SELECT a.*, u.name AS holder_name FROM assets a LEFT JOIN users u ON u.id=a.holder_id WHERE a.tag=?",
        (tag,),
    ).fetchone()
    if not asset:
        flash("No asset found with that tag.", "danger")
        return redirect(url_for("login"))
    return render_template("lookup.html", asset=asset)


# --------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
