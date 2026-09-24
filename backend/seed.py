import os
import time

import psycopg2

from rules import weigh


def connect():
    last = None
    for _ in range(30):
        try:
            return psycopg2.connect(os.environ["DATABASE_URL"])
        except psycopg2.OperationalError as exc:
            last = exc
            time.sleep(1)
    raise last


def main():
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()

    # 既有审评表（保留）
    cur.execute(
        """CREATE TABLE IF NOT EXISTS cuppings (
            id serial PRIMARY KEY,
            lot text NOT NULL,
            aroma double precision NOT NULL,
            taste double precision NOT NULL,
            liquor double precision NOT NULL,
            score double precision NOT NULL,
            verdict text NOT NULL,
            note text NOT NULL,
            created_by text NOT NULL
        )"""
    )
    # 交评必须挂一个已生效配方版本（历史基线数据允许为空）
    cur.execute(
        """ALTER TABLE cuppings
           ADD COLUMN IF NOT EXISTS recipe_version_id integer"""
    )

    # 母叶目录：草案从中勾选母叶
    cur.execute(
        """CREATE TABLE IF NOT EXISTS leaves (
            id serial PRIMARY KEY,
            name text NOT NULL UNIQUE,
            note text NOT NULL DEFAULT ''
        )"""
    )

    # 配方草案：未生效前可反复改母叶清单
    cur.execute(
        """CREATE TABLE IF NOT EXISTS recipe_drafts (
            id serial PRIMARY KEY,
            name text NOT NULL DEFAULT '拼配配方',
            status text NOT NULL DEFAULT 'open',
            created_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            activated_at timestamptz,
            created_version_id integer,
            CONSTRAINT recipe_drafts_status_chk CHECK (status IN ('open', 'activated'))
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS recipe_draft_leaves (
            draft_id integer NOT NULL REFERENCES recipe_drafts(id) ON DELETE CASCADE,
            leaf_id integer NOT NULL REFERENCES leaves(id),
            position integer NOT NULL DEFAULT 0,
            PRIMARY KEY (draft_id, leaf_id)
        )"""
    )

    # 生效版本号（全局递增，首次生效即“版本一”）
    cur.execute("CREATE SEQUENCE IF NOT EXISTS recipe_version_no_seq")
    cur.execute(
        """CREATE TABLE IF NOT EXISTS recipe_versions (
            id serial PRIMARY KEY,
            version_no bigint NOT NULL UNIQUE
                DEFAULT nextval('recipe_version_no_seq'),
            name text NOT NULL DEFAULT '拼配配方',
            activated_by text NOT NULL,
            activated_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    # 生效正文：母叶清单在生效瞬间快照，leaf_name 一并冗余，母叶目录后改也不动正文
    cur.execute(
        """CREATE TABLE IF NOT EXISTS recipe_version_leaves (
            version_id integer NOT NULL REFERENCES recipe_versions(id),
            leaf_id integer NOT NULL,
            leaf_name text NOT NULL,
            position integer NOT NULL DEFAULT 0,
            PRIMARY KEY (version_id, leaf_id)
        )"""
    )
    cur.execute(
        """ALTER TABLE cuppings
           DROP CONSTRAINT IF EXISTS cuppings_recipe_version_fkey"""
    )
    cur.execute(
        """ALTER TABLE cuppings
           ADD CONSTRAINT cuppings_recipe_version_fkey
           FOREIGN KEY (recipe_version_id) REFERENCES recipe_versions(id)"""
    )

    # 已生效版本只读：数据库层拒绝任何 UPDATE / DELETE
    cur.execute(
        """CREATE OR REPLACE FUNCTION recipe_version_frozen()
           RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
               RAISE EXCEPTION '已生效配方版本为只读快照，不可修改或删除';
           END; $$"""
    )
    cur.execute("DROP TRIGGER IF EXISTS trg_versions_frozen ON recipe_versions")
    cur.execute(
        """CREATE TRIGGER trg_versions_frozen
           BEFORE UPDATE OR DELETE ON recipe_versions
           FOR EACH ROW EXECUTE FUNCTION recipe_version_frozen()"""
    )
    cur.execute("DROP TRIGGER IF EXISTS trg_version_leaves_frozen ON recipe_version_leaves")
    cur.execute(
        """CREATE TRIGGER trg_version_leaves_frozen
           BEFORE UPDATE OR DELETE ON recipe_version_leaves
           FOR EACH ROW EXECUTE FUNCTION recipe_version_frozen()"""
    )

    # 种子母叶
    cur.execute("SELECT COUNT(*) FROM leaves")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO leaves (name, note) VALUES (%s, %s)",
            [
                ("福鼎大白", "鲜爽底味"),
                ("勐库大叶", "厚重回甘"),
                ("铁观音", "兰花香韵"),
                ("凤凰水仙", "花香高扬"),
                ("紫鹃", "清甜花青素"),
                ("梅占", "音韵分明"),
            ],
        )

    # 种子审评（版本功能上线前的历史数据，无绑定版本）
    cur.execute("SELECT COUNT(*) FROM cuppings")
    if cur.fetchone()[0] == 0:
        for lot, aroma, taste, liquor in (("春茶-A", 8, 8, 7), ("夏茶-C", 5, 4, 6)):
            verdict, note, score = weigh(aroma, taste, liquor)
            cur.execute(
                """INSERT INTO cuppings
                       (lot, aroma, taste, liquor, score, verdict, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (lot, aroma, taste, liquor, score, verdict, note, "taster"),
            )

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
