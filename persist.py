import os, sqlite3, tempfile, threading, time

URL = os.environ.get("DATABASE_URL", "").strip()
INTERVAL = 15
_ready = False


def _connect():
    import psycopg2
    return psycopg2.connect(URL, connect_timeout=15)


def _table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS kare_snapshot(
        id INTEGER PRIMARY KEY, data BYTEA NOT NULL, updated TIMESTAMPTZ DEFAULT now())""")


def restore(db_file):
    global _ready
    if not URL:
        return
    try:
        con = _connect()
        try:
            with con, con.cursor() as cur:
                _table(cur)
                cur.execute("SELECT data FROM kare_snapshot WHERE id=1")
                row = cur.fetchone()
            if row:
                with open(db_file, "wb") as f:
                    f.write(bytes(row[0]))
                print(f"[persist] restored database ({len(bytes(row[0]))} bytes)")
            else:
                print("[persist] no saved copy yet - starting fresh")
            _ready = True
        finally:
            con.close()
    except Exception as e:
        print("[persist] RESTORE FAILED, saving is switched off:", e)


def _snapshot_bytes(db_file):
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(db_file)
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        dst.close()
        src.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def save(db_file):
    data = _snapshot_bytes(db_file)
    con = _connect()
    try:
        with con, con.cursor() as cur:
            _table(cur)
            cur.execute("""INSERT INTO kare_snapshot(id, data, updated) VALUES(1, %s, now())
                           ON CONFLICT(id) DO UPDATE SET data=EXCLUDED.data, updated=now()""",
                        (data,))
    finally:
        con.close()
    return len(data)


def _loop(db_file):
    last = None
    while True:
        time.sleep(INTERVAL)
        try:
            st = os.stat(db_file)
            mark = (st.st_mtime_ns, st.st_size)
            if mark != last:
                n = save(db_file)
                last = mark
                print(f"[persist] saved ({n} bytes)")
        except Exception as e:
            print("[persist] save failed, will retry:", e)


def start(db_file):
    if URL and _ready:
        threading.Thread(target=_loop, args=(db_file,), daemon=True).start()
    elif URL:
        print("[persist] not saving because restore did not succeed")


