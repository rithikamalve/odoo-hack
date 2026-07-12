"""
AssetFlow - Enterprise Asset & Resource Management System
Single-file Flask app with SQLite.

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

import json
import os
import sqlite3
from datetime import datetime
from functools import wraps

import qrcode
from flask import (Flask, g, redirect, render_template, request,
                    session, url_for, flash, Response)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "assetflow.db")
QR_DIR = os.path.join(BASE_DIR, "static", "qr")
os.makedirs(QR_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = "hackathon-secret-key-change-me"

DATE_FMT = "%Y-%m-%dT%H:%M"        # matches <input type="datetime-local">
DATE_ONLY_FMT = "%Y-%m-%d"         # matches <input type="date">

ALL_ROLES = ["Admin", "AssetManager", "DepartmentHead", "Employee"]
MANAGER_ROLES = ("Admin", "AssetManager")


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
        CREATE TABLE IF NOT EXISTS departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            head_user_id INTEGER,
            parent_id INTEGER,
            status TEXT NOT NULL DEFAULT 'Active',   -- Active / Inactive
            FOREIGN KEY (head_user_id) REFERENCES users(id),
            FOREIGN KEY (parent_id) REFERENCES departments(id)
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            fields_json TEXT NOT NULL DEFAULT '[]',  -- list of category-specific field names
            status TEXT NOT NULL DEFAULT 'Active'
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'Employee',   -- Admin / AssetManager / DepartmentHead / Employee
            department_id INTEGER,
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );

        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            category_id INTEGER,
            department_id INTEGER,
            status TEXT NOT NULL DEFAULT 'Available',  -- Available/Allocated/Under Maintenance/Retired/Lost
            holder_id INTEGER,
            bookable INTEGER NOT NULL DEFAULT 0,
            acquisition_cost REAL DEFAULT 0,
            custom_fields_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (holder_id) REFERENCES users(id),
            FOREIGN KEY (category_id) REFERENCES categories(id),
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );

        CREATE TABLE IF NOT EXISTS allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            employee_id INTEGER NOT NULL,
            allocated_at TEXT NOT NULL,
            expected_return TEXT,
            returned_at TEXT,
            condition_note TEXT,
            FOREIGN KEY (asset_id) REFERENCES assets(id),
            FOREIGN KEY (employee_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS transfer_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            from_employee_id INTEGER,
            to_employee_id INTEGER NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'Requested',  -- Requested/Approved/Reallocated/Rejected
            requested_by INTEGER,
            requested_at TEXT NOT NULL,
            decided_at TEXT,
            FOREIGN KEY (asset_id) REFERENCES assets(id),
            FOREIGN KEY (from_employee_id) REFERENCES users(id),
            FOREIGN KEY (to_employee_id) REFERENCES users(id)
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

        CREATE TABLE IF NOT EXISTS maintenance_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            raised_by INTEGER NOT NULL,
            issue_details TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'Medium',   -- Low/Medium/High/Critical
            attachment_name TEXT,
            status TEXT NOT NULL DEFAULT 'Pending',    -- Pending/Approved/Rejected/Technician Assigned/In Progress/Resolved
            technician_name TEXT,
            resolution_notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (asset_id) REFERENCES assets(id),
            FOREIGN KEY (raised_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS audit_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            department_id INTEGER,
            location TEXT,
            start_date TEXT,
            end_date TEXT,
            auditor_id INTEGER,
            status TEXT NOT NULL DEFAULT 'Open',   -- Open / Locked
            created_at TEXT NOT NULL,
            FOREIGN KEY (department_id) REFERENCES departments(id),
            FOREIGN KEY (auditor_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS audit_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            verification_status TEXT NOT NULL DEFAULT 'Pending',  -- Pending/Verified/Missing/Damaged
            notes TEXT,
            verified_at TEXT,
            FOREIGN KEY (cycle_id) REFERENCES audit_cycles(id),
            FOREIGN KEY (asset_id) REFERENCES assets(id)
        );

        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'General',
            is_read INTEGER NOT NULL DEFAULT 0,
            link TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """
    )
    conn.commit()

    if fresh:
        seed(conn)
    conn.close()


def seed(conn):
    now = datetime.utcnow().strftime(DATE_FMT)

    def add_dept(name, head_id=None, parent_id=None, status="Active"):
        cur = conn.execute(
            "INSERT INTO departments (name, head_user_id, parent_id, status) VALUES (?,?,?,?)",
            (name, head_id, parent_id, status),
        )
        return cur.lastrowid

    eng_id = add_dept("Engineering")
    facilities_id = add_dept("Facilities")
    fieldops_id = add_dept("Field Ops", parent_id=None, status="Inactive")
    conn.commit()

    def add_category(name, fields):
        conn.execute(
            "INSERT INTO categories (name, fields_json, status) VALUES (?,?,?)",
            (name, json.dumps(fields), "Active"),
        )

    add_category("Electronics", ["Serial Number", "Warranty Expiry"])
    add_category("Furniture", ["Material"])
    add_category("Vehicle", ["Registration Number", "Fuel Type"])
    add_category("Room", ["Capacity"])
    add_category("Other", [])
    conn.commit()

    def add_user(name, email, pw, role, department_id=None):
        cur = conn.execute(
            "INSERT INTO users (name, email, password, role, department_id) VALUES (?,?,?,?,?)",
            (name, email, generate_password_hash(pw), role, department_id),
        )
        return cur.lastrowid

    add_user("Admin User", "admin@assetflow.com", "admin123", "Admin")
    add_user("Asset Manager", "manager@assetflow.com", "manager123", "AssetManager")
    alice_id = add_user("Alice", "alice@assetflow.com", "password123", "Employee", eng_id)
    add_user("Bob", "bob@assetflow.com", "password123", "Employee", facilities_id)
    add_user("Carol", "carol@assetflow.com", "password123", "Employee", eng_id)
    conn.commit()

    conn.execute("UPDATE departments SET head_user_id=? WHERE id=?", (alice_id, eng_id))
    conn.commit()

    cat_ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}

    def add_asset(tag, name, category, cost, bookable=0, department_id=None):
        conn.execute(
            "INSERT INTO assets (tag,name,category,category_id,department_id,status,bookable,"
            "acquisition_cost,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tag, name, category, cat_ids.get(category), department_id, "Available",
             bookable, cost, now),
        )

    add_asset("AF-0001", "Dell Laptop 14\"", "Electronics", 65000, department_id=eng_id)
    add_asset("AF-0002", "Ergonomic Office Chair", "Furniture", 8000, department_id=facilities_id)
    add_asset("AF-0003", "Projector - Epson X200", "Electronics", 32000, department_id=eng_id)
    add_asset("AF-0004", "Conference Room B2", "Room", 0, bookable=1, department_id=facilities_id)
    add_asset("AF-0005", "Company Vehicle - Van 03", "Vehicle", 900000, bookable=1, department_id=facilities_id)
    add_asset("AF-0006", "Meeting Pod - Alpha", "Room", 0, bookable=1, department_id=eng_id)
    conn.commit()

    # Give Alice a laptop that's already been returned late once (for trust score demo)
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
    user = current_user()
    unread = 0
    if user:
        unread = get_db().execute(
            "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0", (user["id"],)
        ).fetchone()["c"]
    return {"current_user": user, "unread_notifications": unread}


# --------------------------------------------------------------------------
# Cross-cutting helpers: activity log + notifications
# --------------------------------------------------------------------------
def log_activity(action, details=""):
    db = get_db()
    user = current_user()
    db.execute(
        "INSERT INTO activity_logs (user_id, action, details, created_at) VALUES (?,?,?,?)",
        (user["id"] if user else None, action, details, datetime.utcnow().strftime(DATE_FMT)),
    )
    db.commit()


def notify(user_id, message, category="General", link=None):
    if not user_id:
        return
    db = get_db()
    db.execute(
        "INSERT INTO notifications (user_id, message, category, is_read, link, created_at) "
        "VALUES (?,?,?,0,?,?)",
        (user_id, message, category, link, datetime.utcnow().strftime(DATE_FMT)),
    )
    db.commit()


def notify_managers(message, category="General", link=None):
    db = get_db()
    managers = db.execute("SELECT id FROM users WHERE role IN ('Admin','AssetManager')").fetchall()
    for m in managers:
        notify(m["id"], message, category, link)


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
            log_activity("Login", f"{user['name']} logged in")
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
        "pending_transfers": db.execute(
            "SELECT COUNT(*) c FROM transfer_requests WHERE status='Requested'"
        ).fetchone()["c"],
        "open_maintenance": db.execute(
            "SELECT COUNT(*) c FROM maintenance_requests WHERE status NOT IN ('Resolved','Rejected')"
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

    recent_activity = db.execute(
        """SELECT al.*, u.name AS user_name FROM activity_logs al
           LEFT JOIN users u ON u.id = al.user_id ORDER BY al.id DESC LIMIT 8"""
    ).fetchall()

    return render_template(
        "dashboard.html", kpis=kpis, overdue_rows=overdue_rows,
        upcoming_bookings=upcoming_bookings, recent_activity=recent_activity,
    )


# --------------------------------------------------------------------------
# Organization Setup (Admin only): Departments / Categories / Employees
# --------------------------------------------------------------------------
@app.route("/org-setup")
@roles_required("Admin")
def org_setup():
    db = get_db()
    departments = db.execute(
        """SELECT d.*, h.name AS head_name, p.name AS parent_name
           FROM departments d
           LEFT JOIN users h ON h.id = d.head_user_id
           LEFT JOIN departments p ON p.id = d.parent_id
           ORDER BY d.name"""
    ).fetchall()
    categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
    for c in categories:
        pass
    categories_parsed = [
        {**dict(c), "fields": json.loads(c["fields_json"] or "[]")} for c in categories
    ]
    employees = db.execute(
        """SELECT u.*, d.name AS department_name FROM users u
           LEFT JOIN departments d ON d.id = u.department_id ORDER BY u.name"""
    ).fetchall()
    all_departments = departments
    tab = request.args.get("tab", "departments")
    return render_template(
        "org_setup.html", departments=departments, categories=categories_parsed,
        employees=employees, all_departments=all_departments, all_roles=ALL_ROLES, tab=tab,
    )


@app.route("/org-setup/departments", methods=["POST"])
@roles_required("Admin")
def org_departments():
    db = get_db()
    action = request.form.get("action")
    if action == "add":
        name = request.form["name"].strip()
        head_user_id = request.form.get("head_user_id") or None
        parent_id = request.form.get("parent_id") or None
        try:
            db.execute(
                "INSERT INTO departments (name, head_user_id, parent_id, status) VALUES (?,?,?,?)",
                (name, head_user_id, parent_id, "Active"),
            )
            db.commit()
            log_activity("Department created", name)
            flash(f"Department '{name}' created.", "success")
        except sqlite3.IntegrityError:
            flash("A department with that name already exists.", "danger")
    elif action == "edit":
        dept_id = request.form["dept_id"]
        name = request.form["name"].strip()
        head_user_id = request.form.get("head_user_id") or None
        parent_id = request.form.get("parent_id") or None
        db.execute(
            "UPDATE departments SET name=?, head_user_id=?, parent_id=? WHERE id=?",
            (name, head_user_id, parent_id, dept_id),
        )
        db.commit()
        log_activity("Department updated", name)
        flash(f"Department '{name}' updated.", "success")
    elif action == "toggle":
        dept_id = request.form["dept_id"]
        row = db.execute("SELECT * FROM departments WHERE id=?", (dept_id,)).fetchone()
        new_status = "Inactive" if row["status"] == "Active" else "Active"
        db.execute("UPDATE departments SET status=? WHERE id=?", (new_status, dept_id))
        db.commit()
        log_activity("Department status changed", f"{row['name']} -> {new_status}")
        flash(f"Department '{row['name']}' marked {new_status}.", "success")
    elif action == "delete":
        dept_id = request.form["dept_id"]
        in_use = db.execute("SELECT COUNT(*) c FROM users WHERE department_id=?", (dept_id,)).fetchone()["c"]
        if in_use:
            flash("Cannot delete a department that still has employees assigned. Deactivate it instead.", "danger")
        else:
            db.execute("DELETE FROM departments WHERE id=?", (dept_id,))
            db.commit()
            flash("Department deleted.", "success")
    return redirect(url_for("org_setup", tab="departments"))


@app.route("/org-setup/categories", methods=["POST"])
@roles_required("Admin")
def org_categories():
    db = get_db()
    action = request.form.get("action")
    if action == "add":
        name = request.form["name"].strip()
        fields_raw = request.form.get("fields", "")
        fields = [f.strip() for f in fields_raw.split(",") if f.strip()]
        try:
            db.execute(
                "INSERT INTO categories (name, fields_json, status) VALUES (?,?,?)",
                (name, json.dumps(fields), "Active"),
            )
            db.commit()
            log_activity("Category created", name)
            flash(f"Category '{name}' created.", "success")
        except sqlite3.IntegrityError:
            flash("A category with that name already exists.", "danger")
    elif action == "edit":
        cat_id = request.form["cat_id"]
        name = request.form["name"].strip()
        fields_raw = request.form.get("fields", "")
        fields = [f.strip() for f in fields_raw.split(",") if f.strip()]
        db.execute(
            "UPDATE categories SET name=?, fields_json=? WHERE id=?",
            (name, json.dumps(fields), cat_id),
        )
        db.commit()
        log_activity("Category updated", name)
        flash(f"Category '{name}' updated.", "success")
    elif action == "toggle":
        cat_id = request.form["cat_id"]
        row = db.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
        new_status = "Inactive" if row["status"] == "Active" else "Active"
        db.execute("UPDATE categories SET status=? WHERE id=?", (new_status, cat_id))
        db.commit()
        flash(f"Category '{row['name']}' marked {new_status}.", "success")
    return redirect(url_for("org_setup", tab="categories"))


@app.route("/org-setup/employees", methods=["POST"])
@roles_required("Admin")
def org_employees():
    db = get_db()
    user_id = request.form["user_id"]
    role = request.form["role"]
    department_id = request.form.get("department_id") or None
    if role not in ALL_ROLES:
        flash("Invalid role.", "danger")
        return redirect(url_for("org_setup", tab="employees"))
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    db.execute("UPDATE users SET role=?, department_id=? WHERE id=?", (role, department_id, user_id))
    db.commit()
    log_activity("Employee role updated", f"{user['name']} -> {role}")
    notify(user_id, f"Your role was updated to {role}.", "Admin")
    flash(f"Updated {user['name']}'s role/department.", "success")
    return redirect(url_for("org_setup", tab="employees"))


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------
@app.route("/assets", methods=["GET", "POST"])
@login_required
def assets():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        if user["role"] not in MANAGER_ROLES:
            flash("Only Admins/Asset Managers can register assets.", "danger")
            return redirect(url_for("assets"))
        name = request.form["name"].strip()
        category = request.form["category"].strip()
        cost = float(request.form.get("acquisition_cost") or 0)
        bookable = 1 if request.form.get("bookable") == "on" else 0
        department_id = request.form.get("department_id") or None
        cat_row = db.execute("SELECT id FROM categories WHERE name=?", (category,)).fetchone()
        tag = next_asset_tag(db)
        db.execute(
            "INSERT INTO assets (tag,name,category,category_id,department_id,status,bookable,"
            "acquisition_cost,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tag, name, category, cat_row["id"] if cat_row else None, department_id,
             "Available", bookable, cost, datetime.utcnow().strftime(DATE_FMT)),
        )
        db.commit()
        log_activity("Asset registered", f"{tag} - {name}")
        flash(f"Asset {tag} registered.", "success")
        return redirect(url_for("assets"))

    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")
    query = ("SELECT a.*, u.name AS holder_name, d.name AS department_name FROM assets a "
             "LEFT JOIN users u ON u.id=a.holder_id LEFT JOIN departments d ON d.id=a.department_id WHERE 1=1")
    params = []
    if q:
        query += " AND (a.tag LIKE ? OR a.name LIKE ? OR a.category LIKE ?)"
        params += [f"%{q}%"] * 3
    if status_filter:
        query += " AND a.status = ?"
        params.append(status_filter)
    query += " ORDER BY a.id DESC"
    rows = db.execute(query, params).fetchall()

    categories = db.execute("SELECT * FROM categories WHERE status='Active' ORDER BY name").fetchall()
    departments = db.execute("SELECT * FROM departments WHERE status='Active' ORDER BY name").fetchall()

    return render_template("assets.html", assets=rows, q=q, status_filter=status_filter,
                           categories=categories, departments=departments)


@app.route("/assets/<int:asset_id>")
@login_required
def asset_detail(asset_id):
    db = get_db()
    asset = db.execute(
        "SELECT a.*, u.name AS holder_name, d.name AS department_name FROM assets a "
        "LEFT JOIN users u ON u.id=a.holder_id LEFT JOIN departments d ON d.id=a.department_id WHERE a.id=?",
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

    maintenance_history = db.execute(
        """SELECT m.*, u.name AS raised_by_name FROM maintenance_requests m
           JOIN users u ON u.id = m.raised_by WHERE m.asset_id=? ORDER BY m.created_at DESC""",
        (asset_id,),
    ).fetchall()

    qr_rel_path = get_qr_path(asset["tag"])
    return render_template("asset_detail.html", asset=asset, history=history,
                           maintenance_history=maintenance_history, qr_rel_path=qr_rel_path)


@app.route("/assets/<int:asset_id>/return", methods=["POST"])
@roles_required("Admin", "AssetManager")
def return_asset(asset_id):
    db = get_db()
    asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not asset or asset["status"] != "Allocated":
        flash("Asset is not currently allocated.", "warning")
        return redirect(url_for("assets"))

    condition_note = request.form.get("condition_note", "").strip() or None
    now = datetime.utcnow().strftime(DATE_FMT)
    holder_id = asset["holder_id"]
    db.execute(
        "UPDATE allocations SET returned_at=?, condition_note=? WHERE asset_id=? AND returned_at IS NULL",
        (now, condition_note, asset_id),
    )
    db.execute("UPDATE assets SET status='Available', holder_id=NULL WHERE id=?", (asset_id,))
    db.commit()
    log_activity("Asset returned", f"{asset['tag']} returned")
    notify(holder_id, f"Return of {asset['tag']} - {asset['name']} confirmed.", "Allocation")
    flash("Asset marked as returned.", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


# --------------------------------------------------------------------------
# Allocation (with Trust Score feature) + Transfer Requests
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
                f"Use a Transfer Request instead of re-allocating directly.",
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
        log_activity("Asset allocated", f"{asset['tag']} -> user #{employee_id}")
        notify(employee_id, f"{asset['tag']} - {asset['name']} has been allocated to you.", "Allocation",
               link=url_for("asset_detail", asset_id=asset_id))
        flash("Asset allocated successfully.", "success")
        return redirect(url_for("assets"))

    available_assets = db.execute("SELECT * FROM assets WHERE status='Available' ORDER BY name").fetchall()
    employees = db.execute("SELECT * FROM users WHERE role != 'Admin' ORDER BY name").fetchall()
    employees_with_scores = [
        {"user": e, "score": compute_trust_score(db, e["id"])} for e in employees
    ]

    transfer_requests = db.execute(
        """SELECT t.*, a.tag AS asset_tag, a.name AS asset_name,
                  f.name AS from_name, to_u.name AS to_name
           FROM transfer_requests t
           JOIN assets a ON a.id = t.asset_id
           LEFT JOIN users f ON f.id = t.from_employee_id
           JOIN users to_u ON to_u.id = t.to_employee_id
           ORDER BY t.requested_at DESC"""
    ).fetchall()

    allocated_assets = db.execute(
        "SELECT a.*, u.name AS holder_name FROM assets a JOIN users u ON u.id=a.holder_id "
        "WHERE a.status='Allocated' ORDER BY a.name"
    ).fetchall()

    return render_template(
        "allocate.html", available_assets=available_assets, employees=employees_with_scores,
        transfer_requests=transfer_requests, allocated_assets=allocated_assets,
    )


@app.route("/transfer-requests", methods=["POST"])
@login_required
def create_transfer_request():
    db = get_db()
    user = current_user()
    asset_id = int(request.form["asset_id"])
    to_employee_id = int(request.form["to_employee_id"])
    reason = request.form.get("reason", "").strip()

    asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not asset or asset["status"] != "Allocated":
        flash("Asset is not currently allocated, so it can't be transferred.", "danger")
        return redirect(url_for("allocate"))

    now = datetime.utcnow().strftime(DATE_FMT)
    db.execute(
        "INSERT INTO transfer_requests (asset_id, from_employee_id, to_employee_id, reason, status, "
        "requested_by, requested_at) VALUES (?,?,?,?,?,?,?)",
        (asset_id, asset["holder_id"], to_employee_id, reason, "Requested", user["id"], now),
    )
    db.commit()
    log_activity("Transfer requested", f"{asset['tag']} -> user #{to_employee_id}")
    notify_managers(f"Transfer request raised for {asset['tag']} - {asset['name']}.", "Transfer",
                     link=url_for("allocate"))
    flash("Transfer request submitted for approval.", "success")
    return redirect(url_for("allocate"))


@app.route("/transfer-requests/<int:req_id>/<action>", methods=["POST"])
@roles_required("Admin", "AssetManager")
def decide_transfer_request(req_id, action):
    db = get_db()
    tr = db.execute("SELECT * FROM transfer_requests WHERE id=?", (req_id,)).fetchone()
    if not tr or tr["status"] != "Requested":
        flash("Transfer request not found or already decided.", "warning")
        return redirect(url_for("allocate"))

    now = datetime.utcnow().strftime(DATE_FMT)
    asset = db.execute("SELECT * FROM assets WHERE id=?", (tr["asset_id"],)).fetchone()

    if action == "approve":
        # Approved -> Reallocated: close old allocation, open new one
        db.execute(
            "UPDATE allocations SET returned_at=? WHERE asset_id=? AND returned_at IS NULL",
            (now, tr["asset_id"]),
        )
        db.execute(
            "INSERT INTO allocations (asset_id, employee_id, allocated_at) VALUES (?,?,?)",
            (tr["asset_id"], tr["to_employee_id"], now),
        )
        db.execute("UPDATE assets SET holder_id=? WHERE id=?", (tr["to_employee_id"], tr["asset_id"]))
        db.execute(
            "UPDATE transfer_requests SET status='Reallocated', decided_at=? WHERE id=?", (now, req_id)
        )
        db.commit()
        log_activity("Transfer approved", f"{asset['tag']} reallocated")
        notify(tr["to_employee_id"], f"{asset['tag']} - {asset['name']} has been transferred to you.",
               "Transfer", link=url_for("asset_detail", asset_id=asset["id"]))
        notify(tr["from_employee_id"], f"{asset['tag']} - {asset['name']} has been transferred away from you.",
               "Transfer")
        flash("Transfer approved and asset reallocated.", "success")
    elif action == "reject":
        db.execute("UPDATE transfer_requests SET status='Rejected', decided_at=? WHERE id=?", (now, req_id))
        db.commit()
        log_activity("Transfer rejected", f"{asset['tag']}")
        notify(tr["requested_by"], f"Transfer request for {asset['tag']} was rejected.", "Transfer")
        flash("Transfer request rejected.", "success")
    return redirect(url_for("allocate"))


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
        asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        log_activity("Resource booked", f"{asset['tag']} {start_time} - {end_time}")
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
    if booking["employee_id"] != user["id"] and user["role"] not in MANAGER_ROLES:
        flash("You can only cancel your own bookings.", "danger")
        return redirect(url_for("bookings"))
    db.execute("UPDATE bookings SET status='Cancelled' WHERE id=?", (booking_id,))
    db.commit()
    log_activity("Booking cancelled", f"booking #{booking_id}")
    flash("Booking cancelled.", "success")
    return redirect(url_for("bookings"))


# --------------------------------------------------------------------------
# Maintenance Management (Kanban board)
# --------------------------------------------------------------------------
MAINTENANCE_STAGES = ["Pending", "Approved", "Technician Assigned", "In Progress", "Resolved"]


@app.route("/maintenance", methods=["GET", "POST"])
@login_required
def maintenance():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        asset_id = int(request.form["asset_id"])
        issue_details = request.form["issue_details"].strip()
        priority = request.form.get("priority", "Medium")
        attachment_name = request.form.get("attachment_name", "").strip() or None
        now = datetime.utcnow().strftime(DATE_FMT)
        db.execute(
            "INSERT INTO maintenance_requests (asset_id, raised_by, issue_details, priority, "
            "attachment_name, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (asset_id, user["id"], issue_details, priority, attachment_name, "Pending", now, now),
        )
        db.commit()
        asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        log_activity("Maintenance requested", f"{asset['tag']} - {issue_details[:60]}")
        notify_managers(f"New maintenance request for {asset['tag']} - {asset['name']} ({priority}).",
                         "Maintenance", link=url_for("maintenance"))
        flash("Maintenance request raised.", "success")
        return redirect(url_for("maintenance"))

    rows = db.execute(
        """SELECT m.*, a.tag AS asset_tag, a.name AS asset_name, u.name AS raised_by_name
           FROM maintenance_requests m
           JOIN assets a ON a.id = m.asset_id
           JOIN users u ON u.id = m.raised_by
           ORDER BY m.created_at DESC"""
    ).fetchall()
    board = {stage: [] for stage in MAINTENANCE_STAGES}
    board["Rejected"] = []
    for r in rows:
        board.setdefault(r["status"], []).append(r)

    bookable_or_all_assets = db.execute("SELECT * FROM assets ORDER BY name").fetchall()
    return render_template("maintenance.html", board=board, stages=MAINTENANCE_STAGES,
                           assets=bookable_or_all_assets)


@app.route("/maintenance/<int:req_id>/advance", methods=["POST"])
@roles_required("Admin", "AssetManager")
def maintenance_advance(req_id):
    db = get_db()
    mr = db.execute("SELECT * FROM maintenance_requests WHERE id=?", (req_id,)).fetchone()
    if not mr:
        flash("Maintenance request not found.", "danger")
        return redirect(url_for("maintenance"))

    new_status = request.form["new_status"]
    technician_name = request.form.get("technician_name", "").strip() or mr["technician_name"]
    resolution_notes = request.form.get("resolution_notes", "").strip() or mr["resolution_notes"]
    now = datetime.utcnow().strftime(DATE_FMT)

    valid_targets = MAINTENANCE_STAGES + ["Rejected"]
    if new_status not in valid_targets:
        flash("Invalid status.", "danger")
        return redirect(url_for("maintenance"))

    db.execute(
        "UPDATE maintenance_requests SET status=?, technician_name=?, resolution_notes=?, updated_at=? "
        "WHERE id=?",
        (new_status, technician_name, resolution_notes, now, req_id),
    )

    asset = db.execute("SELECT * FROM assets WHERE id=?", (mr["asset_id"],)).fetchone()
    # Keep asset status in sync with the maintenance workflow
    if new_status in ("Approved", "Technician Assigned", "In Progress"):
        db.execute("UPDATE assets SET status='Under Maintenance' WHERE id=?", (mr["asset_id"],))
    elif new_status == "Resolved":
        if asset["status"] == "Under Maintenance":
            db.execute("UPDATE assets SET status='Available' WHERE id=?", (mr["asset_id"],))
    db.commit()

    log_activity("Maintenance updated", f"{asset['tag']} -> {new_status}")
    notify(mr["raised_by"], f"Your maintenance request for {asset['tag']} is now '{new_status}'.",
           "Maintenance", link=url_for("maintenance"))
    flash(f"Maintenance request moved to '{new_status}'.", "success")
    return redirect(url_for("maintenance"))


# --------------------------------------------------------------------------
# Asset Audit
# --------------------------------------------------------------------------
@app.route("/audit", methods=["GET", "POST"])
@roles_required("Admin", "AssetManager")
def audit():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        name = request.form["name"].strip()
        department_id = request.form.get("department_id") or None
        location = request.form.get("location", "").strip()
        start_date = request.form.get("start_date") or None
        end_date = request.form.get("end_date") or None
        auditor_id = request.form.get("auditor_id") or user["id"]
        now = datetime.utcnow().strftime(DATE_FMT)

        cur = db.execute(
            "INSERT INTO audit_cycles (name, department_id, location, start_date, end_date, "
            "auditor_id, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, department_id, location, start_date, end_date, auditor_id, "Open", now),
        )
        cycle_id = cur.lastrowid

        # Populate audit items with assets matching the department (or all, if none chosen)
        if department_id:
            asset_rows = db.execute("SELECT id FROM assets WHERE department_id=?", (department_id,)).fetchall()
        else:
            asset_rows = db.execute("SELECT id FROM assets").fetchall()
        for a in asset_rows:
            db.execute(
                "INSERT INTO audit_items (cycle_id, asset_id, verification_status) VALUES (?,?,?)",
                (cycle_id, a["id"], "Pending"),
            )
        db.commit()
        log_activity("Audit cycle created", name)
        notify(auditor_id, f"You've been assigned as auditor for '{name}'.", "Audit",
               link=url_for("audit_detail", cycle_id=cycle_id))
        flash(f"Audit cycle '{name}' created with {len(asset_rows)} assets.", "success")
        return redirect(url_for("audit_detail", cycle_id=cycle_id))

    cycles = db.execute(
        """SELECT c.*, d.name AS department_name, u.name AS auditor_name,
                  (SELECT COUNT(*) FROM audit_items i WHERE i.cycle_id=c.id) AS total_items,
                  (SELECT COUNT(*) FROM audit_items i WHERE i.cycle_id=c.id AND i.verification_status != 'Pending') AS done_items,
                  (SELECT COUNT(*) FROM audit_items i WHERE i.cycle_id=c.id AND i.verification_status IN ('Missing','Damaged')) AS discrepancies
           FROM audit_cycles c
           LEFT JOIN departments d ON d.id = c.department_id
           LEFT JOIN users u ON u.id = c.auditor_id
           ORDER BY c.id DESC"""
    ).fetchall()
    departments = db.execute("SELECT * FROM departments ORDER BY name").fetchall()
    auditors = db.execute("SELECT * FROM users WHERE role IN ('Admin','AssetManager','DepartmentHead') ORDER BY name").fetchall()
    return render_template("audit.html", cycles=cycles, departments=departments, auditors=auditors)


@app.route("/audit/<int:cycle_id>", methods=["GET"])
@roles_required("Admin", "AssetManager")
def audit_detail(cycle_id):
    db = get_db()
    cycle = db.execute(
        """SELECT c.*, d.name AS department_name, u.name AS auditor_name FROM audit_cycles c
           LEFT JOIN departments d ON d.id=c.department_id
           LEFT JOIN users u ON u.id=c.auditor_id WHERE c.id=?""",
        (cycle_id,),
    ).fetchone()
    if not cycle:
        flash("Audit cycle not found.", "danger")
        return redirect(url_for("audit"))

    items = db.execute(
        """SELECT i.*, a.tag AS asset_tag, a.name AS asset_name, a.category AS asset_category
           FROM audit_items i JOIN assets a ON a.id = i.asset_id WHERE i.cycle_id=? ORDER BY a.tag""",
        (cycle_id,),
    ).fetchall()
    discrepancies = [i for i in items if i["verification_status"] in ("Missing", "Damaged")]
    return render_template("audit_detail.html", cycle=cycle, items=items, discrepancies=discrepancies)


@app.route("/audit/<int:cycle_id>/verify/<int:item_id>", methods=["POST"])
@roles_required("Admin", "AssetManager")
def audit_verify_item(cycle_id, item_id):
    db = get_db()
    cycle = db.execute("SELECT * FROM audit_cycles WHERE id=?", (cycle_id,)).fetchone()
    if not cycle or cycle["status"] == "Locked":
        flash("This audit cycle is locked and can no longer be edited.", "warning")
        return redirect(url_for("audit_detail", cycle_id=cycle_id))

    verification_status = request.form["verification_status"]
    notes = request.form.get("notes", "").strip() or None
    now = datetime.utcnow().strftime(DATE_FMT)
    db.execute(
        "UPDATE audit_items SET verification_status=?, notes=?, verified_at=? WHERE id=?",
        (verification_status, notes, now, item_id),
    )
    db.commit()

    if verification_status in ("Missing", "Damaged"):
        item = db.execute(
            "SELECT i.*, a.tag AS asset_tag, a.name AS asset_name FROM audit_items i "
            "JOIN assets a ON a.id=i.asset_id WHERE i.id=?", (item_id,)
        ).fetchone()
        if verification_status == "Damaged":
            db.execute("UPDATE assets SET status='Under Maintenance' WHERE id=?", (item["asset_id"],))
            db.commit()
        elif verification_status == "Missing":
            db.execute("UPDATE assets SET status='Lost' WHERE id=?", (item["asset_id"],))
            db.commit()
        notify_managers(
            f"Audit discrepancy: {item['asset_tag']} - {item['asset_name']} flagged as {verification_status}.",
            "Audit", link=url_for("audit_detail", cycle_id=cycle_id),
        )
        log_activity("Audit discrepancy flagged", f"{item['asset_tag']} - {verification_status}")

    flash("Asset verification recorded.", "success")
    return redirect(url_for("audit_detail", cycle_id=cycle_id))


@app.route("/audit/<int:cycle_id>/close", methods=["POST"])
@roles_required("Admin", "AssetManager")
def audit_close(cycle_id):
    db = get_db()
    cycle = db.execute("SELECT * FROM audit_cycles WHERE id=?", (cycle_id,)).fetchone()
    if not cycle:
        flash("Audit cycle not found.", "danger")
        return redirect(url_for("audit"))
    db.execute("UPDATE audit_cycles SET status='Locked' WHERE id=?", (cycle_id,))
    db.commit()
    log_activity("Audit cycle closed", cycle["name"])
    flash(f"Audit cycle '{cycle['name']}' closed and locked.", "success")
    return redirect(url_for("audit_detail", cycle_id=cycle_id))


# --------------------------------------------------------------------------
# Reports & Analytics
# --------------------------------------------------------------------------
@app.route("/reports")
@roles_required("Admin", "AssetManager")
def reports():
    db = get_db()
    refresh_booking_statuses(db)

    utilization_by_dept = db.execute(
        """SELECT COALESCE(d.name,'Unassigned') AS department, COUNT(*) AS total,
                  SUM(CASE WHEN a.status='Allocated' THEN 1 ELSE 0 END) AS allocated
           FROM assets a LEFT JOIN departments d ON d.id=a.department_id
           GROUP BY department"""
    ).fetchall()

    maintenance_by_month = db.execute(
        """SELECT substr(created_at,1,7) AS month, COUNT(*) AS c
           FROM maintenance_requests GROUP BY month ORDER BY month"""
    ).fetchall()

    most_used = db.execute(
        """SELECT a.tag, a.name, COUNT(b.id) AS booking_count
           FROM assets a JOIN bookings b ON b.asset_id=a.id
           GROUP BY a.id ORDER BY booking_count DESC LIMIT 5"""
    ).fetchall()

    idle_assets = db.execute(
        """SELECT a.tag, a.name, a.status,
                  CAST(julianday('now') - julianday(a.created_at) AS INT) AS days_since_registered
           FROM assets a WHERE a.status='Available'
           ORDER BY a.created_at ASC LIMIT 5"""
    ).fetchall()

    dept_allocation = utilization_by_dept

    upcoming_maintenance = db.execute(
        """SELECT m.*, a.tag AS asset_tag, a.name AS asset_name FROM maintenance_requests m
           JOIN assets a ON a.id=m.asset_id WHERE m.status NOT IN ('Resolved','Rejected')
           ORDER BY m.created_at ASC LIMIT 10"""
    ).fetchall()

    booking_heatmap_rows = db.execute(
        "SELECT start_time FROM bookings WHERE status != 'Cancelled'"
    ).fetchall()
    heatmap = {}
    for r in booking_heatmap_rows:
        dt = parse_dt(r["start_time"])
        if dt:
            key = f"{dt.strftime('%a')}-{dt.hour}"
            heatmap[key] = heatmap.get(key, 0) + 1

    return render_template(
        "reports.html",
        utilization_by_dept=utilization_by_dept,
        maintenance_by_month=maintenance_by_month,
        most_used=most_used,
        idle_assets=idle_assets,
        dept_allocation=dept_allocation,
        upcoming_maintenance=upcoming_maintenance,
        heatmap=json.dumps(heatmap),
    )


@app.route("/reports/export")
@roles_required("Admin", "AssetManager")
def reports_export():
    db = get_db()
    rows = db.execute(
        "SELECT a.tag, a.name, a.category, a.status, u.name AS holder, d.name AS department "
        "FROM assets a LEFT JOIN users u ON u.id=a.holder_id LEFT JOIN departments d ON d.id=a.department_id "
        "ORDER BY a.tag"
    ).fetchall()
    lines = ["Tag,Name,Category,Status,Holder,Department"]
    for r in rows:
        lines.append(
            f'{r["tag"]},{r["name"]},{r["category"]},{r["status"]},{r["holder"] or ""},{r["department"] or ""}'
        )
    csv_data = "\n".join(lines)
    log_activity("Report exported", "Asset registry CSV")
    return Response(
        csv_data, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=assetflow_report.csv"},
    )


# --------------------------------------------------------------------------
# Activity Logs & Notifications
# --------------------------------------------------------------------------
@app.route("/notifications")
@login_required
def notifications():
    db = get_db()
    user = current_user()
    filter_cat = request.args.get("category", "All")

    notif_query = "SELECT * FROM notifications WHERE user_id=?"
    params = [user["id"]]
    if filter_cat != "All":
        notif_query += " AND category=?"
        params.append(filter_cat)
    notif_query += " ORDER BY id DESC LIMIT 100"
    my_notifications = db.execute(notif_query, params).fetchall()

    activity_logs = []
    if user["role"] in MANAGER_ROLES:
        activity_logs = db.execute(
            """SELECT al.*, u.name AS user_name FROM activity_logs al
               LEFT JOIN users u ON u.id=al.user_id ORDER BY al.id DESC LIMIT 100"""
        ).fetchall()

    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
    db.commit()

    categories = ["All", "Allocation", "Transfer", "Maintenance", "Audit", "Admin", "General"]
    return render_template("notifications.html", notifications=my_notifications,
                           activity_logs=activity_logs, categories=categories, filter_cat=filter_cat)


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
