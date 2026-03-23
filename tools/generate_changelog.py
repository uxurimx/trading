#!/usr/bin/env python3
"""
tools/generate_changelog.py
────────────────────────────
Genera index.html — Dashboard visual del changelog de QTS.
Se ejecuta automáticamente vía pre-commit hook de git.

Uso manual:
    python3 tools/generate_changelog.py
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_FILE  = PROJECT_ROOT / "index.html"
GEN_DATE     = datetime.now().strftime("%Y-%m-%d %H:%M")

# ─── Versiones manuales (hash_prefix → etiqueta de version) ──────────────────
# Cuando hay commits clave que marcan hitos, se definen aquí.
VERSION_MILESTONES: dict[str, str] = {
    "a416c36": "v0.5",
    "0a447db": "v0.4",
    "06aedd1": "v0.3",
    "1965f1e": "v0.2",
    "5348966": "v0.1",
}

# ─── Categorías ───────────────────────────────────────────────────────────────
CATEGORIES = {
    "feat":    ("✨", "Feature",   "#6366f1"),
    "fix":     ("🐛", "Fix",       "#f59e0b"),
    "risk":    ("🛡️", "Risk/SL",   "#22c55e"),
    "ai":      ("🤖", "AI/IA",     "#8b5cf6"),
    "session": ("📊", "Sesión",    "#06b6d4"),
    "market":  ("📈", "Mercado",   "#3b82f6"),
    "perf":    ("⚡", "Perf",      "#eab308"),
    "ui":      ("🎨", "UI",        "#ec4899"),
    "refactor":("🔧", "Refactor",  "#64748b"),
    "other":   ("📝", "Misc",      "#94a3b8"),
}


def run_git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=PROJECT_ROOT)
    return r.stdout.strip()


def categorize(message: str) -> str:
    m = message.lower()
    if any(k in m for k in ["fix", "error", "bug", "arreglo", "correc", "fail", "crash", "broken"]):
        return "fix"
    if any(k in m for k in ["sl", " tp ", "trailing", "stop loss", "breakeven", "be y", "riesgo",
                              "slippage", "watchdog", "viabilit", "g1", "g2", "g3", "trailing stop"]):
        return "risk"
    if any(k in m for k in ["ai", " ia ", "strategy", "estrategia", "gpt", "llm", "modelo", "modelos"]):
        return "ai"
    if any(k in m for k in ["session", "sesion", "sesión", "tsaa"]):
        return "session"
    if any(k in m for k in ["symbol", "ticker", "market", "mercado", "bybit", "volumen", "volume"]):
        return "market"
    if any(k in m for k in ["perf", "optim", "speed", "fast", "velocidad", "nano"]):
        return "perf"
    if any(k in m for k in ["ui", "interface", "gtk", "display", "view", "panel", "dashboard"]):
        return "ui"
    if any(k in m for k in ["refactor", "clean", "limpia", "restructure", "reorgan"]):
        return "refactor"
    if any(k in m for k in ["add", "agrega", "nuevo", "nueva", "feat", "implementa", "crea",
                              "indicadores", "indicators", "bar", "logo", "zona"]):
        return "feat"
    return "other"


# ─── Parsear git log ──────────────────────────────────────────────────────────

def parse_commits() -> tuple[list[dict], dict]:
    # 1. Metadata: hash|short|subject|author|date|refs
    meta_raw = run_git(
        "log",
        "--pretty=format:%H\x1f%h\x1f%s\x1f%an\x1f%ad\x1f%D",
        "--date=format:%Y-%m-%d %H:%M",
    )
    commits_meta: list[dict] = []
    for line in meta_raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) < 5:
            continue
        refs  = parts[5] if len(parts) > 5 else ""
        tags  = [r.strip().replace("tag:", "").strip()
                 for r in refs.split(",") if "tag:" in r]
        commits_meta.append({
            "hash":    parts[0],
            "short":   parts[1],
            "message": parts[2],
            "author":  parts[3],
            "date":    parts[4],
            "tags":    tags,
        })

    # 2. Commit bodies — second pass using END_BODY sentinel
    bodies: dict[str, str] = {}
    body_raw = run_git(
        "log",
        "--pretty=format:COMMIT_HASH:%H%n%b%nEND_BODY",
    )
    current_hash = ""
    body_lines: list[str] = []
    for line in body_raw.splitlines():
        if line.startswith("COMMIT_HASH:"):
            if current_hash:
                bodies[current_hash] = "\n".join(body_lines).strip()
            current_hash = line[len("COMMIT_HASH:"):]
            body_lines = []
        elif line == "END_BODY":
            if current_hash:
                bodies[current_hash] = "\n".join(body_lines).strip()
            current_hash = ""
            body_lines = []
        else:
            if current_hash:
                body_lines.append(line)
    if current_hash:
        bodies[current_hash] = "\n".join(body_lines).strip()

    # 3. Numstat: insertions/deletions/files per commit — also store per-commit file list
    numstat_raw = run_git("log", "--pretty=format:COMMIT:%H", "--numstat")
    stats: dict[str, dict] = {}
    files_per_commit: dict[str, list[dict]] = {}
    current_hash = ""
    file_changes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for line in numstat_raw.splitlines():
        if line.startswith("COMMIT:"):
            current_hash = line[7:]
            stats[current_hash] = {"ins": 0, "del": 0, "files": 0}
            files_per_commit[current_hash] = []
        elif current_hash and line.strip() and "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 3:
                try:
                    ins = int(parts[0]) if parts[0] != "-" else 0
                    dels = int(parts[1]) if parts[1] != "-" else 0
                    fname = parts[2]
                    stats[current_hash]["ins"]   += ins
                    stats[current_hash]["del"]   += dels
                    stats[current_hash]["files"] += 1
                    files_per_commit[current_hash].append({"file": fname, "ins": ins, "del": dels})
                    file_changes[fname]["ins"]   += ins
                    file_changes[fname]["dels"]  += dels
                    file_changes[fname]["commits"] += 1
                except ValueError:
                    pass

    # 4. Merge
    commits = []
    for c in commits_meta:
        h = c["hash"]
        s = stats.get(h, {"ins": 0, "del": 0, "files": 0})
        cat = categorize(c["message"])
        # Check if this is a version milestone
        version_label = ""
        for prefix, label in VERSION_MILESTONES.items():
            if h.startswith(prefix) or c["short"].startswith(prefix):
                version_label = label
                break
        commits.append({
            **c,
            **s,
            "cat": cat,
            "version": version_label,
            "body": bodies.get(h, ""),
            "files_list": files_per_commit.get(h, []),
        })

    return commits, dict(file_changes)


def compute_stats(commits: list[dict], file_changes: dict) -> dict:
    authors  = list(dict.fromkeys(c["author"] for c in commits))
    days     = defaultdict(int)
    cat_dist = defaultdict(int)
    total_ins  = sum(c["ins"]   for c in commits)
    total_dels = sum(c["del"]   for c in commits)
    total_files_changed = sum(c["files"] for c in commits)

    for c in commits:
        days[c["date"][:10]] += 1
        cat_dist[c["cat"]]   += 1

    top_files = sorted(
        [(f, v) for f, v in file_changes.items()],
        key=lambda x: x[1].get("commits", 0),
        reverse=True,
    )[:8]

    # Actividad por semana (últimas 4)
    activity: dict[str, int] = defaultdict(int)
    for c in commits:
        try:
            d = datetime.strptime(c["date"][:10], "%Y-%m-%d")
            week = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
            activity[week] += 1
        except ValueError:
            pass

    # Módulos más activos
    module_counts: dict[str, int] = defaultdict(int)
    for fname, v in file_changes.items():
        parts = Path(fname).parts
        module = parts[0] if parts else "root"
        module_counts[module] += v.get("commits", 0)

    return {
        "total":        len(commits),
        "authors":      authors,
        "total_ins":    total_ins,
        "total_dels":   total_dels,
        "total_files":  total_files_changed,
        "first_date":   commits[-1]["date"][:10] if commits else "—",
        "last_date":    commits[0]["date"][:10]  if commits else "—",
        "cat_dist":     dict(cat_dist),
        "top_files":    top_files,
        "activity":     dict(sorted(activity.items())),
        "modules":      dict(sorted(module_counts.items(), key=lambda x: x[1], reverse=True)),
    }


# ─── Agrupar commits por día ──────────────────────────────────────────────────

def group_by_day(commits: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for c in commits:
        day = c["date"][:10]
        groups.setdefault(day, []).append(c)
    return [{"day": day, "commits": clist} for day, clist in groups.items()]


# ─── Renderizado HTML ─────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def render_commit_card(c: dict) -> str:
    cat   = c["cat"]
    emoji, label, color = CATEGORIES.get(cat, ("📝", "Misc", "#94a3b8"))
    msg   = _esc(c["message"])
    short = _esc(c["short"])
    author= _esc(c["author"])
    date  = _esc(c["date"][11:16])  # HH:MM
    ins   = c["ins"]
    dels  = c["del"]
    files = c["files"]
    full_hash = _esc(c["hash"])

    tags_html = ""
    for t in c.get("tags", []):
        tags_html += f'<span class="tag-badge">{_esc(t)}</span>'

    version_html = ""
    if c.get("version"):
        version_html = f'<span class="version-badge">{_esc(c["version"])}</span>'

    stats_html = ""
    if files:
        stats_html = (
            f'<span class="stat-chip files">{files} {"file" if files == 1 else "files"}</span>'
            f'<span class="stat-chip ins">+{ins}</span>'
            f'<span class="stat-chip del">−{dels}</span>'
        )

    return f"""
    <div class="commit-card" data-cat="{cat}" data-msg="{msg.lower()}" data-hash="{full_hash}" onclick="showCommitDetail(this)">
      <div class="commit-left">
        <span class="cat-pill" style="background:{color}22;color:{color};border-color:{color}44"
              title="{label}">{emoji} {label}</span>
      </div>
      <div class="commit-body">
        <div class="commit-msg">{version_html}{tags_html}{msg}</div>
        <div class="commit-meta">
          <code class="hash-chip">{short}</code>
          <span class="meta-author">👤 {author}</span>
          <span class="meta-time">🕐 {date}</span>
          {stats_html}
        </div>
      </div>
    </div>"""


def render_day_group(group: dict) -> str:
    day = group["day"]
    try:
        d   = datetime.strptime(day, "%Y-%m-%d")
        day_label = d.strftime("%A %d %B %Y")
    except ValueError:
        day_label = day
    count   = len(group["commits"])
    cards   = "\n".join(render_commit_card(c) for c in group["commits"])
    plural  = "commit" if count == 1 else "commits"
    return f"""
  <div class="day-group">
    <div class="day-header">
      <div class="day-dot"></div>
      <span class="day-label">{_esc(day_label)}</span>
      <span class="day-count">{count} {plural}</span>
    </div>
    <div class="day-commits">
{cards}
    </div>
  </div>"""


def render_top_files(top_files: list) -> str:
    if not top_files:
        return ""
    rows = ""
    max_c = max(v.get("commits", 1) for _, v in top_files) or 1
    for fname, v in top_files:
        pct  = int(v.get("commits", 0) / max_c * 100)
        name = _esc(Path(fname).name)
        path = _esc(str(Path(fname).parent))
        rows += f"""
        <div class="file-row">
          <div class="file-name">
            <span class="file-path">{path}/</span><strong>{name}</strong>
          </div>
          <div class="file-bar-wrap">
            <div class="file-bar" style="width:{pct}%"></div>
          </div>
          <span class="file-count">{v.get("commits",0)}c</span>
        </div>"""
    return rows


def render_cat_bars(cat_dist: dict) -> str:
    total = sum(cat_dist.values()) or 1
    bars  = ""
    for cat, count in sorted(cat_dist.items(), key=lambda x: x[1], reverse=True):
        emoji, label, color = CATEGORIES.get(cat, ("📝", "Misc", "#94a3b8"))
        pct = count / total * 100
        bars += f"""
        <div class="cat-bar-row">
          <span class="cat-bar-label">{emoji} {label}</span>
          <div class="cat-bar-track">
            <div class="cat-bar-fill" style="width:{pct:.1f}%;background:{color}"></div>
          </div>
          <span class="cat-bar-count">{count}</span>
        </div>"""
    return bars


def render_module_pills(modules: dict) -> str:
    if not modules:
        return ""
    pills = ""
    total = sum(modules.values()) or 1
    colors = {
        "core": "#6366f1", "interface": "#ec4899", "streams": "#06b6d4",
        "tools": "#f59e0b", "root": "#94a3b8",
    }
    for mod, count in list(modules.items())[:6]:
        color = colors.get(mod, "#94a3b8")
        pct   = int(count / total * 100)
        pills += f'<div class="mod-pill" style="border-color:{color}44;color:{color}">{mod}/ <strong>{count}</strong> <small>({pct}%)</small></div>'
    return pills


def build_commits_data_json(commits: list[dict]) -> str:
    """Build window.COMMITS_DATA as JSON: hash -> {message, body, author, date, files_list, ins, del, cat, version}"""
    data = {}
    for c in commits:
        data[c["hash"]] = {
            "message":    c["message"],
            "body":       c.get("body", ""),
            "author":     c["author"],
            "date":       c["date"],
            "files_list": c.get("files_list", []),
            "ins":        c["ins"],
            "del":        c["del"],
            "cat":        c["cat"],
            "version":    c.get("version", ""),
            "short":      c["short"],
        }
    return json.dumps(data, ensure_ascii=False)


# ─── Template HTML principal ──────────────────────────────────────────────────

def build_html(commits: list[dict], stats: dict, file_changes: dict) -> str:
    groups       = group_by_day(commits)
    timeline_html = "\n".join(render_day_group(g) for g in groups)
    top_files_html = render_top_files(stats["top_files"])
    cat_bars_html  = render_cat_bars(stats["cat_dist"])
    module_html    = render_module_pills(stats["modules"])
    commits_data_json = build_commits_data_json(commits)

    # Category filter pills (for JS filtering)
    filter_pills = '<button class="filter-pill active" data-cat="all">All</button>'
    for cat, (emoji, label, color) in CATEGORIES.items():
        if stats["cat_dist"].get(cat, 0) > 0:
            n = stats["cat_dist"][cat]
            filter_pills += (
                f'<button class="filter-pill" data-cat="{cat}" '
                f'style="--pill-color:{color}">{emoji} {label} <sup>{n}</sup></button>'
            )

    authors_html = " · ".join(f"<strong>{_esc(a)}</strong>" for a in stats["authors"])

    return f"""<!DOCTYPE html>
<html lang="es" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QTS — Quantum Trading System · Changelog</title>
<style>
/* ─── Reset & Variables ─────────────────────────────────────────────────── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

:root {{
  --bg:         #0d1117;
  --bg-card:    #161b22;
  --bg-card2:   #1c2128;
  --border:     #30363d;
  --border2:    #21262d;
  --text:       #e6edf3;
  --text-sub:   #8b949e;
  --text-muted: #484f58;
  --accent:     #7c5cbf;
  --accent2:    #58a6ff;
  --green:      #3fb950;
  --red:        #f85149;
  --yellow:     #e3b341;
  --radius:     10px;
  --radius-sm:  6px;
  --shadow:     0 2px 12px #0006;
  --trans:      .2s ease;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}}

[data-theme="light"] {{
  --bg:         #f6f8fa;
  --bg-card:    #ffffff;
  --bg-card2:   #f0f2f5;
  --border:     #d0d7de;
  --border2:    #e8ecf0;
  --text:       #1f2328;
  --text-sub:   #57606a;
  --text-muted: #8c959f;
  --shadow:     0 2px 12px #0001;
}}

html {{ scroll-behavior: smooth; }}
body {{
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  line-height: 1.6;
  transition: background var(--trans), color var(--trans);
}}

a {{ color: var(--accent2); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}

/* ─── Header ────────────────────────────────────────────────────────────── */
.site-header {{
  background: var(--bg-card);
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 100;
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
}}
.header-inner {{
  max-width: 1100px; margin: 0 auto;
  padding: 14px 24px;
  display: flex; align-items: center; gap: 16px;
}}
.logo-mark {{
  width: 36px; height: 36px;
  background: linear-gradient(135deg, #7c5cbf, #58a6ff);
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-size: 18px; flex-shrink: 0;
}}
.logo-text {{ flex: 1; }}
.logo-text h1 {{
  font-size: 16px; font-weight: 700; letter-spacing: -.3px;
  background: linear-gradient(90deg, #7c5cbf, #58a6ff);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}}
.logo-text p {{ font-size: 11px; color: var(--text-sub); }}

.header-actions {{ display: flex; align-items: center; gap: 10px; }}
.gen-date {{ font-size: 11px; color: var(--text-muted); font-family: monospace; }}

.theme-btn {{
  background: var(--bg-card2); border: 1px solid var(--border);
  color: var(--text); border-radius: var(--radius-sm);
  padding: 6px 10px; cursor: pointer; font-size: 14px;
  transition: background var(--trans), border-color var(--trans);
}}
.theme-btn:hover {{ background: var(--bg); border-color: var(--accent); }}

/* ─── Top Nav Tabs ────────────────────────────────────────────────────────── */
.top-nav {{
  max-width: 1100px; margin: 0 auto;
  padding: 0 24px;
  display: flex; gap: 4px; border-bottom: 1px solid var(--border2);
}}
.nav-tab {{
  padding: 10px 18px; font-size: 13px; font-weight: 500;
  color: var(--text-sub); border: none; background: none;
  cursor: pointer; border-bottom: 2px solid transparent;
  transition: color var(--trans), border-color var(--trans);
  white-space: nowrap;
}}
.nav-tab:hover {{ color: var(--text); }}
.nav-tab.active {{ color: var(--text); border-bottom-color: var(--accent); }}

/* ─── Tab Panels ─────────────────────────────────────────────────────────── */
.tab-panel {{ display: block; }}
.tab-panel.hidden {{ display: none; }}

/* ─── Layout ─────────────────────────────────────────────────────────────── */
.main-layout {{
  max-width: 1100px; margin: 0 auto; padding: 32px 24px;
  display: grid; grid-template-columns: 1fr 320px; gap: 28px;
}}
@media (max-width: 900px) {{
  .main-layout {{ grid-template-columns: 1fr; }}
  .sidebar {{ order: -1; }}
}}

/* ─── Stats Bar ──────────────────────────────────────────────────────────── */
.stats-bar {{
  grid-column: 1 / -1;
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 14px; margin-bottom: 4px;
}}
.stat-card {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 20px;
  transition: border-color var(--trans), transform var(--trans);
}}
.stat-card:hover {{ border-color: var(--accent); transform: translateY(-2px); }}
.stat-card .stat-icon {{ font-size: 22px; margin-bottom: 6px; }}
.stat-card .stat-value {{
  font-size: 28px; font-weight: 700; letter-spacing: -1px;
  background: linear-gradient(135deg, var(--text), var(--text-sub));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}}
.stat-card .stat-label {{ font-size: 12px; color: var(--text-sub); margin-top: 2px; }}

/* ─── Section cards ──────────────────────────────────────────────────────── */
.section-card {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden; margin-bottom: 20px;
}}
.section-head {{
  padding: 14px 20px;
  border-bottom: 1px solid var(--border2);
  font-size: 13px; font-weight: 600; color: var(--text-sub);
  letter-spacing: .5px; text-transform: uppercase;
  display: flex; align-items: center; gap: 8px;
}}
.section-body {{ padding: 16px 20px; }}

/* ─── Filters + Search ───────────────────────────────────────────────────── */
.controls {{
  grid-column: 1 / -1;
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
}}
.search-wrap {{
  flex: 1; min-width: 200px;
  position: relative;
}}
.search-icon {{
  position: absolute; left: 12px; top: 50%; transform: translateY(-50%);
  color: var(--text-muted); pointer-events: none; font-size: 14px;
}}
#search {{
  width: 100%; padding: 9px 12px 9px 36px;
  background: var(--bg-card); border: 1px solid var(--border);
  color: var(--text); border-radius: var(--radius-sm); font-size: 13px;
  transition: border-color var(--trans);
}}
#search:focus {{
  outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px #7c5cbf22;
}}
#search::placeholder {{ color: var(--text-muted); }}

.filter-pills {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.filter-pill {{
  padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: 500;
  cursor: pointer; border: 1px solid var(--border);
  background: var(--bg-card2); color: var(--text-sub);
  transition: all var(--trans); white-space: nowrap;
}}
.filter-pill:hover {{ border-color: var(--pill-color, var(--accent)); color: var(--text); }}
.filter-pill.active {{
  background: var(--pill-color, var(--accent));
  border-color: var(--pill-color, var(--accent));
  color: #fff;
}}
.filter-pill[data-cat="all"] {{ --pill-color: var(--accent); }}
.filter-pill sup {{ font-size: 9px; opacity: .7; }}

/* ─── Timeline ───────────────────────────────────────────────────────────── */
.timeline {{
  position: relative;
  padding-left: 24px;
}}
.timeline::before {{
  content: ''; position: absolute;
  left: 7px; top: 0; bottom: 0;
  width: 2px; background: var(--border2);
  border-radius: 2px;
}}

.day-group {{ margin-bottom: 28px; }}

.day-header {{
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 12px;
  position: relative;
}}
.day-dot {{
  width: 14px; height: 14px; border-radius: 50%;
  background: linear-gradient(135deg, #7c5cbf, #58a6ff);
  border: 2px solid var(--bg);
  box-shadow: 0 0 0 2px var(--accent);
  flex-shrink: 0;
  margin-left: -21px;
}}
.day-label {{ font-size: 13px; font-weight: 600; color: var(--text); }}
.day-count {{
  font-size: 11px; color: var(--text-muted);
  background: var(--bg-card2); padding: 2px 8px;
  border-radius: 10px; border: 1px solid var(--border2);
}}

.day-commits {{ display: flex; flex-direction: column; gap: 8px; }}

/* ─── Commit Card ────────────────────────────────────────────────────────── */
.commit-card {{
  background: var(--bg-card2);
  border: 1px solid var(--border2);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  display: flex; gap: 12px; align-items: flex-start;
  transition: border-color var(--trans), background var(--trans), transform var(--trans);
  cursor: pointer;
}}
.commit-card:hover {{
  border-color: var(--border);
  background: var(--bg-card);
  transform: translateX(2px);
}}
.commit-card.hidden {{ display: none; }}

.commit-left {{ padding-top: 1px; flex-shrink: 0; }}
.cat-pill {{
  font-size: 10px; font-weight: 600; letter-spacing: .3px;
  padding: 3px 8px; border-radius: 10px; border: 1px solid;
  white-space: nowrap; display: inline-block;
}}

.commit-body {{ flex: 1; min-width: 0; }}
.commit-msg {{
  font-size: 13px; color: var(--text); margin-bottom: 6px;
  word-break: break-word; line-height: 1.4;
}}
.commit-meta {{
  display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
}}

.hash-chip {{
  font-size: 11px; color: var(--accent2);
  background: var(--bg); padding: 1px 6px;
  border-radius: 4px; border: 1px solid var(--border2);
  font-family: "SFMono-Regular", Consolas, monospace;
}}
.meta-author, .meta-time {{
  font-size: 11px; color: var(--text-muted);
}}

.version-badge, .tag-badge {{
  font-size: 10px; font-weight: 700;
  padding: 2px 7px; border-radius: 10px;
  margin-right: 5px; display: inline-block;
}}
.version-badge {{
  background: #7c5cbf22; color: #7c5cbf;
  border: 1px solid #7c5cbf44;
}}
.tag-badge {{
  background: #22c55e22; color: #22c55e;
  border: 1px solid #22c55e44;
}}

.stat-chip {{
  font-size: 10px; font-family: monospace;
  padding: 1px 6px; border-radius: 4px;
}}
.stat-chip.files {{ background: var(--bg); color: var(--text-sub); border: 1px solid var(--border2); }}
.stat-chip.ins    {{ background: #3fb95018; color: #3fb950; }}
.stat-chip.del    {{ background: #f8514918; color: #f85149; }}

/* ─── Sidebar ────────────────────────────────────────────────────────────── */
.sidebar {{ display: flex; flex-direction: column; gap: 20px; }}

/* Top Files */
.file-row {{
  display: grid; grid-template-columns: 1fr 80px 32px;
  gap: 8px; align-items: center; padding: 6px 0;
  border-bottom: 1px solid var(--border2);
  font-size: 12px;
}}
.file-row:last-child {{ border-bottom: none; }}
.file-name {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.file-path {{ color: var(--text-muted); }}
.file-bar-wrap {{
  background: var(--bg-card2); border-radius: 3px;
  height: 5px; overflow: hidden;
}}
.file-bar {{
  height: 100%; background: linear-gradient(90deg, #7c5cbf, #58a6ff);
  border-radius: 3px; transition: width .4s ease;
}}
.file-count {{ color: var(--text-muted); text-align: right; font-size: 10px; }}

/* Category bars */
.cat-bar-row {{
  display: flex; align-items: center; gap: 8px;
  padding: 5px 0; font-size: 12px;
}}
.cat-bar-label {{ width: 90px; flex-shrink: 0; color: var(--text-sub); }}
.cat-bar-track {{
  flex: 1; background: var(--bg-card2); border-radius: 3px;
  height: 6px; overflow: hidden;
}}
.cat-bar-fill {{ height: 100%; border-radius: 3px; transition: width .4s ease; }}
.cat-bar-count {{ color: var(--text-muted); font-size: 11px; width: 20px; text-align: right; }}

/* Modules */
.modules-grid {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.mod-pill {{
  font-size: 11px; padding: 5px 10px; border-radius: 6px;
  border: 1px solid; background: transparent;
}}
.mod-pill strong {{ font-weight: 700; }}
.mod-pill small {{ opacity: .6; }}

/* Authors */
.authors-list {{ font-size: 13px; line-height: 2; }}

/* No results */
.no-results {{
  text-align: center; padding: 48px 24px;
  color: var(--text-muted); font-size: 14px;
  display: none;
}}
.no-results .nr-icon {{ font-size: 36px; margin-bottom: 10px; }}

/* ─── Footer ─────────────────────────────────────────────────────────────── */
footer {{
  border-top: 1px solid var(--border);
  padding: 20px 24px;
  text-align: center;
  font-size: 12px; color: var(--text-muted);
}}
footer strong {{ color: var(--text-sub); }}

/* ─── Scroll to top ──────────────────────────────────────────────────────── */
.scroll-top {{
  position: fixed; bottom: 24px; right: 24px;
  background: var(--accent); color: #fff;
  border: none; border-radius: 50%; width: 40px; height: 40px;
  font-size: 18px; cursor: pointer;
  box-shadow: 0 4px 12px #7c5cbf55;
  opacity: 0; pointer-events: none; transition: opacity var(--trans);
  display: flex; align-items: center; justify-content: center;
}}
.scroll-top.visible {{ opacity: 1; pointer-events: all; }}

/* ─── Modal ──────────────────────────────────────────────────────────────── */
.modal-overlay {{
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,0.7);
  display: flex; align-items: center; justify-content: center;
  padding: 24px;
  backdrop-filter: blur(4px);
}}
.modal-overlay.hidden {{ display: none; }}
.modal-box {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  max-width: 680px; width: 100%;
  max-height: 85vh; overflow-y: auto;
  box-shadow: 0 20px 60px #0009;
  animation: modalIn .18s ease;
}}
@keyframes modalIn {{
  from {{ opacity: 0; transform: scale(.96) translateY(8px); }}
  to   {{ opacity: 1; transform: scale(1)  translateY(0); }}
}}
.modal-header {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px; border-bottom: 1px solid var(--border2);
}}
.modal-close {{
  background: none; border: none; color: var(--text-muted);
  font-size: 18px; cursor: pointer; padding: 4px 8px;
  border-radius: var(--radius-sm);
  transition: color var(--trans), background var(--trans);
}}
.modal-close:hover {{ color: var(--text); background: var(--bg-card2); }}
.modal-box h2 {{
  font-size: 15px; font-weight: 600; padding: 16px 20px 8px;
  line-height: 1.4; color: var(--text);
}}
.modal-meta {{
  padding: 0 20px 12px;
  display: flex; flex-wrap: wrap; gap: 10px;
  font-size: 12px; color: var(--text-sub);
  border-bottom: 1px solid var(--border2);
}}
.modal-meta code {{
  font-family: "SFMono-Regular", Consolas, monospace;
  background: var(--bg-card2); padding: 1px 6px;
  border-radius: 4px; border: 1px solid var(--border2);
  color: var(--accent2); font-size: 11px;
}}
.modal-section {{
  padding: 14px 20px;
  border-bottom: 1px solid var(--border2);
}}
.modal-section:last-child {{ border-bottom: none; }}
.modal-section h4 {{
  font-size: 11px; font-weight: 600; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: .5px; margin-bottom: 10px;
}}
.modal-body-pre {{
  font-size: 13px; color: var(--text-sub);
  white-space: pre-wrap; word-break: break-word; line-height: 1.6;
}}
.modal-notes-area {{
  width: 100%; background: var(--bg-card2);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  color: var(--text); padding: 10px 12px; font-size: 13px;
  resize: vertical; min-height: 80px; line-height: 1.5;
  margin-bottom: 8px;
}}
.modal-notes-area:focus {{ outline: none; border-color: var(--accent); }}
.btn-save-notes {{
  font-size: 12px; padding: 6px 14px;
  background: var(--accent); color: #fff; border: none;
  border-radius: var(--radius-sm); cursor: pointer;
  transition: opacity var(--trans);
}}
.btn-save-notes:hover {{ opacity: .85; }}

/* ─── File change rows (modal) ───────────────────────────────────────────── */
.file-change-row {{
  display: flex; align-items: center; gap: 10px;
  padding: 5px 0; font-size: 12px;
  border-bottom: 1px solid var(--border2);
}}
.file-change-row:last-child {{ border-bottom: none; }}
.file-change-name {{
  flex: 1; font-family: "SFMono-Regular", Consolas, monospace;
  color: var(--text-sub); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}}
.file-change-ins {{ color: var(--green); font-family: monospace; font-size: 11px; white-space: nowrap; }}
.file-change-del {{ color: var(--red);   font-family: monospace; font-size: 11px; white-space: nowrap; }}

/* ─── Tasks Panel ────────────────────────────────────────────────────────── */
#panel-tasks {{
  max-width: 1100px; margin: 0 auto; padding: 32px 24px;
}}
.tasks-header {{
  margin-bottom: 24px;
}}
.tasks-header h2 {{
  font-size: 20px; font-weight: 700; margin-bottom: 14px;
}}
.task-add-row {{
  display: flex; gap: 8px; flex-wrap: wrap;
}}
.task-add-row select, .task-add-row input {{
  background: var(--bg-card); border: 1px solid var(--border);
  color: var(--text); border-radius: var(--radius-sm);
  padding: 8px 12px; font-size: 13px;
  transition: border-color var(--trans);
}}
.task-add-row select {{ min-width: 130px; }}
.task-add-row input {{ flex: 1; min-width: 200px; }}
.task-add-row select:focus, .task-add-row input:focus {{
  outline: none; border-color: var(--accent);
}}
.task-add-row button {{
  background: var(--accent); color: #fff; border: none;
  border-radius: var(--radius-sm); padding: 8px 18px;
  font-size: 13px; cursor: pointer; font-weight: 600;
  transition: opacity var(--trans);
}}
.task-add-row button:hover {{ opacity: .85; }}

.tasks-group {{ margin-bottom: 28px; }}
.tasks-group-title {{
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .6px; color: var(--text-muted); margin-bottom: 10px;
  padding-bottom: 6px; border-bottom: 1px solid var(--border2);
}}

.task-item {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-left: 3px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  display: flex; align-items: flex-start; gap: 10px;
  margin-bottom: 8px;
  transition: border-color var(--trans);
}}
.task-item:hover {{ border-color: var(--border); }}
.task-done {{ opacity: .5; }}
.task-done .task-text {{ text-decoration: line-through; }}

.type-task      {{ border-left-color: #3b82f6; }}
.type-objective {{ border-left-color: #f59e0b; }}
.type-note      {{ border-left-color: #8b5cf6; }}
.type-bug       {{ border-left-color: #f85149; }}

.task-check {{
  margin-top: 2px; flex-shrink: 0;
  width: 16px; height: 16px; cursor: pointer; accent-color: var(--accent);
}}
.task-text {{ flex: 1; font-size: 13px; line-height: 1.5; }}
.task-meta {{ font-size: 10px; color: var(--text-muted); margin-top: 3px; }}
.task-del {{
  background: none; border: none; color: var(--text-muted);
  font-size: 14px; cursor: pointer; padding: 2px 4px;
  border-radius: 4px; line-height: 1;
  transition: color var(--trans), background var(--trans);
}}
.task-del:hover {{ color: var(--red); background: #f8514918; }}

/* ─── Commit Panel ───────────────────────────────────────────────────────── */
#panel-commit {{
  max-width: 800px; margin: 0 auto; padding: 32px 24px;
}}
.commit-panel h2 {{
  font-size: 20px; font-weight: 700; margin-bottom: 20px;
}}
.server-status {{
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; padding: 4px 12px;
  border-radius: 20px; margin-bottom: 20px;
  border: 1px solid;
}}
.server-status.ok    {{ color: var(--green); border-color: #3fb95033; background: #3fb95011; }}
.server-status.error {{ color: var(--red);   border-color: #f8514933; background: #f8514911; }}
.server-status.checking {{ color: var(--yellow); border-color: #e3b34133; background: #e3b34111; }}

#commit-form input, #commit-form textarea {{
  width: 100%; background: var(--bg-card);
  border: 1px solid var(--border); color: var(--text);
  border-radius: var(--radius-sm); padding: 10px 14px;
  font-size: 13px; margin-bottom: 12px;
  transition: border-color var(--trans);
  font-family: inherit;
}}
#commit-form input:focus, #commit-form textarea:focus {{
  outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px #7c5cbf22;
}}
#commit-form textarea {{ resize: vertical; line-height: 1.5; }}

.staged-file {{
  display: flex; align-items: center; gap: 8px;
  padding: 6px 0; font-size: 12px;
  border-bottom: 1px solid var(--border2);
  font-family: "SFMono-Regular", Consolas, monospace;
  color: var(--text-sub);
}}
.staged-file:last-child {{ border-bottom: none; }}
.staged-file-status {{
  font-size: 10px; font-weight: 700; padding: 1px 5px;
  border-radius: 3px; text-transform: uppercase;
}}
.staged-M {{ color: #e3b341; background: #e3b34120; }}
.staged-A {{ color: var(--green); background: #3fb95020; }}
.staged-D {{ color: var(--red); background: #f8514920; }}
.staged-R {{ color: var(--accent2); background: #58a6ff20; }}

#staged-files-list {{
  background: var(--bg-card2); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 10px 14px;
  margin-bottom: 14px; min-height: 48px;
}}
.staged-empty {{ font-size: 13px; color: var(--text-muted); text-align: center; padding: 8px 0; }}

.commit-actions {{
  display: flex; gap: 10px; margin-bottom: 12px;
}}
.btn-primary {{
  background: var(--accent); color: #fff; border: none;
  border-radius: var(--radius-sm); padding: 9px 20px;
  font-size: 13px; font-weight: 600; cursor: pointer;
  transition: opacity var(--trans);
}}
.btn-primary:hover {{ opacity: .85; }}
.btn-primary:disabled {{ opacity: .4; cursor: not-allowed; }}
.btn-secondary {{
  background: var(--bg-card); color: var(--text); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 9px 16px;
  font-size: 13px; cursor: pointer;
  transition: border-color var(--trans);
}}
.btn-secondary:hover {{ border-color: var(--accent); }}

#commit-result {{
  font-size: 13px; padding: 10px 14px;
  border-radius: var(--radius-sm); margin-top: 8px;
}}
#commit-result.ok    {{ background: #3fb95018; color: var(--green); border: 1px solid #3fb95033; }}
#commit-result.error {{ background: #f8514918; color: var(--red);   border: 1px solid #f8514933; }}

#no-server-msg {{
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 24px; text-align: center;
}}
#no-server-msg p {{ color: var(--text-sub); margin-bottom: 12px; font-size: 14px; }}
#no-server-msg code {{
  display: block; background: var(--bg-card2); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 10px 16px;
  font-family: "SFMono-Regular", Consolas, monospace;
  color: var(--accent2); font-size: 13px;
}}
</style>
</head>
<body>

<!-- HEADER -->
<header class="site-header">
  <div class="header-inner">
    <div class="logo-mark">⚡</div>
    <div class="logo-text">
      <h1>QTS — Quantum Trading System</h1>
      <p>Development Changelog · Bybit Perpetuals · AI-Powered</p>
    </div>
    <div class="header-actions">
      <span class="gen-date">Generated {GEN_DATE}</span>
      <button class="theme-btn" id="themeBtn" title="Toggle theme">🌙</button>
    </div>
  </div>
  <nav class="top-nav">
    <button class="nav-tab active" data-panel="changelog" onclick="switchTab(this)">📋 Changelog</button>
    <button class="nav-tab" data-panel="tasks" onclick="switchTab(this)">📋 Tareas</button>
    <button class="nav-tab" data-panel="commit" onclick="switchTab(this)">💾 Commit</button>
  </nav>
</header>

<!-- CHANGELOG PANEL -->
<div id="panel-changelog" class="tab-panel">
<div class="main-layout">

  <!-- STATS BAR -->
  <div class="stats-bar">
    <div class="stat-card">
      <div class="stat-icon">📦</div>
      <div class="stat-value">{stats["total"]}</div>
      <div class="stat-label">Total Commits</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">👥</div>
      <div class="stat-value">{len(stats["authors"])}</div>
      <div class="stat-label">{"Autor" if len(stats["authors"]) == 1 else "Autores"}</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">📝</div>
      <div class="stat-value">{stats["total_files"]:,}</div>
      <div class="stat-label">Archivos Tocados</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">➕</div>
      <div class="stat-value" style="color:var(--green)">{stats["total_ins"]:,}</div>
      <div class="stat-label">Líneas Añadidas</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">➖</div>
      <div class="stat-value" style="color:var(--red)">{stats["total_dels"]:,}</div>
      <div class="stat-label">Líneas Eliminadas</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">📅</div>
      <div class="stat-value" style="font-size:18px">{stats["first_date"]}</div>
      <div class="stat-label">Primer Commit</div>
    </div>
  </div>

  <!-- CONTROLS -->
  <div class="controls" style="grid-column:1/-1">
    <div class="search-wrap">
      <span class="search-icon">🔍</span>
      <input type="text" id="search" placeholder="Buscar commits…" autocomplete="off">
    </div>
    <div class="filter-pills" id="filterPills">
      {filter_pills}
    </div>
  </div>

  <!-- TIMELINE -->
  <main>
    <div class="timeline" id="timeline">
{timeline_html}
    </div>
    <div class="no-results" id="noResults">
      <div class="nr-icon">🔍</div>
      <p>No se encontraron commits para este filtro.</p>
    </div>
  </main>

  <!-- SIDEBAR -->
  <aside class="sidebar">

    <!-- Archivos más activos -->
    <div class="section-card">
      <div class="section-head">🔥 Archivos Más Activos</div>
      <div class="section-body">
        {top_files_html}
      </div>
    </div>

    <!-- Distribución por tipo -->
    <div class="section-card">
      <div class="section-head">📊 Distribución por Tipo</div>
      <div class="section-body">
        {cat_bars_html}
      </div>
    </div>

    <!-- Módulos -->
    <div class="section-card">
      <div class="section-head">📂 Actividad por Módulo</div>
      <div class="section-body">
        <div class="modules-grid">
          {module_html}
        </div>
      </div>
    </div>

    <!-- Autores -->
    <div class="section-card">
      <div class="section-head">👥 Autores</div>
      <div class="section-body">
        <div class="authors-list">{authors_html}</div>
      </div>
    </div>

    <!-- Milestones -->
    <div class="section-card">
      <div class="section-head">🏁 Versiones / Hitos</div>
      <div class="section-body" style="font-size:12px;line-height:2">
        <div><span class="version-badge">v0.5</span> Viability Monitor · G1/G2/G3 Trailing · Sessions Multi-Goal</div>
        <div><span class="version-badge">v0.4</span> Dynamic Symbols · Slippage Guard · Watchdog</div>
        <div><span class="version-badge">v0.3</span> TSAA Sessions · AI Strategy · SL Fixes</div>
        <div><span class="version-badge">v0.2</span> Trailing Stop · BE · Profit Lock · AI Models</div>
        <div><span class="version-badge">v0.1</span> Foundation · Indicators · UI · Bybit Integration</div>
      </div>
    </div>

  </aside>

</div>
</div>

<!-- TASKS PANEL -->
<div id="panel-tasks" class="tab-panel hidden">
  <div class="tasks-header">
    <h2>Tareas & Objetivos</h2>
    <div class="task-add-row">
      <select id="task-type">
        <option value="task">✅ Tarea</option>
        <option value="objective">🎯 Objetivo</option>
        <option value="note">📝 Nota</option>
        <option value="bug">🐛 Bug</option>
      </select>
      <input id="task-input" placeholder="Descripción..." onkeydown="if(event.key==='Enter') addTask()" />
      <button onclick="addTask()">Agregar</button>
    </div>
  </div>
  <div id="tasks-list"></div>
</div>

<!-- COMMIT PANEL -->
<div id="panel-commit" class="tab-panel hidden">
  <div class="commit-panel">
    <h2>💾 Hacer Commit</h2>
    <div class="server-status checking" id="server-status">⚡ Verificando servidor...</div>
    <div id="commit-form" class="hidden">
      <div id="staged-files-list"><div class="staged-empty">Cargando archivos...</div></div>
      <input id="commit-title" placeholder="Título del commit (requerido)" />
      <textarea id="commit-body" placeholder="Descripción detallada (opcional)&#10;&#10;Qué cambió y por qué..." rows="6"></textarea>
      <div class="commit-actions">
        <button onclick="loadGitStatus()" class="btn-secondary">↻ Actualizar</button>
        <button onclick="doCommit()" class="btn-primary" id="btn-commit">💾 Hacer Commit</button>
      </div>
      <div id="commit-result"></div>
    </div>
    <div id="no-server-msg" class="hidden">
      <p>Servidor no disponible. Ejecuta:</p>
      <code>python3 tools/changelog_server.py</code>
    </div>
  </div>
</div>

<!-- COMMIT DETAIL MODAL -->
<div id="commit-modal" class="modal-overlay hidden" onclick="closeModal(event)">
  <div class="modal-box">
    <div class="modal-header">
      <span id="modal-cat-pill"></span>
      <button class="modal-close" onclick="document.getElementById('commit-modal').classList.add('hidden')">✕</button>
    </div>
    <h2 id="modal-title"></h2>
    <div class="modal-meta" id="modal-meta"></div>
    <div class="modal-section" id="modal-body-section">
      <h4>📋 Descripción</h4>
      <div id="modal-body-text"></div>
    </div>
    <div class="modal-section" id="modal-files-section">
      <h4>📁 Archivos cambiados</h4>
      <div id="modal-files-list"></div>
    </div>
  </div>
</div>

<footer>
  Auto-generated by <strong>QTS Changelog Generator</strong> ·
  <strong>{stats["total"]}</strong> commits ·
  <strong>{stats["last_date"]}</strong>
</footer>

<button class="scroll-top" id="scrollTop" title="Volver arriba">↑</button>

<script>
// ─── Commits data (embedded) ────────────────────────────────────────────────
window.COMMITS_DATA = {commits_data_json};

// ─── Tab switching ──────────────────────────────────────────────────────────
function switchTab(btn) {{
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  const panel = btn.dataset.panel;
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
  document.getElementById('panel-' + panel).classList.remove('hidden');
  if (panel === 'commit') initCommitPanel();
  if (panel === 'tasks') renderTasks();
}}

// ─── Theme toggle ──────────────────────────────────────────────────────────
const root    = document.documentElement;
const themeBtn= document.getElementById('themeBtn');
const saved   = localStorage.getItem('qts-theme') || 'dark';
root.dataset.theme = saved;
themeBtn.textContent = saved === 'dark' ? '☀️' : '🌙';

themeBtn.addEventListener('click', () => {{
  const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
  root.dataset.theme = next;
  themeBtn.textContent = next === 'dark' ? '☀️' : '🌙';
  localStorage.setItem('qts-theme', next);
}});

// ─── Filter + Search ───────────────────────────────────────────────────────
let activeCat = 'all';
let searchVal = '';

const pills     = document.querySelectorAll('.filter-pill');
const cards     = document.querySelectorAll('.commit-card');
const noResults = document.getElementById('noResults');

function applyFilters() {{
  let visible = 0;
  cards.forEach(card => {{
    const catMatch  = activeCat === 'all' || card.dataset.cat === activeCat;
    const msgMatch  = !searchVal || card.dataset.msg.includes(searchVal);
    const show      = catMatch && msgMatch;
    card.classList.toggle('hidden', !show);
    if (show) visible++;
  }});
  // Show/hide day groups with no visible cards
  document.querySelectorAll('.day-group').forEach(group => {{
    const anyVisible = Array.from(group.querySelectorAll('.commit-card'))
                            .some(c => !c.classList.contains('hidden'));
    group.style.display = anyVisible ? '' : 'none';
  }});
  noResults.style.display = visible === 0 ? 'block' : 'none';
}}

pills.forEach(pill => {{
  pill.addEventListener('click', () => {{
    pills.forEach(p => p.classList.remove('active'));
    pill.classList.add('active');
    activeCat = pill.dataset.cat;
    applyFilters();
  }});
}});

document.getElementById('search').addEventListener('input', e => {{
  searchVal = e.target.value.toLowerCase().trim();
  applyFilters();
}});

// ─── Scroll to top ─────────────────────────────────────────────────────────
const scrollBtn = document.getElementById('scrollTop');
window.addEventListener('scroll', () => {{
  scrollBtn.classList.toggle('visible', window.scrollY > 400);
}});
scrollBtn.addEventListener('click', () => window.scrollTo({{ top: 0, behavior: 'smooth' }}));

// ─── Commit Detail Modal ───────────────────────────────────────────────────
const CATEGORIES_META = {json.dumps({k: list(v) for k, v in CATEGORIES.items()})};

function showCommitDetail(card) {{
  const hash = card.dataset.hash;
  const data = window.COMMITS_DATA[hash];
  if (!data) return;

  const [emoji, label, color] = CATEGORIES_META[data.cat] || ['📝', 'Misc', '#94a3b8'];

  // Category pill
  const pill = document.getElementById('modal-cat-pill');
  pill.textContent = emoji + ' ' + label;
  pill.style.cssText = 'font-size:11px;font-weight:600;padding:3px 10px;border-radius:10px;border:1px solid;' +
    'background:' + color + '22;color:' + color + ';border-color:' + color + '44';

  // Title
  document.getElementById('modal-title').textContent = data.message;

  // Meta
  document.getElementById('modal-meta').innerHTML =
    '<code>' + data.short + '</code>' +
    '<span>👤 ' + escHtml(data.author) + '</span>' +
    '<span>📅 ' + escHtml(data.date) + '</span>' +
    (data.version ? '<span class="version-badge">' + escHtml(data.version) + '</span>' : '') +
    '<span class="stat-chip ins">+' + data.ins + '</span>' +
    '<span class="stat-chip del">−' + data.del + '</span>';

  // Body / notes
  const bodySection = document.getElementById('modal-body-section');
  const bodyText    = document.getElementById('modal-body-text');
  if (data.body && data.body.trim()) {{
    bodyText.innerHTML = '<div class="modal-body-pre">' + escHtml(data.body) + '</div>';
    bodySection.style.display = '';
  }} else {{
    // Editable notes area — saves to server if available
    bodyText.innerHTML =
      '<textarea class="modal-notes-area" id="notes-area-' + escHtml(hash) + '" ' +
      'placeholder="Agregar descripción/notas para este commit..."></textarea>' +
      '<button class="btn-save-notes" onclick="saveNotes(\'' + escHtml(hash) + '\')">Guardar nota</button>' +
      '<span id="notes-save-msg-' + escHtml(hash) + '" style="font-size:11px;color:var(--text-muted);margin-left:8px"></span>';
    // Pre-fill from localStorage
    const stored = localStorage.getItem('qts-note-' + hash);
    if (stored) document.getElementById('notes-area-' + hash).value = stored;
    bodySection.style.display = '';
  }}

  // Files changed
  const filesSection = document.getElementById('modal-files-section');
  const filesList    = document.getElementById('modal-files-list');
  if (data.files_list && data.files_list.length > 0) {{
    filesList.innerHTML = data.files_list.map(f =>
      '<div class="file-change-row">' +
      '<span class="file-change-name">' + escHtml(f.file) + '</span>' +
      '<span class="file-change-ins">+' + f.ins + '</span>' +
      '<span class="file-change-del">-' + f.del + '</span>' +
      '</div>'
    ).join('');
    filesSection.style.display = '';
  }} else {{
    filesSection.style.display = 'none';
  }}

  document.getElementById('commit-modal').classList.remove('hidden');
}}

function closeModal(event) {{
  const box = document.querySelector('.modal-box');
  if (!box.contains(event.target)) {{
    document.getElementById('commit-modal').classList.add('hidden');
  }}
}}

document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') {{
    document.getElementById('commit-modal').classList.add('hidden');
  }}
}});

function saveNotes(hash) {{
  const area = document.getElementById('notes-area-' + hash);
  if (!area) return;
  const text = area.value.trim();
  localStorage.setItem('qts-note-' + hash, text);
  const msg = document.getElementById('notes-save-msg-' + hash);
  if (msg) {{ msg.textContent = '✓ Guardado'; setTimeout(() => {{ msg.textContent = ''; }}, 2000); }}
  // Also try server
  fetch('http://localhost:7000/api/commit-notes', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ hash, note: text }})
  }}).catch(() => {{}});
}}

function escHtml(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

// ─── Tasks (localStorage) ──────────────────────────────────────────────────
const TYPE_ICONS = {{ task: '✅', objective: '🎯', note: '📝', bug: '🐛' }};
const TYPE_LABELS = {{ task: 'Tareas', objective: 'Objetivos', note: 'Notas', bug: 'Bugs' }};

function loadTasks() {{
  try {{
    return JSON.parse(localStorage.getItem('qts_tasks') || '[]');
  }} catch(e) {{ return []; }}
}}

function saveTasks(tasks) {{
  localStorage.setItem('qts_tasks', JSON.stringify(tasks));
}}

function addTask() {{
  const typeEl = document.getElementById('task-type');
  const inputEl = document.getElementById('task-input');
  const text = inputEl.value.trim();
  if (!text) {{ inputEl.focus(); return; }}
  const tasks = loadTasks();
  tasks.unshift({{
    id: Date.now(),
    type: typeEl.value,
    text,
    done: false,
    created: new Date().toLocaleString()
  }});
  saveTasks(tasks);
  inputEl.value = '';
  renderTasks();
}}

function toggleTask(id) {{
  const tasks = loadTasks();
  const t = tasks.find(t => t.id === id);
  if (t) {{ t.done = !t.done; saveTasks(tasks); renderTasks(); }}
}}

function deleteTask(id) {{
  const tasks = loadTasks().filter(t => t.id !== id);
  saveTasks(tasks);
  renderTasks();
}}

function renderTasks() {{
  const tasks = loadTasks();
  const container = document.getElementById('tasks-list');
  if (!container) return;

  if (tasks.length === 0) {{
    container.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:40px 0;font-size:14px">No hay tareas aún. ¡Agrega una arriba!</p>';
    return;
  }}

  const byType = {{}};
  tasks.forEach(t => {{
    if (!byType[t.type]) byType[t.type] = [];
    byType[t.type].push(t);
  }});

  const order = ['bug', 'objective', 'task', 'note'];
  let html = '';
  order.forEach(type => {{
    const group = byType[type];
    if (!group || group.length === 0) return;
    html += '<div class="tasks-group">';
    html += '<div class="tasks-group-title">' + TYPE_ICONS[type] + ' ' + TYPE_LABELS[type] + ' (' + group.length + ')</div>';
    group.forEach(t => {{
      html += '<div class="task-item type-' + t.type + (t.done ? ' task-done' : '') + '">' +
        '<input type="checkbox" class="task-check" ' + (t.done ? 'checked' : '') +
        ' onchange="toggleTask(' + t.id + ')" title="Marcar como ' + (t.done ? 'pendiente' : 'hecho') + '">' +
        '<div style="flex:1">' +
        '<div class="task-text">' + escHtml(t.text) + '</div>' +
        '<div class="task-meta">' + escHtml(t.created) + '</div>' +
        '</div>' +
        '<button class="task-del" onclick="deleteTask(' + t.id + ')" title="Eliminar">✕</button>' +
        '</div>';
    }});
    html += '</div>';
  }});
  container.innerHTML = html;
}}

// ─── Commit Panel ──────────────────────────────────────────────────────────
let serverAvailable = false;

async function initCommitPanel() {{
  const statusEl = document.getElementById('server-status');
  const formEl   = document.getElementById('commit-form');
  const noSrvEl  = document.getElementById('no-server-msg');
  statusEl.className = 'server-status checking';
  statusEl.textContent = '⚡ Verificando servidor...';
  try {{
    const r = await fetch('http://localhost:7000/api/ping', {{ signal: AbortSignal.timeout(2000) }});
    const d = await r.json();
    if (d.ok) {{
      serverAvailable = true;
      statusEl.className = 'server-status ok';
      statusEl.textContent = '● Servidor conectado en localhost:7000';
      formEl.classList.remove('hidden');
      noSrvEl.classList.add('hidden');
      loadGitStatus();
      return;
    }}
  }} catch(e) {{}}
  serverAvailable = false;
  statusEl.className = 'server-status error';
  statusEl.textContent = '● Servidor no disponible';
  formEl.classList.add('hidden');
  noSrvEl.classList.remove('hidden');
}}

async function loadGitStatus() {{
  if (!serverAvailable) return;
  const listEl = document.getElementById('staged-files-list');
  try {{
    const r = await fetch('http://localhost:7000/api/status');
    const d = await r.json();
    const all = [
      ...(d.staged   || []).map(f => ({{ ...f, area: 'staged' }})),
      ...(d.unstaged || []).map(f => ({{ ...f, area: 'unstaged' }})),
    ];
    if (all.length === 0) {{
      listEl.innerHTML = '<div class="staged-empty">Sin cambios detectados.</div>';
      return;
    }}
    listEl.innerHTML = all.map(f =>
      '<div class="staged-file">' +
      '<span class="staged-file-status staged-' + f.status + '">' + f.status + '</span>' +
      '<span style="flex:1">' + escHtml(f.file) + '</span>' +
      (f.area === 'staged'
        ? '<span style="font-size:10px;color:var(--green)">staged</span>'
        : '<span style="font-size:10px;color:var(--yellow)">unstaged</span>') +
      '</div>'
    ).join('');
  }} catch(e) {{
    listEl.innerHTML = '<div class="staged-empty" style="color:var(--red)">Error al obtener estado git.</div>';
  }}
}}

async function doCommit() {{
  if (!serverAvailable) return;
  const title  = document.getElementById('commit-title').value.trim();
  const body   = document.getElementById('commit-body').value.trim();
  const btn    = document.getElementById('btn-commit');
  const result = document.getElementById('commit-result');
  if (!title) {{
    document.getElementById('commit-title').focus();
    result.className = 'error';
    result.textContent = 'El título del commit es requerido.';
    return;
  }}
  btn.disabled = true;
  btn.textContent = '⏳ Haciendo commit...';
  result.className = '';
  result.textContent = '';
  try {{
    const r = await fetch('http://localhost:7000/api/commit', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ title, body }})
    }});
    const d = await r.json();
    if (d.ok) {{
      result.className = 'ok';
      result.textContent = '✓ Commit realizado: ' + (d.output || '').split('\\n')[0];
      document.getElementById('commit-title').value = '';
      document.getElementById('commit-body').value = '';
      setTimeout(() => {{ location.reload(); }}, 2000);
    }} else {{
      result.className = 'error';
      result.textContent = 'Error: ' + (d.error || 'Desconocido');
    }}
  }} catch(e) {{
    result.className = 'error';
    result.textContent = 'Error de red: ' + e.message;
  }}
  btn.disabled = false;
  btn.textContent = '💾 Hacer Commit';
  loadGitStatus();
}}
</script>
</body>
</html>"""


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    print("⚡ QTS Changelog Generator — generando index.html…")
    commits, file_changes = parse_commits()
    if not commits:
        print("  ⚠️  No se encontraron commits. ¿Estás en un repositorio git?")
        sys.exit(1)

    stats = compute_stats(commits, file_changes)
    html_content = build_html(commits, stats, file_changes)

    OUTPUT_FILE.write_text(html_content, encoding="utf-8")
    size_kb = OUTPUT_FILE.stat().st_size / 1024
    print(f"  ✅  {OUTPUT_FILE} — {stats['total']} commits · {size_kb:.1f} KB")


if __name__ == "__main__":
    main()
