from flask import Flask, request, render_template, redirect, send_file, session, url_for
import io
import logging
import os
from datetime import date, timedelta

import pandas as pd
import psycopg2
import requests
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO)

# ---------------- DATABASE ----------------
DATABASE_URL = os.environ.get("DATABASE_URL")
DEFAULT_ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@gmail.com").strip().lower()
DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin@123")


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. Check your .env file.")

    try:
        return psycopg2.connect(DATABASE_URL)
    except psycopg2.OperationalError as exc:
        raise RuntimeError(
            "Database connection failed. Check DATABASE_URL, PostgreSQL host/port, username, and password."
        ) from exc


def create_tables():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS visitors (
            id SERIAL PRIMARY KEY,
            student_name TEXT,
            student_number TEXT,
            course_name TEXT,
            parent_name TEXT,
            parent_contact TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    cur.execute(
        """
        ALTER TABLE visitors
        ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    conn.commit()
    cur.close()
    conn.close()


def seed_default_admin():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM admins WHERE email = %s", (DEFAULT_ADMIN_EMAIL,))
    admin = cur.fetchone()

    if not admin:
        cur.execute(
            """
            INSERT INTO admins (email, password_hash)
            VALUES (%s, %s)
            """,
            (DEFAULT_ADMIN_EMAIL, generate_password_hash(DEFAULT_ADMIN_PASSWORD)),
        )
        conn.commit()
        logging.info("Default admin created: %s", DEFAULT_ADMIN_EMAIL)

    cur.close()
    conn.close()


create_tables()
seed_default_admin()
print("Connected successfully")


# ---------------- SAVE TO DB ----------------
def save_to_db(data):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO visitors
        (student_name, student_number, course_name, parent_name, parent_contact)
        VALUES (%s, %s, %s, %s, %s)
        """,
        data,
    )

    conn.commit()
    cur.close()
    conn.close()


# ---------------- CHECK DUPLICATE ----------------
def is_duplicate(phone):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT 1 FROM visitors WHERE student_number = %s", (phone,))
    result = cur.fetchone()

    cur.close()
    conn.close()

    return result is not None


# ---------------- GET TOTAL ----------------
def get_total():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM visitors")
    total = cur.fetchone()[0]

    cur.close()
    conn.close()
    return total

def get_course_stats():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT course_name, COUNT(*) FROM visitors GROUP BY course_name")
    stats = cur.fetchall()

    cur.close()
    conn.close()
    return stats


def get_gauge_stats():
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT COUNT(*) FROM visitors 
        WHERE EXTRACT(MONTH FROM created_at) = EXTRACT(MONTH FROM CURRENT_DATE)
        AND EXTRACT(YEAR FROM created_at) = EXTRACT(YEAR FROM CURRENT_DATE)
    """)
    monthly_visitors = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM visitors 
        WHERE DATE(created_at) = CURRENT_DATE
    """)
    today_visitors = cur.fetchone()[0]

    cur.close()
    conn.close()
    
    return {
        "monthly": monthly_visitors,
        "today": today_visitors,
        "target": 100
    }

def get_weekly_trend():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DATE(created_at), COUNT(*) 
        FROM visitors 
        WHERE created_at >= CURRENT_DATE - INTERVAL '6 days'
        GROUP BY DATE(created_at)
        ORDER BY DATE(created_at) ASC
    """)
    db_rows = cur.fetchall()
    cur.close()
    conn.close()

    db_dict = {}
    for row in db_rows:
        try:
            key = row[0].strftime('%Y-%m-%d') if hasattr(row[0], 'strftime') else str(row[0])
            db_dict[key] = row[1]
        except Exception:
            pass
            
    today_dt = date.today()
    labels = []
    data = []
    
    for i in range(6, -1, -1):
        d = today_dt - timedelta(days=i)
        labels.append(d.strftime("%a"))
        data.append(db_dict.get(d.strftime('%Y-%m-%d'), 0))
        
    return {"labels": labels, "data": data}


# ---------------- GET ALL VISITORS ----------------
def get_all_visitors(filter_type=None):
    conn = get_connection()
    cur = conn.cursor()

    if filter_type == "today":
        cur.execute(
            """
            SELECT * FROM visitors
            WHERE DATE(created_at) = CURRENT_DATE
            ORDER BY id DESC
            """
        )
    elif filter_type == "week":
        cur.execute(
            """
            SELECT * FROM visitors
            WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY id DESC
            """
        )
    else:
        cur.execute("SELECT * FROM visitors ORDER BY id DESC")

    rows = cur.fetchall()
    headers = ["ID", "Student Name", "Phone", "Course", "Parent", "Parent Contact", "Date Added"]

    cur.close()
    conn.close()

    return headers, rows


def verify_admin_login(email, password):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT id, email, password_hash FROM admins WHERE email = %s",
        (email.strip().lower(),),
    )
    admin = cur.fetchone()

    cur.close()
    conn.close()

    if not admin:
        return None

    admin_id, admin_email, password_hash = admin

    if check_password_hash(password_hash, password):
        return {"id": admin_id, "email": admin_email}

    return None


# ---------------- API CONFIG ----------------
INSTANCE_KEY = os.environ.get("ULTRAMSG_INSTANCE_ID", "instance143653")
TOKEN = os.environ.get("ULTRAMSG_TOKEN")
API_URL = f"https://api.ultramsg.com/{INSTANCE_KEY}/messages/chat"

# ---------------- ADMIN SESSION ----------------
app.secret_key = "super_secret_key_123"


# ---------------- WHATSAPP FUNCTION ----------------
def send_whatsapp_message(phone_number, message):
    if not TOKEN:
        return {"error": "Token not configured"}

    if not phone_number.startswith("+91"):
        phone_number = "+91" + phone_number.lstrip("0")

    payload = {"to": phone_number, "body": message}

    try:
        response = requests.post(f"{API_URL}?token={TOKEN}", json=payload, timeout=10)
        return response.json()
    except Exception as exc:
        return {"error": str(exc)}


# ---------------- HOME ----------------
@app.route("/")
def home():
    return render_template("home.html")


# ---------------- VISITOR FORM ----------------
@app.route("/visitor_form")
def visitor_form():
    return render_template("index.html")


# ---------------- SEND MESSAGE ----------------
@app.route("/send_message", methods=["POST"])
def send_message():
    student_name = request.form.get("student_name", "").strip()
    student_number = request.form.get("student_number", "").strip()
    course_name = request.form.get("course_name", "").strip()
    parent_name = request.form.get("parent_name", "").strip()
    parent_contact = request.form.get("parent_contact", "").strip()

    if not all([student_name, student_number, course_name, parent_name, parent_contact]):
        return "All fields are required."

    if not student_number.isdigit() or len(student_number) != 10:
        return "Invalid phone number. Must be 10 digits."

    if is_duplicate(student_number):
        return "This phone number is already registered."

    save_to_db([student_name, student_number, course_name, parent_name, parent_contact])
    logging.info("New visitor added: %s", student_name)

    message = f"""
Hello {student_name},

Welcome to Vikrant Group Of Institutions, Indore.

Thank you for visiting our campus.

Courses: Engineering, Management, Nursing, Pharmacy, Law

Scholarship:
https://www.vitm.edu.in/scholarship.html

Instagram:
https://www.instagram.com/vikrant.indore

Thanks
"""

    result = send_whatsapp_message(student_number, message)

    if "error" in result:
        return f"Saved but message failed: {result['error']}"

    return redirect("/visitor_form?success=1")


# ---------------- BULK MESSAGE ----------------
@app.route("/bulk_message", methods=["GET", "POST"])
def bulk_message():
    if not session.get("admin"):
        return redirect("/login")

    if request.method == "POST":
        message = request.form.get("message")
        manual_input = request.form.get("manual_numbers", "")
        numbers = []

        if manual_input.strip():
            for num in manual_input.replace(",", "\n").split("\n"):
                if num.strip():
                    numbers.append(num.strip())

        if not numbers:
            return "No numbers found."

        failed = []

        for number in numbers:
            result = send_whatsapp_message(number, message)
            if "error" in result:
                failed.append(number)

        if failed:
            return f"Failed for: {', '.join(failed)}"

        return "Bulk messages sent successfully!"

    return render_template("bulk_message.html")


# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():
    if not session.get("admin"):
        return redirect("/login")

    total = get_total()
    course_stats = get_course_stats()
    gauge_stats = get_gauge_stats()
    weekly_trend = get_weekly_trend()
    return render_template("dashboard.html", 
                           total=total, 
                           course_stats=course_stats,
                           gauge_stats=gauge_stats,
                           weekly_trend=weekly_trend)


# ---------------- VIEW VISITORS ----------------
@app.route("/view_visitors")
def view_visitors():
    if not session.get("admin"):
        return redirect("/login")

    filter_type = request.args.get("filter")
    headers, rows = get_all_visitors(filter_type)
    return render_template("view.html", headers=headers, rows=rows)


# ---------------- DOWNLOAD FILE ----------------
@app.route("/download")
def download():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM visitors ORDER BY id DESC")
    rows = cur.fetchall()

    headers = ["ID", "Student Name", "Phone", "Course", "Parent", "Parent Contact", "Date Added"]

    cur.close()
    conn.close()

    df = pd.DataFrame(rows, columns=headers)
    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        download_name="visitors.xlsx",
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------- DELETE VISITOR FUNCTION ----------------
@app.route("/delete/<int:id>")
def delete_visitor(id):
    if not session.get("admin"):
        return redirect("/login")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM visitors WHERE id = %s", (id,))

    conn.commit()
    cur.close()
    conn.close()

    return redirect("/view_visitors")


# ---------------- EDIT VISITOR ----------------
@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit_visitor(id):
    if not session.get("admin"):
        return redirect("/login")

    conn = get_connection()
    cur = conn.cursor()

    if request.method == "POST":
        student_name = request.form.get("student_name")
        student_number = request.form.get("student_number")
        course_name = request.form.get("course_name")
        parent_name = request.form.get("parent_name")
        parent_contact = request.form.get("parent_contact")

        cur.execute(
            """
            UPDATE visitors
            SET student_name = %s, student_number = %s, course_name = %s,
                parent_name = %s, parent_contact = %s
            WHERE id = %s
            """,
            (student_name, student_number, course_name, parent_name, parent_contact, id),
        )

        conn.commit()
        cur.close()
        conn.close()

        return redirect("/view_visitors")

    cur.execute("SELECT * FROM visitors WHERE id = %s", (id,))
    visitor = cur.fetchone()

    cur.close()
    conn.close()

    return render_template("edit.html", visitor=visitor)


# ---------------- LOGIN METHOD ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        admin = verify_admin_login(email, password)

        if admin:
            session["admin"] = True
            session["admin_id"] = admin["id"]
            session["admin_email"] = admin["email"]
            return redirect("/dashboard")

        return "Invalid email or password"

    return render_template("login.html")


# ---------------- LOGOUT CONFIG ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ---------------- ERROR ----------------
@app.errorhandler(404)
def not_found(e):
    return "<h1>Page Not Found</h1>", 404


# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
