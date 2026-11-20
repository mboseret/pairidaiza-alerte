"""Pairi Daiza date watcher: 29 December 2026, one night.

Fail-safe: only inspect the date cell and send mail on an interpretable change.
A live run requires ENABLE_MONITOR=true; manual diagnostic never sends mail.
TEST_EMAIL=true sends a test email without interpreting availability, only if
run manually with an explicit workflow option.
"""
import json
import os
import smtplib
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "https://booking.pairidaiza.eu/en/rooms/?duration=1"
TARGET_DATE = "2026-12-29"
OUT = Path("diagnostic")
OUT.mkdir(exist_ok=True)

def enabled(name):
    return os.getenv(name, "").strip().lower() == "true"

# The last validated HTML for unavailable 29 Dec 2026 was:
# calendar__cell__resort--disabled, disabled=true, background rgb(234,234,234).
# Do not assume available dates are orange: any non-grey color is interesting.
INSPECT_JS = r"""() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const headers = [...document.querySelectorAll('body *')].filter(e =>
     e.children.length === 0 && /^(December|Décembre|Dezember)\s+2026$/i.test(clean(e.textContent))
  );
  if (headers.length === 0) return {status:'unknown',reason:'Titre décembre 2026 introuvable'};
  for (const header of headers) {
    let root = header.parentElement;
    for (let i=0;i<7 && root;i++,root=root.parentElement) {
      const candidates = [...root.querySelectorAll('.calendar__cell__resort')].filter(cell => {
        const r = cell.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && [...cell.querySelectorAll('.calendar__number')]
          .some(n => clean(n.textContent) === '29');
      });
      if (candidates.length !== 1) continue;
      const cell = candidates[0];
      const circle = cell.querySelector('.calendar__content');
      if (!circle) return {status:'unknown',reason:'Élément visuel calendar__content introuvable'};
      const style = getComputedStyle(circle);
      const bg = style.backgroundColor;
      const match = bg.match(/^rgba?\(\s*(\d+)[, ]+\s*(\d+)[, ]+\s*(\d+)/);
      if (!match) return {status:'unknown',reason:'Couleur de fond illisible',background:bg};
      const rgb = match.slice(1,4).map(Number);
      const [r,g,b] = rgb;
      const lightGray = Math.max(r,g,b)-Math.min(r,g,b) <= 8 && r>=190 && r<=245;
      const disabled = cell.matches('[disabled], [aria-disabled="true"]') ||
        cell.classList.contains('calendar__cell__resort--disabled');
      const link = cell.querySelector('a.calendar__content');
      const linkUsable = Boolean(link) && !link.matches('[disabled], [aria-disabled="true"]') &&
        getComputedStyle(link).pointerEvents !== 'none';
      const evidence = {cellClass:cell.className,cellDisabledAttribute:cell.hasAttribute('disabled'),
        ariaDisabled:cell.getAttribute('aria-disabled'),background:bg,rgb,lightGray,
        hasLink:Boolean(link),linkUsable};
      if (disabled && lightGray)
        return {status:'unavailable',reason:'Case grise et désactivée',evidence};
      if (!disabled && !lightGray && linkUsable)
        return {status:'available',reason:'Case non grise, non désactivée, lien utilisable (à confirmer sur le site)',evidence};
      // Any different-looking state is a prompt for human review; do not silently discard it.
      return {status:'check',reason:'Changement de couleur ou de propriété disabled à vérifier',evidence};
    }
  }
  return {status:'unknown',reason:'Case 29 décembre introuvable sans ambiguïté'};
}"""

def inspect_calendar():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000},
                                locale="en-GB", timezone_id="Europe/Brussels")
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4500)
            result = {"status":"unknown", "reason":"Le calendrier n'a pas été inspecté"}
            for _ in range(5):
                result = page.evaluate(INSPECT_JS)
                if result.get("reason") != "Titre décembre 2026 introuvable":
                    break
                next_btn = page.locator('button[aria-label*="next" i], button[title*="next" i], button[aria-label*="suivant" i]').first
                if next_btn.count() == 0:
                    break
                next_btn.click(timeout=5000)
                page.wait_for_timeout(1200)
            result = page.evaluate(INSPECT_JS)
            page.screenshot(path=str(OUT / "calendar.png"), full_page=True)
            payload = {"target":TARGET_DATE,"url":page.url,"duration_nights":1,**result}
            (OUT / "result.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({"target":TARGET_DATE,"status":result["status"],
                              "reason":result.get("reason")},ensure_ascii=False))
            return result["status"]
        finally:
            browser.close()

def github_api(path, method="GET", payload=None):
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        "https://api.github.com/repos/" + repository + path,
        data=data, method=method,
        headers={"Authorization":"Bearer " + token,
                 "Accept":"application/vnd.github+json",
                 "X-GitHub-Api-Version":"2022-11-28",
                 "Content-Type":"application/json",
                 "User-Agent":"pairidaiza-monitor"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)

def get_state_issue():
    issues = github_api("/issues?state=all&per_page=100")
    return next((item for item in issues if item.get("title") ==
                 "[monitor-state] Pairi Daiza 2026-12-29" and "pull_request" not in item),None)

def write_state(issue, status):
    body = json.dumps({"status":status,"updated_utc":datetime.now(timezone.utc).isoformat()})
    if issue:
        github_api("/issues/" + str(issue["number"]),"PATCH",{"body":body})
    else:
        github_api("/issues","POST",{"title":"[monitor-state] Pairi Daiza 2026-12-29", "body":body})

def send_email(status, test=False):
    if test:
        subject = "[TEST] Alerte Pairi Daiza : messagerie fonctionnelle"
        body = "Ceci est un TEST d'envoi. Il ne signifie PAS que le 29 décembre est disponible."
    else:
        subject = ("Pairi Daiza : 29 décembre — date potentiellement DISPONIBLE" if status == "available"
                   else "Pairi Daiza : changement du 29 décembre — À VÉRIFIER")
        body = ("Un changement a été détecté pour le 29 décembre 2026, une nuit.\n"
                "Statut : " + status + "\nVérifie immédiatement et réserve si possible.\n"
                "Attention : le robot ne confirme pas une réservation ni les places pour 2 adultes.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(body + "\n\nRéservations : " + URL)
    with smtplib.SMTP_SSL("smtp.gmail.com",465,context=ssl.create_default_context(),timeout=25) as smtp:
        smtp.login(os.environ["SMTP_USER"],os.environ["SMTP_APP_PASSWORD"])
        smtp.send_message(msg)

def main():
    if enabled("TEST_EMAIL"):
        if os.getenv("GITHUB_EVENT_NAME") != "workflow_dispatch":
            raise RuntimeError("TEST_EMAIL autorisé uniquement en lancement manuel")
        send_email("test",test=True)
        print("E-mail de TEST envoyé (sans contrôle de disponibilité).")
        return
    status = inspect_calendar()
    if enabled("DIAGNOSTIC"):
        print("Mode diagnostic : aucun e-mail et aucune modification d'état.")
        return
    if not enabled("ENABLE_MONITOR"):
        print("Surveillance non activée (ENABLE_MONITOR != true) : aucun e-mail.")
        return
    if status == "unknown":
        raise RuntimeError("Calendrier non interprétable : aucune alerte. Voir les fichiers diagnostic.")
    issue = get_state_issue()
    previous = None
    if issue:
        try:
            previous = json.loads(issue.get("body") or "{}").get("status")
        except json.JSONDecodeError:
            pass
    print(f"Statut précédent : {previous}; état actuel : {status}")
    if status in ("available","check") and status != previous:
        send_email(status)
        print("Alerte e-mail envoyée.")
    if previous != status:
        write_state(issue,status)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR : {exc}",file=sys.stderr)
        sys.exit(1)
