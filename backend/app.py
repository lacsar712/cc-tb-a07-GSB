import os
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
            return ("仅审评员可操作配方草案与交评", 403)
        return fn(*args, **kwargs)

    return wrap


CUP_COLUMNS = """c.*, rv.version_no AS recipe_version_no
                FROM cuppings c
                LEFT JOIN recipe_versions rv ON rv.id = c.recipe_version_id"""


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
        cur.execute(f"SELECT {CUP_COLUMNS} ORDER BY c.id DESC")
        rows = cur.fetchall()
        cur.execute("SELECT id, version_no FROM recipe_versions ORDER BY version_no")
        versions = cur.fetchall()
    return render_template(
        "home.html",
        rows=rows,
        versions=versions,
        can_write=session.get("role") == "writer",
    )


@app.post("/cuppings")
@writer_required
def create():
    # 交评必须挑选一个已生效版本号，否则拒交
    raw_version = request.form.get("recipe_version_id", "").strip()
    if not raw_version:
        return ("交评必须选择一个已生效配方版本", 400)
    try:
        recipe_version_id = int(raw_version)
    except ValueError:
        return ("交评必须选择一个已生效配方版本", 400)

    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)

    conn = db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT 1 FROM recipe_versions WHERE id = %s", (recipe_version_id,))
            if cur.fetchone() is None:
                return ("交评必须选择一个已生效配方版本", 400)
            cur.execute(
                """INSERT INTO cuppings
                       (lot, aroma, taste, liquor, score, verdict, note,
                        created_by, recipe_version_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (lot, aroma, taste, liquor, score, verdict, note,
                 session["user"], recipe_version_id),
            )
            new_id = cur.fetchone()["id"]
            cur.execute(
                f"SELECT {CUP_COLUMNS} WHERE c.id = %s", (new_id,)
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# 拼配配方：草案 -> 生效只读版本
# ---------------------------------------------------------------------------


@app.get("/recipes")
@login_required
def recipes_page():
    can_write = session.get("role") == "writer"
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT v.id, v.version_no, v.name, v.activated_by, v.activated_at,
                      (SELECT count(*) FROM recipe_version_leaves l
                        WHERE l.version_id = v.id) AS leaf_count
               FROM recipe_versions v
               ORDER BY v.version_no"""
        )
        versions = cur.fetchall()

        draft = None
        leaves = []
        if can_write:
            cur.execute(
                """SELECT * FROM recipe_drafts
                   WHERE status = 'open'
                   ORDER BY id DESC LIMIT 1"""
            )
            draft = cur.fetchone()
            cur.execute("SELECT id, name, note FROM leaves ORDER BY name")
            leaves = cur.fetchall()
            if draft:
                cur.execute(
                    "SELECT leaf_id FROM recipe_draft_leaves WHERE draft_id = %s",
                    (draft["id"],),
                )
                selected = {r["leaf_id"] for r in cur.fetchall()}
                for leaf in leaves:
                    leaf["selected"] = leaf["id"] in selected
    return render_template(
        "recipes.html", versions=versions, draft=draft, leaves=leaves, can_write=can_write
    )


@app.post("/recipes/drafts")
@writer_required
def create_draft():
    conn = db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM recipe_drafts WHERE status = 'open' LIMIT 1")
            if cur.fetchone() is not None:
                flash("已有一份进行中的草案，请先改完并生效。")
                return redirect(url_for("recipes_page"))
            cur.execute(
                "INSERT INTO recipe_drafts (name, created_by) VALUES (%s, %s) RETURNING id",
                (request.form.get("name", "拼配配方").strip() or "拼配配方", session["user"]),
            )
            draft_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("recipes_page") + f"#draft-{draft_id}")


def _save_draft_leaves(cur, draft_id, leaf_ids):
    cur.execute("DELETE FROM recipe_draft_leaves WHERE draft_id = %s", (draft_id,))
    for position, leaf_id in enumerate(leaf_ids):
        cur.execute(
            """INSERT INTO recipe_draft_leaves (draft_id, leaf_id, position)
               VALUES (%s, %s, %s)""",
            (draft_id, leaf_id, position),
        )


@app.post("/recipes/drafts/<int:draft_id>")
@writer_required
def save_or_activate_draft(draft_id):
    op = request.form.get("op", "save")
    name = (request.form.get("name", "").strip() or "拼配配方")
    try:
        leaf_ids = [int(x) for x in request.form.getlist("leaf_ids")]
    except ValueError:
        return ("母叶选择不合法", 400)

    conn = db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM recipe_drafts WHERE id = %s AND status = 'open' FOR UPDATE",
                (draft_id,),
            )
            draft = cur.fetchone()
            if draft is None:
                return ("草案不存在或已生效，不能再改", 404)

            if op == "activate":
                if not leaf_ids:
                    flash("生效前至少要挂一味母叶。")
                    return redirect(url_for("recipes_page") + f"#draft-{draft_id}")
                # 先以本次提交清单落草案（所见即所得），再据此快照出版本正文
                _save_draft_leaves(cur, draft_id, leaf_ids)
                cur.execute(
                    """INSERT INTO recipe_versions (name, activated_by)
                       VALUES (%s, %s) RETURNING id, version_no""",
                    (name, session["user"]),
                )
                created = cur.fetchone()
                cur.execute(
                    """INSERT INTO recipe_version_leaves
                           (version_id, leaf_id, leaf_name, position)
                       SELECT %s, dl.leaf_id, l.name, dl.position
                       FROM recipe_draft_leaves dl
                       JOIN leaves l ON l.id = dl.leaf_id
                       WHERE dl.draft_id = %s""",
                    (created["id"], draft_id),
                )
                cur.execute(
                    """UPDATE recipe_drafts
                       SET status = 'activated',
                           activated_at = now(),
                           created_version_id = %s,
                           name = %s
                       WHERE id = %s""",
                    (created["id"], name, draft_id),
                )
                conn.commit()
                flash(f"已生效：版本 {created['version_no']} 为只读版本。")
                return redirect(url_for("version_detail", version_id=created["id"]))

            # 仅保存草案
            _save_draft_leaves(cur, draft_id, leaf_ids)
            cur.execute("UPDATE recipe_drafts SET name = %s WHERE id = %s", (name, draft_id))
        conn.commit()
    finally:
        conn.close()
    flash("草案已保存，尚未生效。")
    return redirect(url_for("recipes_page") + f"#draft-{draft_id}")


@app.get("/recipes/versions/<int:version_id>")
@login_required
def version_detail(version_id):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM recipe_versions WHERE id = %s", (version_id,))
        version = cur.fetchone()
        if version is None:
            return ("没有这个版本", 404)
        cur.execute(
            """SELECT leaf_name, position
               FROM recipe_version_leaves
               WHERE version_id = %s
               ORDER BY position""",
            (version_id,),
        )
        version_leaves = cur.fetchall()
    return render_template(
        "version.html",
        version=version,
        version_leaves=version_leaves,
        can_write=session.get("role") == "writer",
    )
