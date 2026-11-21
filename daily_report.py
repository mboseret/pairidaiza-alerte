"""Bilan des exécutions GitHub Actions des deux robots Pairi Daiza sur 24 h.

Ne vérifie pas en direct les chambres : consulte uniquement l'historique GitHub.
"""
import json
import os
import smtplib
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import getaddresses

WORKFLOWS = [
    ("2 adultes", "monitor.yml"),
    ("3 et 4 adultes", "monitor_3_4.yml"),
]


def fmt(dt):
    if not dt:
        return "aucune"
    return dt.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def api_get(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pairidaiza-daily-report",
    })
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def get_runs(repo, workflow_file, since, now, token):
    base = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs"
    results = []
    # Search in UTC; the created filter limits the historical results.
    created = f"{since.strftime('%Y-%m-%dT%H:%M:%SZ')}..{now.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    for page in range(1, 12):
        params = urllib.parse.urlencode({
            "event": "schedule", "created": created,
            "per_page": 100, "page": page,
        })
        batch = api_get(base + "?" + params, token).get("workflow_runs", [])
        results.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise RuntimeError(f"Trop de résultats pour {workflow_file}; pagination incomplète")
    # Defensive filtering in case the API changes date range handling.
    return [r for r in results if (t := parse_time(r.get("created_at"))) and since <= t <= now and r.get("event") == "schedule"]


def summarize(name, workflow, runs, repo, now):
    completed = [r for r in runs if r.get("status") == "completed"]
    successes = sum(r.get("conclusion") == "success" for r in completed)
    failed = [r for r in completed if r.get("conclusion") not in ("success",)]
    ongoing = [r for r in runs if r.get("status") != "completed"]
    latest = max(runs, key=lambda r: r.get("created_at", ""), default=None)
    lines = [f"{name} ({workflow})", "-" * 36,
             f"Déclenchements automatiques : {len(runs)} / 96 théoriques",
             f"Exécutions réussies : {successes}",
             f"Échecs, annulations ou autres conclusions : {len(failed)}",
             f"Exécutions encore en cours / en attente : {len(ongoing)}",
             f"Dernier lancement automatique : {fmt(parse_time(latest['created_at'])) if latest else 'aucun'}"]
    if latest:
        lines.append(f"Dernier statut : {latest.get('status')} / {latest.get('conclusion') or 'pas encore terminé'}")
        lines.append(f"Dernière exécution : {latest.get('html_url')}")
        age = now - parse_time(latest["created_at"])
        if age > timedelta(minutes=45):
            lines.append(f"⚠ Dernière vérification lancée il y a {int(age.total_seconds() // 60)} minutes")
    else:
        lines.append("⚠ Aucun lancement automatique pendant les dernières 24 heures")
    if failed:
        lines.append("Exécutions problématiques (5 max) :")
        for r in failed[:5]:
            lines.append(f"  - {r.get('conclusion')}: {r.get('html_url')}")
    lines.append(f"Historique : https://github.com/{repo}/actions/workflows/{workflow}")
    return "\n".join(lines), len(failed), len(runs), successes


def send(subject, content):
    sender = os.environ["SMTP_USER"]
    addresses = [addr for _, addr in getaddresses([os.environ["EMAIL_TO"]]) if addr]
    if not addresses:
        raise ValueError("EMAIL_TO ne contient pas d'adresse valable")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(addresses)
    msg.set_content(content)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as server:
        server.login(sender, os.environ["SMTP_APP_PASSWORD"])
        server.send_message(msg)


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    sections = []
    problems = False
    totals = []
    for name, wf in WORKFLOWS:
        runs = get_runs(repo, wf, since, now, token)
        section, failures, count, successes = summarize(name, wf, runs, repo, now)
        sections.append(section)
        totals.append((name, count, successes, failures))
        if failures or count == 0 or (runs and (now - parse_time(max(runs, key=lambda x:x['created_at'])['created_at'])) > timedelta(minutes=45)):
            problems = True

    overview = "\n".join(f"- {name}: {s} réussites / {c} lancements automatiques, {f} autres conclusions" for name, c, s, f in totals)
    body = (f"BILAN QUOTIDIEN PAIRI DAIZA — nuit du 29 au 30 décembre 2026\n"
            f"Période : {fmt(since)} → {fmt(now)}\n\n"
            f"SYNTHÈSE\n{overview}\n\n" + "\n\n".join(sections) +
            "\n\nNOTE IMPORTANTE\n"
            "Ce rapport mesure les démarrages et issues des workflows GitHub Actions, "
            "pas les disponibilités des chambres. Une exécution réussie ne prouve pas "
            "à elle seule que le calendrier a été interprété correctement. "
            "Le nombre 96 correspond à la fréquence théorique de 15 minutes sur 24 h "
            "et non à une garantie de GitHub.\n"
            "Les e-mails de disponibilité restent gérés par les deux robots existants.\n")
    subject = ("⚠ Pairi Daiza — bilan 24 h : anomalie à vérifier" if problems
               else "Pairi Daiza — bilan quotidien des 2 robots")
    send(subject, body)
    print("Rapport envoyé. Résumé : " + overview.replace("\n", " | "))


if __name__ == "__main__":
    main()

