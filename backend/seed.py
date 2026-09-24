import os
import time
from datetime import date

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
    cur = conn.cursor()
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
    cur.execute(
        """CREATE TABLE IF NOT EXISTS roster (
            book text NOT NULL CHECK (book IN ('today', 'tomorrow')),
            name text NOT NULL,
            added_by text NOT NULL,
            added_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (book, name)
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS roster_audit (
            id serial PRIMARY KEY,
            at timestamptz NOT NULL DEFAULT now(),
            actor text NOT NULL,
            action text NOT NULL,
            book text,
            name text,
            detail text
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS roster_meta (
            id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            active_date date NOT NULL
        )"""
    )

    cur.execute("SELECT COUNT(*) FROM cuppings")
    if cur.fetchone()[0] == 0:
        for lot, aroma, taste, liquor in (("春茶-A", 8, 8, 7), ("夏茶-C", 5, 4, 6)):
            verdict, note, score = weigh(aroma, taste, liquor)
            cur.execute(
                """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (lot, aroma, taste, liquor, score, verdict, note, "taster"),
            )

    # 初始：今日生效簿含 taster，明日预排簿为空；履历记一条初始化
    cur.execute("SELECT COUNT(*) FROM roster_meta")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO roster_meta (id, active_date) VALUES (1, %s)", (date.today(),))
        cur.execute(
            """INSERT INTO roster (book, name, added_by)
               SELECT 'today', 'taster', 'system'
               WHERE NOT EXISTS (SELECT 1 FROM roster WHERE book = 'today' AND name = 'taster')"""
        )
        cur.execute(
            """INSERT INTO roster_audit (actor, action, book, name, detail)
               VALUES ('system', 'init', 'today', 'taster', '初始今日生效簿')"""
        )

    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
