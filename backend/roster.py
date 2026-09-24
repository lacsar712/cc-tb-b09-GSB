"""轮值名单领域逻辑：今日生效簿 / 明日预排簿 / 日切 / 排班履历。"""
import re

ROSTER_ADVISORY_KEY = 92731
BOOK_TODAY = "today"
BOOK_TOMORROW = "tomorrow"
BOOKS = (BOOK_TODAY, BOOK_TOMORROW)
BOOK_LABELS = {BOOK_TODAY: "今日生效簿", BOOK_TOMORROW: "明日预排簿"}
SYSTEM_ACTOR = "系统"

# 人名之间允许用空白、换行或常见中英文标点分隔
_NAME_SPLIT = re.compile(r"[\s,，、;；]+")


def parse_names(text):
    """把一次粘贴的多个人名拆成 (去重后的有序名单, 粘贴内容里的重名)。"""
    seen = set()
    names, dups = [], []
    for part in _NAME_SPLIT.split(text or ""):
        name = part.strip()
        if not name:
            continue
        if name in seen:
            dups.append(name)
        else:
            seen.add(name)
            names.append(name)
    return names, dups


def _write_audit(cur, actor, action, book=None, target=None, detail=None):
    cur.execute(
        """INSERT INTO roster_audit (actor, action, book, target, detail)
           VALUES (%s, %s, %s, %s, %s)""",
        (actor, action, book, target, detail),
    )


def add_names(cur, book, names, actor):
    """批量写入某簿；同簿重名跳过。返回 (已加入, 重名跳过)。"""
    added, skipped = [], []
    for name in names:
        cur.execute(
            """INSERT INTO roster_entries (book, name, added_by)
               VALUES (%s, %s, %s)
               ON CONFLICT (book, name) DO NOTHING""",
            (book, name, actor),
        )
        if cur.rowcount:
            added.append(name)
        else:
            skipped.append(name)
    for name in added:
        _write_audit(cur, actor, "add", book=book, target=name)
    return added, skipped


def remove_name(cur, book, name, actor):
    """从某簿移出人；不存在返回 False，且不写履历。"""
    cur.execute(
        "DELETE FROM roster_entries WHERE book = %s AND name = %s",
        (book, name),
    )
    if not cur.rowcount:
        return False
    _write_audit(cur, actor, "remove", book=book, target=name)
    return True


def _run_daycut(cur, actor, manual):
    """执行一次日切：明日预排提升为今日生效，明日簿清空，写一条日切履历。"""
    cur.execute(
        "SELECT name FROM roster_entries WHERE book = %s ORDER BY id",
        (BOOK_TOMORROW,),
    )
    promoted_names = [row[0] for row in cur.fetchall()]

    cur.execute("DELETE FROM roster_entries WHERE book = %s", (BOOK_TODAY,))
    cur.execute(
        """INSERT INTO roster_entries (book, name, added_by)
           SELECT %s, name, %s FROM roster_entries WHERE book = %s
           ON CONFLICT (book, name) DO NOTHING""",
        (BOOK_TODAY, SYSTEM_ACTOR, BOOK_TOMORROW),
    )
    cur.execute("DELETE FROM roster_entries WHERE book = %s", (BOOK_TOMORROW,))

    promoted = "、".join(promoted_names) if promoted_names else "（无）"
    kind = "手动日切" if manual else "跨日日切"
    _write_audit(
        cur,
        actor,
        "daycut",
        detail=f"{kind}：明日预排提升为新的今日生效，提升 {len(promoted_names)} 人：{promoted}",
    )
    return promoted_names


def sync_roster(cur, force=False, actor=SYSTEM_ACTOR):
    """跨自然日则自动日切；force=True 时强制执行一次（手动日切/模拟跨日）。

    自动模式按自然日差额补齐（多日未访问也只追赶到今天）；手动模式始终切一次。
    返回执行的日切次数。调用方负责提交事务。
    """
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (ROSTER_ADVISORY_KEY,))
    cur.execute("SELECT roster_day, CURRENT_DATE FROM roster_meta WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        # 理论上 seed.py 已建好锚点行；兜底，避免功能在空库上整体不可用
        cur.execute(
            "INSERT INTO roster_meta (id, roster_day) VALUES (1, CURRENT_DATE) ON CONFLICT (id) DO NOTHING"
        )
        return 0

    roster_day, current_day = row
    if force:
        _run_daycut(cur, actor, manual=True)
        # 手动日切把锚点钉在真实今天：同日不再自动补切，次日仍会正常跨日
        cur.execute("UPDATE roster_meta SET roster_day = CURRENT_DATE WHERE id = 1")
        return 1

    steps = max(0, (current_day - roster_day).days)
    for _ in range(steps):
        _run_daycut(cur, SYSTEM_ACTOR, manual=False)
    if steps:
        cur.execute("UPDATE roster_meta SET roster_day = CURRENT_DATE WHERE id = 1")
    return steps
