import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pgserver
import psycopg2
import pytest

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))


@pytest.fixture(scope="session")
def database_url():
    tmp = Path(tempfile.mkdtemp(prefix="pgtest-"))
    server = pgserver.get_server(tmp, cleanup_mode="delete")
    url = server.get_uri()
    # pgserver 默认库是 postgres，另建应用库 tea_cup（URI 是 unix socket 形式，host 在 query 里）
    conn = psycopg2.connect(url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS tea_cup")
        cur.execute("CREATE DATABASE tea_cup")
    conn.close()
    app_url = urlunparse(urlparse(url)._replace(path="/tea_cup"))
    os.environ["DATABASE_URL"] = app_url
    yield app_url
    server.cleanup()


@pytest.fixture()
def client(database_url):
    # 每个用例重建一个干净库：重新 seed
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS roster_audit, roster_entries, roster_meta, cuppings")
    conn.close()

    import seed
    seed.main()

    import app as flask_app
    flask_app.app.config["WTF_CSRF_ENABLED"] = False
    c = flask_app.app.test_client()
    yield c
    c.get("/logout")


def login(client, username, password):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def get_book_names(database_url, book):
    conn = psycopg2.connect(database_url)
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM roster_entries WHERE book=%s ORDER BY id", (book,))
        names = [r[0] for r in cur.fetchall()]
    conn.close()
    return names


def get_audit_actions(database_url):
    conn = psycopg2.connect(database_url)
    with conn.cursor() as cur:
        cur.execute("SELECT actor, action, book, target FROM roster_audit ORDER BY id")
        rows = cur.fetchall()
    conn.close()
    return rows


def submit_cupping(client, lot="春芽", aroma=9, taste=9, liquor=8, hx=True):
    headers = {"HX-Request": "true"} if hx else {}
    return client.post(
        "/cuppings",
        data={"lot": lot, "aroma": aroma, "taste": taste, "liquor": liquor},
        headers=headers,
    )


# ---------- 基础 ----------

def test_seed_taster_on_today_and_topnav(client):
    r = login(client, "taster", "tea123456")
    assert r.status_code == 200
    page = client.get("/roster").get_data(as_text=True)
    assert "轮值名单" in page
    assert "taster" in page
    # 顶栏入口
    home = client.get("/").get_data(as_text=True)
    assert "/roster" in home


def test_baseline_cuppings_visible(client):
    login(client, "taster", "tea123456")
    home = client.get("/").get_data(as_text=True)
    assert "春茶-A" in home and "通过" in home
    assert "夏茶-C" in home and "不通过" in home


# ---------- 移出/加回今日簿与交评门控 ----------

def test_remove_blocks_submission_addback_allows(client, database_url):
    login(client, "taster", "tea123456")

    # 在今日簿：交春芽成功
    ok = submit_cupping(client, lot="春芽-在簿")
    assert ok.status_code == 200
    assert "春芽-在簿" in ok.get_data(as_text=True)

    # 移出今日簿
    r = client.post("/roster/remove", data={"book": "today", "name": "taster"})
    assert r.status_code in (302, 303)
    assert get_book_names(database_url, "today") == []

    # 再交评必须失败
    blocked = submit_cupping(client, lot="春芽-移出")
    assert blocked.status_code == 403
    assert "不在今日生效簿" in blocked.get_data(as_text=True)

    # 履历记录：谁在何时把谁移出
    audits = get_audit_actions(database_url)
    assert ("taster", "remove", "today", "taster") in audits

    # 加回今日簿
    r = client.post("/roster/add", data={"book": "today", "names": "taster"}, follow_redirects=True)
    assert r.status_code == 200
    assert get_book_names(database_url, "today") == ["taster"]

    # 交春芽成功
    ok2 = submit_cupping(client, lot="春芽-加回")
    assert ok2.status_code == 200
    assert "春芽-加回" in ok2.get_data(as_text=True)

    audits = get_audit_actions(database_url)
    assert ("taster", "add", "today", "taster") in audits


# ---------- 批量写入 + 重名跳过 ----------

def test_batch_paste_dedup(client, database_url):
    login(client, "taster", "tea123456")
    r = client.post(
        "/roster/add",
        data={"book": "today", "names": "阿甲\n阿乙,阿丙、阿乙\n阿丁 阿甲\n阿戊"},
        follow_redirects=True,
    )
    text = r.get_data(as_text=True)
    assert "已加入今日生效簿" in text
    # 阿乙、阿甲 在粘贴内或簿内重复 -> 重名跳过并提示
    assert "重名跳过" in text
    assert "阿乙" in text

    names = get_book_names(database_url, "today")
    assert names == ["taster", "阿甲", "阿乙", "阿丙", "阿丁", "阿戊"]


# ---------- 观察员只读 ----------

def test_observer_can_view_but_not_schedule(client, database_url):
    login(client, "observer", "look123456")
    page = client.get("/roster").get_data(as_text=True)
    assert "今日生效簿" in page and "明日预排簿" in page and "排班履历" in page
    # 看不到任何排班控件
    assert "/roster/add" not in page
    assert "/roster/remove" not in page
    assert "执行日切" not in page

    r = client.post("/roster/add", data={"book": "today", "names": "黑客"})
    assert r.status_code == 403
    r = client.post("/roster/remove", data={"book": "today", "name": "taster"})
    assert r.status_code == 403
    r = client.post("/roster/daycut")
    assert r.status_code == 403
    # 名单未被改动
    assert get_book_names(database_url, "today") == ["taster"]


# ---------- 明日预排 + 日切 ----------

def test_tomorrow_then_daycut(client, database_url):
    login(client, "taster", "tea123456")
    r = client.post(
        "/roster/add",
        data={"book": "tomorrow", "names": "轮值乙"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    # 履历可见
    page = client.get("/roster").get_data(as_text=True)
    assert "轮值乙" in page
    assert ("taster", "add", "tomorrow", "轮值乙") in get_audit_actions(database_url)

    # 执行日切
    r = client.post("/roster/daycut", follow_redirects=True)
    assert r.status_code == 200
    assert get_book_names(database_url, "today") == ["轮值乙"]
    assert get_book_names(database_url, "tomorrow") == []

    page = client.get("/roster").get_data(as_text=True)
    assert "日切" in page
    assert "轮值乙" in page
    audits = get_audit_actions(database_url)
    assert any(a[1] == "daycut" and a[0] == "taster" for a in audits)

    # 日切后 taster 已不在今日簿 -> 交评被拒
    blocked = submit_cupping(client)
    assert blocked.status_code == 403


def test_cross_natural_day_auto_promotes(client, database_url):
    """模拟跨自然日：把锚点拨到昨天，访问任意页面即自动日切。"""
    login(client, "taster", "tea123456")
    client.post("/roster/add", data={"book": "tomorrow", "names": "跨日新人"})

    conn = psycopg2.connect(database_url)
    with conn.cursor() as cur:
        cur.execute("UPDATE roster_meta SET roster_day = CURRENT_DATE - 1 WHERE id = 1")
    conn.commit()
    conn.close()

    # 访问首页触发 before_request 自动日切
    client.get("/")

    assert get_book_names(database_url, "today") == ["跨日新人"]
    assert get_book_names(database_url, "tomorrow") == []
    audits = get_audit_actions(database_url)
    assert any(a[1] == "daycut" and a[0] == "系统" and a[3] is None for a in audits)

    # 锚点已回到今天：再访问不会重复切
    n = len(audits)
    client.get("/")
    assert len(get_audit_actions(database_url)) == n


def test_manual_daycut_then_next_real_day_still_cuts(client, database_url):
    """同日手动日切后，次日跨日仍应自动切。"""
    login(client, "taster", "tea123456")
    client.post("/roster/add", data={"book": "tomorrow", "names": "首日"})
    client.post("/roster/daycut")
    assert get_book_names(database_url, "today") == ["首日"]

    client.post("/roster/add", data={"book": "tomorrow", "names": "次日"})
    conn = psycopg2.connect(database_url)
    with conn.cursor() as cur:
        cur.execute("UPDATE roster_meta SET roster_day = CURRENT_DATE - 1 WHERE id = 1")
    conn.commit()
    conn.close()
    client.get("/")
    assert get_book_names(database_url, "today") == ["次日"]
    assert get_book_names(database_url, "tomorrow") == []


def test_empty_tomorrow_daycut_clears_today(client, database_url):
    login(client, "taster", "tea123456")
    client.post("/roster/daycut", follow_redirects=True)
    assert get_book_names(database_url, "today") == []
    assert get_book_names(database_url, "tomorrow") == []
    # 没人在今日簿 -> taster 自己也被挡
    assert submit_cupping(client).status_code == 403
