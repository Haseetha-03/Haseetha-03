#!/usr/bin/env python3
"""Generate repository analytics for a GitHub profile README.

Reads public data from the GitHub REST + GraphQL APIs (using the workflow's
built-in GITHUB_TOKEN), renders a Markdown dashboard plus dark/light SVG
charts, and rewrites ONLY the block between the START/END markers in README.md.

Standard library only. No third-party services. No secrets beyond GITHUB_TOKEN.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
START = "<!-- START_REPO_ANALYTICS -->"
END = "<!-- END_REPO_ANALYTICS -->"

README = Path(os.environ.get("README_PATH", "README.md"))
ASSET_DIR = Path(os.environ.get("ASSET_DIR", "assets/analytics"))
USER = os.environ.get("GH_USER") or os.environ.get("GITHUB_REPOSITORY_OWNER", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
PROFILE_REPO = os.environ.get("GITHUB_REPOSITORY") or f"{USER}/{USER}"

FEATURED_COUNT = 4
RECENT_DAYS = 90
CHART_WEEKS = 26

# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------


class RateLimited(RuntimeError):
    pass


def api(path, *, method="GET", payload=None, params=None):
    """Return (status, headers, json_body). 404/409/451 are returned, not raised."""
    url = path if path.startswith("http") else API + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "profile-readme-analytics",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(payload).encode() if payload is not None else None

    for attempt in range(4):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, resp.headers, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as err:
            if err.code in (404, 409, 451):
                return err.code, err.headers, None
            if err.code in (403, 429):
                retry_after = err.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(int(retry_after), 60))
                    continue
                if err.headers.get("X-RateLimit-Remaining") == "0":
                    raise RateLimited("GitHub API rate limit reached") from err
                time.sleep(15 * (attempt + 1))  # secondary rate limit
                continue
            if err.code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"GitHub API request failed after retries: {path}")


def count_items(path, params):
    """Count items in a list endpoint using the Link header (1 request)."""
    status, headers, body = api(path, params=dict(params, per_page=1))
    if status != 200 or body is None:
        return 0
    match = re.search(r'<[^>]*[?&]page=(\d+)[^>]*>;\s*rel="last"', headers.get("Link", ""))
    return int(match.group(1)) if match else len(body)


def search_count(query):
    status, _, body = api("/search/issues", params={"q": query, "per_page": 1})
    time.sleep(2)  # stay well under the search rate limit
    if status == 200 and body:
        return body.get("total_count")
    return None


def parse_ts(value):
    if not value:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------


def fetch_repos():
    repos, page = [], 1
    while True:
        status, _, body = api(
            f"/users/{USER}/repos",
            params={"type": "owner", "per_page": 100, "page": page, "sort": "pushed"},
        )
        if status != 200 or not body:
            break
        repos.extend(body)
        if len(body) < 100:
            break
        page += 1
    return repos


def enrich(repo, since_iso):
    full = repo["full_name"]
    d = {
        "name": repo["name"],
        "full": full,
        "url": repo["html_url"],
        "description": (repo.get("description") or "").strip(),
        "language": repo.get("language") or "",
        "stars": repo.get("stargazers_count", 0),
        "forks": repo.get("forks_count", 0),
        "pushed": parse_ts(repo.get("pushed_at") or repo.get("updated_at")),
        "topics": repo.get("topics") or [],
        "homepage": (repo.get("homepage") or "").strip(),
        "archived": bool(repo.get("archived")),
        "commits": 0,
        "commits_recent": 0,
        "languages": {},
        "collaborators": set(),
        "has_readme": False,
        "prs": None,
        "issues": None,
        "score": 0.0,
    }
    d["commits"] = count_items(f"/repos/{full}/commits", {"author": USER})
    if d["commits"] == 0:
        return d  # empty repo or no commits by the owner
    d["commits_recent"] = count_items(f"/repos/{full}/commits", {"author": USER, "since": since_iso})

    status, _, langs = api(f"/repos/{full}/languages")
    if status == 200 and langs:
        d["languages"] = langs

    status, _, people = api(f"/repos/{full}/contributors", params={"per_page": 100})
    if status == 200 and people:
        d["collaborators"] = {
            p["login"] for p in people
            if p.get("type") == "User" and p.get("login", "").lower() != USER.lower()
        }

    status, _, _ = api(f"/repos/{full}/readme")
    d["has_readme"] = status == 200
    return d


GQL = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { firstDay contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def fetch_calendar():
    try:
        status, _, body = api("/graphql", method="POST", payload={"query": GQL, "variables": {"login": USER}})
        cal = body["data"]["user"]["contributionsCollection"]["contributionCalendar"]
        return cal
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001 - analytics must degrade gracefully
        print(f"Contribution calendar unavailable: {exc}", file=sys.stderr)
        return None


def calendar_stats(cal, today):
    if not cal:
        return None
    days = sorted(
        (day["date"], day["contributionCount"])
        for week in cal["weeks"] for day in week["contributionDays"]
        if day["date"] <= today.isoformat()
    )
    longest = run = 0
    for _, count in days:
        run = run + 1 if count > 0 else 0
        longest = max(longest, run)
    idx = len(days) - 1
    if idx >= 0 and days[idx][1] == 0:
        idx -= 1  # today may not have contributions yet
    current = 0
    while idx >= 0 and days[idx][1] > 0:
        current += 1
        idx -= 1
    weekly = []
    for week in cal["weeks"][-CHART_WEEKS:]:
        total = sum(d["contributionCount"] for d in week["contributionDays"] if d["date"] <= today.isoformat())
        weekly.append((week["firstDay"], total))
    return {"total": cal["totalContributions"], "current": current, "longest": longest, "weekly": weekly}


def fetch_recent_activity(limit=6):
    status, _, body = api(f"/users/{USER}/events/public", params={"per_page": 60})
    if status != 200 or not body:
        return []
    merged, order = {}, []
    for ev in body:
        kind, repo = ev.get("type"), ev.get("repo", {}).get("name", "")
        payload, date = ev.get("payload") or {}, ev.get("created_at", "")[:10]
        if kind == "PushEvent":
            key, label = ("push", repo, date), "push"
            n = payload.get("size") or len(payload.get("commits") or []) or 0
        elif kind == "PullRequestEvent":
            pr = payload.get("pull_request") or {}
            action = "merged" if pr.get("merged") else payload.get("action", "updated")
            key, label, n = ("pr", repo, date, action), f"{action} pull request", 0
        elif kind == "IssuesEvent":
            action = payload.get("action", "updated")
            key, label, n = ("issue", repo, date, action), f"{action} issue", 0
        elif kind == "CreateEvent" and payload.get("ref_type") == "repository":
            key, label, n = ("create", repo, date), "created repository", 0
        else:
            continue
        if key in merged:
            merged[key]["n"] += n
        else:
            merged[key] = {"label": label, "repo": repo, "date": date, "n": n}
            order.append(key)
    return [merged[k] for k in order[:limit]]


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------


def score_repos(repos, now):
    """Composite 0-100 score. Not just stars.

    recency 30% | commits in last 90 days 25% | commit history 15%
    relevance 15% | stars 10% | forks 5%
    """
    if not repos:
        return
    max_recent = max(r["commits_recent"] for r in repos) or 1
    max_commits = max(r["commits"] for r in repos) or 1
    max_stars = max(r["stars"] for r in repos)
    max_forks = max(r["forks"] for r in repos)
    for r in repos:
        weeks = max(0, (now - r["pushed"]).days) // 7  # weekly steps keep output stable
        recency = 0.5 ** (weeks / 8.0)
        activity = math.sqrt(r["commits_recent"] / max_recent)
        depth = math.log1p(r["commits"]) / math.log1p(max_commits)
        stars = r["stars"] / max_stars if max_stars else 0.0
        forks = r["forks"] / max_forks if max_forks else 0.0
        relevance = sum([
            bool(r["description"]), bool(r["topics"]), r["has_readme"],
            bool(r["language"] or r["homepage"]),
        ]) / 4
        r["score"] = 100 * (
            0.30 * recency + 0.25 * activity + 0.15 * depth
            + 0.15 * relevance + 0.10 * stars + 0.05 * forks
        )


# --------------------------------------------------------------------------
# SVG rendering
# --------------------------------------------------------------------------

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
THEMES = {
    "dark": dict(bg="#0d1117", border="#30363d", text="#e6edf3", muted="#8b949e",
                 track="#21262d", a="#2dd4bf", b="#818cf8"),
    "light": dict(bg="#ffffff", border="#d0d7de", text="#1f2328", muted="#656d76",
                  track="#e6eaef", a="#0f766e", b="#4f46e5"),
}
LANG_COLORS = {
    "C": "#9ca3af", "C++": "#f34b7d", "Python": "#4b8bbe", "JavaScript": "#f1e05a",
    "TypeScript": "#3178c6", "HTML": "#e34c26", "CSS": "#a78bfa", "Java": "#f89820",
    "Shell": "#89e051", "Jupyter Notebook": "#da5b0b", "Kotlin": "#a97bff",
    "Dart": "#00b4ab", "SCSS": "#c6538c", "PHP": "#8892bf", "Go": "#00add8",
    "Dockerfile": "#2496ed", "Batchfile": "#c1f12e", "Other": "#6e7681",
}
FALLBACK = ["#2dd4bf", "#818cf8", "#f472b6", "#fbbf24", "#34d399", "#60a5fa", "#fb923c"]


def esc(text):
    return html.escape(str(text), quote=True)


def svg_open(w, h, title, t):
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-label="{esc(title)}" font-family="{FONT}">',
        f"<title>{esc(title)}</title>",
        f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10" fill="{t["bg"]}" stroke="{t["border"]}"/>',
    ]


def svg_languages(langs, theme):
    t = THEMES[theme]
    pad, W = 24, 760
    barw = W - 2 * pad
    total = sum(langs.values())
    items = sorted(langs.items(), key=lambda kv: -kv[1])
    top = items[:7]
    rest = sum(v for _, v in items[7:])
    if rest:
        top.append(("Other", rest))
    rows = max(1, math.ceil(len(top) / 2))
    H = 112 + rows * 26 + 8
    out = svg_open(W, H, "Languages used across repositories", t)
    out.append(f'<text x="{pad}" y="36" font-size="16" font-weight="600" fill="{t["text"]}">Languages</text>')
    out.append(f'<text x="{pad}" y="56" font-size="12" fill="{t["muted"]}">Share of code by size across public repositories (forks excluded)</text>')
    if not total:
        out.append(f'<text x="{pad}" y="92" font-size="13" fill="{t["muted"]}">No language data available yet.</text>')
    else:
        out.append(f'<clipPath id="bar"><rect x="{pad}" y="72" width="{barw}" height="10" rx="5"/></clipPath>')
        out.append(f'<rect x="{pad}" y="72" width="{barw}" height="10" rx="5" fill="{t["track"]}"/>')
        out.append('<g clip-path="url(#bar)">')
        x, colors = float(pad), {}
        for i, (name, size) in enumerate(top):
            colors[name] = LANG_COLORS.get(name, FALLBACK[i % len(FALLBACK)])
            w = barw * size / total
            out.append(f'<rect x="{x:.2f}" y="72" width="{max(w, 0.5):.2f}" height="10" fill="{colors[name]}"/>')
            x += w
        out.append("</g>")
        col_w = barw / 2
        for i, (name, size) in enumerate(top):
            cx = pad + (i % 2) * col_w
            cy = 114 + (i // 2) * 26
            out.append(f'<circle cx="{cx + 5:.1f}" cy="{cy - 4}" r="5" fill="{colors[name]}"/>')
            out.append(f'<text x="{cx + 18:.1f}" y="{cy}" font-size="13" fill="{t["text"]}">{esc(name)}</text>')
            out.append(f'<text x="{cx + col_w - 24:.1f}" y="{cy}" font-size="13" text-anchor="end" fill="{t["muted"]}">{size / total * 100:.1f}%</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def svg_activity(weekly, theme):
    t = THEMES[theme]
    W, H, pad = 760, 220, 24
    out = svg_open(W, H, "Weekly contribution activity", t)
    out.append(f'<text x="{pad}" y="36" font-size="16" font-weight="600" fill="{t["text"]}">Contribution activity</text>')
    out.append(f'<text x="{pad}" y="56" font-size="12" fill="{t["muted"]}">Public contributions per week, last {len(weekly) or CHART_WEEKS} weeks</text>')
    if not weekly:
        out.append(f'<text x="{pad}" y="120" font-size="13" fill="{t["muted"]}">Contribution data is unavailable right now.</text>')
        out.append("</svg>")
        return "\n".join(out) + "\n"
    top, bottom = 80, H - 40
    chart_h = bottom - top
    peak = max(c for _, c in weekly) or 1
    n, gap = len(weekly), 4
    bw = (W - 2 * pad - gap * (n - 1)) / n
    out.append(f'<defs><linearGradient id="g" x1="0" y1="1" x2="0" y2="0">'
               f'<stop offset="0" stop-color="{t["a"]}"/><stop offset="1" stop-color="{t["b"]}"/></linearGradient></defs>')
    for i, (day, count) in enumerate(weekly):
        h = max(3.0, count / peak * chart_h) if count else 3.0
        x = pad + i * (bw + gap)
        fill = "url(#g)" if count else t["track"]
        out.append(f'<rect x="{x:.2f}" y="{bottom - h:.2f}" width="{bw:.2f}" height="{h:.2f}" rx="2" fill="{fill}">'
                   f'<title>Week of {esc(day)}: {count} contributions</title></rect>')
    out.append(f'<text x="{pad}" y="{H - 14}" font-size="11" fill="{t["muted"]}">{esc(weekly[0][0])}</text>')
    out.append(f'<text x="{W - pad}" y="{H - 14}" font-size="11" text-anchor="end" fill="{t["muted"]}">{esc(weekly[-1][0])}</text>')
    out.append(f'<text x="{W - pad}" y="36" font-size="12" text-anchor="end" fill="{t["muted"]}">peak {peak} / week</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def write_if_changed(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def image_urls(name, content_by_theme):
    base = f"https://raw.githubusercontent.com/{PROFILE_REPO}/HEAD/{ASSET_DIR.as_posix()}"
    urls = {}
    for theme, content in content_by_theme.items():
        digest = hashlib.md5(content.encode()).hexdigest()[:8]  # cache-buster only
        urls[theme] = f"{base}/{name}-{theme}.svg?v={digest}"
    return urls


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------


def num(value):
    return "–" if value is None else f"{value:,}"


def short_date(d):
    return f"{d:%b} {d.day}, {d.year}"


def clean(text, limit=90):
    text = " ".join(text.split()).replace("|", "/")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return html.escape(text, quote=False)


def score_bar(score, width=10):
    filled = max(0, min(width, round(score / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def link(r):
    return f"[{r['name']}]({r['url']})"


def picture(urls, alt):
    return (
        "<picture>\n"
        f'  <source media="(prefers-color-scheme: dark)" srcset="{urls["dark"]}">\n'
        f'  <source media="(prefers-color-scheme: light)" srcset="{urls["light"]}">\n'
        f'  <img alt="{esc(alt)}" src="{urls["light"]}" width="100%">\n'
        "</picture>"
    )


def render_markdown(d):
    L = []
    L.append(f"<sub>Generated from the GitHub API by a scheduled workflow. Last refreshed {d['stamp']} (UTC).</sub>")
    L.append("")
    L.append("**Repositories (public)**")
    L.append("")
    L.append("| Repositories | Stars | Forks | Commits | Collaborators |")
    L.append("|:---:|:---:|:---:|:---:|:---:|")
    L.append(f"| **{num(d['repos'])}** | **{num(d['stars'])}** | **{num(d['forks'])}** | **{num(d['commits'])}** | **{num(d['collaborators'])}** |")
    L.append("")
    L.append("**Contributions**")
    L.append("")
    L.append("| Pull requests | Merged | Issues opened | Last 12 months | Current streak | Longest streak |")
    L.append("|:---:|:---:|:---:|:---:|:---:|:---:|")
    cal = d["calendar"] or {}
    cur = f"{cal['current']} d" if cal else "–"
    lng = f"{cal['longest']} d" if cal else "–"
    L.append(f"| **{num(d['prs'])}** | **{num(d['prs_merged'])}** | **{num(d['issues'])}** | **{num(cal.get('total'))}** | **{cur}** | **{lng}** |")
    L.append("")
    L.append(picture(d["img_activity"], "Bar chart of weekly public contributions"))
    L.append("")
    L.append("#### Featured repository activity")
    L.append("")
    if d["featured"]:
        L.append("| Repository | Language | Commits (90d / all) | Stars / Forks | PRs / Issues | Updated | Score |")
        L.append("|:---|:---|:---:|:---:|:---:|:---:|:---|")
        for r in d["featured"]:
            desc = clean(r["description"]) if r["description"] else "No description yet"
            L.append(
                f"| **{link(r)}**<br/><sub>{desc}</sub> | {r['language'] or '–'} "
                f"| {r['commits_recent']} / {r['commits']} | {r['stars']} / {r['forks']} "
                f"| {num(r['prs'])} / {num(r['issues'])} | {short_date(r['pushed'])} "
                f"| `{score_bar(r['score'])}` {round(r['score'])} |"
            )
    else:
        L.append("_No repositories with commit history yet._")
    L.append("")
    L.append("<details>")
    L.append("<summary>How repositories are ranked</summary>")
    L.append("")
    L.append("Not sorted by stars. Each non-fork, non-archived repository gets a 0-100 score: "
             "recency of last push (30%), commits in the last 90 days (25%), total commit history (15%), "
             "completeness of description, topics, README and language (15%), stars (10%), forks (5%). "
             "The score is a rough guide to current activity, not a measure of code quality.")
    L.append("")
    L.append("</details>")
    L.append("")
    L.append("#### Highlights")
    L.append("")

    def lines(items, fmt):
        return "<br/>".join(f"{i}. {fmt(r)}" for i, r in enumerate(items, 1)) or "<sub>Nothing yet</sub>"

    starred = lines(d["starred"], lambda r: f"{link(r)} ({r['stars']} ★)") if d["starred"] else "<sub>No stars yet</sub>"
    L.append("| Most active (90 days) | Recently updated | Most starred |")
    L.append("|:---|:---|:---|")
    L.append("| " + " | ".join([
        lines(d["most_active"], lambda r: f"{link(r)} ({r['commits_recent']} commits)"),
        lines(d["recent"], lambda r: f"{link(r)} ({short_date(r['pushed'])})"),
        starred,
    ]) + " |")
    L.append("")
    L.append(picture(d["img_languages"], "Bar chart of languages used across repositories"))
    L.append("")
    L.append("#### Recent activity")
    L.append("")
    if d["activity"]:
        for ev in d["activity"]:
            repo = ev["repo"]
            name = repo.split("/", 1)[1] if repo.lower().startswith(USER.lower() + "/") else repo
            label = ev["label"]
            if label == "push":
                label = f"Pushed {ev['n']} commit{'s' if ev['n'] != 1 else ''} to" if ev["n"] else "Pushed to"
            else:
                label = label[0].upper() + label[1:] + " in" if "request" in label or "issue" in label else label[0].upper() + label[1:]
            when = short_date(dt.date.fromisoformat(ev["date"]))
            L.append(f"- {label} [{name}](https://github.com/{repo}) · {when}")
    else:
        L.append("_No public activity in the last 90 days._")
    return "\n".join(L)


def strip_stamp(text):
    return re.sub(r"<sub>Generated from the GitHub API[^\n]*</sub>", "", text)


def update_readme(section):
    text = README.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    found = pattern.search(text)
    if not found:
        sys.exit(f"Markers {START} / {END} not found in {README}.")
    block = f"{START}\n{section}\n{END}"
    if strip_stamp(found.group(0)) == strip_stamp(block):
        return False
    README.write_text(pattern.sub(lambda _m: block, text, count=1), encoding="utf-8")
    return True


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def collect():
    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repos = fetch_repos()
    if not repos:
        sys.exit("No public repositories returned; leaving README untouched.")
    own = [r for r in repos if not r.get("fork")]
    print(f"{len(repos)} public repositories ({len(own)} original). Fetching details...")
    details = [enrich(r, since) for r in own]

    languages, collaborators = {}, set()
    for r in details:
        collaborators |= r["collaborators"]
        for lang, size in r["languages"].items():
            languages[lang] = languages.get(lang, 0) + size

    eligible = [r for r in details
                if not r["archived"] and r["commits"] > 0 and r["name"].lower() != USER.lower()]
    score_repos(eligible, now)
    ranked = sorted(eligible, key=lambda r: (-r["score"], -r["pushed"].timestamp()))
    featured = ranked[:FEATURED_COUNT]
    for r in featured:
        r["prs"] = search_count(f"repo:{r['full']} is:pr")
        r["issues"] = search_count(f"repo:{r['full']} is:issue")

    cal_stats = calendar_stats(fetch_calendar(), now.date())
    data = {
        "stamp": now.strftime("%Y-%m-%d"),
        "repos": len(repos),
        "stars": sum(r["stars"] for r in details),
        "forks": sum(r["forks"] for r in details),
        "commits": sum(r["commits"] for r in details),
        "collaborators": len(collaborators),
        "prs": search_count(f"author:{USER} is:pr"),
        "prs_merged": search_count(f"author:{USER} is:pr is:merged"),
        "issues": search_count(f"author:{USER} is:issue"),
        "calendar": cal_stats,
        "weekly": cal_stats["weekly"] if cal_stats else [],
        "languages": languages,
        "featured": featured,
        "most_active": [r for r in sorted(eligible, key=lambda r: -r["commits_recent"]) if r["commits_recent"] > 0][:3],
        "recent": sorted(eligible, key=lambda r: -r["pushed"].timestamp())[:3],
        "starred": [r for r in sorted(eligible, key=lambda r: -r["stars"]) if r["stars"] > 0][:3],
        "activity": fetch_recent_activity(),
    }
    return data


def build(data):
    """Write SVG assets and return the Markdown section. Pure function of `data`."""
    lang_svgs = {th: svg_languages(data["languages"], th) for th in THEMES}
    act_svgs = {th: svg_activity(data["weekly"], th) for th in THEMES}
    changed = False
    for th in THEMES:
        changed |= write_if_changed(ASSET_DIR / f"languages-{th}.svg", lang_svgs[th])
        changed |= write_if_changed(ASSET_DIR / f"activity-{th}.svg", act_svgs[th])
    data["img_languages"] = image_urls("languages", lang_svgs)
    data["img_activity"] = image_urls("activity", act_svgs)
    return render_markdown(data), changed


def main():
    if not USER:
        sys.exit("Set GH_USER (or run inside GitHub Actions).")
    data = collect()
    section, assets_changed = build(data)
    readme_changed = update_readme(section)
    print(f"README changed: {readme_changed} | assets changed: {assets_changed}")


if __name__ == "__main__":
    main()
