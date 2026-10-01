"""Build and seed the SahayaLink database.

Patient-centric model:
  patients  - one row per patient (with their own login)
  episodes  - one care journey: teleconsult -> medicine -> review -> test -> complete
  tasks     - work items for ASHAs and CHOs (follow-ups, call backs, escalations)
A simulated day clock (settings.sim_date) lets the night handover move the demo forward.
"""
import sqlite3, json, os, re, csv, hashlib, random
from datetime import date, timedelta, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get('DB_PATH', os.path.join(HERE, 'sahayalink.db'))
rng = random.Random(42)


def norm(x):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9\s]', ' ', str(x).lower())).strip() if x else ''


def pin(s):
    return hashlib.sha256(s.encode()).hexdigest()


SCHEMA = '''
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE, name TEXT, role TEXT, pin_hash TEXT,
  phone_last4 TEXT, district TEXT, block TEXT, village TEXT, facility TEXT, patient_id INTEGER);
CREATE TABLE villages(
  village TEXT PRIMARY KEY, district TEXT, block TEXT, lat REAL, lng REAL,
  mobile_connectivity TEXT, road_access TEXT, population INTEGER);
CREATE TABLE worker_villages(user_id INTEGER, village TEXT, is_primary INTEGER DEFAULT 0,
  PRIMARY KEY(user_id, village));
CREATE TABLE hospitals(id INTEGER PRIMARY KEY, name TEXT, district TEXT, hosp_type TEXT);
CREATE TABLE doctors(id INTEGER PRIMARY KEY, name TEXT, specialty TEXT, hospital_id INTEGER, mode TEXT);
CREATE TABLE patients(
  id INTEGER PRIMARY KEY, patient_ref TEXT UNIQUE, name TEXT, age INTEGER, gender TEXT,
  village TEXT, block TEXT, district TEXT, language TEXT, diagnosis_group TEXT,
  known_ncd TEXT, vulnerability TEXT, phone_last4 TEXT,
  asha_id INTEGER, alt_asha_id INTEGER, cho_id INTEGER,
  distance_km REAL, connectivity TEXT, is_hard INTEGER DEFAULT 0,
  risk_pct REAL, priority TEXT, risk_reason TEXT, user_id INTEGER, is_demo INTEGER DEFAULT 0);
CREATE TABLE episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER,
  source TEXT,              -- patient_request | model_flag | upload | seed
  category TEXT, complaint TEXT, preferred_slot TEXT, mode TEXT,
  consult_status TEXT,      -- requested | scheduled | completed
  cho_id INTEGER, doctor_id INTEGER, hospital_id INTEGER, slot TEXT, consult_date TEXT,
  cho_notes TEXT,
  needs_medicine INTEGER DEFAULT 0, needs_review INTEGER DEFAULT 0, needs_test INTEGER DEFAULT 0,
  medicine_done INTEGER DEFAULT 0, review_done INTEGER DEFAULT 0, test_done INTEGER DEFAULT 0,
  medicine_self_reported INTEGER DEFAULT 0,
  handover_status TEXT DEFAULT 'na',   -- na | pending | sent
  status TEXT DEFAULT 'open',          -- open | closed
  created_date TEXT, closed_date TEXT, rating INTEGER, rating_comment TEXT);
CREATE TABLE tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, episode_id INTEGER, patient_id INTEGER,
  kind TEXT,        -- followup | callback | escalation | reconnect
  step TEXT,        -- medicine | review | test | callback | reconnect
  assigned_user_id INTEGER, attempt_no INTEGER DEFAULT 1,
  origin TEXT,      -- handover | reassign | escalation | patient_request | no_show
  request_reason TEXT, request_note TEXT,
  due_date TEXT, created_date TEXT,
  status TEXT DEFAULT 'open',   -- open | done | unreachable | needs_cho | closed_by_other
  outcome_note TEXT, closed_date TEXT, closed_by INTEGER, successor_created INTEGER DEFAULT 0);
CREATE TABLE action_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, sim_date TEXT,
  user_id INTEGER, action TEXT, task_id INTEGER, episode_id INTEGER, patient_id INTEGER, detail TEXT);
CREATE TABLE point_rules(rule TEXT PRIMARY KEY, points INTEGER, label TEXT, role TEXT);
CREATE TABLE points_ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, points INTEGER,
  rule TEXT, sim_date TEXT, ts TEXT, task_id INTEGER);
CREATE TABLE awards(id INTEGER PRIMARY KEY AUTOINCREMENT, week_label TEXT, sim_date TEXT,
  user_id INTEGER, title TEXT, reward TEXT, points INTEGER, announced_by INTEGER);
CREATE TABLE notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, sim_date TEXT,
  ts TEXT, text TEXT, kind TEXT, is_read INTEGER DEFAULT 0);
CREATE INDEX ix_tasks_assignee ON tasks(assigned_user_id, status, due_date);
CREATE INDEX ix_tasks_episode ON tasks(episode_id, step, status);
CREATE INDEX ix_ep_patient ON episodes(patient_id, status);
CREATE INDEX ix_points_user ON points_ledger(user_id, sim_date);
CREATE INDEX ix_notif_user ON notifications(user_id, id);
'''

POINT_RULES = [
    ('callback_resolved', 15, 'Resolved a patient call-back request', 'ASHA'),
    ('followup_critical', 10, 'Completed a Critical-priority follow-up', 'ASHA'),
    ('followup_high', 6, 'Completed a High-priority follow-up', 'ASHA'),
    ('followup_other', 4, 'Completed a follow-up', 'ASHA'),
    ('reconnect_done', 6, 'Reconnected a patient who missed a teleconsult', 'ASHA'),
    ('hard_bonus', 3, 'Bonus: patient in a far or weak-signal village', 'ASHA'),
    ('attempt', 1, 'Logged a contact attempt', 'ASHA'),
    ('journey_closed', 5, 'Patient completed the full care journey', 'ASHA'),
    ('consult_completed', 5, 'Completed a teleconsultation', 'CHO'),
    ('escalation_resolved', 8, 'Resolved an escalated case', 'CHO'),
    ('weekly_winner', 20, 'Weekly winner bonus', 'ANY'),
]

ASHA_NAMES = ['Lakshmi', 'Padma', 'Anjali', 'Sunitha', 'Radha', 'Kavitha', 'Sarala', 'Manjula',
              'Bharathi', 'Vijaya', 'Latha', 'Sridevi', 'Nirmala', 'Saroja', 'Renuka', 'Shobha',
              'Geetha', 'Uma']
CHO_NAMES = ['Ramesh Kumar', 'Sneha Reddy', 'Kiran Rao', 'Anitha Devi', 'Prasad Goud',
             'Meena Kumari', 'Srinivas Naidu', 'Swapna Rani', 'Venkat Reddy']
SPECIALTIES = ['General Medicine', 'Cardiology', 'Diabetology', 'Internal Medicine',
               'Family Medicine', 'General Physician']
DR_NAMES = ['Dr. Ravi Shankar', 'Dr. Kavya Iyer', 'Dr. Arjun Mehta', 'Dr. Deepa Nair', 'Dr. Suresh Babu',
            'Dr. Swathi Rao', 'Dr. Naveen Kumar', 'Dr. Divya Menon', 'Dr. Mohan Das', 'Dr. Latha Pillai',
            'Dr. Rahul Varma', 'Dr. Sangeetha R', 'Dr. Kiran Joshi', 'Dr. Anil Reddy', 'Dr. Fathima S',
            'Dr. Ganesh P', 'Dr. Harini K', 'Dr. Imran Khan']
COMPLAINTS = {
    'Hypertension': ('BP or sugar check', 'Headache and dizziness, BP was high at the camp'),
    'Diabetes': ('BP or sugar check', 'Sugar reading high, feeling tired'),
    'Hypertension+Diabetes': ('Medicine refill', 'BP and sugar tablets finished, need review'),
    'Fever/Infection': ('Fever or infection', 'Fever for 3 days with body pain'),
    'Respiratory': ('Breathing problem', 'Cough and breathlessness at night'),
    'Gastrointestinal': ('Stomach problem', 'Stomach pain and loose motions'),
    'Dermatology': ('Skin problem', 'Itching rash on arms for a week'),
}
CALLBACK_REASONS = ['Medicine pickup centre is too far', 'Medicine not available at PHC',
                    'Question about my tablets', 'Feeling side effects from medicine',
                    'Need help to get my test done']
SLOTS = ['09:30', '10:00', '10:30', '11:00', '11:30', '12:00', '14:30', '15:00', '15:30', '16:00']


def build(db_path=DB, start_date=None):
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    c = con.cursor()
    c.executescript(SCHEMA)
    today = date.fromisoformat(start_date) if start_date else date.today()
    D = today.isoformat()
    now = datetime.now().isoformat(timespec='seconds')
    c.executemany('INSERT INTO settings VALUES(?,?)', [
        ('sim_date', D), ('start_date', D), ('daily_cap', '12')])
    c.executemany('INSERT INTO point_rules VALUES(?,?,?,?)', POINT_RULES)

    # ---- geography ----
    geo = {}
    with open(os.path.join(HERE, 'geography_reference.csv'), encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            geo[norm(r['village'])] = r
            c.execute('INSERT INTO villages VALUES(?,?,?,?,?,?,?,?)',
                      (r['village'], r['district'], r['block'], float(r['latitude']), float(r['longitude']),
                       r['mobile_connectivity'], r['road_access'], int(r['population'])))
    blocks = {}
    for g in geo.values():
        blocks.setdefault((g['district'], g['block']), []).append(g['village'])

    facilities = {}
    with open(os.path.join(HERE, 'facility_reference.csv'), encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            if r['facility_type'] == 'PHC':
                facilities[r['block']] = r['facility_name']

    # ---- staff ----
    uid = 1
    primary_asha, alt_asha, block_cho = {}, {}, {}
    asha_i = 0
    for bi, ((d, b), vills) in enumerate(sorted(blocks.items())):
        vills = sorted(vills)
        ids = []
        for _ in range(2):
            nm = ASHA_NAMES[asha_i]; asha_i += 1
            c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (uid, nm.lower(), nm, 'ASHA', pin('1234'), f'{rng.randint(1000, 9999)}',
                       d, b, '', facilities.get(b, f'{b} PHC-1'), None))
            ids.append(uid); uid += 1
        for i, v in enumerate(vills):
            p = ids[i % 2]; o = ids[(i + 1) % 2]
            primary_asha[v] = p; alt_asha[v] = o
            c.execute('INSERT INTO worker_villages VALUES(?,?,1)', (p, v))
            c.execute('INSERT INTO worker_villages VALUES(?,?,0)', (o, v))
        nm = CHO_NAMES[bi]
        c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (uid, 'cho_' + b.lower(), nm, 'CHO', pin('4321'), f'{rng.randint(1000, 9999)}',
                   d, b, '', facilities.get(b, f'{b} PHC-1'), None))
        for v in vills:
            c.execute('INSERT INTO worker_villages VALUES(?,?,1)', (uid, v))
        block_cho[(d, b)] = uid; uid += 1
    c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?,?)',
              (uid, 'supervisor', 'District Programme Supervisor', 'SUPERVISOR', pin('0000'), '0000',
               '', '', '', 'District Health Office', None))
    sup_id = uid; uid += 1

    # ---- hospitals & doctors ----
    hid = did = 1
    hosp_by_district = {}
    for d in ['Mahabubnagar', 'Nalgonda', 'Sangareddy']:
        for htype, hname in [('District Hospital', f'{d} District Hospital'),
                             ('Teleconsult Hub', f'{d} CHC Teleconsult Hub')]:
            c.execute('INSERT INTO hospitals VALUES(?,?,?,?)', (hid, hname, d, htype))
            hosp_by_district.setdefault(d, []).append(hid)
            for k in range(3):
                c.execute('INSERT INTO doctors VALUES(?,?,?,?,?)',
                          (did, DR_NAMES[did - 1], SPECIALTIES[(did - 1) % len(SPECIALTIES)], hid,
                           'Video' if k != 2 else 'Audio'))
                did += 1
            hid += 1
    docs_by_hosp = {}
    for (dd, hh) in c.execute('SELECT id, hospital_id FROM doctors').fetchall():
        docs_by_hosp.setdefault(hh, []).append(dd)

    # ---- patients from the risk-model output ----
    ref = {}
    with open(os.path.join(HERE, 'patient_360_reference.csv'), encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            ref[r['patient_id']] = r
    cases = json.load(open(os.path.join(HERE, 'app_data.json')))
    seen = set()
    patients = []
    for r in cases:
        pr = r['patient_ref']
        if pr in seen or pr not in ref:
            continue
        seen.add(pr)
        p = ref[pr]
        v = p['village']
        if v not in primary_asha:
            continue
        d, b = p['district'], p['block']
        conn = geo[norm(v)]['mobile_connectivity']
        dist = float(r.get('distance_km') or 0)
        hard = 1 if (conn in ('Weak', 'Very weak') or dist >= 15) else 0
        c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (uid, pr.lower(), p['canonical_name'], 'PATIENT', pin('1111'), p['masked_mobile'][-4:],
                   d, b, v, '', None))
        user_id = uid; uid += 1
        c.execute('''INSERT INTO patients(patient_ref,name,age,gender,village,block,district,language,
            diagnosis_group,known_ncd,vulnerability,phone_last4,asha_id,alt_asha_id,cho_id,distance_km,
            connectivity,is_hard,risk_pct,priority,risk_reason,user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (pr, p['canonical_name'], int(p['age_as_of_2026']), p['gender'], v, b, d,
                   p['preferred_language'], r.get('diagnosis_group') or 'Other', p['known_ncd_status'],
                   p['vulnerability_group'], p['masked_mobile'][-4:], primary_asha[v], alt_asha[v],
                   block_cho[(d, b)], dist, conn, hard, r['risk_pct'], r['priority'], r['reason'], user_id))
        pid = c.lastrowid
        c.execute('UPDATE users SET patient_id=? WHERE id=?', (pid, user_id))
        patients.append((pid, r, d, b, v, user_id))

    def notify(user_id, text, kind='info', day=D):
        c.execute('INSERT INTO notifications(user_id,sim_date,ts,text,kind) VALUES(?,?,?,?,?)',
                  (user_id, day, now, text, kind))

    def pick_doctor(d, diag):
        hosps = hosp_by_district[d]
        h = hosps[rng.randint(0, len(hosps) - 1)]
        dl = docs_by_hosp[h]
        return dl[rng.randint(0, len(dl) - 1)], h

    def add_task(ep, pid, kind, step, assignee, attempt, origin, due, status='open', created=None,
                 closed=None, reason=None, note=None, succ=0):
        c.execute('''INSERT INTO tasks(episode_id,patient_id,kind,step,assigned_user_id,attempt_no,origin,
            request_reason,request_note,due_date,created_date,status,closed_date,closed_by,successor_created)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (ep, pid, kind, step, assignee, attempt, origin, reason, note, due, created or due, status,
                   closed, assignee if closed else None, succ))

    # ---- episodes: spread across every journey stage so the demo shows the full flow ----
    for i, (pid, r, d, b, v, user_id) in enumerate(patients):
        stage = r.get('predicted_dropout_stage') or 'Medicine not collected'
        diag = r.get('diagnosis_group') or 'Other'
        cat, txt = COMPLAINTS.get(diag, ('Other', 'Not feeling well, need doctor advice'))
        cho = block_cho[(d, b)]
        asha = primary_asha[v]; alt = alt_asha[v]
        bucket = i % 20
        past = (today - timedelta(days=1 + i % 5)).isoformat()

        if bucket in (0, 1, 2, 3):
            # waiting for the CHO to schedule a teleconsult
            src = 'model_flag' if bucket == 3 else 'patient_request'
            c.execute('''INSERT INTO episodes(patient_id,source,category,complaint,preferred_slot,mode,
                consult_status,cho_id,created_date) VALUES(?,?,?,?,?,?,?,?,?)''',
                      (pid, src, cat if src == 'patient_request' else 'Flagged by risk model',
                       txt if src == 'patient_request' else 'High dropout risk: ' + r['reason'],
                       'Morning' if i % 2 else 'Afternoon', 'Video' if i % 3 else 'Audio', 'requested', cho,
                       (today - timedelta(days=i % 2)).isoformat()))
            if src == 'patient_request':
                notify(user_id, 'Your teleconsultation request was sent to your CHO. You will get a slot soon.', 'request')
            continue

        if bucket in (4, 5):
            doc, hos = pick_doctor(d, diag)
            c.execute('''INSERT INTO episodes(patient_id,source,category,complaint,preferred_slot,mode,
                consult_status,cho_id,doctor_id,hospital_id,slot,created_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (pid, 'patient_request', cat, txt, 'Morning', 'Video', 'scheduled', cho, doc, hos,
                       f'{D} {SLOTS[i % len(SLOTS)]}', past))
            dn = c.execute('SELECT name FROM doctors WHERE id=?', (doc,)).fetchone()[0]
            notify(user_id, f'Teleconsultation scheduled with {dn} today at {SLOTS[i % len(SLOTS)]}.', 'appointment')
            continue

        # consult already done
        doc, hos = pick_doctor(d, diag)
        need_rev = 1 if (stage == 'Review not attended' or 'review' in r['recommended_action'].lower()) else 0
        need_test = 1 if stage == 'Test not completed' else 0
        med_done = 1 if stage in ('Test not completed', 'Review not attended') else 0
        rev_done = 1 if (stage == 'Test not completed' and need_rev) else 0

        if bucket == 6:
            # consult finished today, waiting for tonight's handover to the ASHA
            c.execute('''INSERT INTO episodes(patient_id,source,category,complaint,mode,consult_status,cho_id,
                doctor_id,hospital_id,slot,consult_date,cho_notes,needs_medicine,needs_review,needs_test,
                handover_status,created_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (pid, 'patient_request', cat, txt, 'Video', 'completed', cho, doc, hos, f'{D} 10:00', D,
                       'Started on medicines. ASHA to confirm pickup.', 1, need_rev, need_test, 'pending', past))
            continue

        closed = bucket == 19
        c.execute('''INSERT INTO episodes(patient_id,source,category,complaint,mode,consult_status,cho_id,
            doctor_id,hospital_id,slot,consult_date,cho_notes,needs_medicine,needs_review,needs_test,
            medicine_done,review_done,test_done,medicine_self_reported,handover_status,status,created_date,
            closed_date,rating) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (pid, 'seed', cat, txt, 'Video', 'completed', cho, doc, hos, f'{past} 10:00', past,
                   'Medicines prescribed. Follow up as advised.', 1, need_rev, need_test,
                   1 if closed else med_done, 1 if closed else rev_done, 1 if closed else 0,
                   1 if bucket == 8 else 0, 'sent', 'closed' if closed else 'open', past,
                   D if closed else None, (3 + i % 3) if (closed or i % 4 == 0) else None))
        ep = c.lastrowid
        if closed:
            continue
        cur = 'medicine' if not med_done else ('review' if need_rev and not rev_done else ('test' if need_test else None))
        if cur is None:
            c.execute("UPDATE episodes SET status='closed', closed_date=? WHERE id=?", (D, ep))
            continue
        yday = (today - timedelta(days=1)).isoformat()
        if i % 9 == 0:
            # primary ASHA could not reach yesterday -> alternate ASHA today
            add_task(ep, pid, 'followup', cur, asha, 1, 'handover', yday, status='unreachable', closed=yday, succ=1)
            add_task(ep, pid, 'followup', cur, alt, 2, 'reassign', D, created=yday)
        elif i % 23 == 0:
            # two ASHA attempts failed -> CHO escalation
            d2 = (today - timedelta(days=2)).isoformat()
            add_task(ep, pid, 'followup', cur, asha, 1, 'handover', d2, status='unreachable', closed=d2, succ=1)
            add_task(ep, pid, 'followup', cur, alt, 2, 'reassign', yday, status='unreachable', closed=yday, succ=1)
            add_task(ep, pid, 'escalation', cur, cho, 3, 'escalation', D, created=yday)
        else:
            add_task(ep, pid, 'followup', cur, asha, 1, 'handover', D, created=yday)

        if bucket == 7:
            add_task(None, pid, 'callback', 'callback', asha, 1, 'patient_request', D,
                     reason=CALLBACK_REASONS[i % len(CALLBACK_REASONS)],
                     note='Please call me after 5 pm' if i % 2 else '')
            notify(user_id, 'Your call-back request was sent to your ASHA worker.', 'request')

    # ---- the live-demo patient: starts with NO request, so a presenter can show the whole journey ----
    dv = 'Gundlapally'
    dg = geo[norm(dv)]
    dd, db_ = dg['district'], dg['block']
    c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?,?)',
              (uid, 'demo', 'Venkatesh Rao', 'PATIENT', pin('1111'), '4821', dd, db_, dv, '', None))
    demo_uid = uid; uid += 1
    c.execute('''INSERT INTO patients(patient_ref,name,age,gender,village,block,district,language,diagnosis_group,
        known_ncd,vulnerability,phone_last4,asha_id,alt_asha_id,cho_id,distance_km,connectivity,is_hard,risk_pct,
        priority,risk_reason,user_id,is_demo) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              ('P900001', 'Venkatesh Rao', 58, 'M', dv, db_, dd, 'Telugu', 'Hypertension+Diabetes',
               'Hypertension+Diabetes', 'Elderly living alone', '4821', primary_asha[dv], alt_asha[dv],
               block_cho[(dd, db_)], 18.5, dg['mobile_connectivity'], 1, 94.0, 'Critical',
               'BP and sugar uncontrolled; lives alone; 18 km from the PHC', demo_uid, 1))
    c.execute('UPDATE users SET patient_id=? WHERE id=?', (c.lastrowid, demo_uid))
    notify(demo_uid, 'Welcome to SahayaLink. Request a teleconsult whenever you need a doctor.', 'info')

    # ---- point history for the past week so leaderboards and weekly winners are meaningful ----
    for (u, role) in c.execute("SELECT id, role FROM users WHERE role IN ('ASHA','CHO')").fetchall():
        activity = rng.choice([0, 1, 1, 2, 2, 3])   # some workers are far more active than others
        for back in range(1, 7):
            dd = (today - timedelta(days=back)).isoformat()
            for _ in range(rng.randint(0, activity)):
                rule, pts = (('followup_high', 6) if rng.random() < .55 else ('callback_resolved', 15)) \
                    if role == 'ASHA' else (('consult_completed', 5) if rng.random() < .8 else ('escalation_resolved', 8))
                c.execute('INSERT INTO points_ledger(user_id,points,rule,sim_date,ts) VALUES(?,?,?,?,?)',
                          (u, pts, rule, dd, now))
            if role == 'ASHA' and rng.random() < .5:
                c.execute('INSERT INTO points_ledger(user_id,points,rule,sim_date,ts) VALUES(?,?,?,?,?)',
                          (u, 3, 'hard_bonus', dd, now))

    # last week's winners, already announced
    lw = (today - timedelta(days=7)).isoformat()
    a1 = c.execute("SELECT id FROM users WHERE role='ASHA' ORDER BY id LIMIT 1 OFFSET 4").fetchone()[0]
    c1 = c.execute("SELECT id FROM users WHERE role='CHO' ORDER BY id LIMIT 1 OFFSET 2").fetchone()[0]
    for u, t in [(a1, 'ASHA of the Week'), (c1, 'CHO of the Week')]:
        c.execute('INSERT INTO awards(week_label,sim_date,user_id,title,reward,points,announced_by) VALUES(?,?,?,?,?,?,?)',
                  (f'Week ending {lw}', lw, u, t, 'Certificate of recognition + performance incentive', 20, sup_id))

    # welcome notifications for staff
    for (u, role, nm) in c.execute("SELECT id, role, name FROM users WHERE role IN ('ASHA','CHO','SUPERVISOR')").fetchall():
        notify(u, f'Good morning {nm.split()[0]}. Your list for {D} is ready.', 'info')

    con.commit()
    n = {k: c.execute(q).fetchone()[0] for k, q in {
        'patients': 'SELECT COUNT(*) FROM patients',
        'episodes': 'SELECT COUNT(*) FROM episodes',
        'open_tasks': "SELECT COUNT(*) FROM tasks WHERE status='open'",
        'asha': "SELECT COUNT(*) FROM users WHERE role='ASHA'",
        'cho': "SELECT COUNT(*) FROM users WHERE role='CHO'"}.items()}
    con.close()
    return n


if __name__ == '__main__':
    print('Seeded:', build())
