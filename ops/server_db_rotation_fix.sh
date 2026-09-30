#!/bin/bash
# =============================================================================
# Karnex — permanent fix for the 7-day RDS password rotation lockout
# -----------------------------------------------------------------------------
# Run from Git Bash on your PC (one line):
#   ssh -i /c/Users/Pavan.Sanap/Downloads/karnex-app.pem ubuntu@3.110.174.227 'bash -s' \
#       < /f/AI-Interview-Model-B-V2/ops/server_db_rotation_fix.sh
#
# What it does (all idempotent — safe to run twice):
#   1. main.py + crm_db.py : percent-encode DB credentials in the DSN
#                            (a '[' or '#' in a rotated password can no longer break urlparse)
#   2. main.py             : a configured Postgres that fails to init now CRASHES the process
#                            instead of silently falling back to an empty SQLite file
#   3. karnex-run          : fetches the LIVE password from Secrets Manager at every start
#   4. karnex-dbcheck.timer: every 2 min, restarts the app if the secret rotated
#   5. Restarts and VERIFIES end to end, then prints PASS / FAIL
# =============================================================================
set -uo pipefail
APP=/srv/karnex/app/AI-Interview-Model-B-V2/backend
VENV=/srv/karnex/venv/bin/python
STAMP=$(date +%Y%m%d-%H%M%S)
FAIL=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=1; }
info() { printf '  ....  %s\n' "$1"; }

echo "================ 1/6  Patch code (idempotent) ================"
sudo cp "$APP/main.py"   "$APP/main.py.bak-rotationfix-$STAMP"
sudo cp "$APP/crm_db.py" "$APP/crm_db.py.bak-rotationfix-$STAMP"
sudo "$VENV" - "$APP" <<'PY'
import sys, re, io
app = sys.argv[1]

def rw(path, fn):
    raw = open(path, "rb").read().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    src = raw.replace("\r\n", "\n")
    new, msg = fn(src)
    if new != src:
        open(path, "wb").write(new.replace("\n", nl).encode("utf-8"))
    print(f"  {path.split('/')[-1]:10} {msg}")

OLD_DSN = 'return f"postgresql://{user}:{password}@{host}:{port}/{name}"'
NEW_DSN = ('from urllib.parse import quote as _q\n'
           '        return f"postgresql://{_q(user, safe=\'\')}:{_q(password, safe=\'\')}@{host}:{port}/{name}"')

def enc(src):
    if "quote as _q" in src:
        return src, "encoding: already applied"
    if OLD_DSN not in src:
        return src, "encoding: PATTERN NOT FOUND (check manually)"
    return src.replace(OLD_DSN, NEW_DSN), "encoding: applied"

OLD_HEAD = "AUTH_DB_TARGET = _auth_db_target()\ntry:\n    init_auth_db(AUTH_DB_TARGET)"
NEW_HEAD = ("AUTH_DB_TARGET = _auth_db_target()\n"
            "# A configured Postgres (AUTH_DB_URL *or* DB_HOST/DB_NAME/DB_USER) that fails to init must\n"
            "# CRASH, never fall back to an empty SQLite file: that fallback turned the 9 Sep and\n"
            "# 17 Sep 2026 rotation/config faults into 'Invalid username or password' for every user.\n"
            "# systemd restarts the process and karnex-run re-fetches the live secret on each start,\n"
            "# so the restart loop IS the recovery.\n"
            "_postgres_configured = _auth_db_url_configured or str(AUTH_DB_TARGET).startswith(\n"
            "    (\"postgresql://\", \"postgres://\")\n"
            ")\n"
            "try:\n    init_auth_db(AUTH_DB_TARGET)")
OLD_GUARD = "    if _auth_db_url_configured:\n        raise\n    AUTH_DB_TARGET = KARNEX_DB_FILE"
NEW_GUARD = "    if _postgres_configured:\n        raise\n    AUTH_DB_TARGET = KARNEX_DB_FILE"

def loud(src):
    if "_postgres_configured" in src:
        return src, "fail-loud guard: already applied"
    if OLD_HEAD not in src or OLD_GUARD not in src:
        return src, "fail-loud guard: PATTERN NOT FOUND (check manually)"
    return src.replace(OLD_HEAD, NEW_HEAD, 1).replace(OLD_GUARD, NEW_GUARD, 1), "fail-loud guard: applied"

# main.py gets both patches in sequence; crm_db.py gets the encoding only
src_main = f"{app}/main.py"
raw = open(src_main, "rb").read().decode("utf-8"); nl = "\r\n" if "\r\n" in raw else "\n"
s = raw.replace("\r\n", "\n")
s, m1 = enc(s); s, m2 = loud(s)
open(src_main, "wb").write(s.replace("\n", nl).encode("utf-8"))
print(f"  main.py    {m1}"); print(f"  main.py    {m2}")
rw(f"{app}/crm_db.py", enc)
PY
if sudo "$VENV" -m py_compile "$APP/main.py" "$APP/crm_db.py"; then ok "main.py + crm_db.py compile"; else bad "syntax error after patch — restoring backups"; sudo cp "$APP/main.py.bak-rotationfix-$STAMP" "$APP/main.py"; sudo cp "$APP/crm_db.py.bak-rotationfix-$STAMP" "$APP/crm_db.py"; fi
grep -q 'quote as _q'         "$APP/main.py"   && ok "main.py: credentials percent-encoded"  || bad "main.py: encoding missing"
grep -q 'quote as _q'         "$APP/crm_db.py" && ok "crm_db.py: credentials percent-encoded" || bad "crm_db.py: encoding missing"
grep -q '_postgres_configured' "$APP/main.py"  && ok "main.py: fail-loud guard present"      || bad "main.py: fail-loud guard missing"

echo "================ 2/6  Live-secret launcher ================"
sudo tee /usr/local/bin/karnex-run >/dev/null <<'RUN'
#!/bin/bash
# Launcher for karnex.service: fetch the LIVE RDS password from Secrets Manager (via the
# instance role) on every start, stamp its sha for karnex-dbcheck, then exec uvicorn.
set -uo pipefail
P="$(/usr/local/bin/karnex-dbpass 2>/dev/null || true)"
if [ -n "${P:-}" ]; then export DB_PASSWORD="$P"; fi
mkdir -p /run/karnex
echo -n "${DB_PASSWORD:-}" | sha256sum | cut -d' ' -f1 > /run/karnex/dbpass.sha
exec /srv/karnex/venv/bin/python -m uvicorn main:app \
     --host 127.0.0.1 --port 2020 --workers 1 \
     --proxy-headers --forwarded-allow-ips=127.0.0.1
RUN
sudo chmod 755 /usr/local/bin/karnex-run
[ -x /usr/local/bin/karnex-dbpass ] && ok "karnex-dbpass helper present" || bad "karnex-dbpass helper MISSING"
LIVE="$(/usr/local/bin/karnex-dbpass 2>/dev/null || true)"
[ "${#LIVE}" -ge 16 ] && ok "instance role can read the live secret (len ${#LIVE})" || bad "cannot read the secret via instance role"
grep -q 'ExecStart=/usr/local/bin/karnex-run' /etc/systemd/system/karnex.service && ok "karnex.service uses karnex-run" || {
  info "wiring karnex.service to karnex-run"
  sudo sed -i -E 's#^ExecStart=.*#ExecStart=/usr/local/bin/karnex-run#' /etc/systemd/system/karnex.service
  grep -q '^RuntimeDirectory=karnex' /etc/systemd/system/karnex.service || sudo sed -i 's#^User=ubuntu#User=ubuntu\nRuntimeDirectory=karnex#' /etc/systemd/system/karnex.service
  grep -q 'ExecStart=/usr/local/bin/karnex-run' /etc/systemd/system/karnex.service && ok "karnex.service now uses karnex-run" || bad "could not wire karnex-run"
}

echo "================ 3/6  Rotation watchdog ================"
sudo tee /usr/local/bin/karnex-dbcheck >/dev/null <<'CHK'
#!/bin/bash
# Every 2 min: if the live RDS secret differs from the one the running app started with, restart.
set -uo pipefail
STAMP=/run/karnex/dbpass.sha
[ -r "$STAMP" ] || exit 0
LIVE=$(/usr/local/bin/karnex-dbpass) || exit 0
[ -n "$LIVE" ] || exit 0
LIVE_SHA=$(printf '%s' "$LIVE" | sha256sum | cut -d' ' -f1)
if [ "$LIVE_SHA" != "$(cat "$STAMP")" ]; then
  logger -t karnex-dbcheck "RDS password rotated - restarting karnex.service"
  systemctl restart karnex.service
fi
CHK
sudo chmod 755 /usr/local/bin/karnex-dbcheck
sudo tee /etc/systemd/system/karnex-dbcheck.service >/dev/null <<'SVC'
[Unit]
Description=Restart Karnex API when the RDS password rotates
[Service]
Type=oneshot
ExecStart=/usr/local/bin/karnex-dbcheck
SVC
sudo tee /etc/systemd/system/karnex-dbcheck.timer >/dev/null <<'TMR'
[Unit]
Description=Check every 2 min whether the RDS password rotated
[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
AccuracySec=1min
[Install]
WantedBy=timers.target
TMR
sudo systemctl daemon-reload
sudo systemctl enable --now karnex-dbcheck.timer >/dev/null 2>&1
[ "$(systemctl is-active karnex-dbcheck.timer)" = active ] && ok "karnex-dbcheck.timer active + enabled" || bad "timer not active"

echo "================ 4/6  Restart ================"
sudo systemctl restart karnex.service
sleep 14
[ "$(systemctl is-active karnex)" = active ] && ok "karnex.service active" || bad "karnex.service NOT active"
RS=$(sudo cat /run/karnex/dbpass.sha 2>/dev/null); LS=$(printf '%s' "$LIVE" | sha256sum | cut -d' ' -f1)
[ -n "$RS" ] && [ "$RS" = "$LS" ] && ok "running app uses the LIVE secret (stamp == secret)" || bad "stamp/secret mismatch — app not on the live password"
N=$(sudo journalctl -u karnex --since "40 sec ago" --no-pager | grep -c 'auth.db.init.failed')
[ "$N" = 0 ] && ok "no auth.db.init.failed on startup" || bad "auth.db.init.failed seen on startup ($N)"
[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:2020/healthz)" = 200 ] && ok "healthz 200" || bad "healthz not 200"

echo "================ 5/6  Prove logins hit Postgres ================"
curl -s -o /dev/null -X POST http://127.0.0.1:2020/auth/login -d 'username=karan.singh@karnex.in&password=rotation-fix-probe-wrong'
sleep 3
RES=$(sudo bash -c "set -a; . /etc/karnex/karnex.env; set +a; DB_PASSWORD=\"\$(/usr/local/bin/karnex-dbpass)\"; $VENV - <<'PY'
import os, psycopg2
from urllib.parse import quote as q
dsn='postgresql://%s:%s@%s:%s/%s?sslmode=require'%(q(os.getenv('DB_USER'),safe=''),q(os.getenv('DB_PASSWORD'),safe=''),os.getenv('DB_HOST'),os.getenv('DB_PORT','5432'),os.getenv('DB_NAME'))
c=psycopg2.connect(dsn);cur=c.cursor()
cur.execute(\"select message from login_data where username='karan.singh@karnex.in' order by id desc limit 1\")
r=cur.fetchone();print(r[0] if r else 'NO ROW')
PY" 2>/dev/null)
[ "$RES" = "Wrong password" ] && ok "HTTP login reached Postgres and found the user (recorded: '$RES')" || bad "login probe not recorded in Postgres (got: '$RES')"

echo "================ 6/6  Simulate the watchdog ================"
sudo /usr/local/bin/karnex-dbcheck && ok "watchdog ran clean (secret in sync, no restart needed)" || bad "watchdog errored"
echo "  next watchdog run: $(systemctl list-timers karnex-dbcheck.timer --no-pager | sed -n 2p | awk '{print $1,$2,$3,$4}')"

echo
echo "======================================================================"
if [ "$FAIL" = 0 ]; then
  printf '\033[32m  ALL CHECKS PASSED — the next rotation will self-heal within 2 minutes.\033[0m\n'
else
  printf '\033[31m  ONE OR MORE CHECKS FAILED — send this whole output to Claude.\033[0m\n'
fi
echo "======================================================================"
