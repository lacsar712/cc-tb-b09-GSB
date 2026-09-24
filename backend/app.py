import os
from functools import wraps

import psycopg2
from flask import Flask, flash, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

import roster
from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}

ACTION_LABELS = {"add": "加入", "remove": "移出", "daycut": "日切"}


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
        if session.get("role") != "writer":
            return ("仅审评员可操作轮值名单", 403)
        return fn(*args, **kwargs)

    return wrap


@app.before_request
def auto_daycut():
    """跨自然日访问时，自动把昨日的明日预排提升为今日生效。"""
    if request.endpoint in ("health", "static"):
        return
    try:
        with db() as conn, conn.cursor() as cur:
            roster.sync_roster(cur)
    except psycopg2.OperationalError:
        # 数据库尚未就绪时不拦健康检查之外的请求，具体页面仍会暴露连接错误
        return


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
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
    return render_template(
        "home.html",
        rows=rows,
        can_write=session.get("role") == "writer",
        on_roster=_is_on_today(session["user"]),
    )


def _is_on_today(user):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM roster_entries WHERE book = %s AND name = %s",
            (roster.BOOK_TODAY, user),
        )
        return cur.fetchone() is not None


@app.post("/cuppings")
@login_required
def create():
    if session.get("role") != "writer":
        return ("仅审评员可提交拼配审评", 403)
    if not _is_on_today(session["user"]):
        return ("你不在今日生效簿内，本轮交评被拒绝", 403)
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
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
    books = {}
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        for book in roster.BOOKS:
            cur.execute(
                "SELECT name, added_by, created_at FROM roster_entries WHERE book = %s ORDER BY id",
                (book,),
            )
            books[book] = cur.fetchall()
        cur.execute(
            """SELECT id, actor, action, book, target, detail, created_at
               FROM roster_audit ORDER BY id DESC LIMIT 100"""
        )
        audits = cur.fetchall()
        cur.execute("SELECT roster_day, CURRENT_DATE AS today FROM roster_meta WHERE id = 1")
        meta = cur.fetchone()
    return render_template(
        "roster.html",
        books=books,
        audits=audits,
        meta=meta,
        labels=roster.BOOK_LABELS,
        action_labels=ACTION_LABELS,
        can_write=session.get("role") == "writer",
    )


@app.post("/roster/add")
@login_required
@writer_required
def roster_add():
    book = request.form.get("book", "")
    if book not in roster.BOOKS:
        return ("未知的簿名", 400)
    names, paste_dups = roster.parse_names(request.form.get("names", ""))
    if not names:
        flash("未识别到任何人名")
        return redirect(url_for("roster_page"))
    with db() as conn, conn.cursor() as cur:
        added, book_dups = roster.add_names(cur, book, names, session["user"])
        conn.commit()
    parts = [f"已加入{roster.BOOK_LABELS[book]}：{'、'.join(added)}" if added else "没有新名字被加入"]
    skipped = book_dups + paste_dups
    if skipped:
        parts.append(f"重名跳过：{'、'.join(skipped)}")
    flash("；".join(parts))
    return redirect(url_for("roster_page"))


@app.post("/roster/remove")
@login_required
@writer_required
def roster_remove():
    book = request.form.get("book", "")
    name = request.form.get("name", "").strip()
    if book not in roster.BOOKS:
        return ("未知的簿名", 400)
    with db() as conn, conn.cursor() as cur:
        existed = roster.remove_name(cur, book, name, session["user"])
        conn.commit()
    if existed:
        flash(f"已将 {name} 移出{roster.BOOK_LABELS[book]}")
    else:
        flash(f"{name} 本就不在{roster.BOOK_LABELS[book]}内")
    return redirect(url_for("roster_page"))


@app.post("/roster/daycut")
@login_required
@writer_required
def roster_daycut():
    with db() as conn, conn.cursor() as cur:
        roster.sync_roster(cur, force=True, actor=session["user"])
        conn.commit()
    flash("已执行日切：明日预排已提升为今日生效，明日预排已清空")
    return redirect(url_for("roster_page"))
