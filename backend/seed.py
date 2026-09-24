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
            created_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
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

    # ---- 轮值名单 ----
    cur.execute(
        """CREATE TABLE IF NOT EXISTS roster_entries (
            id serial PRIMARY KEY,
            book text NOT NULL CHECK (book IN ('today', 'tomorrow')),
            name text NOT NULL,
            added_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (book, name)
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS roster_audit (
            id serial PRIMARY KEY,
            actor text NOT NULL,
            action text NOT NULL,
            book text,
            target text,
            detail text,
            created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    # roster_day = 当前今日生效簿所对应的自然日；落后于 CURRENT_DATE 时触发自动日切
    cur.execute(
        """CREATE TABLE IF NOT EXISTS roster_meta (
            id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            roster_day date NOT NULL
        )"""
    )
    cur.execute(
        "INSERT INTO roster_meta (id, roster_day) VALUES (1, CURRENT_DATE) ON CONFLICT (id) DO NOTHING"
    )
    # 初始班底：taster 在今日生效簿内，开箱即可交评
    cur.execute(
        """INSERT INTO roster_entries (book, name, added_by)
           VALUES ('today', 'taster', '系统')
           ON CONFLICT (book, name) DO NOTHING"""
    )

    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
