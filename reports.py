"""
Class details (department / year / section) and Excel downloads.
Used by server.py:  app.include_router(make_router(db, current_staff))
"""
import io, os, re, secrets, shutil, sqlite3, time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Response

PRESENT_LIKE = ("present", "late", "od")   # these count as attended in the percentage
STATUS_TEXT = {"present": "Present", "absent": "Absent", "od": "On Duty", "leave": "Leave", "late": "Late"}
SHORTAGE = 75.0   # students below this percentage go on the shortage sheet
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def session_workbook(info: dict, rows):
    """One class period: who was present and who was absent."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"
    ws.append(["Attendance Report"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Department", info["department"], "Year", info["year"], "Section", info["section"]])
    ws.append(["Class", info["class_name"], "Subject", info["subject"], "Date", info["date"],
               "Hour", info.get("period") or "-"])
    ws.append(["Taken by", info["staff"]])
    ws.append([])
    ws.append(["S.No", "Register No", "Name", "Status"])
    for c in ws[6]:
        c.font = Font(bold=True)
    red = PatternFill("solid", fgColor="FFC7CE")
    present = 0
    for n, r in enumerate(rows, 1):
        ok = r["status"] in PRESENT_LIKE
        present += ok
        ws.append([n, str(r["reg_no"]), r["name"], STATUS_TEXT.get(r["status"], r["status"])])
        if not ok:
            for c in ws[ws.max_row]:
                c.fill = red
    total = len(rows)
    ws.append([])
    ws.append(["Total students", total])
    ws.append(["Present (including Late and On Duty)", present])
    ws.append(["Absent (including Leave)", total - present])
    for key in ("late", "od", "leave"):
        n_key = sum(1 for r in rows if r["status"] == key)
        if n_key:
            ws.append([STATUS_TEXT[key], n_key])
    ws.append(["Percentage", round(100 * present / total, 1) if total else 0])
    for col, w in zip("ABCDEF", (16, 16, 34, 12, 10, 14)):
        ws.column_dimensions[col].width = w
    return wb


def class_workbook(class_name: str, meta: dict, per: dict, sessions: int, subject: Optional[str]):
    """Every saved period of a class: percentage per student and a shortage sheet."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    red = PatternFill("solid", fgColor="FFC7CE")
    entries = []
    for reg, d in sorted(per.items()):
        pct = round(100 * d["present"] / d["total"], 1) if d["total"] else None
        entries.append((reg, d["name"], d["total"], d["present"], d["total"] - d["present"], pct))

    def fill(ws, items):
        ws.append(["Attendance Percentage"])
        ws["A1"].font = Font(bold=True, size=14)
        ws.append(["Department", meta["department"], "Year", meta["year"], "Section", meta["section"]])
        ws.append(["Class", class_name, "Subject", subject or "All subjects", "Classes held", sessions])
        ws.append([])
        ws.append(["S.No", "Register No", "Name", "Classes", "Present", "Absent", "Percentage"])
        for c in ws[5]:
            c.font = Font(bold=True)
        for n, (reg, name, total, pres, ab, pct) in enumerate(items, 1):
            ws.append([n, str(reg), name, total, pres, ab, pct if pct is not None else "-"])
            if pct is not None and pct < SHORTAGE:
                for c in ws[ws.max_row]:
                    c.fill = red
        for col, w in zip("ABCDEFG", (8, 16, 34, 10, 10, 10, 12)):
            ws.column_dimensions[col].width = w

    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance percentage"
    fill(ws, entries)
    short = sorted((e for e in entries if e[5] is not None and e[5] < SHORTAGE), key=lambda e: e[5])
    ws2 = wb.create_sheet(f"Shortage below {int(SHORTAGE)} percent")
    fill(ws2, short)
    if not short:
        ws2.append([])
        ws2.append([f"Nobody is below {int(SHORTAGE)} percent."])
    return wb


def make_backup(db_file, folder="backups", keep=14):
    """Copies the whole database (students, faces, attendance) to a dated file. Keeps the newest `keep` copies."""
    os.makedirs(folder, exist_ok=True)
    name = f"attendance_{datetime.now():%Y-%m-%d_%H%M%S}.db"
    src = sqlite3.connect(db_file)
    dst = sqlite3.connect(os.path.join(folder, name))
    with dst:
        src.backup(dst)           # safe even while the server is working
    src.close()
    dst.close()
    files = sorted(f for f in os.listdir(folder) if re.fullmatch(r"attendance_[\d_-]+\.db", f))
    for old in files[:-keep]:
        os.remove(os.path.join(folder, old))
    return name


def backup_today(folder="backups"):
    today = f"attendance_{datetime.now():%Y-%m-%d}"
    return os.path.isdir(folder) and any(f.startswith(today) for f in os.listdir(folder))


def make_router(db, current_staff, visible=None, claim=None, db_file=None, backup_dir="backups"):
    """visible(staff) -> set of class names the staff may use, or None for all. claim(staff, class) -> checks or grabs a class."""
    router = APIRouter()
    tickets = {}   # download ticket -> (staff, expiry). The browser cannot send a login header for a download.

    def allowed(staff):
        return None if visible is None else visible(staff)

    def need(staff, class_name):
        v = allowed(staff)
        if v is not None and class_name not in v:
            raise HTTPException(403, f"Class {class_name} is not yours. Ask the head to give you access.")

    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS class_access(
            class_name TEXT, staff_id TEXT, PRIMARY KEY(class_name, staff_id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS classes(
            class_name TEXT PRIMARY KEY, department TEXT, year TEXT, section TEXT)""")

    def xlsx(wb, name):
        buf = io.BytesIO()
        wb.save(buf)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        return Response(content=buf.getvalue(), media_type=XLSX,
                        headers={"Content-Disposition": f'attachment; filename="{safe}"'})

    def use_ticket(ticket: str):
        v = tickets.get(ticket)
        if not v or v[1] < time.time():
            raise HTTPException(401, "The download link expired. Please try again.")
        return v[0]

    def meta_of(con, class_name):
        m = con.execute("SELECT department, year, section FROM classes WHERE class_name=?",
                        (class_name,)).fetchone()
        return {"department": (m["department"] if m else "") or "",
                "year": (m["year"] if m else "") or "",
                "section": (m["section"] if m else "") or ""}

    @router.get("/class-info")
    def class_info(staff: str = Depends(current_staff)):
        with db() as con:
            names = {r["class_name"] for r in con.execute("SELECT DISTINCT class_name FROM students")}
            names |= {r["class_name"] for r in con.execute("SELECT class_name FROM classes")}
            v = allowed(staff)
            return [{"class_name": n, **meta_of(con, n)} for n in sorted(names) if v is None or n in v]

    @router.post("/class-info")
    def save_class_info(class_name: str = Form(...), department: str = Form(""),
                        year: str = Form(""), section: str = Form(""),
                        staff: str = Depends(current_staff)):
        if claim is not None:
            claim(staff, class_name.strip())
        with db() as con:
            con.execute("INSERT OR REPLACE INTO classes VALUES(?,?,?,?)",
                        (class_name.strip(), department.strip(), year.strip(), section.strip()))
        return {"ok": True}

    @router.post("/export/ticket")
    def export_ticket(staff: str = Depends(current_staff)):
        now = time.time()
        for k in [k for k, v in tickets.items() if v[1] < now]:
            del tickets[k]
        t = secrets.token_urlsafe(16)
        tickets[t] = (staff, now + 120)
        return {"ticket": t}

    @router.get("/export/session")
    def export_session(date: str, class_name: str, subject: str, ticket: str):
        need(use_ticket(ticket), class_name)
        with db() as con:
            rows = con.execute("""SELECT reg_no, name, status FROM attendance_v2
                                  WHERE date=? AND class_name=? AND subject=? ORDER BY reg_no""",
                               (date, class_name, subject)).fetchall()
            if not rows:
                raise HTTPException(404, "No saved attendance found for this class")
            who = con.execute("""SELECT s.name FROM attendance_v2 a JOIN staff s ON s.staff_id = a.staff_id
                                 WHERE a.date=? AND a.class_name=? AND a.subject=? LIMIT 1""",
                              (date, class_name, subject)).fetchone()
            per = con.execute("SELECT period FROM attendance_v2 WHERE date=? AND class_name=? AND subject=? LIMIT 1",
                              (date, class_name, subject)).fetchone()
            info = {"class_name": class_name, "subject": subject, "date": date,
                    "period": per["period"] if per else None,
                    "staff": who["name"] if who else "", **meta_of(con, class_name)}
        return xlsx(session_workbook(info, rows), f"attendance_{class_name}_{subject}_{date}.xlsx")

    @router.get("/export/class")
    def export_class(class_name: str, ticket: str, subject: Optional[str] = None):
        need(use_ticket(ticket), class_name)
        sql = "SELECT reg_no, name, status, date, subject FROM attendance_v2 WHERE class_name=?"
        args = [class_name]
        if subject:
            sql += " AND subject=?"
            args.append(subject)
        with db() as con:
            rows = con.execute(sql, args).fetchall()
            roster = con.execute("SELECT reg_no, name FROM students WHERE class_name=?", (class_name,)).fetchall()
            meta = meta_of(con, class_name)
        if not rows:
            raise HTTPException(404, "No saved attendance found for this class")
        per = {r["reg_no"]: {"name": r["name"], "total": 0, "present": 0} for r in roster}
        for r in rows:
            d = per.setdefault(r["reg_no"], {"name": r["name"], "total": 0, "present": 0})
            d["total"] += 1
            d["present"] += r["status"] in PRESENT_LIKE
        sessions = len({(r["date"], r["subject"]) for r in rows})
        label = f"_{subject}" if subject else ""
        return xlsx(class_workbook(class_name, meta, per, sessions, subject),
                    f"attendance_report_{class_name}{label}.xlsx")

    @router.delete("/classes/{class_name}")
    def delete_class(class_name: str, with_attendance: bool = False,
                     staff: str = Depends(current_staff)):
        """Deletes a whole class: its students, face data and details (and optionally its saved attendance)."""
        need(staff, class_name)
        with db() as con:
            con.execute("DELETE FROM class_access WHERE class_name=?", (class_name,))
            n = con.execute("SELECT COUNT(*) FROM students WHERE class_name=?", (class_name,)).fetchone()[0]
            con.execute("DELETE FROM students WHERE class_name=?", (class_name,))
            con.execute("DELETE FROM classes WHERE class_name=?", (class_name,))
            if with_attendance:
                con.execute("DELETE FROM attendance_v2 WHERE class_name=?", (class_name,))
        return {"ok": True, "students_deleted": n}

    def percent_rows(con, class_name=None):
        """Attendance percentage of every student (optionally of one class)."""
        sql = "SELECT class_name, reg_no, name, status FROM attendance_v2"
        args = []
        if class_name:
            sql += " WHERE class_name=?"
            args.append(class_name)
        per = {}
        for r in con.execute(sql, args):
            d = per.setdefault((r["class_name"], r["reg_no"]),
                               {"class_name": r["class_name"], "reg_no": r["reg_no"],
                                "name": r["name"], "total": 0, "present": 0})
            d["total"] += 1
            d["present"] += r["status"] in PRESENT_LIKE
        out = list(per.values())
        for d in out:
            d["pct"] = round(100 * d["present"] / d["total"], 1) if d["total"] else None
        return out

    @router.get("/class-summary")
    def class_summary(class_name: str, staff: str = Depends(current_staff)):
        """Everything about one class: student-wise percentage and subject-wise average."""
        need(staff, class_name)
        with db() as con:
            students = percent_rows(con, class_name)
            seen = {s["reg_no"] for s in students}
            for r in con.execute("SELECT reg_no, name FROM students WHERE class_name=?", (class_name,)):
                if r["reg_no"] not in seen:
                    students.append({"class_name": class_name, "reg_no": r["reg_no"], "name": r["name"],
                                     "total": 0, "present": 0, "pct": None})
            rows = con.execute("SELECT date, subject, status FROM attendance_v2 WHERE class_name=?",
                               (class_name,)).fetchall()
        students.sort(key=lambda s: (s["pct"] is None, s["pct"] if s["pct"] is not None else 0))
        subjects = {}
        for r in rows:
            d = subjects.setdefault(r["subject"], {"subject": r["subject"], "periods": set(), "present": 0, "total": 0})
            d["periods"].add(r["date"])
            d["total"] += 1
            d["present"] += r["status"] in PRESENT_LIKE
        subj = [{"subject": d["subject"], "periods": len(d["periods"]),
                 "avg": round(100 * d["present"] / d["total"], 1) if d["total"] else None}
                for d in subjects.values()]
        subj.sort(key=lambda d: d["subject"].lower())
        tot = sum(1 for _ in rows)
        pres = sum(1 for r in rows if r["status"] in PRESENT_LIKE)
        return {"class_name": class_name,
                "periods": len({(r["date"], r["subject"]) for r in rows}),
                "average": round(100 * pres / tot, 1) if tot else None,
                "shortage_limit": SHORTAGE,
                "students": students, "subjects": subj}

    @router.get("/shortage")
    def shortage(staff: str = Depends(current_staff)):
        """Students below the attendance limit in any class, lowest first."""
        v = allowed(staff)
        with db() as con:
            rows = [d for d in percent_rows(con) if d["pct"] is not None and d["pct"] < SHORTAGE
                    and (v is None or d["class_name"] in v)]
        rows.sort(key=lambda d: (d["pct"], d["class_name"]))
        return {"limit": SHORTAGE, "students": rows}

    def head_only(staff):
        if visible is not None and visible(staff) is not None:
            raise HTTPException(403, "Only the head can do this.")

    @router.get("/backups")
    def list_backups(staff: str = Depends(current_staff)):
        head_only(staff)
        out = []
        if os.path.isdir(backup_dir):
            for f in sorted(os.listdir(backup_dir), reverse=True):
                if re.fullmatch(r"attendance_[\d_-]+\.db", f):
                    p = os.path.join(backup_dir, f)
                    out.append({"name": f, "size_kb": round(os.path.getsize(p) / 1024), "time": os.path.getmtime(p)})
        return out

    @router.post("/backups")
    def backup_now(staff: str = Depends(current_staff)):
        head_only(staff)
        if not db_file:
            raise HTTPException(500, "Backup is not set up on the server.")
        return {"ok": True, "name": make_backup(db_file, backup_dir)}

    @router.get("/backups/download")
    def download_backup(name: str, ticket: str):
        head_only(use_ticket(ticket))
        if not re.fullmatch(r"attendance_[\d_-]+\.db", name) or not os.path.exists(os.path.join(backup_dir, name)):
            raise HTTPException(404, "No such backup")
        with open(os.path.join(backup_dir, name), "rb") as fh:
            data = fh.read()
        return Response(content=data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return router
