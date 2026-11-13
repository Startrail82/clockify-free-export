#!/usr/bin/env python3
"""
Clockify free plan export -> Solidtime CSV files

Fetches your own Clockify data through the API while respecting the rate limit
of the free plan (30 requests per hour), and writes:

  export/raw/*.json          raw backup of what Clockify returned
  export/projects.csv        for Solidtime: Import > Generic Projects
  export/time_entries.csv    for Solidtime: Import > Generic Time Entries

Progress is kept in the "cache" folder. If you interrupt the script (Ctrl+C)
or it stops for any reason, just start it again: it continues where it left off.

Requires: Python 3.9 or newer, no extra packages.
The API key is read from the environment variable CLOCKIFY_API_KEY.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_BASE = os.environ.get("CLOCKIFY_API_BASE", "https://api.clockify.me/api/v1")
PAGE_SIZE = 200
USER_AGENT = "clockify-free-export/1.0"


def say(msg=""):
    print(msg, flush=True)


def fatal(msg):
    say("")
    say("ERROR: " + msg)
    sys.exit(1)


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d h %02d min" % (h, m)
    if m:
        return "%d min %02d s" % (m, s)
    return "%d s" % s


# ---------------------------------------------------------------------------
# API client with rate limit, resume and retries
# ---------------------------------------------------------------------------
class ClockifyClient:
    def __init__(self, api_key, cache_dir, limit, window, refresh):
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.limit = limit
        self.window = window
        self.refresh = refresh
        os.makedirs(cache_dir, exist_ok=True)
        self.log_path = os.path.join(cache_dir, "request_log.json")
        self.log = self._load_log()
        self.requests_made = 0
        self.from_cache = 0

    # -- request log (kept between runs, because the limit applies per hour)
    def _load_log(self):
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return sorted(float(t) for t in data)
        except (OSError, ValueError):
            return []

    def _save_log(self):
        with open(self.log_path, "w", encoding="utf-8") as f:
            json.dump(self.log, f)

    def _prune(self):
        now = time.time()
        self.log = [t for t in self.log if now - t < self.window + 5]

    def _sleep(self, seconds, reason):
        end = time.time() + seconds
        say("    Waiting: %s (%s)" % (fmt_duration(seconds), reason))
        last_print = time.time()
        while True:
            left = end - time.time()
            if left <= 0:
                break
            time.sleep(min(5.0, left))
            if time.time() - last_print >= 60 and end - time.time() > 0:
                say("    %s left to wait ..." % fmt_duration(end - time.time()))
                last_print = time.time()
        say("    Continuing.")

    def _wait_for_slot(self):
        self._prune()
        while len(self.log) >= self.limit:
            wait = self.log[0] + self.window + 5 - time.time()
            self._sleep(max(wait, 1), "limit of %d requests per hour reached" % self.limit)
            self._prune()

    # -- one request
    def get(self, path, params=None):
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        attempt = 0
        while True:
            self._wait_for_slot()
            self.log.append(time.time())
            self._save_log()
            self.requests_made += 1
            req = urllib.request.Request(
                url,
                headers={
                    "X-Api-Key": self.api_key,
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    if retry_after and retry_after.strip().isdigit():
                        wait = int(retry_after.strip()) + 5
                    else:
                        wait = self.window + 5
                    say("    Clockify says: too many requests (429).")
                    self._sleep(wait, "Clockify limit reached")
                    self.log = []
                    self._save_log()
                    continue
                if e.code in (401, 403):
                    fatal("Clockify rejected the key (HTTP %d). Check CLOCKIFY_API_KEY." % e.code)
                if e.code >= 500 and attempt < 3:
                    attempt += 1
                    self._sleep(10 * attempt, "server error %d, retry %d of 3" % (e.code, attempt))
                    continue
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                fatal("Clockify returned HTTP %d for %s\n%s" % (e.code, path, body))
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < 3:
                    attempt += 1
                    self._sleep(10 * attempt, "network error (%s), retry %d of 3" % (e, attempt))
                    continue
                fatal("Network error for %s: %s" % (path, e))

    # -- with cache
    def cached(self, name, path, params=None):
        cache_file = os.path.join(self.cache_dir, name + ".json")
        if not self.refresh and os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.from_cache += 1
                return data
            except (OSError, ValueError):
                pass
        data = self.get(path, params)
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, cache_file)
        return data

    def fetch_all(self, name, path, params, label):
        items = []
        page = 1
        while True:
            q = dict(params)
            q["page"] = page
            q["page-size"] = PAGE_SIZE
            data = self.cached("%s_p%03d" % (name, page), path, q)
            if not isinstance(data, list):
                fatal("Unexpected answer from Clockify for %s (expected a list)." % path)
            items.extend(data)
            say("    %s: page %d, %d items so far" % (label, page, len(items)))
            if len(data) < PAGE_SIZE:
                break
            page += 1
        return items


# ---------------------------------------------------------------------------
# Helper functions for the conversion
# ---------------------------------------------------------------------------
def iso_utc(value):
    """Convert a Clockify timestamp to ISO 8601 in UTC with a Z suffix."""
    if not value:
        return ""
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_DUR = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def iso_duration_seconds(value):
    if not value or not isinstance(value, str):
        return ""
    m = _DUR.match(value.strip())
    if not m:
        return ""
    d, h, mi, s = m.groups()
    total = int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    total = int(round(total))
    return str(total) if total > 0 else ""


def b(value):
    return "true" if value else "false"


def merge_by_id(*lists):
    seen = {}
    for lst in lists:
        for item in lst:
            seen[item["id"]] = item
    return list(seen.values())


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=1)


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Clockify free plan export to Solidtime CSV files (rate limited, resumable)")
    ap.add_argument("--out", default="export", help="output folder (default: export)")
    ap.add_argument("--cache", default="cache", help="cache folder used for resuming (default: cache)")
    ap.add_argument("--workspace", default=os.environ.get("CLOCKIFY_WORKSPACE_ID", ""),
                    help="workspace ID (default: your default workspace)")
    ap.add_argument("--from", dest="date_from", default="",
                    help="only time entries from this date, e.g. 2023-01-01")
    ap.add_argument("--limit", type=int, default=28,
                    help="max. requests per hour (default 28, the limit is 30)")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the cache and fetch everything again")
    ap.add_argument("--window", type=int, default=3600, help=argparse.SUPPRESS)
    args = ap.parse_args()

    api_key = os.environ.get("CLOCKIFY_API_KEY", "").strip()
    if not api_key:
        fatal("The environment variable CLOCKIFY_API_KEY is not set.\n"
              "       Windows (cmd):  set CLOCKIFY_API_KEY=your_key\n"
              "       Linux/macOS:    export CLOCKIFY_API_KEY=your_key")

    date_from = ""
    if args.date_from:
        try:
            date_from = datetime.strptime(args.date_from, "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00Z")
        except ValueError:
            fatal("--from must have the form YYYY-MM-DD, e.g. 2023-01-01")

    c = ClockifyClient(api_key, args.cache, args.limit, args.window, args.refresh)
    raw_dir = os.path.join(args.out, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    started = time.time()

    say("=" * 60)
    say("Clockify free plan export")
    say("Limit: at most %d requests per %s" % (args.limit, fmt_duration(args.window)))
    if c.log:
        say("Note: %d recent requests are still in the request log." % len(c.log))
    say("=" * 60)

    # Step 1: user
    say("")
    say("Step 1/4: fetching your Clockify user")
    user = c.cached("user", "/user")
    workspace = args.workspace or user.get("defaultWorkspace", "")
    if not workspace:
        fatal("No workspace found. Pass one with --workspace.")
    user_name = user.get("name", "")
    user_email = user.get("email", "")
    say("    User      : %s <%s>" % (user_name, user_email))
    say("    Workspace : %s" % workspace)
    write_json(os.path.join(raw_dir, "user.json"), user)

    # Step 2: clients
    say("")
    say("Step 2/4: fetching clients (including archived)")
    ws = "/workspaces/%s" % workspace
    clients = merge_by_id(
        c.fetch_all("clients", ws + "/clients", {}, "clients"),
        c.fetch_all("clients_archived", ws + "/clients", {"archived": "true"}, "clients (archived)"),
    )
    say("    Total clients: %d" % len(clients))
    write_json(os.path.join(raw_dir, "clients.json"), clients)
    client_names = dict((x["id"], x.get("name", "")) for x in clients)

    # Step 3: projects
    say("")
    say("Step 3/4: fetching projects (including archived)")
    projects = merge_by_id(
        c.fetch_all("projects", ws + "/projects", {}, "projects"),
        c.fetch_all("projects_archived", ws + "/projects", {"archived": "true"}, "projects (archived)"),
    )
    say("    Total projects: %d" % len(projects))
    write_json(os.path.join(raw_dir, "projects.json"), projects)
    project_by_id = dict((p["id"], p) for p in projects)

    # Step 4: time entries
    say("")
    say("Step 4/4: fetching your time entries")
    params = {"hydrated": "true"}
    if date_from:
        params["start"] = date_from
        say("    Only from %s" % args.date_from)
    entries = c.fetch_all(
        "entries" + ("_from_" + args.date_from if args.date_from else ""),
        "%s/user/%s/time-entries" % (ws, user["id"]),
        params,
        "time entries",
    )
    entries = merge_by_id(entries)
    say("    Total time entries fetched: %d" % len(entries))
    write_json(os.path.join(raw_dir, "time_entries.json"), entries)

    # ---- converting ----------------------------------------------------------
    say("")
    say("Converting to Solidtime format ...")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    project_rows = []
    for p in sorted(projects, key=lambda x: (x.get("name") or "").lower()):
        client = p.get("clientName") or client_names.get(p.get("clientId") or "", "")
        rate = ""
        hr = p.get("hourlyRate")
        if isinstance(hr, dict) and hr.get("amount"):
            rate = str(int(hr["amount"]))
        est = p.get("timeEstimate") or p.get("estimate")
        est_seconds = ""
        if isinstance(est, dict):
            est_seconds = iso_duration_seconds(est.get("estimate"))
        elif isinstance(est, str):
            est_seconds = iso_duration_seconds(est)
        project_rows.append([
            p.get("name", ""),
            p.get("color", "") or "",
            rate,
            b(p.get("public")),
            client,
            b(p.get("billable")),
            est_seconds,
            now_iso if p.get("archived") else "",
        ])
    write_csv(
        os.path.join(args.out, "projects.csv"),
        ["name", "color", "billable_rate", "is_public", "client", "billable_default",
         "estimated_time", "archived_at"],
        project_rows,
    )

    skipped_running = 0
    skipped_other = 0
    entry_rows = []
    for e in sorted(entries, key=lambda x: (x.get("timeInterval") or {}).get("start") or ""):
        ti = e.get("timeInterval") or {}
        if not ti.get("end"):
            skipped_running += 1
            continue
        if (e.get("type") or "REGULAR") != "REGULAR":
            skipped_other += 1
            continue
        proj = e.get("project") or project_by_id.get(e.get("projectId") or "") or {}
        client = proj.get("clientName") or client_names.get(proj.get("clientId") or "", "")
        task = (e.get("task") or {}).get("name", "") if isinstance(e.get("task"), dict) else ""
        tags = ",".join(t.get("name", "") for t in (e.get("tags") or []) if isinstance(t, dict))
        entry_rows.append([
            e.get("description", "") or "",
            b(e.get("billable")),
            client,
            proj.get("name", "") or "",
            tags,
            iso_utc(ti.get("start")),
            iso_utc(ti.get("end")),
            task,
            user_name,
            user_email,
        ])
    write_csv(
        os.path.join(args.out, "time_entries.csv"),
        ["description", "billable", "client", "project", "tags", "start", "end", "task",
         "user_name", "user_email"],
        entry_rows,
    )

    # ---- summary ------------------------------------------------------
    say("")
    say("=" * 60)
    say("DONE")
    say("  Clients                : %d" % len(clients))
    say("  Projects               : %d (%s)" % (
        len(projects), "of which %d archived" % sum(1 for p in projects if p.get("archived"))))
    say("  Time entries written   : %d" % len(entry_rows))
    if entry_rows:
        say("  Period                 : %s to %s" % (entry_rows[0][5], entry_rows[-1][6]))
    if skipped_running:
        say("  Skipped (timer still running): %d" % skipped_running)
    if skipped_other:
        say("  Skipped (breaks etc.)        : %d" % skipped_other)
    say("  Requests to Clockify   : %d (from cache: %d)" % (c.requests_made, c.from_cache))
    say("  Duration               : %s" % fmt_duration(time.time() - started))
    say("")
    say("Files in %s:" % os.path.abspath(args.out))
    say("  projects.csv      -> Solidtime: Import > Generic Projects (first)")
    say("  time_entries.csv  -> Solidtime: Import > Generic Time Entries (then)")
    say("  raw/*.json        -> raw backup from Clockify")
    say("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("")
        say("Interrupted. Progress is saved in the cache folder.")
        say("Start the script again to continue.")
        sys.exit(130)
