#!/usr/bin/env python3
"""
tools/generate_changelog.py
────────────────────────────
Genera index.html — Dashboard visual del changelog de QTS.
Se ejecuta automáticamente vía post-commit hook de git.

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

def parse_commits() -> list[dict]:
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

    # 2. Numstat: insertions/deletions/files per commit
    numstat_raw = run_git("log", "--pretty=format:COMMIT:%H", "--numstat")
    stats: dict[str, dict] = {}
    current_hash = ""
    file_changes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for line in numstat_raw.splitlines():
        if line.startswith("COMMIT:"):
            current_hash = line[7:]
            stats[current_hash] = {"ins": 0, "del": 0, "files": 0}
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
                    file_changes[fname]["ins"]   += ins
                    file_changes[fname]["dels"]  += dels
                    file_changes[fname]["commits"] += 1
                except ValueError:
                    pass

    # 3. Merge
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
        commits.append({**c, **s, "cat": cat, "version": version_label})

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
    <div class="commit-card" data-cat="{cat}" data-msg="{msg.lower()}">
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


# ─── Template HTML principal ──────────────────────────────────────────────────

def build_html(commits: list[dict], stats: dict, file_changes: dict) -> str:
    groups       = group_by_day(commits)
    timeline_html = "\n".join(render_day_group(g) for g in groups)
    top_files_html = render_top_files(stats["top_files"])
    cat_bars_html  = render_cat_bars(stats["cat_dist"])
    module_html    = render_module_pills(stats["modules"])

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
</header>

<!-- MAIN -->
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

<footer>
  Auto-generated by <strong>QTS Changelog Generator</strong> ·
  <strong>{stats["total"]}</strong> commits ·
  <strong>{stats["last_date"]}</strong>
</footer>

<button class="scroll-top" id="scrollTop" title="Volver arriba">↑</button>

<script>
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
