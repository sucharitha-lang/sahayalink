# SahayaLink

Follow-up assurance for rural teleconsultations (INFINUM 2026).
One app with four connected views: Patient, ASHA worker, CHO and Supervisor.
Built with FastAPI (Python) and SQLite, plus a single-page web frontend. All data is synthetic.

## The flow

1. **Patient** opens the app and requests a teleconsult (or asks their ASHA for a call back).
2. **CHO** sees the request, books a doctor at a hospital with a time slot, and the patient is told.
3. **CHO** completes the teleconsult and ticks what the patient needs next (medicine pickup, review visit, lab test).
4. **Night handover:** those follow-ups are handed to the patient's ASHA for the next day.
5. **ASHA** contacts the patient. If she cannot reach them, the **other ASHA** of that village tries the next day, and after that the case goes to the **CHO**. A task nobody acts on for two days moves on in the same way.
6. **Supervisor** sees everyone's progress (patients, ASHAs, CHOs), gets alerts, runs the night handover and announces **weekly winners**.

Every person sees the same journey steps for a patient:
Teleconsult, Medicine pickup, Review visit, Lab test, Care complete.

## Logins

| Who | Username | PIN |
|---|---|---|
| Supervisor | `supervisor` | `0000` |
| CHO | `cho_` + block name, for example `cho_devarakonda`, `cho_balanagar` | `4321` |
| ASHA worker | first name in small letters, for example `bharathi`, `lakshmi`, `geetha` | `1234` |
| Patient | patient ID in small letters, for example `p004357` | `1111` |

**Demo mode** (on by default): the login page has "Explore the demo" to open anyone's dashboard,
and the supervisor's People tab has "Open dashboard" for every ASHA, CHO and patient, with a
"Back to supervisor" button. Turn demo mode off by setting the environment variable `DEMO_MODE=0`;
then each person can only sign in with their own username and PIN.

## A 3-minute demo script

1. Sign in as a **patient** (Explore the demo, Patient). Show the care journey, the ASHA and CHO contact cards, and press **Request a call back**.
2. Switch to that patient's **ASHA**. The call back is at the top of her list (worth 15 points). Mark it done.
3. Switch to the **CHO**. Book a doctor for a waiting patient, then on a booked patient tick the follow-up needs and press **Consult done, hand over to ASHA**. Show **Tonight's handover** and **My treated patients**.
4. Switch to the **Supervisor**. Show alerts, the journey funnel and People. Press **Run night handover and start next day**.
5. Open the ASHA again: the new follow-up from the CHO is there, with the doctor's note. Mark one **Not reachable**, run the night again, and show it has moved to the other ASHA.
6. Supervisor, Rewards: **Announce this week's winners**.

Supervisor, Daily operations, **Reset demo data** starts everything again from day 1.

## Deploy on Render (new link)

1. Create a new **empty** GitHub repository.
2. Unzip this folder and upload **everything inside it** (including the `static` folder) to the top level of the repository. `app.py` and `render.yaml` must be at the top level, not inside a sub-folder.
3. On Render: New, Web Service, pick the repository. Build command `pip install -r requirements.txt`, start command `bash start.sh`, instance type Free.
   (Or New, Blueprint, which reads `render.yaml` and fills these in for you.)
4. Wait for "Live" and open the link.

Notes for the free plan:
- The app sleeps after about 15 minutes without visits; the first visit then takes up to a minute. Open it once before presenting.
- The database is rebuilt fresh whenever Render restarts the app, so the demo starts again at day 1. That is expected for a demo.

## Run on your own computer

```
bash run.sh
```
Then open http://127.0.0.1:8000

## Files

- `app.py` - backend API for all four roles, the night handover engine and weekly rewards
- `seed.py` - builds the demo database from the analysis output (`app_data.json`) and the reference CSVs
- `static/index.html` - the whole frontend (teal theme, Arial)
- `static/sw.js` - lets the app keep working with weak or no signal
- `app_data.json`, `patient_360_reference.csv`, `geography_reference.csv`, `facility_reference.csv` - input data
- `requirements.txt`, `start.sh`, `run.sh`, `render.yaml` - running and deployment

## Points (incentive programme)

| Action | Points |
|---|---|
| Resolve a patient call-back request | 15 |
| Complete a Critical follow-up | 10 |
| Complete a High follow-up | 6 |
| Reconnect a patient who missed a teleconsult | 6 |
| Patient completes the full care journey | 5 |
| Bonus: far or weak-signal village | 3 |
| Any contact attempt | 1 |
| CHO completes a teleconsult | 5 |
| CHO resolves an escalated case | 8 |
| Weekly winner | 20 |

Tiers: Bronze 0 to 49, Silver 50 to 119, Gold 120 and above.

## Not built yet (roadmap)

SMS or WhatsApp reminders, OTP login, Telugu interface, live link to ABDM and the state HMIS,
and real video calling inside the app.
