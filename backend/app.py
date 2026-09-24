import os
from functools import wraps

import psycopg2
from flask import Flask, flash, redirect, render_template, request, session, url_for
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
        if session.get("role") != "writer":
            return ("仅审评员可操作拼配配方", 403)
        return fn(*args, **kwargs)

    return wrap


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
        cur.execute(
            """SELECT c.*, r.version AS recipe_version
               FROM cuppings c LEFT JOIN recipes r ON r.id = c.recipe_id
               ORDER BY c.id DESC"""
        )
        rows = cur.fetchall()
        cur.execute(
            """SELECT r.id, r.version, string_agg(l.name, '、' ORDER BY l.position, l.id) AS leaves
               FROM recipes r JOIN recipe_leaves l ON l.recipe_id = r.id
               WHERE r.status = 'active'
               GROUP BY r.id, r.version
               ORDER BY r.version"""
        )
        versions = cur.fetchall()
    return render_template(
        "home.html", rows=rows, versions=versions, can_write=session.get("role") == "writer"
    )


@app.post("/cuppings")
@login_required
def create():
    if session.get("role") != "writer":
        return ("仅审评员可提交拼配审评", 403)
    recipe_id = request.form.get("recipe_id", "").strip()
    if not recipe_id.isdigit():
        return ("交评必须挑选一个已生效的配方版本号", 400)
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id FROM recipes WHERE id = %s AND status = 'active'", (int(recipe_id),)
        )
        if not cur.fetchone():
            return ("配方版本不存在或尚未生效，交评被拒", 400)
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by, recipe_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"], int(recipe_id)),
        )
        new_id = cur.fetchone()["id"]
        cur.execute(
            """SELECT c.*, r.version AS recipe_version
               FROM cuppings c LEFT JOIN recipes r ON r.id = c.recipe_id
               WHERE c.id = %s""",
            (new_id,),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


@app.get("/recipes")
@login_required
def recipes():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM recipes WHERE status = 'active' ORDER BY version")
        versions = cur.fetchall()
        drafts = []
        if session.get("role") == "writer":
            cur.execute("SELECT * FROM recipes WHERE status = 'draft' ORDER BY id")
            drafts = cur.fetchall()
        ids = [r["id"] for r in versions + drafts]
        leaves_by_recipe = {i: [] for i in ids}
        if ids:
            cur.execute(
                "SELECT * FROM recipe_leaves WHERE recipe_id = ANY(%s) ORDER BY position, id",
                (ids,),
            )
            for leaf in cur.fetchall():
                leaves_by_recipe[leaf["recipe_id"]].append(leaf)
    for recipe in versions + drafts:
        recipe["leaves"] = leaves_by_recipe[recipe["id"]]
    return render_template(
        "recipes.html",
        versions=versions,
        drafts=drafts,
        can_write=session.get("role") == "writer",
    )


@app.post("/recipes/draft")
@login_required
@writer_required
def create_draft():
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO recipes (created_by) VALUES (%s)", (session["user"],))
        conn.commit()
    return redirect(url_for("recipes"))


@app.post("/recipes/<int:recipe_id>/leaves")
@login_required
@writer_required
def add_leaf(recipe_id):
    name = request.form.get("name", "").strip()
    if not name:
        flash("母叶名称不能为空")
        return redirect(url_for("recipes"))
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status FROM recipes WHERE id = %s", (recipe_id,))
        recipe = cur.fetchone()
        if not recipe or recipe["status"] != "draft":
            flash("已生效版本是只读的，只能新建草案再改")
            return redirect(url_for("recipes"))
        cur.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS pos FROM recipe_leaves WHERE recipe_id = %s",
            (recipe_id,),
        )
        position = cur.fetchone()["pos"]
        cur.execute(
            "INSERT INTO recipe_leaves (recipe_id, name, position) VALUES (%s, %s, %s)",
            (recipe_id, name, position),
        )
        conn.commit()
    return redirect(url_for("recipes"))


@app.post("/recipes/<int:recipe_id>/leaves/<int:leaf_id>/delete")
@login_required
@writer_required
def delete_leaf(recipe_id, leaf_id):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status FROM recipes WHERE id = %s", (recipe_id,))
        recipe = cur.fetchone()
        if not recipe or recipe["status"] != "draft":
            flash("已生效版本是只读的，只能新建草案再改")
            return redirect(url_for("recipes"))
        cur.execute(
            "DELETE FROM recipe_leaves WHERE id = %s AND recipe_id = %s", (leaf_id, recipe_id)
        )
        conn.commit()
    return redirect(url_for("recipes"))


@app.post("/recipes/<int:recipe_id>/activate")
@login_required
@writer_required
def activate_recipe(recipe_id):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status FROM recipes WHERE id = %s", (recipe_id,))
        recipe = cur.fetchone()
        if not recipe or recipe["status"] != "draft":
            flash("只有草案能生效；已生效版本不能改动")
            return redirect(url_for("recipes"))
        cur.execute("SELECT COUNT(*) AS n FROM recipe_leaves WHERE recipe_id = %s", (recipe_id,))
        if cur.fetchone()["n"] == 0:
            flash("草案还没挂母叶，先挂母叶再生效")
            return redirect(url_for("recipes"))
        cur.execute("SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM recipes")
        version = cur.fetchone()["next_version"]
        cur.execute(
            "UPDATE recipes SET status = 'active', version = %s, activated_at = now() WHERE id = %s",
            (version, recipe_id),
        )
        conn.commit()
    flash(f"配方已生效为版本 {version}，之后只能新建草案")
    return redirect(url_for("recipes"))
