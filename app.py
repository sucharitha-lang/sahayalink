"""SahayaLink backend (FastAPI + SQLite).

Roles: PATIENT, ASHA, CHO, SUPERVISOR.
Flow: patient requests teleconsult -> CHO schedules doctor -> CHO completes consult ->
handover to ASHA at night -> ASHA follows up next day -> unreachable goes to the other ASHA,
then to the CHO -> supervisor oversees everyone and announces weekly winners.
"""
import os, io, csv, hmac, hashlib, base64, sqlite3, json
from datetime import date, timedelta, datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get('DB_PATH', os.path.join(HERE, 'sahayalink.db'))
SECRET = os.environ.get('SECRET_KEY', 'sahayalink-demo-secret-change-me').encode()
DEMO = os.environ.get('DEMO_MODE', '1') == '1'

if not os.path.exists(DB):
    import seed
    seed.build(DB)

app = FastAPI(title='SahayaLink API')

STEPS = ['medicine', 'review', 'test']
STEP_LABEL = {'consult': 'Teleconsult', 'medicine': 'Medicine pickup', 'review': 'Review visit',
              'test': 'Lab test', 'callback': 'Call back', 'reconnect': 'Reconnect to consult'}
DONE_LABEL = {'medicine': 'Medicine collected', 'review': 'Review visit done', 'test': 'Test done',
              'callback': 'Called back, issue resolved', 'reconnect': 'Reconnected, consult re-booked'}


# ---------------------------------------------------------------- helpers
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def pin_hash(s):
    return hashlib.sha256(s.encode()).hexdigest()


def sim_today(con):
    return con.execute("SELECT value FROM settings WHERE key='sim_date'").fetchone()[0]


def setting(con, key, default=None):
    r = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return r[0] if r else default


def now_ts():
    return datetime.now().isoformat(timespec='seconds')


def make_token(uid):
    payload = f'{uid}.{int(datetime.now().timestamp())}'
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f'{payload}.{sig}'.encode()).decode()


def read_token(tok):
    try:
        uid, ts, sig = base64.urlsafe_b64decode(tok.encode()).decode().split('.')
        good = hmac.new(SECRET, f'{uid}.{ts}'.encode(), hashlib.sha256).hexdigest()[:32]
        return int(uid) if hmac.compare_digest(sig, good) else None
    except Exception:
        return None


def current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Please log in')
    uid = read_token(authorization.split(' ', 1)[1])
    if not uid:
        raise HTTPException(401, 'Session expired, please log in again')
    con = db()
    u = con.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    con.close()
    if not u:
        raise HTTPException(401, 'Unknown user, please log in again')
    return dict(u)


def need(user, *roles):
    if user['role'] not in roles:
        raise HTTPException(403, 'You do not have access to this page')


def notify(con, user_id, text, kind='info'):
    if user_id:
        con.execute('INSERT INTO notifications(user_id,sim_date,ts,text,kind) VALUES(?,?,?,?,?)',
                    (user_id, sim_today(con), now_ts(), text, kind))


def log(con, user_id, action, task_id=None, episode_id=None, patient_id=None, detail=''):
    con.execute('''INSERT INTO action_log(ts,sim_date,user_id,action,task_id,episode_id,patient_id,detail)
        VALUES(?,?,?,?,?,?,?,?)''', (now_ts(), sim_today(con), user_id, action, task_id, episode_id, patient_id, detail))


def award(con, user_id, rule, task_id=None):
    r = con.execute('SELECT points FROM point_rules WHERE rule=?', (rule,)).fetchone()
    if not r:
        return 0
    con.execute('INSERT INTO points_ledger(user_id,points,rule,sim_date,ts,task_id) VALUES(?,?,?,?,?,?)',
                (user_id, r[0], rule, sim_today(con), now_ts(), task_id))
    return r[0]


def name_of(con, uid):
    if not uid:
        return None
    r = con.execute('SELECT name FROM users WHERE id=?', (uid,)).fetchone()
    return r[0] if r else None


def tier(points):
    return 'Gold' if points >= 120 else 'Silver' if points >= 50 else 'Bronze'


def week_window(con):
    d = date.fromisoformat(sim_today(con))
    return (d - timedelta(days=6)).isoformat(), d.isoformat()


def journey(ep):
    """Turn an episode row into an ordered list of journey steps with states."""
    if ep is None:
        return None
    ep = dict(ep)
    cs = ep['consult_status']
    steps = [{'key': 'consult', 'label': 'Teleconsult',
              'state': 'done' if cs == 'completed' else 'current',
              'detail': {'requested': 'Requested', 'scheduled': 'Scheduled', 'completed': 'Done'}.get(cs, cs)}]
    current = None if cs == 'completed' else 'consult'
    if cs != 'completed':
        # follow-up needs are decided by the doctor, so show them as one step until then
        steps.append({'key': 'followup', 'label': 'Follow-up care', 'state': 'pending', 'detail': 'Decided by doctor'})
        steps.append({'key': 'closed', 'label': 'Care complete', 'state': 'pending', 'detail': ''})
        stage = 'Teleconsult requested' if cs == 'requested' else 'Teleconsult scheduled'
        return {'steps': steps, 'current': 'consult', 'stage': stage, 'stage_key': cs}
    for k in STEPS:
        if not ep.get('needs_' + k):
            steps.append({'key': k, 'label': STEP_LABEL[k], 'state': 'na', 'detail': 'Not needed'})
            continue
        if ep.get(k + '_done'):
            st = 'done'
        elif current is None:
            st = 'current'; current = k
        else:
            st = 'pending'
        steps.append({'key': k, 'label': STEP_LABEL[k], 'state': st,
                      'detail': {'done': 'Done', 'current': 'In progress', 'pending': 'Next'}[st]})
    closed = ep['status'] == 'closed'
    steps.append({'key': 'closed', 'label': 'Care complete', 'state': 'done' if closed else 'pending',
                  'detail': 'Done' if closed else ''})
    if closed:
        stage_key, stage = 'closed', 'Care complete'
    elif current == 'consult':
        stage_key = cs
        stage = 'Teleconsult requested' if cs == 'requested' else 'Teleconsult scheduled'
    elif ep.get('handover_status') == 'pending':
        stage_key, stage = 'handover', 'Handover to ASHA tonight'
    else:
        stage_key, stage = current or 'closed', STEP_LABEL.get(current, 'Care complete')
    return {'steps': steps, 'current': current, 'stage': stage, 'stage_key': stage_key}


def episode_full(con, ep_id):
    ep = con.execute('''SELECT e.*, d.name doctor_name, d.specialty, d.mode doctor_mode, h.name hospital_name,
        u.name cho_name FROM episodes e LEFT JOIN doctors d ON d.id=e.doctor_id
        LEFT JOIN hospitals h ON h.id=e.hospital_id LEFT JOIN users u ON u.id=e.cho_id WHERE e.id=?''',
                     (ep_id,)).fetchone()
    if not ep:
        return None
    out = dict(ep)
    out['journey'] = journey(ep)
    return out


def patient_card(con, pid):
    p = con.execute('''SELECT p.*, a.name asha_name, a.phone_last4 asha_phone, b.name alt_asha_name,
        c.name cho_name, c.phone_last4 cho_phone, c.facility cho_facility
        FROM patients p LEFT JOIN users a ON a.id=p.asha_id LEFT JOIN users b ON b.id=p.alt_asha_id
        LEFT JOIN users c ON c.id=p.cho_id WHERE p.id=?''', (pid,)).fetchone()
    return dict(p) if p else None


def active_episode(con, pid):
    """The episode a patient is currently in: the oldest open one, else the latest closed."""
    r = con.execute("SELECT id FROM episodes WHERE patient_id=? AND status='open' ORDER BY id LIMIT 1", (pid,)).fetchone()
    if not r:
        r = con.execute('SELECT id FROM episodes WHERE patient_id=? ORDER BY id DESC LIMIT 1', (pid,)).fetchone()
    return episode_full(con, r[0]) if r else None


def holder_of(con, ep_id):
    """Who currently holds the open task on this episode."""
    r = con.execute('''SELECT u.name, u.role, t.attempt_no FROM tasks t JOIN users u ON u.id=t.assigned_user_id
        WHERE t.episode_id=? AND t.status='open' ORDER BY t.id DESC LIMIT 1''', (ep_id,)).fetchone()
    return dict(r) if r else None


def task_view(con, t):
    t = dict(t)
    today = sim_today(con)
    p = patient_card(con, t['patient_id'])
    t['patient'] = p
    t['episode'] = episode_full(con, t['episode_id']) if t['episode_id'] else active_episode(con, t['patient_id'])
    t['overdue'] = t['due_date'] < today and t['status'] == 'open'
    t['step_label'] = STEP_LABEL.get(t['step'], t['step'])
    t['done_label'] = DONE_LABEL.get(t['step'], 'Done')
    prev = con.execute('''SELECT t.attempt_no, t.status, t.closed_date, u.name FROM tasks t
        JOIN users u ON u.id=t.assigned_user_id WHERE t.patient_id=? AND t.step=? AND t.id<?
        AND IFNULL(t.episode_id,0)=IFNULL(?,0) AND t.status IN ('unreachable','needs_cho','missed') ORDER BY t.id''',
                       (t['patient_id'], t['step'], t['id'], t['episode_id'])).fetchall()
    t['history'] = [dict(x) for x in prev]
    ep = t['episode']
    if t['origin'] == 'patient_request':
        t['origin_label'] = 'Patient requested a call back'
    elif t['origin'] == 'reassign':
        t['origin_label'] = 'Reassigned: previous ASHA could not reach the patient'
    elif t['origin'] == 'escalation':
        t['origin_label'] = 'Escalated to CHO after ASHA attempts'
    elif t['origin'] == 'no_show':
        t['origin_label'] = 'Patient missed the teleconsult, reconnect them'
    else:
        who = ep.get('cho_name') if ep else None
        t['origin_label'] = f'Handover from CHO {who} after teleconsult' if who else 'Handover after teleconsult'
    return t


def close_episode_if_complete(con, ep_id, by_user):
    ep = con.execute('SELECT * FROM episodes WHERE id=?', (ep_id,)).fetchone()
    if not ep or ep['status'] == 'closed' or ep['consult_status'] != 'completed':
        return False
    if all((not ep['needs_' + k]) or ep[k + '_done'] for k in STEPS):
        con.execute("UPDATE episodes SET status='closed', closed_date=? WHERE id=?", (sim_today(con), ep_id))
        p = con.execute('SELECT user_id FROM patients WHERE id=?', (ep['patient_id'],)).fetchone()
        notify(con, p[0], 'Your care journey is complete. Stay healthy, and request a teleconsult any time you need one.', 'done')
        if by_user:
            award(con, by_user, 'journey_closed')
        return True
    return False


# ---------------------------------------------------------------- auth
class Login(BaseModel):
    username: str
    pin: str


@app.post('/api/login')
def login(body: Login):
    con = db()
    u = con.execute('SELECT * FROM users WHERE lower(username)=?', (body.username.strip().lower(),)).fetchone()
    con.close()
    if not u or u['pin_hash'] != pin_hash(body.pin.strip()):
        raise HTTPException(401, 'Wrong username or PIN')
    return {'token': make_token(u['id']), 'role': u['role'], 'name': u['name']}


class DemoLogin(BaseModel):
    user_id: int


@app.get('/api/demo/people')
def demo_people(q: str = ''):
    if not DEMO:
        raise HTTPException(404, 'Demo mode is off')
    con = db(); today = sim_today(con)
    sup = rows(con.execute("SELECT id,name,facility FROM users WHERE role='SUPERVISOR'"))
    chos = rows(con.execute('''SELECT u.id,u.name,u.block,u.district,u.facility,
        (SELECT COUNT(*) FROM episodes e WHERE e.cho_id=u.id AND e.consult_status IN ('requested','scheduled')) waiting
        FROM users u WHERE role='CHO' ORDER BY u.district,u.block'''))
    ashas = rows(con.execute('''SELECT u.id,u.name,u.block,u.district,
        (SELECT GROUP_CONCAT(village,', ') FROM worker_villages w WHERE w.user_id=u.id AND w.is_primary=1) villages,
        (SELECT COUNT(*) FROM tasks t WHERE t.assigned_user_id=u.id AND t.status='open' AND t.due_date<=?) tasks
        FROM users u WHERE role='ASHA' ORDER BY u.district,u.block,u.name''', (today,)))
    if q.strip():
        like = f'%{q.strip().lower()}%'
        pats = con.execute('''SELECT p.id pid,p.user_id id,p.name,p.patient_ref,p.village FROM patients p
            WHERE lower(p.name) LIKE ? OR lower(p.patient_ref) LIKE ? OR lower(p.village) LIKE ? LIMIT 20''',
                           (like, like, like)).fetchall()
    else:
        # the live-demo patient first, then one example patient for each stage of the journey
        pats = []
        r = con.execute('SELECT p.id pid,p.user_id id,p.name,p.patient_ref,p.village FROM patients p WHERE is_demo=1').fetchone()
        if r:
            d = dict(r); d['label'] = 'LIVE DEMO: start here'; pats.append(d)
        picks = [("e.consult_status='requested' AND e.source='patient_request'", 'Requested a teleconsult'),
                 ("e.consult_status='scheduled'", 'Teleconsult scheduled'),
                 ("e.handover_status='pending'", 'Consult done, handover tonight'),
                 ("e.status='open' AND e.consult_status='completed' AND e.medicine_done=0 AND e.handover_status='sent'", 'Medicine pickup pending'),
                 ("e.status='open' AND e.medicine_done=1 AND e.needs_test=1", 'Lab test pending'),
                 ("e.status='closed'", 'Care complete')]
        for cond, label in picks:
            r = con.execute(f'''SELECT p.id pid,p.user_id id,p.name,p.patient_ref,p.village FROM patients p
                JOIN episodes e ON e.patient_id=p.id WHERE {cond} ORDER BY p.risk_pct DESC LIMIT 1''').fetchone()
            if r:
                d = dict(r); d['label'] = label; pats.append(d)
        r = con.execute('''SELECT p.id pid,p.user_id id,p.name,p.patient_ref,p.village FROM patients p
            JOIN tasks t ON t.patient_id=p.id WHERE t.kind='callback' AND t.status='open' LIMIT 1''').fetchone()
        if r:
            d = dict(r); d['label'] = 'Asked for a call back'; pats.append(d)
    pats = [dict(x) for x in pats]
    con.close()
    return {'supervisor': sup, 'chos': chos, 'ashas': ashas, 'patients': pats}


@app.post('/api/demo/login')
def demo_login(body: DemoLogin):
    if not DEMO:
        raise HTTPException(404, 'Demo mode is off')
    con = db(); u = con.execute('SELECT * FROM users WHERE id=?', (body.user_id,)).fetchone(); con.close()
    if not u:
        raise HTTPException(404, 'Person not found')
    return {'token': make_token(u['id']), 'role': u['role'], 'name': u['name']}


@app.get('/api/me')
def me(user=Depends(current_user)):
    con = db(); today = sim_today(con)
    out = {k: user[k] for k in ('id', 'username', 'name', 'role', 'district', 'block', 'village', 'facility')}
    out.update({'sim_date': today, 'start_date': setting(con, 'start_date'), 'demo': DEMO,
                'daily_cap': int(setting(con, 'daily_cap', 12))})
    if user['role'] == 'ASHA':
        out['villages'] = [r[0] for r in con.execute(
            'SELECT village FROM worker_villages WHERE user_id=? AND is_primary=1 ORDER BY village', (user['id'],))]
        out['cover_villages'] = [r[0] for r in con.execute(
            'SELECT village FROM worker_villages WHERE user_id=? AND is_primary=0 ORDER BY village', (user['id'],))]
    if user['role'] == 'PATIENT':
        out['patient_id'] = user['patient_id']
    out['unread'] = con.execute('SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0',
                                (user['id'],)).fetchone()[0]
    con.close()
    return out


@app.get('/api/notifications')
def notifications(user=Depends(current_user)):
    con = db()
    out = rows(con.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 40', (user['id'],)))
    con.execute('UPDATE notifications SET is_read=1 WHERE user_id=?', (user['id'],))
    con.commit(); con.close()
    return {'items': out}


# ---------------------------------------------------------------- patient
def my_patient(con, user):
    need(user, 'PATIENT')
    return patient_card(con, user['patient_id'])


@app.get('/api/patient/home')
def patient_home(user=Depends(current_user)):
    con = db(); p = my_patient(con, user); today = sim_today(con)
    eps = [episode_full(con, r[0]) for r in con.execute(
        'SELECT id FROM episodes WHERE patient_id=? ORDER BY id DESC', (p['id'],))]
    open_cb = con.execute('''SELECT t.*, u.name holder FROM tasks t JOIN users u ON u.id=t.assigned_user_id
        WHERE t.patient_id=? AND t.kind='callback' AND t.status='open' ORDER BY t.id DESC LIMIT 1''', (p['id'],)).fetchone()
    for e in eps:
        h = holder_of(con, e['id'])
        e['holder'] = h
    cats = ['Fever or infection', 'BP or sugar check', 'Medicine refill', 'Breathing problem',
            'Stomach problem', 'Skin problem', 'Test results', 'Other']
    con.close()
    return {'patient': p, 'episodes': eps, 'open_callback': dict(open_cb) if open_cb else None,
            'categories': cats, 'today': today,
            'callback_reasons': ['Medicine pickup centre is too far', 'Medicine not available at PHC',
                                 'Question about my tablets', 'Feeling side effects from medicine',
                                 'Need help to get my test done', 'Other']}


class ConsultReq(BaseModel):
    category: str
    complaint: str
    preferred_slot: str = 'Morning'
    mode: str = 'Video'


@app.post('/api/patient/consult-request')
def patient_consult(body: ConsultReq, user=Depends(current_user)):
    con = db(); p = my_patient(con, user)
    if not body.complaint.strip():
        con.close(); raise HTTPException(400, 'Please describe your problem')
    if con.execute("SELECT 1 FROM episodes WHERE patient_id=? AND consult_status IN ('requested','scheduled')",
                   (p['id'],)).fetchone():
        con.close(); raise HTTPException(409, 'You already have a teleconsult request in progress')
    con.execute('''INSERT INTO episodes(patient_id,source,category,complaint,preferred_slot,mode,consult_status,
        cho_id,created_date) VALUES(?,?,?,?,?,?,?,?,?)''',
                (p['id'], 'patient_request', body.category, body.complaint.strip()[:400], body.preferred_slot,
                 body.mode, 'requested', p['cho_id'], sim_today(con)))
    ep = con.execute('SELECT last_insert_rowid()').fetchone()[0]
    notify(con, p['cho_id'], f'New teleconsult request from {p["name"]} ({p["village"]}): {body.category}.', 'request')
    notify(con, user['id'], f'Request sent to CHO {p["cho_name"]}. You will get a doctor and time slot soon.', 'request')
    log(con, user['id'], 'consult_requested', episode_id=ep, patient_id=p['id'], detail=body.category)
    con.commit(); con.close()
    return {'ok': True, 'episode_id': ep}


class CallbackReq(BaseModel):
    reason: str
    note: str = ''


@app.post('/api/patient/callback')
def patient_callback(body: CallbackReq, user=Depends(current_user)):
    con = db(); p = my_patient(con, user); today = sim_today(con)
    if con.execute("SELECT 1 FROM tasks WHERE patient_id=? AND kind='callback' AND status='open'", (p['id'],)).fetchone():
        con.close(); raise HTTPException(409, 'You already have a call-back request open. Your ASHA will call you.')
    con.execute('''INSERT INTO tasks(episode_id,patient_id,kind,step,assigned_user_id,attempt_no,origin,request_reason,
        request_note,due_date,created_date) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (None, p['id'], 'callback', 'callback', p['asha_id'], 1, 'patient_request', body.reason,
                 body.note.strip()[:300], today, today))
    tid = con.execute('SELECT last_insert_rowid()').fetchone()[0]
    notify(con, p['asha_id'], f'{p["name"]} ({p["village"]}) asked for a call back: {body.reason}.', 'callback')
    notify(con, user['id'], f'Call-back request sent to ASHA {p["asha_name"]}. She will call you today.', 'request')
    log(con, user['id'], 'callback_requested', task_id=tid, patient_id=p['id'], detail=body.reason)
    con.commit(); con.close()
    return {'ok': True}


class SelfReport(BaseModel):
    episode_id: int


@app.post('/api/patient/medicine-collected')
def patient_self_report(body: SelfReport, user=Depends(current_user)):
    con = db(); p = my_patient(con, user)
    ep = con.execute('SELECT * FROM episodes WHERE id=? AND patient_id=?', (body.episode_id, p['id'])).fetchone()
    if not ep or not ep['needs_medicine'] or ep['medicine_done']:
        con.close(); raise HTTPException(400, 'There is no medicine pickup pending')
    con.execute('UPDATE episodes SET medicine_self_reported=1 WHERE id=?', (ep['id'],))
    t = con.execute("SELECT assigned_user_id FROM tasks WHERE episode_id=? AND step='medicine' AND status='open'",
                    (ep['id'],)).fetchone()
    notify(con, t[0] if t else p['asha_id'], f'{p["name"]} says the medicine was collected. Please confirm.', 'info')
    log(con, user['id'], 'medicine_self_reported', episode_id=ep['id'], patient_id=p['id'])
    con.commit(); con.close()
    return {'ok': True}


class Rating(BaseModel):
    episode_id: int
    rating: int
    comment: str = ''


@app.post('/api/patient/rate')
def patient_rate(body: Rating, user=Depends(current_user)):
    con = db(); p = my_patient(con, user)
    ep = con.execute('SELECT * FROM episodes WHERE id=? AND patient_id=?', (body.episode_id, p['id'])).fetchone()
    if not ep or ep['consult_status'] != 'completed':
        con.close(); raise HTTPException(400, 'You can rate only a completed teleconsult')
    if ep['rating']:
        con.close(); raise HTTPException(409, 'You have already rated this teleconsult')
    r = max(1, min(5, body.rating))
    con.execute('UPDATE episodes SET rating=?, rating_comment=? WHERE id=?', (r, body.comment[:300], ep['id']))
    log(con, user['id'], 'consult_rated', episode_id=ep['id'], patient_id=p['id'], detail=str(r))
    con.commit(); con.close()
    return {'ok': True}


class CallIntent(BaseModel):
    to: str


@app.post('/api/patient/call')
def patient_call(body: CallIntent, user=Depends(current_user)):
    con = db(); p = my_patient(con, user)
    who = p['asha_name'] if body.to == 'asha' else p['cho_name']
    log(con, user['id'], 'call_' + body.to, patient_id=p['id'])
    con.commit(); con.close()
    return {'ok': True, 'message': f'Connecting you to {who} through the facility line (numbers stay private).'}


# ---------------------------------------------------------------- shared: tasks
class Outcome(BaseModel):
    outcome: str        # done | unreachable | needs_cho
    note: str = ''


@app.post('/api/task/{task_id}/outcome')
def task_outcome(task_id: int, body: Outcome, user=Depends(current_user)):
    need(user, 'ASHA', 'CHO')
    con = db(); today = sim_today(con)
    t = con.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not t:
        con.close(); raise HTTPException(404, 'Task not found')
    if t['assigned_user_id'] != user['id']:
        con.close(); raise HTTPException(403, 'This task is not on your list')
    if t['status'] != 'open':
        con.close(); raise HTTPException(409, 'This task is already closed')
    p = patient_card(con, t['patient_id'])
    pts = 0
    if body.outcome == 'done':
        con.execute("UPDATE tasks SET status='done', closed_date=?, closed_by=?, outcome_note=? WHERE id=?",
                    (today, user['id'], body.note[:300], task_id))
        if t['step'] in STEPS and t['episode_id']:
            con.execute(f"UPDATE episodes SET {t['step']}_done=1 WHERE id=?", (t['episode_id'],))
            con.execute('''UPDATE tasks SET status='closed_by_other', closed_date=? WHERE episode_id=? AND step=?
                AND status='open' AND id!=?''', (today, t['episode_id'], t['step'], task_id))
        if user['role'] == 'CHO':
            pts += award(con, user['id'], 'escalation_resolved', task_id)
        elif t['kind'] == 'callback':
            pts += award(con, user['id'], 'callback_resolved', task_id)
        elif t['kind'] == 'reconnect':
            pts += award(con, user['id'], 'reconnect_done', task_id)
        else:
            rule = {'Critical': 'followup_critical', 'High': 'followup_high'}.get(p['priority'], 'followup_other')
            pts += award(con, user['id'], rule, task_id)
        if user['role'] == 'ASHA' and p['is_hard']:
            pts += award(con, user['id'], 'hard_bonus', task_id)
        msg = {'medicine': 'confirmed your medicine pickup', 'review': 'confirmed your review visit',
               'test': 'confirmed your lab test', 'callback': 'called you back',
               'reconnect': 'helped you rebook your teleconsult'}.get(t['step'], 'updated your care')
        notify(con, p['user_id'], f'{user["name"]} {msg}. Thank you.', 'done')
        if t['episode_id'] and close_episode_if_complete(con, t['episode_id'], user['id'] if user['role'] == 'ASHA' else None):
            pts += 5 if user['role'] == 'ASHA' else 0
    elif body.outcome == 'unreachable':
        con.execute("UPDATE tasks SET status='unreachable', closed_date=?, closed_by=?, outcome_note=? WHERE id=?",
                    (today, user['id'], body.note[:300], task_id))
        if user['role'] == 'ASHA':
            pts += award(con, user['id'], 'attempt', task_id)
        nxt = 'another ASHA worker' if t['attempt_no'] == 1 else 'your CHO'
        notify(con, p['user_id'], f'{user["name"]} tried to reach you today. {nxt.capitalize()} will contact you tomorrow.', 'info')
    elif body.outcome == 'needs_cho':
        need(user, 'ASHA')
        con.execute("UPDATE tasks SET status='needs_cho', closed_date=?, closed_by=?, outcome_note=? WHERE id=?",
                    (today, user['id'], body.note[:300], task_id))
        pts += award(con, user['id'], 'attempt', task_id)
        notify(con, p['cho_id'], f'{user["name"]} asked for your help with {p["name"]} ({p["village"]}). It will be on your list tomorrow.', 'escalation')
    else:
        con.close(); raise HTTPException(400, 'Unknown outcome')
    log(con, user['id'], 'task_' + body.outcome, task_id=task_id, episode_id=t['episode_id'],
        patient_id=t['patient_id'], detail=t['step'])
    con.commit(); con.close()
    return {'ok': True, 'points': pts}


def task_list(con, user_id, include_closed_today=True):
    today = sim_today(con)
    q = '''SELECT * FROM tasks WHERE assigned_user_id=? AND ((status='open' AND due_date<=?)'''
    if include_closed_today:
        q += " OR (status IN ('done','unreachable','needs_cho') AND closed_date=?)"
    q += ')'
    ts = con.execute(q, (user_id, today, today) if include_closed_today else (user_id, today)).fetchall()
    out = [task_view(con, t) for t in ts]
    kind_rank = {'callback': 0, 'reconnect': 1, 'escalation': 2, 'followup': 3}
    pr = {'Critical': 0, 'High': 1}
    out.sort(key=lambda t: (t['status'] != 'open', not t['patient'].get('is_demo'), kind_rank.get(t['kind'], 9), not t['overdue'],
                            pr.get(t['patient']['priority'], 2), -(t['patient']['risk_pct'] or 0)))
    return out


def points_summary(con, user_id, role):
    ws, we = week_window(con); today = sim_today(con)
    total = con.execute('SELECT COALESCE(SUM(points),0) FROM points_ledger WHERE user_id=?', (user_id,)).fetchone()[0]
    week = con.execute('SELECT COALESCE(SUM(points),0) FROM points_ledger WHERE user_id=? AND sim_date BETWEEN ? AND ?',
                       (user_id, ws, we)).fetchone()[0]
    tday = con.execute('SELECT COALESCE(SUM(points),0) FROM points_ledger WHERE user_id=? AND sim_date=?',
                       (user_id, today)).fetchone()[0]
    ranks = con.execute('''SELECT u.id, COALESCE(SUM(p.points),0) s FROM users u LEFT JOIN points_ledger p
        ON p.user_id=u.id AND p.sim_date BETWEEN ? AND ? WHERE u.role=? GROUP BY u.id ORDER BY s DESC''',
                        (ws, we, role)).fetchall()
    rank = next((i + 1 for i, r in enumerate(ranks) if r[0] == user_id), None)
    awards = rows(con.execute('SELECT * FROM awards WHERE user_id=? ORDER BY id DESC', (user_id,)))
    return {'total': total, 'week': week, 'today': tday, 'tier': tier(total), 'rank': rank,
            'of': len(ranks), 'awards': awards}


# ---------------------------------------------------------------- ASHA
@app.get('/api/asha/today')
def asha_today(user=Depends(current_user)):
    need(user, 'ASHA')
    con = db(); today = sim_today(con)
    tasks = task_list(con, user['id'])
    open_n = sum(1 for t in tasks if t['status'] == 'open')
    out = {'date': today, 'tasks': tasks, 'cap': int(setting(con, 'daily_cap', 12)),
           'counts': {'open': open_n,
                      'done': sum(1 for t in tasks if t['status'] == 'done'),
                      'unreachable': sum(1 for t in tasks if t['status'] in ('unreachable', 'needs_cho')),
                      'callbacks': sum(1 for t in tasks if t['status'] == 'open' and t['kind'] == 'callback'),
                      'overdue': sum(1 for t in tasks if t['overdue'])},
           'points': points_summary(con, user['id'], 'ASHA'),
           'rules': rows(con.execute("SELECT * FROM point_rules WHERE role IN ('ASHA','ANY') ORDER BY points DESC"))}
    con.close()
    return out


@app.get('/api/asha/patients')
def asha_patients(user=Depends(current_user)):
    need(user, 'ASHA')
    con = db()
    ps = con.execute('SELECT id FROM patients WHERE asha_id=? ORDER BY is_demo DESC, risk_pct DESC', (user['id'],)).fetchall()
    out = []
    for (pid,) in ps:
        p = patient_card(con, pid)
        ep = active_episode(con, pid)
        p['episode'] = ep
        p['holder'] = holder_of(con, ep['id']) if ep else None
        out.append(p)
    con.close()
    return {'patients': out}


@app.get('/api/patient-detail/{pid}')
def patient_detail(pid: int, user=Depends(current_user)):
    con = db()
    p = patient_card(con, pid)
    if not p:
        con.close(); raise HTTPException(404, 'Patient not found')
    allowed = (user['role'] == 'SUPERVISOR' or (user['role'] == 'PATIENT' and user['patient_id'] == pid)
               or (user['role'] == 'ASHA' and user['id'] in (p['asha_id'], p['alt_asha_id']))
               or (user['role'] == 'CHO' and user['id'] == p['cho_id']))
    if not allowed:
        con.close(); raise HTTPException(403, 'This patient is outside your area')
    eps = [episode_full(con, r[0]) for r in con.execute('SELECT id FROM episodes WHERE patient_id=? ORDER BY id DESC', (pid,))]
    hist = rows(con.execute('''SELECT t.*, u.name worker, u.role worker_role FROM tasks t JOIN users u ON u.id=t.assigned_user_id
        WHERE t.patient_id=? ORDER BY t.id DESC''', (pid,)))
    con.close()
    return {'patient': p, 'episodes': eps, 'tasks': hist}


# ---------------------------------------------------------------- CHO
@app.get('/api/network')
def network(user=Depends(current_user)):
    con = db()
    hosp = rows(con.execute('SELECT * FROM hospitals ORDER BY district, name'))
    docs = rows(con.execute('''SELECT d.*, (SELECT COUNT(*) FROM episodes e WHERE e.doctor_id=d.id AND
        e.consult_status='completed') consults, (SELECT ROUND(AVG(rating),1) FROM episodes e WHERE e.doctor_id=d.id
        AND rating IS NOT NULL) rating FROM doctors d ORDER BY hospital_id, name'''))
    con.close()
    return {'hospitals': hosp, 'doctors': docs}


@app.get('/api/cho/requests')
def cho_requests(user=Depends(current_user)):
    need(user, 'CHO')
    con = db()
    eps = [episode_full(con, r[0]) for r in con.execute(
        '''SELECT e.id FROM episodes e JOIN patients p ON p.id=e.patient_id WHERE e.cho_id=? AND
        e.consult_status IN ('requested','scheduled') ORDER BY e.consult_status, p.is_demo DESC, CASE p.priority WHEN 'Critical' THEN 0
        ELSE 1 END, e.created_date, p.risk_pct DESC''', (user['id'],))]
    for e in eps:
        e['patient'] = patient_card(con, e['patient_id'])
    con.close()
    return {'requested': [e for e in eps if e['consult_status'] == 'requested'],
            'scheduled': [e for e in eps if e['consult_status'] == 'scheduled']}


class Schedule(BaseModel):
    episode_id: int
    doctor_id: int
    slot: str


@app.post('/api/cho/schedule')
def cho_schedule(body: Schedule, user=Depends(current_user)):
    need(user, 'CHO')
    con = db()
    ep = con.execute('SELECT * FROM episodes WHERE id=? AND cho_id=?', (body.episode_id, user['id'])).fetchone()
    if not ep or ep['consult_status'] != 'requested':
        con.close(); raise HTTPException(409, 'This request is not waiting to be scheduled')
    d = con.execute('SELECT d.*, h.name hname FROM doctors d JOIN hospitals h ON h.id=d.hospital_id WHERE d.id=?',
                    (body.doctor_id,)).fetchone()
    if not d:
        con.close(); raise HTTPException(400, 'Choose a doctor')
    slot = f'{sim_today(con)} {body.slot}'
    con.execute("UPDATE episodes SET consult_status='scheduled', doctor_id=?, hospital_id=?, slot=? WHERE id=?",
                (d['id'], d['hospital_id'], slot, ep['id']))
    p = patient_card(con, ep['patient_id'])
    notify(con, p['user_id'], f'Teleconsult booked with {d["name"]} ({d["specialty"]}, {d["hname"]}) today at {body.slot}, by {d["mode"].lower()} call.', 'appointment')
    log(con, user['id'], 'consult_scheduled', episode_id=ep['id'], patient_id=p['id'], detail=d['name'])
    con.commit(); con.close()
    return {'ok': True}


class Complete(BaseModel):
    episode_id: int
    needs_medicine: bool = True
    needs_review: bool = False
    needs_test: bool = False
    notes: str = ''


@app.post('/api/cho/complete')
def cho_complete(body: Complete, user=Depends(current_user)):
    need(user, 'CHO')
    con = db(); today = sim_today(con)
    ep = con.execute('SELECT * FROM episodes WHERE id=? AND cho_id=?', (body.episode_id, user['id'])).fetchone()
    if not ep or ep['consult_status'] != 'scheduled':
        con.close(); raise HTTPException(409, 'Schedule a doctor before completing the teleconsult')
    any_need = body.needs_medicine or body.needs_review or body.needs_test
    con.execute('''UPDATE episodes SET consult_status='completed', consult_date=?, cho_notes=?, needs_medicine=?,
        needs_review=?, needs_test=?, handover_status=?, status=?, closed_date=? WHERE id=?''',
                (today, body.notes[:400], int(body.needs_medicine), int(body.needs_review), int(body.needs_test),
                 'pending' if any_need else 'na', 'open' if any_need else 'closed', None if any_need else today, ep['id']))
    pts = award(con, user['id'], 'consult_completed')
    p = patient_card(con, ep['patient_id'])
    dn = con.execute('SELECT name FROM doctors WHERE id=?', (ep['doctor_id'],)).fetchone()[0]
    if any_need:
        nxt = [STEP_LABEL[k].lower() for k in STEPS if getattr(body, 'needs_' + k)]
        notify(con, p['user_id'], f'Your teleconsult with {dn} is complete. Next: {", ".join(nxt)}. ASHA {p["asha_name"]} will contact you tomorrow.', 'done')
    else:
        notify(con, p['user_id'], f'Your teleconsult with {dn} is complete. No further steps needed.', 'done')
    log(con, user['id'], 'consult_completed', episode_id=ep['id'], patient_id=p['id'], detail=dn)
    con.commit(); con.close()
    return {'ok': True, 'points': pts, 'handover_to': p['asha_name'] if any_need else None}


class NoShow(BaseModel):
    episode_id: int


@app.post('/api/cho/no-show')
def cho_no_show(body: NoShow, user=Depends(current_user)):
    need(user, 'CHO')
    con = db(); today = sim_today(con)
    ep = con.execute('SELECT * FROM episodes WHERE id=? AND cho_id=?', (body.episode_id, user['id'])).fetchone()
    if not ep or ep['consult_status'] != 'scheduled':
        con.close(); raise HTTPException(409, 'Only a scheduled teleconsult can be marked as missed')
    con.execute("UPDATE episodes SET consult_status='requested', slot=NULL WHERE id=?", (ep['id'],))
    p = patient_card(con, ep['patient_id'])
    if not con.execute("SELECT 1 FROM tasks WHERE episode_id=? AND kind='reconnect' AND status='open'", (ep['id'],)).fetchone():
        con.execute('''INSERT INTO tasks(episode_id,patient_id,kind,step,assigned_user_id,attempt_no,origin,due_date,
            created_date) VALUES(?,?,?,?,?,?,?,?,?)''',
                    (ep['id'], p['id'], 'reconnect', 'reconnect', p['asha_id'], 1, 'no_show', today, today))
    notify(con, p['asha_id'], f'{p["name"]} missed the teleconsult today. Please reach them so the CHO can rebook.', 'callback')
    notify(con, p['user_id'], 'You missed your teleconsult today. Your ASHA will help you rebook it.', 'info')
    log(con, user['id'], 'consult_no_show', episode_id=ep['id'], patient_id=p['id'])
    con.commit(); con.close()
    return {'ok': True}


@app.get('/api/cho/handover')
def cho_handover(user=Depends(current_user)):
    need(user, 'CHO')
    con = db(); today = sim_today(con)
    eps = [episode_full(con, r[0]) for r in con.execute(
        "SELECT id FROM episodes WHERE cho_id=? AND handover_status='pending' AND status='open'", (user['id'],))]
    for e in eps:
        e['patient'] = patient_card(con, e['patient_id'])
    sent_today = con.execute('''SELECT COUNT(*) FROM action_log WHERE user_id=? AND action='handover_sent' AND sim_date=?''',
                             (user['id'], today)).fetchone()[0]
    con.close()
    return {'pending': eps, 'sent_today': sent_today}


@app.post('/api/cho/send-handover')
def cho_send_handover(user=Depends(current_user)):
    need(user, 'CHO')
    con = db()
    eps = con.execute("SELECT * FROM episodes WHERE cho_id=? AND handover_status='pending' AND status='open'",
                      (user['id'],)).fetchall()
    per = {}
    for e in eps:
        con.execute("UPDATE episodes SET handover_status='sent' WHERE id=?", (e['id'],))
        p = patient_card(con, e['patient_id'])
        per.setdefault(p['asha_id'], []).append(p['name'])
        log(con, user['id'], 'handover_sent', episode_id=e['id'], patient_id=p['id'])
    for a, names in per.items():
        notify(con, a, f'CHO {user["name"]} handed over {len(names)} patient(s) for follow-up tomorrow: {", ".join(names)}.', 'handover')
    con.commit(); con.close()
    return {'ok': True, 'sent': len(eps), 'ashas': len(per)}


@app.get('/api/cho/treated')
def cho_treated(user=Depends(current_user)):
    need(user, 'CHO')
    con = db()
    eps = [episode_full(con, r[0]) for r in con.execute(
        '''SELECT e.id FROM episodes e JOIN patients p ON p.id=e.patient_id WHERE e.cho_id=? AND e.consult_status='completed'
        ORDER BY p.is_demo DESC, e.status, e.consult_date DESC''', (user['id'],))]
    for e in eps:
        e['patient'] = patient_card(con, e['patient_id'])
        e['holder'] = holder_of(con, e['id'])
    con.close()
    return {'episodes': eps}


@app.get('/api/cho/escalations')
def cho_escalations(user=Depends(current_user)):
    need(user, 'CHO')
    con = db()
    out = task_list(con, user['id'])
    con.close()
    return {'tasks': out}


@app.get('/api/cho/summary')
def cho_summary(user=Depends(current_user)):
    need(user, 'CHO')
    con = db(); today = sim_today(con); uid = user['id']
    q = lambda s, *a: con.execute(s, a).fetchone()[0]
    out = {'requested': q("SELECT COUNT(*) FROM episodes WHERE cho_id=? AND consult_status='requested'", uid),
           'scheduled': q("SELECT COUNT(*) FROM episodes WHERE cho_id=? AND consult_status='scheduled'", uid),
           'done_today': q("SELECT COUNT(*) FROM episodes WHERE cho_id=? AND consult_status='completed' AND consult_date=?", uid, today),
           'handover': q("SELECT COUNT(*) FROM episodes WHERE cho_id=? AND handover_status='pending' AND status='open'", uid),
           'escalations': q("SELECT COUNT(*) FROM tasks WHERE assigned_user_id=? AND status='open' AND due_date<=?", uid, today),
           'treated_open': q("SELECT COUNT(*) FROM episodes WHERE cho_id=? AND consult_status='completed' AND status='open'", uid),
           'rating': q("SELECT ROUND(AVG(rating),1) FROM episodes WHERE cho_id=? AND rating IS NOT NULL", uid),
           'points': points_summary(con, uid, 'CHO'), 'date': today}
    con.close()
    return out


# ---------------------------------------------------------------- night handover (the daily engine)
def run_night(con, actor_id):
    today = sim_today(con)
    nd = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    s = {'handovers_sent': 0, 'new_followups': 0, 'reassigned_to_other_asha': 0, 'escalated_to_cho': 0,
         'carried_over': 0, 'journeys_closed': 0, 'from_date': today, 'to_date': nd}

    # 1. CHO handovers still pending are sent automatically, so nothing is forgotten
    for e in con.execute("SELECT * FROM episodes WHERE handover_status='pending' AND status='open'").fetchall():
        con.execute("UPDATE episodes SET handover_status='sent' WHERE id=?", (e['id'],))
        s['handovers_sent'] += 1

    # 2a. a task nobody acted on for two days is treated as a missed attempt, so no patient stays stuck
    stale_before = today   # due yesterday or earlier and still open at the end of today = two untouched days
    s['auto_moved_untouched'] = con.execute(
        "UPDATE tasks SET status='missed', closed_date=?, outcome_note='Not acted on for two days' WHERE status='open' AND due_date<?",
        (today, stale_before)).rowcount

    # 2b. failed attempts move up the ladder: primary ASHA -> other ASHA -> CHO
    failed = con.execute("SELECT * FROM tasks WHERE status IN ('unreachable','needs_cho','missed') AND successor_created=0").fetchall()
    for t in failed:
        con.execute('UPDATE tasks SET successor_created=1 WHERE id=?', (t['id'],))
        if t['episode_id'] and t['step'] in STEPS:
            ep = con.execute('SELECT * FROM episodes WHERE id=?', (t['episode_id'],)).fetchone()
            if ep['status'] == 'closed' or ep[t['step'] + '_done']:
                continue
        p = con.execute('SELECT * FROM patients WHERE id=?', (t['patient_id'],)).fetchone()
        att = t['attempt_no'] + 1
        if t['status'] == 'needs_cho' or att >= 3:
            assignee, kind, origin, att = p['cho_id'], 'escalation', 'escalation', max(att, 3)
            s['escalated_to_cho'] += 1
        else:
            assignee = p['alt_asha_id'] or p['asha_id']
            kind, origin = t['kind'], 'reassign'
            s['reassigned_to_other_asha'] += 1
        con.execute('''INSERT INTO tasks(episode_id,patient_id,kind,step,assigned_user_id,attempt_no,origin,request_reason,
            request_note,due_date,created_date) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (t['episode_id'], t['patient_id'], kind, t['step'], assignee, att, origin, t['request_reason'],
                     t['request_note'], nd, today))

    # 3. every open journey must have someone working on its current step
    for e in con.execute("SELECT * FROM episodes WHERE status='open' AND consult_status='completed' AND handover_status='sent'").fetchall():
        j = journey(e)
        cur = j['current']
        if cur not in STEPS:
            if cur is None and close_episode_if_complete(con, e['id'], None):
                s['journeys_closed'] += 1
            continue
        busy = con.execute('''SELECT 1 FROM tasks WHERE episode_id=? AND step=? AND (status='open' OR
            (status IN ('unreachable','needs_cho','missed') AND successor_created=0))''', (e['id'], cur)).fetchone()
        if busy:
            continue
        p = con.execute('SELECT * FROM patients WHERE id=?', (e['patient_id'],)).fetchone()
        con.execute('''INSERT INTO tasks(episode_id,patient_id,kind,step,assigned_user_id,attempt_no,origin,due_date,
            created_date) VALUES(?,?,?,?,?,?,?,?,?)''',
                    (e['id'], e['patient_id'], 'followup', cur, p['asha_id'], 1, 'handover', nd, today))
        s['new_followups'] += 1

    s['carried_over'] = con.execute("SELECT COUNT(*) FROM tasks WHERE status='open' AND due_date<=?", (today,)).fetchone()[0]

    # 4. a new day begins
    con.execute("UPDATE settings SET value=? WHERE key='sim_date'", (nd,))
    for (u, nm) in con.execute("SELECT id, name FROM users WHERE role IN ('ASHA','CHO')").fetchall():
        n = con.execute("SELECT COUNT(*) FROM tasks WHERE assigned_user_id=? AND status='open' AND due_date<=?", (u, nd)).fetchone()[0]
        if n:
            con.execute('INSERT INTO notifications(user_id,sim_date,ts,text,kind) VALUES(?,?,?,?,?)',
                        (u, nd, now_ts(), f'Good morning {nm.split()[0]}. You have {n} follow-up task(s) today.', 'info'))
    for t in con.execute("SELECT DISTINCT patient_id, assigned_user_id FROM tasks WHERE status='open' AND due_date=? AND created_date=?", (nd, today)).fetchall():
        p = con.execute('SELECT user_id FROM patients WHERE id=?', (t[0],)).fetchone()
        w = con.execute('SELECT name, role FROM users WHERE id=?', (t[1],)).fetchone()
        con.execute('INSERT INTO notifications(user_id,sim_date,ts,text,kind) VALUES(?,?,?,?,?)',
                    (p[0], nd, now_ts(), f'{"ASHA" if w[1] == "ASHA" else "CHO"} {w[0]} will contact you today about your care.', 'info'))
    con.execute('''INSERT INTO action_log(ts,sim_date,user_id,action,detail) VALUES(?,?,?,?,?)''',
                (now_ts(), today, actor_id, 'night_handover', json.dumps(s)))
    return s


@app.post('/api/sup/night')
def sup_night(user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    con = db()
    s = run_night(con, user['id'])
    con.commit(); con.close()
    return s


# ---------------------------------------------------------------- supervisor
@app.get('/api/sup/overview')
def sup_overview(user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    con = db(); today = sim_today(con); cap = int(setting(con, 'daily_cap', 12))
    q = lambda s, *a: con.execute(s, a).fetchone()[0]
    k = {
        'patients': q('SELECT COUNT(*) FROM patients'),
        'active': q("SELECT COUNT(*) FROM episodes WHERE status='open'"),
        'requested': q("SELECT COUNT(*) FROM episodes WHERE consult_status='requested'"),
        'scheduled': q("SELECT COUNT(*) FROM episodes WHERE consult_status='scheduled'"),
        'consults_today': q("SELECT COUNT(*) FROM episodes WHERE consult_status='completed' AND consult_date=?", today),
        'handover': q("SELECT COUNT(*) FROM episodes WHERE handover_status='pending' AND status='open'"),
        'tasks_open': q("SELECT COUNT(*) FROM tasks WHERE status='open' AND due_date<=?", today),
        'tasks_done': q("SELECT COUNT(*) FROM tasks WHERE status='done' AND closed_date=?", today),
        'unreachable': q("SELECT COUNT(*) FROM tasks WHERE status IN ('unreachable','needs_cho') AND closed_date=?", today),
        'callbacks': q("SELECT COUNT(*) FROM tasks WHERE kind='callback' AND status='open'"),
        'escalations': q("SELECT COUNT(*) FROM tasks WHERE kind='escalation' AND status='open'"),
        'closed': q("SELECT COUNT(*) FROM episodes WHERE status='closed'"),
        'rating': q('SELECT ROUND(AVG(rating),1) FROM episodes WHERE rating IS NOT NULL'),
    }
    acted = k['tasks_done'] + k['unreachable']
    k['completion_today'] = round(100 * k['tasks_done'] / (k['tasks_open'] + acted)) if (k['tasks_open'] + acted) else 0
    funnel = {}
    for e in con.execute('SELECT * FROM episodes').fetchall():
        sk = journey(e)['stage_key']
        funnel[sk] = funnel.get(sk, 0) + 1
    order = [('requested', 'Teleconsult requested'), ('scheduled', 'Teleconsult scheduled'),
             ('handover', 'Handover to ASHA tonight'), ('medicine', 'Medicine pickup'),
             ('review', 'Review visit'), ('test', 'Lab test'), ('closed', 'Care complete')]
    districts = rows(con.execute('''SELECT p.district, COUNT(*) patients, SUM(e.status='open') active,
        SUM(e.status='closed') closed, SUM(e.consult_status IN ('requested','scheduled')) waiting
        FROM episodes e JOIN patients p ON p.id=e.patient_id GROUP BY p.district'''))
    alerts = []
    for r in con.execute('''SELECT u.id, u.name, u.block, COUNT(t.id) n FROM users u JOIN tasks t ON t.assigned_user_id=u.id
        AND t.status='open' AND t.due_date<=? WHERE u.role='ASHA' GROUP BY u.id HAVING n>? ORDER BY n DESC''', (today, cap)):
        alerts.append({'level': 'bad', 'text': f'{r["name"]} ({r["block"]}) has {r["n"]} tasks today, above the cap of {cap}. Consider moving a village.', 'user_id': r['id']})
    n = q("SELECT COUNT(*) FROM tasks WHERE kind='callback' AND status='open' AND created_date<?", today)
    if n:
        alerts.append({'level': 'bad', 'text': f'{n} patient call-back request(s) waiting since before today.'})
    n = q("SELECT COUNT(*) FROM episodes WHERE consult_status='requested' AND created_date<?", today)
    if n:
        alerts.append({'level': 'bad', 'text': f'{n} teleconsult request(s) waiting more than a day for a CHO slot.'})
    n = q("SELECT COUNT(*) FROM tasks WHERE status='open' AND attempt_no>=2")
    if n:
        alerts.append({'level': 'info', 'text': f'{n} patient(s) are on a second or third contact attempt.'})
    n = q("SELECT COUNT(*) FROM tasks WHERE status='open' AND due_date<?", today)
    if n:
        alerts.append({'level': 'bad', 'text': f'{n} task(s) are overdue from previous days.'})
    feed = rows(con.execute('''SELECT a.ts, a.sim_date, a.action, a.detail, u.name, u.role, p.name patient
        FROM action_log a LEFT JOIN users u ON u.id=a.user_id LEFT JOIN patients p ON p.id=a.patient_id
        WHERE a.action!='night_handover' ORDER BY a.id DESC LIMIT 15'''))
    nights = rows(con.execute("SELECT sim_date, detail FROM action_log WHERE action='night_handover' ORDER BY id DESC LIMIT 5"))
    con.close()
    return {'date': today, 'kpi': k, 'funnel': [{'key': a, 'label': b, 'n': funnel.get(a, 0)} for a, b in order],
            'districts': districts, 'alerts': alerts, 'feed': feed,
            'nights': [{'date': x['sim_date'], **json.loads(x['detail'])} for x in nights]}


@app.get('/api/sup/ashas')
def sup_ashas(user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    con = db(); today = sim_today(con); ws, we = week_window(con); cap = int(setting(con, 'daily_cap', 12))
    out = rows(con.execute('''SELECT u.id, u.name, u.username, u.block, u.district,
        (SELECT GROUP_CONCAT(village,', ') FROM worker_villages w WHERE w.user_id=u.id AND w.is_primary=1) villages,
        (SELECT COUNT(*) FROM patients p WHERE p.asha_id=u.id) panel,
        (SELECT COUNT(*) FROM tasks t WHERE t.assigned_user_id=u.id AND t.status='open' AND t.due_date<=?) open_tasks,
        (SELECT COUNT(*) FROM tasks t WHERE t.assigned_user_id=u.id AND t.status='done' AND t.closed_date=?) done_today,
        (SELECT COUNT(*) FROM tasks t WHERE t.assigned_user_id=u.id AND t.status IN ('unreachable','needs_cho') AND t.closed_date=?) unreachable_today,
        (SELECT COUNT(*) FROM tasks t WHERE t.assigned_user_id=u.id AND t.kind='callback' AND t.status='open') callbacks,
        (SELECT COUNT(*) FROM tasks t WHERE t.assigned_user_id=u.id AND t.kind='callback' AND t.status='done') callbacks_resolved,
        (SELECT COUNT(*) FROM tasks t WHERE t.assigned_user_id=u.id AND t.status='open' AND t.due_date<?) overdue,
        (SELECT COALESCE(SUM(points),0) FROM points_ledger l WHERE l.user_id=u.id AND l.sim_date BETWEEN ? AND ?) week_pts,
        (SELECT COALESCE(SUM(points),0) FROM points_ledger l WHERE l.user_id=u.id) total_pts
        FROM users u WHERE u.role='ASHA' ORDER BY u.district, u.block, u.name''', (today, today, today, today, ws, we)))
    for a in out:
        a['tier'] = tier(a['total_pts']); a['cap'] = cap
    con.close()
    return {'ashas': out, 'cap': cap}


@app.get('/api/sup/chos')
def sup_chos(user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    con = db(); today = sim_today(con); ws, we = week_window(con)
    out = rows(con.execute('''SELECT u.id, u.name, u.username, u.block, u.district, u.facility,
        (SELECT COUNT(*) FROM episodes e WHERE e.cho_id=u.id AND e.consult_status='requested') requested,
        (SELECT COUNT(*) FROM episodes e WHERE e.cho_id=u.id AND e.consult_status='scheduled') scheduled,
        (SELECT COUNT(*) FROM episodes e WHERE e.cho_id=u.id AND e.consult_status='completed' AND e.consult_date=?) done_today,
        (SELECT COUNT(*) FROM episodes e WHERE e.cho_id=u.id AND e.consult_status='completed') done_total,
        (SELECT COUNT(*) FROM episodes e WHERE e.cho_id=u.id AND e.handover_status='pending' AND e.status='open') handover,
        (SELECT COUNT(*) FROM tasks t WHERE t.assigned_user_id=u.id AND t.status='open') escalations,
        (SELECT ROUND(AVG(rating),1) FROM episodes e WHERE e.cho_id=u.id AND rating IS NOT NULL) rating,
        (SELECT COALESCE(SUM(points),0) FROM points_ledger l WHERE l.user_id=u.id AND l.sim_date BETWEEN ? AND ?) week_pts
        FROM users u WHERE u.role='CHO' ORDER BY u.district, u.block''', (today, ws, we)))
    con.close()
    return {'chos': out}


@app.get('/api/sup/patients')
def sup_patients(q: str = '', stage: str = '', user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    con = db()
    sql = 'SELECT id FROM patients'
    args = ()
    if q.strip():
        like = f'%{q.strip().lower()}%'
        sql += ' WHERE lower(name) LIKE ? OR lower(patient_ref) LIKE ? OR lower(village) LIKE ?'
        args = (like, like, like)
    sql += ' ORDER BY is_demo DESC, risk_pct DESC'
    out = []
    for (pid,) in con.execute(sql, args).fetchall():
        p = patient_card(con, pid)
        ep = active_episode(con, pid)
        sk = ep['journey']['stage_key'] if ep else 'none'
        if stage and sk != stage:
            continue
        p['episode'] = ep
        p['holder'] = holder_of(con, ep['id']) if ep else None
        out.append(p)
        if len(out) >= 120:
            break
    con.close()
    return {'patients': out}


@app.get('/api/sup/rewards')
def sup_rewards(user=Depends(current_user)):
    need(user, 'SUPERVISOR', 'ASHA', 'CHO')
    con = db(); ws, we = week_window(con)

    def board(role):
        return rows(con.execute('''SELECT u.id, u.name, u.block, COALESCE(SUM(l.points),0) pts,
            SUM(l.rule='callback_resolved') callbacks, SUM(l.rule='hard_bonus') hard,
            SUM(l.rule IN ('consult_completed')) consults
            FROM users u LEFT JOIN points_ledger l ON l.user_id=u.id AND l.sim_date BETWEEN ? AND ?
            WHERE u.role=? GROUP BY u.id ORDER BY pts DESC''', (ws, we, role)))
    out = {'week_start': ws, 'week_end': we, 'asha': board('ASHA'), 'cho': board('CHO'),
           'awards': rows(con.execute('''SELECT a.*, u.name, u.role, u.block FROM awards a JOIN users u ON u.id=a.user_id
               ORDER BY a.id DESC LIMIT 30''')),
           'announced': bool(con.execute('SELECT 1 FROM awards WHERE week_label=?', (f'Week ending {we}',)).fetchone())}
    con.close()
    return out


class Announce(BaseModel):
    reward: str = 'Certificate of recognition + performance incentive'


@app.post('/api/sup/announce-winners')
def sup_announce(body: Announce, user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    con = db(); ws, we = week_window(con); label = f'Week ending {we}'
    if con.execute('SELECT 1 FROM awards WHERE week_label=?', (label,)).fetchone():
        con.close(); raise HTTPException(409, 'Winners for this week are already announced')
    picks = []
    q = '''SELECT u.id, u.name, {expr} v FROM users u JOIN points_ledger l ON l.user_id=u.id AND l.sim_date BETWEEN ? AND ?
        WHERE u.role=? GROUP BY u.id HAVING v>0 ORDER BY v DESC LIMIT 1'''
    for title, expr, role in [('ASHA of the Week', 'SUM(l.points)', 'ASHA'),
                              ('CHO of the Week', 'SUM(l.points)', 'CHO'),
                              ('Best Call-back Responder', "SUM(l.rule='callback_resolved')", 'ASHA'),
                              ('Hard-to-Reach Hero', "SUM(l.rule='hard_bonus')", 'ASHA')]:
        r = con.execute(q.format(expr=expr), (ws, we, role)).fetchone()
        if r:
            picks.append((title, r['id'], r['name']))
    if not picks:
        con.close(); raise HTTPException(400, 'No activity this week yet')
    for title, uid, nm in picks:
        con.execute('INSERT INTO awards(week_label,sim_date,user_id,title,reward,points,announced_by) VALUES(?,?,?,?,?,?,?)',
                    (label, we, uid, title, body.reward[:120], 20, user['id']))
        award(con, uid, 'weekly_winner')
        notify(con, uid, f'Congratulations! You are {title} ({label}). Reward: {body.reward}.', 'award')
    summary = '; '.join(f'{t}: {n}' for t, _, n in picks)
    for (u,) in con.execute("SELECT id FROM users WHERE role IN ('ASHA','CHO')").fetchall():
        notify(con, u, f'This week\'s winners are out. {summary}.', 'award')
    log(con, user['id'], 'winners_announced', detail=summary)
    con.commit(); con.close()
    return {'ok': True, 'winners': [{'title': t, 'name': n} for t, _, n in picks]}


@app.get('/api/sup/regions')
def sup_regions(user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    con = db()
    out = rows(con.execute('''SELECT v.village, v.district, v.block, v.mobile_connectivity, v.road_access,
        (SELECT u.name FROM worker_villages w JOIN users u ON u.id=w.user_id WHERE w.village=v.village AND w.is_primary=1 AND u.role='ASHA') primary_asha,
        (SELECT u.id FROM worker_villages w JOIN users u ON u.id=w.user_id WHERE w.village=v.village AND w.is_primary=1 AND u.role='ASHA') primary_id,
        (SELECT COUNT(*) FROM patients p WHERE p.village=v.village) patients
        FROM villages v ORDER BY v.district, v.block, v.village'''))
    ashas = rows(con.execute("SELECT id, name, block, district FROM users WHERE role='ASHA' ORDER BY district, block, name"))
    con.close()
    return {'regions': out, 'ashas': ashas}


class Reassign(BaseModel):
    village: str
    to_user_id: int


@app.post('/api/sup/reassign-village')
def sup_reassign(body: Reassign, user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    con = db()
    v = con.execute('SELECT * FROM villages WHERE village=?', (body.village,)).fetchone()
    to = con.execute("SELECT * FROM users WHERE id=? AND role='ASHA'", (body.to_user_id,)).fetchone()
    if not v or not to:
        con.close(); raise HTTPException(400, 'Choose a valid village and ASHA')
    old = con.execute('''SELECT u.id FROM worker_villages w JOIN users u ON u.id=w.user_id
        WHERE w.village=? AND w.is_primary=1 AND u.role='ASHA' ''', (v['village'],)).fetchone()
    old_id = old[0] if old else None
    if old_id == to['id']:
        con.close(); raise HTTPException(400, f'{to["name"]} already covers {v["village"]}')
    con.execute('''DELETE FROM worker_villages WHERE village=? AND user_id IN (SELECT id FROM users WHERE role='ASHA')''', (v['village'],))
    con.execute('INSERT INTO worker_villages VALUES(?,?,1)', (to['id'], v['village']))
    if old_id:
        con.execute('INSERT OR REPLACE INTO worker_villages VALUES(?,?,0)', (old_id, v['village']))
    con.execute('UPDATE patients SET asha_id=?, alt_asha_id=? WHERE village=?', (to['id'], old_id or to['id'], v['village']))
    moved = con.execute('''UPDATE tasks SET assigned_user_id=? WHERE status='open' AND assigned_user_id=? AND attempt_no=1
        AND patient_id IN (SELECT id FROM patients WHERE village=?)''', (to['id'], old_id or -1, v['village'])).rowcount
    notify(con, to['id'], f'You now cover {v["village"]}. {moved} open task(s) moved to your list.', 'info')
    if old_id:
        notify(con, old_id, f'{v["village"]} has moved to {to["name"]} to balance workload. You remain the back-up worker.', 'info')
    log(con, user['id'], 'village_reassigned', detail=f'{v["village"]} -> {to["name"]}')
    con.commit(); con.close()
    return {'ok': True, 'moved': moved}


# ---------------------------------------------------------------- reports, upload
def csv_response(header, data, fname):
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(header)
    for r in data:
        w.writerow([r.get(h, '') for h in header])
    return Response(buf.getvalue(), media_type='text/csv', headers={'Content-Disposition': f'attachment; filename={fname}'})


@app.get('/api/report/{user_id}')
def report(user_id: int, user=Depends(current_user)):
    if user['role'] != 'SUPERVISOR' and user['id'] != user_id:
        raise HTTPException(403, 'You can download only your own report')
    con = db(); today = sim_today(con)
    target = con.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not target:
        con.close(); raise HTTPException(404, 'Person not found')
    data = rows(con.execute('''SELECT t.id task_id, t.due_date, t.kind, t.step, t.attempt_no, t.origin, t.status,
        t.closed_date, t.request_reason, t.outcome_note, p.patient_ref, p.name patient, p.village, p.priority, p.risk_pct
        FROM tasks t JOIN patients p ON p.id=t.patient_id WHERE t.assigned_user_id=? ORDER BY t.due_date DESC, t.id''', (user_id,)))
    con.close()
    hdr = ['task_id', 'due_date', 'kind', 'step', 'attempt_no', 'origin', 'status', 'closed_date', 'request_reason',
           'outcome_note', 'patient_ref', 'patient', 'village', 'priority', 'risk_pct']
    return csv_response(hdr, data, f'{target["username"]}_report_{today}.csv')


@app.get('/api/sup/export')
def sup_export(user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    con = db(); out = []
    for (pid,) in con.execute('SELECT id FROM patients ORDER BY district, block, village').fetchall():
        p = patient_card(con, pid); ep = active_episode(con, pid); h = holder_of(con, ep['id']) if ep else None
        out.append({'patient_ref': p['patient_ref'], 'name': p['name'], 'village': p['village'], 'block': p['block'],
                    'district': p['district'], 'priority': p['priority'], 'risk_pct': p['risk_pct'],
                    'stage': ep['journey']['stage'] if ep else '', 'doctor': ep.get('doctor_name') if ep else '',
                    'asha': p['asha_name'], 'cho': p['cho_name'], 'current_holder': h['name'] if h else ''})
    today = sim_today(con); con.close()
    return csv_response(list(out[0].keys()), out, f'sahayalink_patient_status_{today}.csv')


@app.get('/api/sup/upload-template')
def upload_template(user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    body = ('patient_ref,name,age,gender,village,priority,risk_pct,diagnosis_group,reason,needs\n'
            'P009001,Ravi Kumar,52,M,Devarakonda,Critical,92,Hypertension,BP uncontrolled; far from facility,consult\n'
            'P009002,Sita Bai,61,F,Kohir,High,78,Diabetes,missed medicine pickup,medicine;review\n')
    return Response(body, media_type='text/csv', headers={'Content-Disposition': 'attachment; filename=sahayalink_upload_template.csv'})


@app.post('/api/sup/upload')
async def sup_upload(file: UploadFile = File(...), user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    import re
    raw = (await file.read()).decode('utf-8-sig', errors='ignore')
    con = db(); today = sim_today(con)
    nv = lambda x: re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9\s]', ' ', str(x or '').lower())).strip()
    vmap = {nv(r[0]): dict(r) for r in con.execute('SELECT * FROM villages')}
    vmap = {k: v for k, v in vmap.items()}
    added = skipped = 0; per_cho = {}; per_asha = {}; problems = []
    for i, r in enumerate(csv.DictReader(io.StringIO(raw)), start=2):
        r = {(k or '').strip().lower(): (v or '').strip() for k, v in r.items()}
        v = vmap.get(nv(r.get('village')))
        if not v or not r.get('patient_ref'):
            skipped += 1; problems.append(f'Row {i}: unknown village "{r.get("village", "")}" or missing patient_ref'); continue
        prim = con.execute('''SELECT u.id FROM worker_villages w JOIN users u ON u.id=w.user_id WHERE w.village=? AND
            w.is_primary=1 AND u.role='ASHA' ''', (v['village'],)).fetchone()
        alt = con.execute('''SELECT u.id FROM worker_villages w JOIN users u ON u.id=w.user_id WHERE w.village=? AND
            w.is_primary=0 AND u.role='ASHA' ''', (v['village'],)).fetchone()
        cho = con.execute("SELECT id FROM users WHERE role='CHO' AND block=?", (v['block'],)).fetchone()
        ref = r['patient_ref'].upper()
        p = con.execute('SELECT * FROM patients WHERE patient_ref=?', (ref,)).fetchone()
        if not p:
            con.execute('INSERT INTO users(username,name,role,pin_hash,district,block,village) VALUES(?,?,?,?,?,?,?)',
                        (ref.lower(), r.get('name') or ref, 'PATIENT', pin_hash('1111'), v['district'], v['block'], v['village']))
            uid = con.execute('SELECT last_insert_rowid()').fetchone()[0]
            try:
                risk = float(r.get('risk_pct') or 0)
            except ValueError:
                risk = 0
            con.execute('''INSERT INTO patients(patient_ref,name,age,gender,village,block,district,language,diagnosis_group,
                asha_id,alt_asha_id,cho_id,connectivity,is_hard,risk_pct,priority,risk_reason,user_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (ref, r.get('name') or ref, int(r.get('age') or 0) if (r.get('age') or '').isdigit() else None,
                         r.get('gender', ''), v['village'], v['block'], v['district'], 'Telugu', r.get('diagnosis_group') or 'Other',
                         prim[0] if prim else None, alt[0] if alt else None, cho[0] if cho else None, v['mobile_connectivity'],
                         1 if v['mobile_connectivity'] in ('Weak', 'Very weak') else 0, risk,
                         (r.get('priority') or 'High').title(), r.get('reason', ''), uid))
            pid = con.execute('SELECT last_insert_rowid()').fetchone()[0]
            con.execute('UPDATE users SET patient_id=? WHERE id=?', (pid, uid))
            p = con.execute('SELECT * FROM patients WHERE id=?', (pid,)).fetchone()
        if con.execute("SELECT 1 FROM episodes WHERE patient_id=? AND status='open'", (p['id'],)).fetchone():
            skipped += 1; problems.append(f'Row {i}: {ref} already has an open care journey'); continue
        needs = [x.strip() for x in (r.get('needs') or 'consult').lower().replace(',', ';').split(';') if x.strip()]
        if 'consult' in needs or not needs:
            con.execute('''INSERT INTO episodes(patient_id,source,category,complaint,consult_status,cho_id,created_date)
                VALUES(?,?,?,?,?,?,?)''', (p['id'], 'upload', 'Flagged by risk model', 'High dropout risk: ' + (r.get('reason') or ''),
                                           'requested', p['cho_id'], today))
            per_cho[p['cho_id']] = per_cho.get(p['cho_id'], 0) + 1
        else:
            con.execute('''INSERT INTO episodes(patient_id,source,category,complaint,consult_status,cho_id,consult_date,
                needs_medicine,needs_review,needs_test,handover_status,created_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (p['id'], 'upload', 'Follow-up from records', r.get('reason', ''), 'completed', p['cho_id'], today,
                         int('medicine' in needs), int('review' in needs), int('test' in needs), 'pending', today))
            per_asha[p['asha_id']] = per_asha.get(p['asha_id'], 0) + 1
        added += 1
    summary = [{'name': name_of(con, k), 'role': 'CHO', 'n': n, 'what': 'teleconsult requests'} for k, n in per_cho.items()] + \
              [{'name': name_of(con, k), 'role': 'ASHA', 'n': n, 'what': 'follow-ups from tonight'} for k, n in per_asha.items()]
    for k, n in per_cho.items():
        notify(con, k, f'{n} new patient(s) flagged by the supervisor need a teleconsult.', 'request')
    log(con, user['id'], 'upload', detail=f'{added} added, {skipped} skipped')
    con.commit(); con.close()
    return {'ok': True, 'added': added, 'skipped': skipped, 'assigned': summary, 'problems': problems[:10]}


@app.post('/api/demo/reset')
def demo_reset():
    """Put every screen back to Day 1 with the original data. Only available in demo mode."""
    if not DEMO:
        raise HTTPException(404, 'Demo mode is off')
    import seed
    seed.build(DB)
    return {'ok': True}


@app.post('/api/sup/reset-demo')
def reset_demo(user=Depends(current_user)):
    need(user, 'SUPERVISOR')
    if not DEMO:
        raise HTTPException(404, 'Demo mode is off')
    import seed
    seed.build(DB)
    return {'ok': True}


# ---------------------------------------------------------------- frontend
@app.get('/')
def index():
    return FileResponse(os.path.join(HERE, 'static', 'index.html'), headers={'Cache-Control': 'no-cache'})


@app.get('/sw.js')
def service_worker():
    return FileResponse(os.path.join(HERE, 'static', 'sw.js'), media_type='application/javascript',
                        headers={'Cache-Control': 'no-cache'})


@app.get('/healthz')
def health():
    return {'ok': True}


app.mount('/static', StaticFiles(directory=os.path.join(HERE, 'static')), name='static')
