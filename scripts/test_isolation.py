#!/usr/bin/env python3
"""
Scout Client Portal — tenant isolation tests.

Run this after migrations 001 and 002 are applied, and BEFORE any sign-in link
goes to a client. It answers one question: can a Bouldering Project login reach
another client's data?

Standard library only. No installs.

  # Part A needs nothing but the publishable key (already in the portal source)
  python3 test_isolation.py

  # Part B needs a real signed-in token. Get one by signing in at the portal,
  # opening the browser console, and running:
  #   JSON.parse(Object.entries(localStorage).find(([k])=>k.includes('auth-token'))[1]).access_token
  python3 test_isolation.py --token "eyJhbGci..."
"""

import argparse, json, sys, urllib.request, urllib.error

URL = "https://mxcfinwrgvxrwoyzjhhs.supabase.co"
KEY = "sb_publishable_GfXEXODcURNi7NLlg8htWg_xlcz-fGX"

TABLES = ["clients", "competitors", "signals", "briefings",
          "client_users", "competitor_emails", "brain_history",
          "brain_sweep_proposals"]

# Tables a signed-in client user is allowed to read at all.
CLIENT_READABLE = {"clients", "competitors", "signals", "briefings", "client_users"}

GREEN, RED, YELLOW, GREY, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m"
results = []
unreachable = []


def get(path, token=None):
    req = urllib.request.Request(f"{URL}/rest/v1/{path}")
    req.add_header("apikey", KEY)
    req.add_header("Authorization", f"Bearer {token or KEY}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"[]")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def check(name, passed, detail="", status=None):
    """status=0 means the request never reached Supabase. That is neither a
    pass nor a failure — reporting it as either would be dishonest, so it is
    tracked separately and the run refuses to claim a clean result."""
    if status == 0:
        unreachable.append(name)
        print(f"  [{GREY}????{RESET}] {name}  {GREY}unreachable{RESET}")
        return
    results.append(passed)
    mark = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
    print(f"  [{mark}] {name}" + (f"  {YELLOW}{detail}{RESET}" if detail and not passed else ""))


def part_a():
    """Anonymous requests. The publishable key on its own must return nothing."""
    print("\nPart A — anonymous access (publishable key only)")
    for t in TABLES:
        status, body = get(f"{t}?select=*&limit=5")
        empty = (status == 200 and isinstance(body, list) and len(body) == 0) or status in (401, 403)
        check(f"anon cannot read {t}", empty,
              f"status={status} rows={len(body) if isinstance(body, list) else body}", status)

    # The function the linter flagged. After the revoke it should refuse.
    req = urllib.request.Request(f"{URL}/rest/v1/rpc/apply_brain_proposal",
                                 data=json.dumps({"p_id": "00000000-0000-0000-0000-000000000000"}).encode())
    req.add_header("apikey", KEY)
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0
    check("anon cannot execute apply_brain_proposal", code in (401, 403, 404),
          f"status={code}. A 200 means the revoke in migration 001 section 7 has not been run.", code)


def part_b(token):
    """A real Bouldering Project session. Must see itself and nothing else."""
    print("\nPart B — signed-in Bouldering Project user")

    status, rows = get("clients?select=id,slug,name", token)
    ok = status == 200 and isinstance(rows, list) and len(rows) == 1
    check("sees exactly one client", ok, f"status={status} got={rows}")
    if not ok:
        print(f"  {YELLOW}Stopping Part B — cannot identify the tenant.{RESET}")
        return

    me, my_slug = rows[0]["id"], rows[0]["slug"]
    check("that client is bouldering-project", my_slug == "bouldering-project", f"got {my_slug}")

    # Every scoped table must return rows belonging only to this client.
    for t in ["competitors", "signals", "briefings"]:
        status, rows = get(f"{t}?select=client_id&limit=500", token)
        foreign = [r for r in rows if isinstance(r, dict) and r.get("client_id") != me] \
            if isinstance(rows, list) else ["<error>"]
        check(f"{t} contains no other client's rows", status == 200 and not foreign,
              f"status={status} foreign={len(foreign)}")

    # The direct attack: name another client's id explicitly.
    status, rows = get(f"briefings?select=id&client_id=neq.{me}&limit=5", token)
    check("asking for another client's briefings returns nothing",
          status == 200 and isinstance(rows, list) and len(rows) == 0,
          f"status={status} rows={rows}")

    status, rows = get(f"clients?select=id,slug&id=neq.{me}&limit=5", token)
    check("asking for another client record returns nothing",
          status == 200 and isinstance(rows, list) and len(rows) == 0,
          f"status={status} rows={rows}")

    # Internal-only tables stay closed even to a valid session.
    for t in TABLES:
        if t in CLIENT_READABLE:
            continue
        status, rows = get(f"{t}?select=*&limit=5", token)
        empty = (status == 200 and isinstance(rows, list) and len(rows) == 0) or status in (401, 403)
        check(f"signed-in user cannot read {t}", empty,
              f"status={status} rows={len(rows) if isinstance(rows, list) else rows}")

    # Unpublished briefings must be invisible.
    status, rows = get("briefings?select=id,published_at&published_at=is.null&limit=5", token)
    check("unpublished briefings are hidden",
          status == 200 and isinstance(rows, list) and len(rows) == 0,
          f"status={status} rows={rows}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", help="access_token from a signed-in Bouldering Project session")
    args = ap.parse_args()

    part_a()
    if args.token:
        part_b(args.token)
    else:
        print(f"\n{YELLOW}Part B skipped — no --token. Isolation is NOT verified until "
              f"Part B passes with a real client session.{RESET}")

    failed = results.count(False)
    print(f"\n{len(results) - failed} passed, {failed} failed, {len(unreachable)} unreachable")
    if unreachable:
        print(f"{YELLOW}Could not reach {URL}. Nothing was verified. Run this from a "
              f"machine with network access to Supabase.{RESET}")
    if failed:
        print(f"{RED}Do not issue a client login until this is clean.{RESET}")
    sys.exit(1 if (failed or unreachable or not results) else 0)
