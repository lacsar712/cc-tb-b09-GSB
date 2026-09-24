import os
import re
from datetime import date
from functools import wraps

import psycopg2
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}

BOOKS = {"today": "今日生效簿", "tomorrow": "明日预排簿"}
NAME_SPLIT = re.compile(r"[,\s，、;；]+")


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


def writer_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "writer":
            return ("仅审评员可排班", 403)
        return fn(*args, **kwargs)

    return wrap


def audit(cur, actor, action, book=None, name=None, detail=None):
    cur.execute(
        """INSERT INTO roster_audit (actor, action, book, name, detail)
           VALUES (%s,%s,%s,%s,%s)""",
        (actor, action, book, name, detail),
    )


def ensure_rollover(cur):
    """跨自然日：把昨日的明日预排提升为新的今日生效，清空明日预排，写一条日切履历。"""
    cur.execute("SELECT active_date FROM roster_meta WHERE id = 1 FOR UPDATE")
    row = cur.fetchone()
    active_date = row["active_date"] if row else None
    today = date.today()
    if active_date is not None and active_date >= today:
        return False
    cur.execute("SELECT name FROM roster WHERE book = 'tomorrow' ORDER BY name")
    promoted = [r["name"] for r in cur.fetchall()]
    cur.execute("DELETE FROM roster WHERE book = 'today'")
    cur.execute("UPDATE roster SET book = 'today' WHERE book = 'tomorrow'")
    cur.execute(
        """INSERT INTO roster_meta (id, active_date) VALUES (1, %s)
           ON CONFLICT (id) DO UPDATE SET active_date = EXCLUDED.active_date""",
        (today,),
    )
    detail = "明日预排提升为今日生效：" + ("、".join(promoted) if promoted else "（空）")
    audit(cur, "system", "rollover", detail=detail)
    return True


def do_rollover(cur):
    """立即执行一次日切提升（与跨自然日同流程）。"""
    cur.execute("SELECT name FROM roster WHERE book = 'tomorrow' ORDER BY name")
    promoted = [r["name"] for r in cur.fetchall()]
    cur.execute("DELETE FROM roster WHERE book = 'today'")
    cur.execute("UPDATE roster SET book = 'today' WHERE book = 'tomorrow'")
    today = date.today()
    cur.execute(
        """INSERT INTO roster_meta (id, active_date) VALUES (1, %s)
           ON CONFLICT (id) DO UPDATE SET active_date = EXCLUDED.active_date""",
        (today,),
    )
    detail = "明日预排提升为今日生效：" + ("、".join(promoted) if promoted else "（空）")
    audit(cur, "system", "rollover", detail=detail)
    return promoted


def parse_names(raw):
    # 保留同批次内的重复项，交给入库前去重并统一提示"重名跳过"
    return [p.strip() for p in NAME_SPLIT.split(raw or "") if p.strip()]


@app.get("/health")
def health():
    return {"status": "ok", "service": "tea-blend-cupping"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        account = ACCOUNTS.get(name)
        if not account or account["password"] != request.form.get("password", ""):
            error = "用户名或密码错误"
        else:
            session["user"] = name
            session["role"] = account["role"]
            return redirect(url_for("home"))
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    can_write = session.get("role") == "writer"
    on_duty = False
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        ensure_rollover(cur)
        if can_write:
            cur.execute(
                "SELECT 1 FROM roster WHERE book = 'today' AND name = %s",
                (session["user"],),
            )
            on_duty = cur.fetchone() is not None
        cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
        conn.commit()
    return render_template(
        "home.html", rows=rows, can_write=can_write, on_duty=on_duty
    )


@app.post("/cuppings")
@login_required
def create():
    if session.get("role") != "writer":
        return ("仅审评员可提交拼配审评", 403)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        ensure_rollover(cur)
        cur.execute(
            "SELECT 1 FROM roster WHERE book = 'today' AND name = %s",
            (session["user"],),
        )
        if cur.fetchone() is None:
            conn.commit()
            msg = "你不在今日生效簿内，本轮交评被拒绝"
            if request.headers.get("HX-Request"):
                return (msg, 403)
            flash(msg, "error")
            return redirect(url_for("home"))
        aroma = float(request.form["aroma"])
        taste = float(request.form["taste"])
        liquor = float(request.form["liquor"])
        lot = request.form["lot"].strip()
        verdict, note, score = weigh(aroma, taste, liquor)
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"]),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


@app.get("/roster")
@login_required
def roster_page():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        rolled = ensure_rollover(cur)
        if rolled:
            conn.commit()
        cur.execute("SELECT active_date FROM roster_meta WHERE id = 1")
        active_date = cur.fetchone()["active_date"]
        cur.execute("SELECT * FROM roster WHERE book = 'today' ORDER BY name")
        today_roster = cur.fetchall()
        cur.execute("SELECT * FROM roster WHERE book = 'tomorrow' ORDER BY name")
        tomorrow_roster = cur.fetchall()
        cur.execute("SELECT * FROM roster_audit ORDER BY id DESC LIMIT 100")
        audits = cur.fetchall()
        conn.commit()
    today_names = {r["name"] for r in today_roster}
    return render_template(
        "roster.html",
        today_roster=today_roster,
        tomorrow_roster=tomorrow_roster,
        audits=audits,
        active_date=active_date,
        book_labels=BOOKS,
        can_write=session.get("role") == "writer",
        on_duty=session.get("user") in today_names,
    )


@app.post("/roster/add")
@writer_required
def roster_add():
    book = request.form.get("book", "")
    if book not in BOOKS:
        flash("未知簿名", "error")
        return redirect(url_for("roster_page"))
    names = parse_names(request.form.get("names", ""))
    if not names:
        flash("没有解析到任何人名", "error")
        return redirect(url_for("roster_page"))
    added, skipped = [], []
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        ensure_rollover(cur)
        for name in names:
            cur.execute(
                "SELECT 1 FROM roster WHERE book = %s AND name = %s",
                (book, name),
            )
            if cur.fetchone() is not None:
                skipped.append(name)
                continue
            cur.execute(
                "INSERT INTO roster (book, name, added_by) VALUES (%s,%s,%s)",
                (book, name, session["user"]),
            )
            audit(
                cur,
                session["user"],
                "add",
                book=book,
                name=name,
                detail=f"加入{BOOKS[book]}",
            )
            added.append(name)
        conn.commit()
    if added:
        flash(f"已加入{BOOKS[book]}：{'、'.join(added)}", "ok")
    if skipped:
        unique_skipped = list(dict.fromkeys(skipped))
        flash(f"重名跳过：{'、'.join(unique_skipped)}", "error")
    return redirect(url_for("roster_page"))


@app.post("/roster/remove")
@writer_required
def roster_remove():
    book = request.form.get("book", "")
    name = request.form.get("name", "").strip()
    if book not in BOOKS or not name:
        flash("参数不完整", "error")
        return redirect(url_for("roster_page"))
    removed = False
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        ensure_rollover(cur)
        cur.execute(
            "DELETE FROM roster WHERE book = %s AND name = %s",
            (book, name),
        )
        removed = cur.rowcount > 0
        if removed:
            audit(
                cur,
                session["user"],
                "remove",
                book=book,
                name=name,
                detail=f"移出{BOOKS[book]}",
            )
        conn.commit()
    if removed:
        flash(f"已将 {name} 移出{BOOKS[book]}", "ok")
    else:
        flash(f"{name} 不在{BOOKS[book]}中", "error")
    return redirect(url_for("roster_page"))


@app.post("/roster/rollover")
@writer_required
def roster_rollover():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        promoted = do_rollover(cur)
        conn.commit()
    flash(
        "日切完成，新今日生效簿：" + ("、".join(promoted) if promoted else "（空）"),
        "ok",
    )
    return redirect(url_for("roster_page"))
