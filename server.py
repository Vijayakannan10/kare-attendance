"""
Face Attendance Server (FastAPI + SQLite)
Run:  python -m uvicorn server:app --reload --host 0.0.0.0
Test: http://127.0.0.1:8000/docs
"""
import base64, csv, hashlib, io, json, os, re, secrets, sqlite3, threading, time
from typing import List, Optional

import face_recognition
import jwt
import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel

DB_FILE = "attendance.db"
SECRET = os.environ.get("JWT_SECRET", "change-this-secret-in-real-use")
STRICT = 0.45   # <= sure match  -> present
LOOSE = 0.55    # <= maybe       -> check (teacher confirms)
HAS_FACE = "(encodings IS NOT NULL AND encodings NOT IN ('', '[]'))"

MARGIN = 0.06       # best match must beat the second-best student by this much, else "check"
DUP_DIST = 0.35     # a new face this close to another student's face = probably the same person
CHECK_QUALITY = True            # set False to switch the photo checks off
MIN_FACE, MIN_SHARP, MAX_YAW = 90, 12.0, 0.40
LAST_SCAN = {}      # (staff, class) -> {reg_no: status the computer chose}, used for accuracy

app = FastAPI(title="Face Attendance")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
bearer = HTTPBearer()


# ---------- database ----------
def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS staff(
            staff_id TEXT PRIMARY KEY, name TEXT, faculty_no TEXT,
            pw_hash TEXT, salt TEXT);
        CREATE TABLE IF NOT EXISTS students(
            reg_no TEXT PRIMARY KEY, name TEXT, encodings TEXT, added_by TEXT,
            class_name TEXT DEFAULT 'General');
        CREATE TABLE IF NOT EXISTS attendance(
            date TEXT, subject TEXT, reg_no TEXT, name TEXT, status TEXT,
            staff_id TEXT, PRIMARY KEY(date, subject, reg_no));
        CREATE TABLE IF NOT EXISTS attendance_v2(
            date TEXT, class_name TEXT, subject TEXT, reg_no TEXT, name TEXT,
            status TEXT, staff_id TEXT, auto_status TEXT,
            PRIMARY KEY(date, class_name, subject, reg_no));
        """)
        cols = [r["name"] for r in con.execute("PRAGMA table_info(students)")]
        if "class_name" not in cols:
            con.execute("ALTER TABLE students ADD COLUMN class_name TEXT DEFAULT 'General'")
        con.execute("UPDATE students SET class_name='General' WHERE class_name IS NULL OR class_name=''")
        con.execute("""CREATE TABLE IF NOT EXISTS class_access(
            class_name TEXT, staff_id TEXT, PRIMARY KEY(class_name, staff_id))""")
        cols_s = [r["name"] for r in con.execute("PRAGMA table_info(staff)")]
        if "role" not in cols_s:
            con.execute("ALTER TABLE staff ADD COLUMN role TEXT DEFAULT 'faculty'")
        if "department" not in cols_s:
            con.execute("ALTER TABLE staff ADD COLUMN department TEXT DEFAULT ''")
        # classes made before this feature belong to the staff who added their students
        con.execute("""INSERT OR IGNORE INTO class_access(class_name, staff_id)
            SELECT DISTINCT class_name, added_by FROM students
            WHERE added_by IS NOT NULL AND added_by != '' AND class_name IS NOT NULL""")
        cols2 = [r["name"] for r in con.execute("PRAGMA table_info(attendance_v2)")]
        if "period" not in cols2:
            con.execute("ALTER TABLE attendance_v2 ADD COLUMN period INTEGER")
        if "auto_status" not in cols2:
            con.execute("ALTER TABLE attendance_v2 ADD COLUMN auto_status TEXT")
        con.execute("""INSERT OR IGNORE INTO attendance_v2
            (date, class_name, subject, reg_no, name, status, staff_id)
            SELECT date, 'General', subject, reg_no, name, status, staff_id FROM attendance""")


import persist
persist.restore(DB_FILE)
init_db()


# ---------- auth ----------
def hash_pw(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()


def current_staff(cred: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    try:
        return jwt.decode(cred.credentials, SECRET, algorithms=["HS256"])["sub"]
    except Exception:
        raise HTTPException(401, "Invalid or expired token")


def admin_staff(staff: str = Depends(current_staff)) -> str:
    """Only the head (admin) may use the endpoints that depend on this."""
    with db() as con:
        r = con.execute("SELECT role FROM staff WHERE staff_id=?", (staff,)).fetchone()
    if not r or r["role"] != "admin":
        raise HTTPException(403, "Only the head can do this.")
    return staff


def new_password() -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no look-alike letters
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ---------- who can see which class ----------
def visible_classes(con, staff):
    """None means every class (the head). Otherwise the set of class names this faculty may use."""
    r = con.execute("SELECT role FROM staff WHERE staff_id=?", (staff,)).fetchone()
    if r and r["role"] == "admin":
        return None
    return {x["class_name"] for x in con.execute("SELECT class_name FROM class_access WHERE staff_id=?", (staff,))}


def visible_for(staff):
    with db() as con:
        return visible_classes(con, staff)


def need_class(staff, class_name):
    v = visible_for(staff)
    if v is not None and class_name not in v:
        raise HTTPException(403, f"Class {class_name} is not yours. Ask the head to give you access.")


def claim_class(staff, class_name):
    """Allowed when the class is yours or brand new. A new class becomes yours."""
    with db() as con:
        v = visible_classes(con, staff)
        if v is None or class_name in v:
            return
        taken = (con.execute("SELECT 1 FROM class_access WHERE class_name=? LIMIT 1", (class_name,)).fetchone()
                 or con.execute("SELECT 1 FROM students WHERE class_name=? LIMIT 1", (class_name,)).fetchone())
        if taken:
            raise HTTPException(403, f"Class {class_name} belongs to another faculty. Use a different class code, "
                                     "or ask the head for access.")
        con.execute("INSERT OR IGNORE INTO class_access(class_name, staff_id) VALUES(?,?)", (class_name, staff))


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.get("/setup-status")
def setup_status():
    """The app asks this before login: is the head account created yet?"""
    with db() as con:
        n = con.execute("SELECT COUNT(*) FROM staff WHERE role='admin'").fetchone()[0]
    return {"needs_admin": n == 0}


@app.post("/signup")
def signup(staff_id: str = Form(...), name: str = Form(...),
           faculty_no: str = Form(...), password: str = Form(...)):
    """Creates the head account. It works only once, until a head exists. Faculty are created by the head."""
    with db() as con:
        if con.execute("SELECT COUNT(*) FROM staff WHERE role='admin'").fetchone()[0] > 0:
            raise HTTPException(403, "Sign up is closed. Ask the head to create your account.")
    if len(password) < 6:
        raise HTTPException(400, "The password needs at least 6 characters.")
    salt = secrets.token_hex(8)
    try:
        with db() as con:
            con.execute("INSERT INTO staff(staff_id, name, faculty_no, pw_hash, salt, role) VALUES(?,?,?,?,?,'admin')",
                        (staff_id.strip().lower(), name.strip(), faculty_no.strip(), hash_pw(password, salt), salt))
    except sqlite3.IntegrityError:
        # an old account with this ID already exists: make it the head if the password matches
        with db() as con:
            row = con.execute("SELECT * FROM staff WHERE staff_id=?", (staff_id.strip().lower(),)).fetchone()
            if row and hash_pw(password, row["salt"]) == row["pw_hash"]:
                con.execute("UPDATE staff SET role='admin' WHERE staff_id=?", (row["staff_id"],))
            else:
                raise HTTPException(400, "This ID already exists. Use a different one.")
    return {"ok": True}


@app.post("/login")
def login(staff_id: str = Form(...), password: str = Form(...)):
    sid = staff_id.strip()
    with db() as con:
        row = con.execute("SELECT * FROM staff WHERE staff_id=? OR staff_id=?", (sid, sid.lower())).fetchone()
    if not row or hash_pw(password, row["salt"]) != row["pw_hash"]:
        raise HTTPException(401, "Wrong ID or password")
    token = jwt.encode({"sub": row["staff_id"], "exp": int(time.time()) + 12 * 3600}, SECRET, "HS256")
    return {"token": token, "name": row["name"], "faculty_no": row["faculty_no"],
            "role": row["role"] or "faculty"}


# ---------- faculty accounts (head only) ----------
@app.get("/staff")
def list_staff(head: str = Depends(admin_staff)):
    with db() as con:
        rows = con.execute("SELECT staff_id, name, faculty_no, role, department FROM staff ORDER BY role='admin' DESC, department, name").fetchall()
    return [dict(r) for r in rows]


@app.post("/staff")
def create_staff(staff_id: str = Form(...), name: str = Form(...), faculty_no: str = Form(""),
                 password: str = Form(""), department: str = Form(""), head: str = Depends(admin_staff)):
    sid = staff_id.strip().lower()
    if not EMAIL.match(sid):
        raise HTTPException(400, "Enter the faculty Gmail ID, for example name@gmail.com")
    if not name.strip():
        raise HTTPException(400, "Enter the faculty name.")
    pw = password if password else new_password()
    if len(pw) < 6:
        raise HTTPException(400, "The password needs at least 6 characters.")
    salt = secrets.token_hex(8)
    try:
        with db() as con:
            con.execute("INSERT INTO staff(staff_id, name, faculty_no, pw_hash, salt, role, department) VALUES(?,?,?,?,?,'faculty',?)",
                        (sid, name.strip(), faculty_no.strip(), hash_pw(pw, salt), salt, department.strip()))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "This Gmail ID already has an account.")
    return {"ok": True, "staff_id": sid, "name": name.strip(), "password": pw}


@app.post("/staff/reset")
def reset_staff(staff_id: str = Form(...), head: str = Depends(admin_staff)):
    pw = new_password()
    salt = secrets.token_hex(8)
    with db() as con:
        n = con.execute("UPDATE staff SET pw_hash=?, salt=? WHERE staff_id=?",
                        (hash_pw(pw, salt), salt, staff_id)).rowcount
    if not n:
        raise HTTPException(404, "No such faculty.")
    return {"ok": True, "staff_id": staff_id, "password": pw}


@app.delete("/staff")
def delete_staff(staff_id: str, head: str = Depends(admin_staff)):
    if staff_id == head:
        raise HTTPException(400, "You cannot remove your own account.")
    with db() as con:
        con.execute("DELETE FROM staff WHERE staff_id=?", (staff_id,))
        con.execute("DELETE FROM class_access WHERE staff_id=?", (staff_id,))
    return {"ok": True}


# ---------- face helpers ----------
def read_image(data: bytes, max_side: int) -> Image.Image:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)).convert("RGB"))  # fixes phone rotation
    img.thumbnail((max_side, max_side))
    return img


def photo_problem(img: np.ndarray, loc) -> Optional[str]:
    """Returns why a registration photo is not good enough, or None if it is fine."""
    t, r, b, l = loc
    if min(r - l, b - t) < MIN_FACE:
        return "face is too small, come closer"
    face = img[max(t, 0):b, max(l, 0):r]
    gray = np.asarray(Image.fromarray(face).convert("L").resize((160, 160)), dtype=float)
    if gray.mean() < 45:
        return "face is too dark, use more light"
    if gray.mean() > 215:
        return "face is too bright"
    lap = (4 * gray[1:-1, 1:-1] - gray[:-2, 1:-1] - gray[2:, 1:-1]
           - gray[1:-1, :-2] - gray[1:-1, 2:])
    if lap.var() < MIN_SHARP:
        return "photo is blurry, hold the phone steady"
    try:
        lm = face_recognition.face_landmarks(img, [loc], model="small")
        if lm:
            p = lm[0]
            le = np.mean(p["left_eye"], axis=0)
            re_ = np.mean(p["right_eye"], axis=0)
            nose = np.array(p["nose_tip"][0], dtype=float)
            eye_dist = float(np.linalg.norm(le - re_))
            if eye_dist > 0 and abs(nose[0] - (le[0] + re_[0]) / 2) / eye_dist > MAX_YAW:
                return "face is turned to the side, look straight at the camera"
    except Exception:
        pass   # if the landmark step fails, skip only this check
    return None


def two_nearest(d_all, starts, ids):
    """Nearest and second-nearest STUDENT (not encoding) for one face."""
    per = np.minimum.reduceat(d_all, starts)
    order = np.argsort(per)
    best_i, best_d = int(ids[order[0]]), float(per[order[0]])
    if len(order) > 1:
        return best_i, best_d, int(ids[order[1]]), float(per[order[1]])
    return best_i, best_d, None, 9.9


# ---------- class list import (PDF / Excel / CSV) ----------
def clean_name(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().title()


def people_from_rows(rows) -> dict:
    """Finds a register number and a name in every row, whatever the column order."""
    out = {}
    for row in rows:
        cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
        reg = next((re.sub(r"\.0$", "", c) for c in cells if re.fullmatch(r"\d{6,12}(\.0)?", c)), None)
        if not reg:
            continue
        name = next((c for c in cells if re.search(r"[A-Za-z]{2,}", c)), None)
        if name:
            out.setdefault(reg, clean_name(name))
    return out


def parse_pdf(data: bytes) -> dict:
    import pdfplumber
    rows, text = [], ""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                rows.extend(table)
            text += " " + (page.extract_text() or "")
    people = people_from_rows(rows)
    if not people:   # PDF without table lines: read the plain text instead
        text = re.sub(r"\s+", " ", text)
        for m in re.finditer(r"(?<!\d)\d{1,3}\s+(\d{9,12})\s*([A-Za-z][^\d]*)", text):
            people.setdefault(m.group(1), clean_name(m.group(2)))
    return people


def parse_xlsx(data: bytes) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    rows = []
    for ws in wb.worksheets:
        rows.extend(ws.iter_rows(values_only=True))
    return people_from_rows(rows)


def parse_csv(data: bytes) -> dict:
    return people_from_rows(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="ignore"))))


@app.post("/classes/import")
async def import_class(class_name: str = Form(...), file: UploadFile = File(...),
                       staff: str = Depends(current_staff)):
    class_name = class_name.strip()
    if not class_name:
        raise HTTPException(400, "Class name is required")
    claim_class(staff, class_name)
    data = await file.read()
    ext = os.path.splitext(file.filename or "")[1].lower()
    try:
        if ext == ".pdf":
            people = parse_pdf(data)
        elif ext in (".xlsx", ".xlsm"):
            people = parse_xlsx(data)
        elif ext in (".csv", ".txt"):
            people = parse_csv(data)
        else:
            raise HTTPException(400, "Please upload a PDF, Excel (.xlsx) or CSV file")
    except HTTPException:
        raise
    except ImportError as e:
        raise HTTPException(500, f"Server is missing a package: {e.name}. Run: pip install pdfplumber openpyxl")
    except Exception:
        raise HTTPException(400, "This file could not be read")
    if not people:
        raise HTTPException(400, "No register numbers and names were found in this file")
    added = moved = skipped = 0
    with db() as con:
        mine = visible_classes(con, staff)
        for reg, name in people.items():
            old = con.execute("SELECT class_name FROM students WHERE reg_no=?", (reg,)).fetchone()
            if old and mine is not None and old["class_name"] not in mine and old["class_name"] != class_name:
                skipped += 1          # this student already belongs to another faculty's class
                continue
            if old:
                moved += 1
            else:
                added += 1
            # existing face data is kept; only the name and class are updated
            con.execute("""INSERT INTO students(reg_no, name, encodings, added_by, class_name)
                           VALUES(?,?,'[]',?,?)
                           ON CONFLICT(reg_no) DO UPDATE SET
                           name=excluded.name, class_name=excluded.class_name""",
                        (reg, name, staff, class_name))
    return {"ok": True, "class_name": class_name, "total": len(people) - skipped,
            "added": added, "already_existed": moved, "skipped": skipped}


# ---------- classes ----------
@app.get("/classes")
def list_classes(staff: str = Depends(current_staff)):
    with db() as con:
        rows = con.execute(f"""SELECT class_name, COUNT(*) AS students,
                               SUM(CASE WHEN {HAS_FACE} THEN 1 ELSE 0 END) AS with_faces
                               FROM students GROUP BY class_name ORDER BY class_name""").fetchall()
        mine = visible_classes(con, staff)
    return [dict(r) for r in rows if mine is None or r["class_name"] in mine]


# ---------- students ----------
@app.post("/students")
async def register_student(name: str = Form(...), reg_no: str = Form(...),
                           class_name: str = Form(...),
                           photo1: UploadFile = File(...),
                           photo2: UploadFile = File(...),
                           photo3: Optional[UploadFile] = File(None),
                           staff: str = Depends(current_staff)):
    class_name = class_name.strip()
    if not class_name:
        raise HTTPException(400, "Class name is required")
    claim_class(staff, class_name)
    with db() as con:
        old = con.execute("SELECT class_name FROM students WHERE reg_no=?", (reg_no,)).fetchone()
        mine = visible_classes(con, staff)
    if old and mine is not None and old["class_name"] not in mine:
        raise HTTPException(403, "This register number belongs to another faculty's class.")
    encs, problems = [], []
    photos = [p for p in (photo1, photo2, photo3) if p is not None and p.filename]
    for f in photos:
        img = np.array(read_image(await f.read(), 1600))
        locs = face_recognition.face_locations(img)
        if len(locs) != 1:
            problems.append(f"{f.filename}: {len(locs)} faces found (need exactly 1)")
            continue
        why = photo_problem(img, locs[0]) if CHECK_QUALITY else None
        if why:
            problems.append(f"{f.filename}: {why}")
            continue
        encs.append(face_recognition.face_encodings(img, locs, num_jitters=5)[0].tolist())
    if len(encs) < 2:
        raise HTTPException(400, {"msg": "Need at least 2 clear front-face photos. Please retake:",
                                  "problems": problems})
    # the same face must not already belong to a different student
    with db() as con:
        others = con.execute(f"""SELECT reg_no, name, class_name, encodings FROM students
                                 WHERE reg_no != ? AND {HAS_FACE}""", (reg_no,)).fetchall()
    for o in others:
        known_o = np.array(json.loads(o["encodings"]))
        for e in encs:
            if float(np.min(face_recognition.face_distance(known_o, np.array(e)))) < DUP_DIST:
                raise HTTPException(409, {
                    "msg": f"This face looks the same as {o['name']} ({o['reg_no']}, class {o['class_name']}), "
                           "who is already registered. Check that these photos belong to the right student.",
                    "problems": []})
    with db() as con:
        con.execute("""INSERT OR REPLACE INTO students(reg_no, name, encodings, added_by, class_name)
                       VALUES(?,?,?,?,?)""", (reg_no, name, json.dumps(encs), staff, class_name))
    return {"ok": True, "name": name, "reg_no": reg_no, "class_name": class_name,
            "photos_used": len(encs), "problems": problems}


@app.get("/students")
def list_students(class_name: Optional[str] = None, staff: str = Depends(current_staff)):
    sql = f"SELECT reg_no, name, class_name, {HAS_FACE} AS has_face FROM students"
    args = ()
    if class_name:
        sql += " WHERE class_name=?"
        args = (class_name,)
    with db() as con:
        rows = con.execute(sql + " ORDER BY class_name, reg_no", args).fetchall()
        mine = visible_classes(con, staff)
    return [{**dict(r), "has_face": bool(r["has_face"])} for r in rows
            if mine is None or r["class_name"] in mine]


@app.delete("/students/{reg_no}/faces")
def delete_faces(reg_no: str, staff: str = Depends(current_staff)):
    """Removes only the stored face data, the student stays in the class so new photos can be added."""
    with db() as con:
        r = con.execute("SELECT class_name FROM students WHERE reg_no=?", (reg_no,)).fetchone()
    if r:
        need_class(staff, r["class_name"])
    with db() as con:
        con.execute("UPDATE students SET encodings='[]' WHERE reg_no=?", (reg_no,))
    return {"ok": True}


@app.delete("/students/{reg_no}")
def delete_student(reg_no: str, staff: str = Depends(current_staff)):
    with db() as con:
        r = con.execute("SELECT class_name FROM students WHERE reg_no=?", (reg_no,)).fetchone()
    if r:
        need_class(staff, r["class_name"])
    with db() as con:
        con.execute("DELETE FROM students WHERE reg_no=?", (reg_no,))
    return {"ok": True}


# ---------- scan ----------
@app.post("/scan")
async def scan(class_name: str = Form(...),
               photo1: UploadFile = File(...),
               photo2: Optional[UploadFile] = File(None),
               photo3: Optional[UploadFile] = File(None),
               photo4: Optional[UploadFile] = File(None),
               photo5: Optional[UploadFile] = File(None),
               photo6: Optional[UploadFile] = File(None),
               photo7: Optional[UploadFile] = File(None),
               photo8: Optional[UploadFile] = File(None),
               staff: str = Depends(current_staff)):
    need_class(staff, class_name)
    photos = [p for p in (photo1, photo2, photo3, photo4, photo5, photo6, photo7, photo8)
              if p is not None and p.filename]
    with db() as con:
        rows = con.execute("SELECT reg_no, name, encodings FROM students WHERE class_name=? ORDER BY reg_no",
                           (class_name,)).fetchall()
    if not rows:
        raise HTTPException(400, f"No students in class '{class_name}'")

    known, owner = [], []          # every stored encoding + which student it belongs to
    for i, r in enumerate(rows):
        for e in json.loads(r["encodings"] or "[]"):
            known.append(e)
            owner.append(i)
    if not known:
        raise HTTPException(400, f"No student in class '{class_name}' has face photos yet. "
                                 "Open the Students tab, tap a student and add photos.")
    has_face = set(owner)
    known, owner = np.array(known), np.array(owner)
    starts = np.r_[0, np.flatnonzero(np.diff(owner)) + 1]   # owner is sorted: first encoding of each student
    ids = owner[starts]

    best = {i: 9.9 for i in range(len(rows))}
    amb = {i: False for i in range(len(rows))}   # True when the best match was too close to another student
    faces, annotated = [], []
    try:
        font = ImageFont.load_default(size=28)
    except TypeError:
        font = ImageFont.load_default()

    for f in photos:
        pil = read_image(await f.read(), 3000)
        img = np.array(pil)
        locs = face_recognition.face_locations(img, number_of_times_to_upsample=2)
        encs = face_recognition.face_encodings(img, locs, num_jitters=2)
        draw = ImageDraw.Draw(pil)
        for (t, r_, b, l), e in zip(locs, encs):
            d_all = face_recognition.face_distance(known, e)
            i, d, j, d2 = two_nearest(d_all, starts, ids)
            if d <= LOOSE:
                close = (d2 - d) < MARGIN          # another student looks almost as similar
                if d < best[i]:
                    best[i], amb[i] = d, close
                status = "present" if (d <= STRICT and not close) else "check"
                label = rows[i]["name"]
                if status == "check":
                    label += " ?"
                    if close and j is not None:
                        label += f" (like {rows[j]['name']})"
                color = "lime" if status == "present" else "orange"
            else:
                status, label, color = "unknown", "Unknown", "red"
            draw.rectangle((l, t, r_, b), outline=color, width=4)
            draw.text((l, b + 4), label, fill=color, font=font)
            faces.append({"photo": f.filename, "status": status, "label": label,
                          "reg_no": rows[i]["reg_no"] if status != "unknown" else None,
                          "distance": round(d, 3)})
        buf = io.BytesIO()
        pil.save(buf, "JPEG", quality=85)
        annotated.append(base64.b64encode(buf.getvalue()).decode())

    summary = []
    for i, r in enumerate(rows):
        d = best[i]
        if i not in has_face:
            status = "no_face"      # no photos registered yet, so the computer cannot judge
        elif d > LOOSE:
            status = "absent"
        elif d <= STRICT and not amb[i]:
            status = "present"
        else:
            status = "check"
        summary.append({"reg_no": r["reg_no"], "name": r["name"], "status": status,
                        "distance": None if d > 9 else round(d, 3)})
    LAST_SCAN[(staff, class_name)] = {x["reg_no"]: x["status"] for x in summary}
    return {"class_name": class_name, "summary": summary, "faces": faces,
            "annotated_images_base64": annotated}


# ---------- scan from a short video ----------
MIN_HITS = 2         # video: a student must be matched sharply in this many frames to be "present"
VIDEO_FRAMES = 10    # how many frames of the video are checked


def video_frames(data: bytes, want: int = VIDEO_FRAMES, max_side: int = 2200):
    """Takes the sharpest frame from each slice of the video, so the whole sweep is covered."""
    import tempfile
    import cv2
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
        fh.write(data)
        path = fh.name
    cands = []
    try:
        cap = cv2.VideoCapture(path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            raise HTTPException(400, "Could not read the video. Please record it again.")
        for k in np.linspace(0, total - 1, min(total, want * 3)).astype(int):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(k))
            ok, fr = cap.read()
            if ok:
                sharp = cv2.Laplacian(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
                cands.append((int(k), sharp, fr))
        cap.release()
    finally:
        os.unlink(path)
    if not cands:
        raise HTTPException(400, "Could not read any frame of the video.")
    out = []
    for chunk in np.array_split(np.arange(len(cands)), min(want, len(cands))):
        _, _, fr = max((cands[i] for i in chunk), key=lambda c: c[1])
        im = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
        im.thumbnail((max_side, max_side))
        out.append(im)
    return out


@app.post("/scan-video")
async def scan_video(class_name: str = Form(...), video: UploadFile = File(...),
                     staff: str = Depends(current_staff)):
    need_class(staff, class_name)
    with db() as con:
        rows = con.execute("SELECT reg_no, name, encodings FROM students WHERE class_name=? ORDER BY reg_no",
                           (class_name,)).fetchall()
    if not rows:
        raise HTTPException(400, f"No students in class '{class_name}'")
    known, owner = [], []
    for i, r in enumerate(rows):
        for e in json.loads(r["encodings"] or "[]"):
            known.append(e)
            owner.append(i)
    if not known:
        raise HTTPException(400, f"No student in class '{class_name}' has face photos yet. "
                                 "Open the Students tab, tap a student and add photos.")
    has_face = set(owner)
    known, owner = np.array(known), np.array(owner)
    starts = np.r_[0, np.flatnonzero(np.diff(owner)) + 1]
    ids = owner[starts]

    frames = video_frames(await video.read())
    need = min(MIN_HITS, len(frames))          # a very short video still works
    best = {i: 9.9 for i in range(len(rows))}
    amb = {i: False for i in range(len(rows))}
    hits = {i: 0 for i in range(len(rows))}    # in how many frames the student was matched surely
    shots = []                                 # (faces found, picture, face list) of every frame
    try:
        font = ImageFont.load_default(size=28)
    except TypeError:
        font = ImageFont.load_default()

    for n, pil in enumerate(frames):
        img = np.array(pil)
        locs = face_recognition.face_locations(img, number_of_times_to_upsample=1)
        encs = face_recognition.face_encodings(img, locs, num_jitters=1)
        draw = ImageDraw.Draw(pil)
        seen, flist = set(), []
        for (t, r_, b, l), e in zip(locs, encs):
            d_all = face_recognition.face_distance(known, e)
            i, d, j, d2 = two_nearest(d_all, starts, ids)
            if d <= LOOSE:
                close = (d2 - d) < MARGIN
                if d < best[i]:
                    best[i], amb[i] = d, close
                sure = d <= STRICT and not close
                if sure and i not in seen:
                    hits[i] += 1
                    seen.add(i)
                status = "present" if sure else "check"
                label = rows[i]["name"] + ("" if sure else " ?")
                color = "lime" if sure else "orange"
            else:
                status, label, color = "unknown", "Unknown", "red"
            draw.rectangle((l, t, r_, b), outline=color, width=4)
            draw.text((l, b + 4), label, fill=color, font=font)
            flist.append({"photo": f"frame {n + 1}", "status": status, "label": label,
                          "reg_no": rows[i]["reg_no"] if status != "unknown" else None,
                          "distance": round(d, 3)})
        shots.append((len(locs), pil, flist))

    summary = []
    for i, r in enumerate(rows):
        d = best[i]
        if i not in has_face:
            status = "no_face"
        elif d > LOOSE:
            status = "absent"
        elif d <= STRICT and not amb[i] and hits[i] >= need:
            status = "present"
        else:
            status = "check"     # matched, but not surely or not in enough frames
        summary.append({"reg_no": r["reg_no"], "name": r["name"], "status": status,
                        "distance": None if d > 9 else round(d, 3), "frames_matched": hits[i]})
    # show the three frames with the most faces, in video order
    top = sorted(sorted(range(len(shots)), key=lambda k: -shots[k][0])[:3])
    annotated = []
    for k in top:
        buf = io.BytesIO()
        shots[k][1].save(buf, "JPEG", quality=85)
        annotated.append(base64.b64encode(buf.getvalue()).decode())
    faces = shots[max(range(len(shots)), key=lambda k: shots[k][0])][2]
    LAST_SCAN[(staff, class_name)] = {x["reg_no"]: x["status"] for x in summary}
    return {"class_name": class_name, "summary": summary, "faces": faces,
            "annotated_images_base64": annotated, "frames_used": len(frames)}


# ---------- saved attendance ----------
STATUSES = ("present", "absent", "od", "leave", "late")     # od = On Duty
PRESENT_SQL = "('present','late','od')"                      # these count as attended


class Record(BaseModel):
    reg_no: str
    name: str
    status: str   # present / absent / od / leave / late


class SaveBody(BaseModel):
    date: str
    class_name: str
    subject: str
    records: List[Record]
    period: Optional[int] = None      # hour 1..8
    replace: bool = False             # replace another subject saved for the same hour


@app.get("/attendance/taken")
def period_taken(date: str, class_name: str, period: int, staff: str = Depends(current_staff)):
    """Which subjects already have attendance for this class in this hour of this day."""
    need_class(staff, class_name)
    with db() as con:
        rows = con.execute("""SELECT DISTINCT subject FROM attendance_v2
                              WHERE date=? AND class_name=? AND period=?""", (date, class_name, period)).fetchall()
    return [r["subject"] for r in rows]


@app.post("/attendance")
def save_attendance(body: SaveBody, staff: str = Depends(current_staff)):
    need_class(staff, body.class_name)
    for r in body.records:
        if r.status not in STATUSES:
            raise HTTPException(400, f"Unknown status '{r.status}'.")
    auto = LAST_SCAN.get((staff, body.class_name), {})   # what the computer chose in the latest scan
    with db() as con:
        if body.period is not None:
            others = con.execute("""SELECT DISTINCT subject FROM attendance_v2
                                    WHERE date=? AND class_name=? AND period=? AND subject != ?""",
                                 (body.date, body.class_name, body.period, body.subject)).fetchall()
            if others and not body.replace:
                raise HTTPException(409, f"Hour {body.period} already has attendance saved "
                                         f"({', '.join(o['subject'] for o in others)}).")
            if others:
                con.execute("DELETE FROM attendance_v2 WHERE date=? AND class_name=? AND period=? AND subject != ?",
                            (body.date, body.class_name, body.period, body.subject))
        for r in body.records:
            con.execute("""INSERT OR REPLACE INTO attendance_v2
                (date, class_name, subject, reg_no, name, status, staff_id, auto_status, period)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                        (body.date, body.class_name, body.subject, r.reg_no, r.name,
                         r.status, staff, auto.get(r.reg_no), body.period))
    return {"ok": True, "saved": len(body.records)}


def can_change(con, staff, date, class_name, subject):
    """The head can change any saved period. A faculty can change only the periods they took."""
    r = con.execute("SELECT role FROM staff WHERE staff_id=?", (staff,)).fetchone()
    if r and r["role"] == "admin":
        return True
    who = con.execute("SELECT staff_id FROM attendance_v2 WHERE date=? AND class_name=? AND subject=? LIMIT 1",
                      (date, class_name, subject)).fetchone()
    return bool(who and who["staff_id"] == staff)


@app.delete("/attendance")
def delete_period(date: str, class_name: str, subject: str, staff: str = Depends(current_staff)):
    with db() as con:
        if not can_change(con, staff, date, class_name, subject):
            raise HTTPException(403, "Only the faculty who took this attendance, or the head, can delete it.")
        n = con.execute("DELETE FROM attendance_v2 WHERE date=? AND class_name=? AND subject=?",
                        (date, class_name, subject)).rowcount
    return {"ok": True, "deleted": n}


@app.delete("/attendance/class")
def delete_class_history(class_name: str, head: str = Depends(admin_staff)):
    with db() as con:
        n = con.execute("DELETE FROM attendance_v2 WHERE class_name=?", (class_name,)).rowcount
    return {"ok": True, "deleted": n}


class EditBody(BaseModel):
    date: str
    class_name: str
    subject: str
    reg_no: str
    status: str   # present / absent / od / leave / late


@app.post("/attendance/edit")
def edit_status(body: EditBody, staff: str = Depends(current_staff)):
    if body.status not in STATUSES:
        raise HTTPException(400, "Unknown status.")
    with db() as con:
        if not can_change(con, staff, body.date, body.class_name, body.subject):
            raise HTTPException(403, "Only the faculty who took this attendance, or the head, can change it.")
        con.execute("""UPDATE attendance_v2 SET status=? WHERE date=? AND class_name=? AND subject=? AND reg_no=?""",
                    (body.status, body.date, body.class_name, body.subject, body.reg_no))
    return {"ok": True}


@app.get("/accuracy")
def accuracy(staff: str = Depends(current_staff)):
    """How often the teacher kept what the computer decided."""
    with db() as con:
        mine = visible_classes(con, staff)
        rows = con.execute("""SELECT auto_status, status, COUNT(*) AS n FROM attendance_v2
            WHERE auto_status IN ('present','absent','check') AND (? = 1 OR staff_id = ?)
            GROUP BY auto_status, status""", (1 if mine is None else 0, staff)).fetchall()
    c = {(r["auto_status"], r["status"]): r["n"] for r in rows}
    pp, pa = c.get(("present", "present"), 0), c.get(("present", "absent"), 0)
    ap, aa = c.get(("absent", "present"), 0), c.get(("absent", "absent"), 0)
    cp = c.get(("check", "present"), 0)
    ca = c.get(("check", "absent"), 0)
    decided = pp + pa + ap + aa
    really_present = pp + ap + cp
    return {
        "decided": decided,
        "correct": pp + aa,
        "accuracy": round(100 * (pp + aa) / decided, 1) if decided else None,
        "found_pct": round(100 * pp / really_present, 1) if really_present else None,
        "right_when_present_pct": round(100 * pp / (pp + pa), 1) if (pp + pa) else None,
        "wrong_present": pa,
        "missed": ap,
        "checks": cp + ca,
    }


@app.get("/attendance")
def attendance_sessions(staff: str = Depends(current_staff)):
    with db() as con:
        rows = con.execute(f"""SELECT date, class_name, subject, COUNT(*) AS total,
            SUM(status IN {PRESENT_SQL}) AS present, MIN(period) AS period FROM attendance_v2
            GROUP BY date, class_name, subject
            ORDER BY date DESC, class_name, subject""").fetchall()
        mine = visible_classes(con, staff)
    return [dict(r) for r in rows if mine is None or r["class_name"] in mine]


@app.get("/attendance/detail")
def attendance_detail(date: str, class_name: str, subject: str, staff: str = Depends(current_staff)):
    need_class(staff, class_name)
    with db() as con:
        rows = con.execute(
            """SELECT reg_no, name, status FROM attendance_v2
               WHERE date=? AND class_name=? AND subject=? ORDER BY reg_no""",
            (date, class_name, subject)).fetchall()
    return [dict(r) for r in rows]


# ---------- which faculty can use a class (head only) ----------
@app.get("/classes/access")
def class_access(class_name: str, head: str = Depends(admin_staff)):
    with db() as con:
        rows = con.execute("SELECT staff_id FROM class_access WHERE class_name=?", (class_name,)).fetchall()
    return [r["staff_id"] for r in rows]


@app.post("/classes/access")
def set_class_access(class_name: str = Form(...), staff_ids: str = Form(""), head: str = Depends(admin_staff)):
    """Sets exactly which faculty can use this class. staff_ids is a comma separated list."""
    ids = [x.strip() for x in staff_ids.split(",") if x.strip()]
    with db() as con:
        con.execute("DELETE FROM class_access WHERE class_name=?", (class_name,))
        for sid in ids:
            con.execute("INSERT OR IGNORE INTO class_access(class_name, staff_id) VALUES(?,?)", (class_name, sid))
    return {"ok": True, "count": len(ids)}


# ---------- class details and Excel downloads ----------
from reports import make_router  # noqa: E402
app.include_router(make_router(db, current_staff, visible_for, claim_class, DB_FILE, "backups"))


# ---------- automatic daily backup ----------
def _backup_loop():
    from reports import backup_today, make_backup
    while True:
        try:
            if not backup_today("backups"):
                make_backup(DB_FILE, "backups")
        except Exception as e:      # a failed backup must never stop the server
            print("Backup failed:", e)
        time.sleep(30 * 60)


@app.on_event("startup")
def start_backups():
    persist.start(DB_FILE)
    threading.Thread(target=_backup_loop, daemon=True).start()


# ---------- the website (Flutter web build in the "web" folder), served last so the API routes win ----------
if os.path.isdir("web"):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory="web", html=True), name="web")
