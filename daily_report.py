"""Rapport quotidien de la santé des workflows Pairi Daiza.

L'analyse porte sur les événements 'schedule' GitHub, jamais sur les disponibilités.
Aucun changement n'est apporté aux robots ni à leur état.
"""
import html
import json
import os
import smtplib
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import getaddresses
from zoneinfo import ZoneInfo

LOCAL = ZoneInfo("Europe/Brussels")
WORKFLOWS = [
    ("2 adultes", "monitor.yml", (7, 22, 37, 52)),
    ("3 et 4 adultes", "monitor_3_4.yml", (11, 26, 41, 56)),
]
MAX_DELAY_MINUTES = 20  # derniers créneaux exclus des estimations: GitHub peut démarrer en retard
GAP_WARNING_MINUTES = 45


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def local_time(value):
    dt = parse_time(value) if isinstance(value, str) else value
    return dt.astimezone(LOCAL).strftime("%d/%m %H:%M") if dt else "aucune"


def api_get(url, token):
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pairidaiza-daily-report-v2",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def get_workflow(repo, filename, token):
    return api_get(f"https://api.github.com/repos/{repo}/actions/workflows/{filename}", token)


def get_runs(repo, filename, start, end, token):
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{filename}/runs"
    date_filter = f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}..{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    result = []
    for page in range(1, 12):
        query = urllib.parse.urlencode({"event": "schedule", "created": date_filter,
                                        "page": page, "per_page": 100})
        items = api_get(url + "?" + query, token).get("workflow_runs", [])
        result.extend(items)
        if len(items) < 100:
            break
    else:
        raise RuntimeError(f"Pagination incomplète pour {filename}")
    return sorted((run for run in result if run.get("event") == "schedule"
                   and (dt := parse_time(run.get("created_at"))) and start <= dt <= end),
                  key=lambda r: r["created_at"])


def scheduled_slots(start, end, minutes):
    """Créneaux UTC théoriques dans [start,end], compte tenu du délai de grâce."""
    current = start.replace(second=0, microsecond=0)
    if current < start:
        current += timedelta(minutes=1)
    count = 0
    while current <= end:
        if current.minute in minutes:
            count += 1
        current += timedelta(minutes=1)
    return count


def summary(name, filename, minutes, created, runs, start, now, repo):
    # Date réelle de création du workflow (API GitHub), afin de ne pas compter
    # les créneaux précédant son existence. Pour un fichier ancien, fenêtre 24 h.
    effective_start = max(start, created) if created else start
    mature = created is not None and created <= start
    due_until = now - timedelta(minutes=MAX_DELAY_MINUTES)
    expected = scheduled_slots(effective_start, due_until, minutes) if effective_start <= due_until else 0
    scheduled = [r for r in runs if parse_time(r["created_at"]) >= effective_start]
    successes = sum(r.get("status") == "completed" and r.get("conclusion") == "success" for r in scheduled)
    failures = [r for r in scheduled if r.get("status") == "completed" and r.get("conclusion") != "success"]
    pending = sum(r.get("status") != "completed" for r in scheduled)
    # Certains départs peuvent être retardés; les exécutions et les créneaux
    # ne correspondent donc pas nécessairement exactement.
    estimated_unseen = max(0, expected - len(scheduled))
    times = [parse_time(r["created_at"]) for r in scheduled]
    gaps = [(int((b-a).total_seconds() / 60), a, b) for a, b in zip(times, times[1:])]
    long_gaps = sorted((g for g in gaps if g[0] > GAP_WARNING_MINUTES), reverse=True)
    last = scheduled[-1] if scheduled else None
    age_minutes = int((now - times[-1]).total_seconds() / 60) if times else None
    first_age = int((now - effective_start).total_seconds() / 60)
    # 'Début récent' : aucun retard d'installation ne doit être appelé "manqué".
    recent = not mature
    warnings = []
    if failures:
        warnings.append(f"{len(failures)} exécution(s) échouée(s)/annulée(s)")
    if age_minutes is not None and age_minutes > GAP_WARNING_MINUTES:
        warnings.append(f"dernier lancement il y a {age_minutes} min")
    if age_minutes is None and first_age > GAP_WARNING_MINUTES:
        warnings.append("aucun déclenchement depuis l'activation observée")
    if long_gaps:
        warnings.append(f"{len(long_gaps)} intervalle(s) de plus de 45 min")
    # Écart affiché séparément; ce n'est pas une preuve de vérifications perdues.
    if estimated_unseen and expected and mature:
        warnings.append(f"{estimated_unseen} créneau(x) théorique(s) sans départ observé")
    return {
        "name": name, "file": filename, "created": created, "effective_start": effective_start,
        "recent": recent, "runs": len(scheduled), "success": successes,
        "failed": len(failures), "pending": pending, "expected": expected,
        "estimated_unseen": estimated_unseen, "last": last, "age": age_minutes,
        "long_gaps": long_gaps, "warnings": warnings,
        "history": f"https://github.com/{repo}/actions/workflows/{filename}",
        "fail_links": [r.get("html_url") for r in failures[:3] if r.get("html_url")],
    }


def build_email(rows, start, now):
    has_warning = any(row["warnings"] for row in rows)
    subject = ("⚠ Pairi Daiza — rapport quotidien : points à vérifier" if has_warning
               else "Pairi Daiza — rapport quotidien des 2 robots")
    lines = ["BILAN QUOTIDIEN PAIRI DAIZA — 29 au 30 décembre 2026",
             f"Période : {local_time(start)} → {local_time(now)} (heure belge)", ""]
    trs = []
    for r in rows:
        status = "À vérifier" if r["warnings"] else "OK"
        scope = "depuis la création du workflow" if r["recent"] else "sur 24 h"
        lines += [f"{r['name']} — {status}",
                  f"  {r['success']} réussites / {r['runs']} démarrages, {r['failed']} échec(s), {r['pending']} en cours",
                  f"  {r['expected']} créneaux théoriques échus ({scope}; marge de {MAX_DELAY_MINUTES} min)",
                  f"  Dernier départ : {local_time(r['last']['created_at']) if r['last'] else 'aucun'}",
                  f"  Créé le : {local_time(r['created']) if r['created'] else 'date inconnue'}"]
        if r["recent"]:
            lines.append("  Workflow récent : les heures avant sa création sont exclues.")
        lines += ["  " + w for w in r["warnings"]]
        lines += ["  Historique : " + r["history"], ""]
        warning = "; ".join(r["warnings"]) if r["warnings"] else "Aucune anomalie identifiée"
        # Escaping systématique du texte API et des URL.
        trs.append("<tr>" + "".join(
            "<td style='padding:10px;border-bottom:1px solid #ddd;vertical-align:top'>" + html.escape(str(value)) + "</td>"
            for value in [r["name"], f"{r['success']} / {r['runs']}", str(r["failed"]),
                          str(r["expected"]), local_time(r["last"]["created_at"]) if r["last"] else "Aucun",
                          warning]) + "</tr>")
    lines += ["NOTE : les créneaux théoriques ne sont pas des exécutions garanties.",
              "GitHub peut retarder ou ignorer des déclenchements ; les heures avant la création sont exclues.",
              "Ce rapport mesure uniquement les exécutions GitHub, pas la disponibilité réelle des chambres."]
    table = """<table style="border-collapse:collapse;width:100%;font-size:13px">
<thead><tr style="background:#f1f3f5">""" + "".join(
        f"<th style='padding:10px;text-align:left'>{h}</th>" for h in
        ["Robot", "Réussites / lancements", "Échecs", "Créneaux échus*", "Dernier départ (BE)", "État"]
    ) + "</tr></thead><tbody>" + "".join(trs) + "</tbody></table>"
    links = "".join(f"<li><a href='{html.escape(r['history'], quote=True)}'>{html.escape(r['name'])} — historique GitHub</a></li>" for r in rows)
    content = f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#262626;line-height:1.5;max-width:980px">
<h2>Pairi Daiza — bilan quotidien</h2>
<p>Nuit du <strong>29 au 30 décembre 2026</strong><br>Période analysée : {html.escape(local_time(start))} → {html.escape(local_time(now))} (Belgique)</p>
{table}
<p style="font-size:12px;color:#666">* Les créneaux prévus avant la création de chaque workflow sont exclus.
Les {MAX_DELAY_MINUTES} dernières minutes ne sont pas comptées comme manquées, pour laisser une marge aux retards GitHub.
Un créneau sans exécution observée est une estimation, et non une preuve de panne.</p>
<h3>Consulter les exécutions</h3><ul>{links}</ul>
<p style="font-size:12px;color:#666">Ce rapport mesure la santé des automatisations GitHub, et non les chambres disponibles.
Les alertes de réservation restent indépendantes.</p></body></html>"""
    return subject, "\n".join(lines), content


def send(subject, plain, rich):
    sender = os.environ["SMTP_USER"]
    addresses = [a for _, a in getaddresses([os.environ["EMAIL_TO"]]) if a]
    if not addresses:
        raise ValueError("EMAIL_TO ne contient aucun destinataire")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(addresses)
    msg.set_content(plain)
    msg.add_alternative(rich, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(sender, os.environ["SMTP_APP_PASSWORD"])
        smtp.send_message(msg)


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)
    rows = []
    for name, filename, minutes in WORKFLOWS:
        wf = get_workflow(repo, filename, token)
        created = parse_time(wf.get("created_at"))
        runs = get_runs(repo, filename, start, now, token)
        rows.append(summary(name, filename, minutes, created, runs, start, now, repo))
    subject, plain, rich = build_email(rows, start, now)
    send(subject, plain, rich)
    print("Rapport quotidien envoyé : " + "; ".join(
        f"{r['name']}: {r['success']}/{r['runs']} réussies" for r in rows))


if __name__ == "__main__":
    main()
