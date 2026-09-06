"""One-off: clear a stale lsscloud.com session for the current user.

LifeSaver allows one active session per user. If a previous run didn't log out,
the next login is blocked with "UserAlreadyLoggedIn". Run this to terminate
YOUR OWN dangling session(s), then logins work again.

    LIFESAVER_USERNAME=... LIFESAVER_PASSWORD=... .venv/bin/python scripts/terminate_my_sessions.py

Only terminates sessions whose username matches LIFESAVER_USERNAME -- it will
never knock out another user.
"""

import contextlib
import json
import os
import re
import sys

import requests

BASE = os.environ.get("LIFESAVER_BASE_URL", "https://lsscloud.com")
USERNAME = os.environ["LIFESAVER_USERNAME"]
PASSWORD = os.environ["LIFESAVER_PASSWORD"]


def main() -> int:
    s = requests.Session()
    r = s.post(f"{BASE}/Account/Login", data={"UserName": USERNAME, "Password": PASSWORD})
    r.raise_for_status()

    m = re.search(r"var loginModel = (\{.*?\});", r.text, re.S)
    if not m:
        if "/Account/Login" not in r.url:
            print("Already logged in fine (no stale session). Nothing to do.")
            return 0
        print("Could not find loginModel in the response; login may have failed "
              "for another reason. First 500 chars of visible errors:")
        print(r.text[:500])
        return 1

    model = json.loads(m.group(1))
    errors = model.get("Errors") or []
    user_id = model["UserId"]
    sessions = model.get("ActiveSessions") or []
    print(f"UserId={user_id} Errors={errors} ActiveSessions={len(sessions)}")

    mine = [
        sess for sess in sessions
        if (sess.get("UserAccount") or {}).get("UserName", "").lower() == USERNAME.lower()
    ]
    others = len(sessions) - len(mine)
    if others:
        print(f"NOTE: {others} session(s) belong to other users on this license "
              f"pool -- leaving those alone.")

    if not mine:
        print("No session of your own to terminate.")
        return 0

    for sess in mine:
        unique = sess["UniqueID"]
        created = sess.get("DateCreated")
        resp = s.post(
            f"{BASE}/Account/TerminateSession/",
            data=json.dumps({"Session": unique, "UserId": user_id}),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        print(f"terminate {unique} (created {created}) -> {resp.status_code} {resp.text[:120]}")

    # verify on a throwaway session, then log it back out so we don't just
    # replace the stale session with a new one
    s2 = requests.Session()
    r2 = s2.post(f"{BASE}/Account/Login", data={"UserName": USERNAME, "Password": PASSWORD})
    ok = "/Account/Login" not in r2.url
    print(f"\nre-login {'OK' if ok else 'STILL BLOCKED'} (final url: {r2.url})")
    if ok:
        with contextlib.suppress(requests.RequestException):
            s2.get(f"{BASE}/Account/LogOff", timeout=30)
        print("logged the verification session back out")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
