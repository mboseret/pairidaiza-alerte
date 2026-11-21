"""Rapport journalier Pairi Daiza pour deux workflows déclenchés par cron-job.org.

Le rapport mesure les exécutions GitHub, et non la disponibilité des chambres.
Les événements workflow_dispatch sont inclus (API externe ET lancements manuels) :
GitHub ne permet pas de les distinguer sûrement à partir du seul événement.
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

LOCAL = ZoneInfo('Europe/Brussels')
WORKFLOWS = [
    ('2 adultes', 'monitor.yml', (7, 22, 37, 52), datetime(2026, 11, 21, 10, 37, tzinfo=LOCAL)),
    ('3 et 4 adultes', 'monitor_3_4.yml', (11, 26, 41, 56), datetime(2026, 11, 21, 10, 41, tzinfo=LOCAL)),
]
GRACE_MINUTES = 20
GAP_WARNING_MINUTES = 45


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def formatted(value):
    if isinstance(value, str):
        value = parse_time(value)
    return value.astimezone(LOCAL).strftime('%d/%m %H:%M') if value else 'aucun'


def api_get(url, token):
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'pairidaiza-daily-report-v3',
    })
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def get_runs(repo, filename, start, end, token):
    base = f'https://api.github.com/repos/{repo}/actions/workflows/{filename}/runs'
    date_filter = f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}..{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    all_runs = []
    for page in range(1, 12):
        query = urllib.parse.urlencode({'created': date_filter, 'per_page': 100, 'page': page})
        items = api_get(base + '?' + query, token).get('workflow_runs', [])
        all_runs.extend(items)
        if len(items) < 100:
            break
    else:
        raise RuntimeError(f'Pagination GitHub incomplète pour {filename}')
    relevant = [r for r in all_runs if r.get('event') in ('workflow_dispatch', 'schedule')
                and (dt := parse_time(r.get('created_at'))) and start <= dt <= end]
    return sorted(relevant, key=lambda r: r['created_at'])


def expected_slots(start, end, minutes):
    # Les créneaux de cron-job.org sont dans le fuseau Europe/Brussels.
    # Itération en UTC pour éviter les dates imaginaires lors du changement d'heure.
    current = start.replace(second=0, microsecond=0)
    if current < start:
        current += timedelta(minutes=1)
    count = 0
    while current <= end:
        local = current.astimezone(LOCAL)
        if local.minute in minutes:
            count += 1
        current += timedelta(minutes=1)
    return count


def summarize(name, filename, minutes, activated_local, runs, start, now, repo):
    activated = activated_local.astimezone(timezone.utc)
    effective_start = max(start, activated)
    runs = [r for r in runs if parse_time(r['created_at']) >= effective_start]
    successes = sum(r.get('status') == 'completed' and r.get('conclusion') == 'success' for r in runs)
    failures = [r for r in runs if r.get('status') == 'completed' and r.get('conclusion') != 'success']
    pending = sum(r.get('status') != 'completed' for r in runs)
    external_style = sum(r.get('event') == 'workflow_dispatch' for r in runs)
    old_schedule = sum(r.get('event') == 'schedule' for r in runs)
    due_until = now - timedelta(minutes=GRACE_MINUTES)
    expected = expected_slots(effective_start, due_until, minutes) if effective_start <= due_until else 0
    # Si des lancements GitHub schedule subsistent dans la fenêtre de 24h, ils sont
    # comptés comme observations mais pas considérés comme preuve de cron-job.org.
    estimated_missing = max(0, expected - external_style)
    times = [parse_time(r['created_at']) for r in runs if r.get('event') == 'workflow_dispatch']
    long_gaps = [int((b - a).total_seconds() // 60) for a, b in zip(times, times[1:])
                 if (b - a).total_seconds() > GAP_WARNING_MINUTES * 60]
    last = runs[-1] if runs else None
    last_external = times[-1] if times else None
    age = int((now - last_external).total_seconds() // 60) if last_external else None
    warnings = []
    if failures:
        warnings.append(f'{len(failures)} exécution(s) échouée(s)')
    if long_gaps:
        warnings.append(f'{len(long_gaps)} intervalle(s) externe(s) de plus de {GAP_WARNING_MINUTES} min')
    if age is not None and age > GAP_WARNING_MINUTES:
        warnings.append(f'dernier lancement externe il y a {age} min')
    elif age is None and (now - activated).total_seconds() > GAP_WARNING_MINUTES * 60:
        warnings.append('aucun lancement externe observé')
    if estimated_missing:
        warnings.append(f'{estimated_missing} créneau(x) sans workflow_dispatch observé (estimation)')
    return {
        'name': name, 'runs': len(runs), 'success': successes, 'failed': len(failures),
        'pending': pending, 'dispatch': external_style, 'schedule': old_schedule,
        'expected': expected, 'missing': estimated_missing, 'last': last,
        'last_external': last_external, 'warnings': warnings,
        'recent': activated > start,
        'history': f'https://github.com/{repo}/actions/workflows/{filename}',
    }


def make_email(rows, start, now):
    alert = any(r['warnings'] for r in rows)
    subject = ('⚠ Pairi Daiza — rapport quotidien : points à vérifier' if alert
               else 'Pairi Daiza — rapport quotidien : contrôles OK')
    header = f'Pairi Daiza — nuit du 29 au 30 décembre 2026\nPériode : {formatted(start)} → {formatted(now)} (Belgique)'
    note = ('Les événements workflow_dispatch comprennent les lancements de cron-job.org '
            'et les tests manuels GitHub ; ils ne sont pas identifiables séparément via ce résumé. '
            'Les anciens événements schedule sont indiqués séparément. '
            'Les créneaux théoriques ne garantissent pas une exécution effective. '
            'Ce bilan ne confirme pas la disponibilité des chambres.')
    plain = [header, '']
    trs = []
    for r in rows:
        state = '; '.join(r['warnings']) if r['warnings'] else 'OK'
        last = formatted(r['last_external'])
        plain += [f"{r['name']}: {r['success']}/{r['runs']} réussies, "
                  f"{r['failed']} échec(s), {r['pending']} en cours",
                  f"  workflow_dispatch : {r['dispatch']} | anciens schedule : {r['schedule']}",
                  f"  Créneaux externes échus : {r['expected']} | absents estimés : {r['missing']}",
                  f"  Dernier lancement externe : {last} | État : {state}",
                  f"  Historique : {r['history']}", '']
        values = [r['name'], f"{r['success']} / {r['runs']}", r['failed'],
                  r['dispatch'], r['schedule'], r['expected'], r['missing'], last, state]
        trs.append('<tr>' + ''.join(
            f"<td style='border-bottom:1px solid #ddd;padding:9px;vertical-align:top'>{html.escape(str(v))}</td>"
            for v in values) + '</tr>')
    plain += [note]
    headings = ['Robot', 'Réussites / total', 'Dispatch', 'Schedule ancien',
                'Créneaux échus', 'Sans départ estimés', 'Dernier dispatch', 'État']
    # Colonne des échecs dans le texte; ajout explicite à l'HTML aussi.
    headings = ['Robot', 'Réussites / total', 'Échecs', 'Dispatch', 'Schedule ancien',
                'Créneaux échus', 'Manquants estimés', 'Dernier dispatch', 'État']
    th = ''.join(f"<th style='padding:9px;text-align:left;background:#f1f3f5'>{html.escape(h)}</th>" for h in headings)
    links = ''.join(f"<li><a href='{html.escape(r['history'], quote=True)}'>{html.escape(r['name'])}</a></li>" for r in rows)
    body = ("<!doctype html><html><body style='font-family:Arial,sans-serif;line-height:1.5;color:#222'>"
            "<h2>Pairi Daiza — bilan quotidien</h2>"
            f"<p>Nuit du <b>29 au 30 décembre 2026</b><br>Période : {html.escape(formatted(start))} → {html.escape(formatted(now))} (Belgique)</p>"
            f"<div style='overflow-x:auto'><table style='border-collapse:collapse;font-size:12px;width:100%'><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table></div>"
            f"<p style='font-size:12px;color:#666'>{html.escape(note)}</p>"
            f"<h3>Historique GitHub</h3><ul>{links}</ul>"
            "</body></html>")
    return subject, '\n'.join(plain), body


def send(subject, plain, rich):
    recipients = [address for _, address in getaddresses([os.environ['EMAIL_TO']]) if address]
    if not recipients:
        raise ValueError('EMAIL_TO ne contient aucun destinataire')
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = os.environ['SMTP_USER']
    msg['To'] = ', '.join(recipients)
    msg.set_content(plain)
    msg.add_alternative(rich, subtype='html')
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_APP_PASSWORD'])
        smtp.send_message(msg)


def main():
    repo = os.environ['GITHUB_REPOSITORY']
    token = os.environ['GITHUB_TOKEN']
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)
    rows = []
    for name, filename, minutes, activated in WORKFLOWS:
        runs = get_runs(repo, filename, start, now, token)
        rows.append(summarize(name, filename, minutes, activated, runs, start, now, repo))
    subject, plain, rich = make_email(rows, start, now)
    send(subject, plain, rich)
    print('Rapport envoyé : ' + '; '.join(f"{r['name']} {r['success']}/{r['runs']}" for r in rows))


if __name__ == '__main__':
    main()
