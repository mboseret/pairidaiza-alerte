"""Independent Pairi Daiza monitor for 3 and 4 adults, night 29 Dec 2026.

Does not modify monitor.py or the existing 2-adult GitHub state issue.
Manual runs are diagnostic; scheduled runs only send if ENABLE_MONITOR_34=true.
Guest-count failures cannot trigger an availability notification.
"""
import json
import os
import re
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
ADULT_COUNTS = (3, 4)
OUT = Path("diagnostic_3_4")
OUT.mkdir(exist_ok=True)
ISSUE_TITLE = "[monitor-state-34] Pairi Daiza 2026-12-29"


def enabled(name):
    return os.getenv(name, "").strip().lower() == "true"

# This is intentionally based on the v3 detector validated against the
# actual 29 Dec HTML (disabled cell + rgb(234,234,234)).
INSPECT_JS = r"""() => {
  const clean = s => (s || '').replace(/\s+/g,' ').trim();
  const headers = [...document.querySelectorAll('body *')].filter(e =>
    e.children.length === 0 && /^(December|Décembre|Dezember)\s+2026$/i.test(clean(e.textContent))
  );
  if (!headers.length) return {status:'unknown',reason:'Titre décembre 2026 introuvable'};
  for (const header of headers) {
    let root = header.parentElement;
    for (let i=0;i<7 && root;i++,root=root.parentElement) {
      const cells = [...root.querySelectorAll('.calendar__cell__resort')].filter(cell => {
        const r=cell.getBoundingClientRect();
        return r.width>0 && r.height>0 && [...cell.querySelectorAll('.calendar__number')]
          .some(n=>clean(n.textContent)==='29');
      });
      if (cells.length!==1) continue;
      const cell=cells[0],circle=cell.querySelector('.calendar__content');
      if (!circle) return {status:'unknown',reason:'Élément calendar__content introuvable'};
      const bg=getComputedStyle(circle).backgroundColor;
      const match=bg.match(/^rgba?\(\s*(\d+)[, ]+\s*(\d+)[, ]+\s*(\d+)/);
      if (!match) return {status:'unknown',reason:'Couleur illisible',background:bg};
      const [r,g,b]=match.slice(1,4).map(Number);
      const lightGray=Math.max(r,g,b)-Math.min(r,g,b)<=8 && r>=190 && r<=245;
      const disabled=cell.matches('[disabled],[aria-disabled="true"]') ||
        cell.classList.contains('calendar__cell__resort--disabled');
      const link=cell.querySelector('a.calendar__content');
      const linkUsable=Boolean(link) && !link.matches('[disabled],[aria-disabled="true"]') &&
        getComputedStyle(link).pointerEvents!=='none';
      const evidence={cellClass:cell.className,disabledAttribute:cell.hasAttribute('disabled'),
        ariaDisabled:cell.getAttribute('aria-disabled'),background:bg,lightGray,
        hasLink:Boolean(link),linkUsable};
      if (disabled && lightGray) return {status:'unavailable',reason:'Case grise et désactivée',evidence};
      if (!disabled && !lightGray && linkUsable)
        return {status:'available',reason:'Case non grise, non désactivée, lien utilisable (à confirmer)',evidence};
      return {status:'check',reason:'État ou couleur inhabituel du 29 : à vérifier',evidence};
    }
  }
  return {status:'unknown',reason:'Case 29 introuvable sans ambiguïté'};
}"""

# Find the specific '+' control in the Adults row while the Participants
# dropdown is open. Only return a marker if there is one unambiguous row.
MARK_ADULT_PLUS_JS = r"""() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = e => {const r=e.getBoundingClientRect(); const st=getComputedStyle(e); return r.width>0 && r.height>0 && st.visibility!=='hidden' && st.display!=='none'};
  // Prefer the smallest element whose text is the Adults label.  This works
  // even when the guest controls are divs or SVGs instead of HTML buttons.
  const labels=[...document.querySelectorAll('body *')]
    .filter(e=>visible(e) && /^Adults(?:\s*\(\+?\d+\))?$/i.test(clean(e.textContent)))
    .sort((a,b)=>a.getBoundingClientRect().width-b.getBoundingClientRect().width);
  if (!labels.length) return {ok:false,reason:'Label Adults introuvable',candidates:[]};
  const label=labels[0], lr=label.getBoundingClientRect();
  const centerY=lr.top+lr.height/2;
  // Only examine controls on the very same horizontal Adults line.  The
  // rightmost circular control in this row is the + in the shown layout.
  const seen=new Map();
  for(const e of document.querySelectorAll('body *')) {
    if (!visible(e) || e===label || e.contains(label)) continue;
    const r=e.getBoundingClientRect(), cx=r.left+r.width/2, cy=r.top+r.height/2;
    if(r.width<20||r.width>58||r.height<20||r.height>58) continue;
    if(Math.abs(cy-centerY)>17) continue;
    if(cx<lr.right+65||cx>lr.right+560) continue;
    const k=`${Math.round(cx/5)*5},${Math.round(cy/5)*5}`;
    const cand={x:cx,y:cy,tag:e.tagName,role:e.getAttribute('role'),
      text:clean(e.textContent).slice(0,35),className:typeof e.className==='string'?e.className.slice(0,100):''};
    if(!seen.has(k)) seen.set(k,cand);
  }
  const candidates=[...seen.values()].sort((a,b)=>a.x-b.x);
  // Expect at least a decrement control and increment control. If the
  // layout is different, refuse to guess rather than risk a wrong click.
  if(candidates.length<2) return {ok:false,reason:'Commandes Adults non identifiables',label:clean(label.textContent),candidates};
  const plus=candidates[candidates.length-1];
  const previous=candidates[candidates.length-2];
  if(plus.x-previous.x<15 || plus.x-previous.x>160) {
    return {ok:false,reason:'Espacement boutons Adults ambigu',candidates};
  }
  return {ok:true,reason:'Cible + repérée par position sur ligne Adults',
          x:plus.x,y:plus.y,label:clean(label.textContent),candidates};
}"""

# Check that the Participants summary really switched to the intended number.
VERIFY_ADULTS_JS = r"""expected => {
  const clean=s=>(s||'').replace(/\s+/g,' ').trim();
  const all=[...document.querySelectorAll('body *')].filter(e=>e.children.length===0 &&
    new RegExp('^'+expected+'\\s+adults?$','i').test(clean(e.textContent)) &&
    e.getBoundingClientRect().width>0);
  return {matchCount:all.length,matchingText:all.slice(0,3).map(e=>clean(e.textContent))};
}"""


def dismiss_cookie_dialog(page):
    for candidate in (
        page.get_by_text("Continue without Accepting", exact=False),
        page.get_by_text("Continue without accepting", exact=False),
        page.get_by_role("button", name=re.compile(r"I CONSENT|Accept all", re.I)),
    ):
        try:
            if candidate.count() and candidate.first.is_visible(timeout=1000):
                candidate.first.click(timeout=2500)
                page.wait_for_timeout(450)
                return True
        except Exception:
            pass
    return False


def set_adults(page, count):
    """Return verification evidence. Do not silently assume successful clicks."""
    if count == 2:
        verified = page.evaluate(VERIFY_ADULTS_JS, 2)
        if not verified["matchCount"]:
            return {"ok": False, "reason": "Résumé '2 adults' non confirmé", "verify": verified}
        return {"ok": True, "method": "Page default (2 adults) confirmé", "requested": count, "verify": verified}
    # The summary is '2 adults' in the validated screenshot.
    summary = page.get_by_text(re.compile(r"^2\s+adults?$", re.I)).first
    if summary.count() == 0:
        return {"ok": False, "reason": "Résumé '2 adults' absent; état initial incertain"}
    try:
        summary.click(timeout=3500)
        page.wait_for_timeout(350)
        marks = []
        for expected in range(3, count + 1):
            mark = page.evaluate(MARK_ADULT_PLUS_JS)
            marks.append(mark)
            if not mark.get("ok"):
                return {"ok": False, "reason": mark.get("reason"), "clicks": marks}
            page.mouse.click(mark["x"], mark["y"])
            page.wait_for_timeout(900)
            interim = page.evaluate(VERIFY_ADULTS_JS, expected)
            if not interim["matchCount"]:
                return {"ok": False, "reason": f"Après clic, le résumé '{expected} adults' n'est pas confirmé", "clicks": marks, "verify": interim}
        # Close popup by clicking visible calendar heading, never a day cell.
        header = page.get_by_text(re.compile(r"^December\s+2026$", re.I)).first
        if header.count() and header.is_visible():
            header.click(timeout=3500)
        page.wait_for_timeout(1200)
        verified = page.evaluate(VERIFY_ADULTS_JS, count)
        if not verified["matchCount"]:
            return {"ok": False, "reason": f"Résumé '{count} adults' non confirmé après clic", "verify": verified, "clicks": marks}
        return {"ok": True, "requested": count, "verify": verified, "clicks": marks}
    except Exception as exc:
        return {"ok": False, "reason": f"Sélecteur Participants non validé: {type(exc).__name__}: {str(exc)[:180]}"}


def inspect_calendar_one(browser, count):
    page = browser.new_page(viewport={"width": 1440, "height": 1000},
                            locale="en-GB", timezone_id="Europe/Brussels")
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4500)
        cookies_dismissed = dismiss_cookie_dialog(page)
        participants = set_adults(page, count)
        if not participants["ok"]:
            result = {"status": "unknown", "reason": participants.get("reason", "Participants non confirmés")}
        else:
            result = {"status": "unknown", "reason": "Calendrier non inspecté"}
            for _ in range(5):
                result = page.evaluate(INSPECT_JS)
                if result.get("reason") != "Titre décembre 2026 introuvable":
                    break
                next_btn = page.locator('button[aria-label*="next" i],button[title*="next" i],button[aria-label*="suivant" i]').first
                if not next_btn.count():
                    break
                next_btn.click(timeout=5000)
                page.wait_for_timeout(1200)
            result = page.evaluate(INSPECT_JS)
        page.screenshot(path=str(OUT / f"calendar_{count}_adults.png"), full_page=True)
        return {"target": TARGET_DATE, "adults": count, "children": 0, "babies": 0,
                "duration_nights": 1, "url": page.url, "cookies_dismissed": cookies_dismissed,
                "participants": participants, **result}
    except Exception as exc:
        try:
            page.screenshot(path=str(OUT / f"calendar_{count}_adults.png"), full_page=True)
        except Exception:
            pass
        return {"target": TARGET_DATE, "adults": count, "status": "unknown",
                "reason": f"Erreur technique: {type(exc).__name__}: {str(exc)[:250]}"}
    finally:
        page.close()


def inspect_all():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            results = {str(count): inspect_calendar_one(browser, count) for count in ADULT_COUNTS}
        finally:
            browser.close()
    (OUT / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for count, item in results.items():
        print(json.dumps({"adults": int(count), "target": TARGET_DATE,
                          "status": item["status"], "reason": item.get("reason")}, ensure_ascii=False))
    return results


def github_api(path, method="GET", payload=None):
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        "https://api.github.com/repos/" + repo + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json", "User-Agent": "pairidaiza-monitor"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def get_state_issue():
    issues = github_api("/issues?state=all&per_page=100")
    return next((item for item in issues if item.get("title") == ISSUE_TITLE
                 and "pull_request" not in item), None)


def read_previous(issue):
    if not issue:
        return {}
    try:
        data = json.loads(issue.get("body") or "{}")
        if isinstance(data.get("statuses"), dict):
            return {str(k): v for k, v in data["statuses"].items()}
    except (ValueError, TypeError):
        pass
    return {}


def write_state(issue, statuses):
    body = json.dumps({"statuses": statuses,
                       "updated_utc": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False)
    if issue:
        github_api("/issues/" + str(issue["number"]), "PATCH", {"body": body})
    else:
        github_api("/issues", "POST", {"title": ISSUE_TITLE, "body": body})


def send_email(changes=None, test=False):
    if test:
        subject = "[TEST 3-4 adultes] Alerte Pairi Daiza : messagerie fonctionnelle"
        body = "Ceci est un TEST d'envoi. Cela ne signifie PAS que le 29 décembre est disponible."
    else:
        subject = "Pairi Daiza : 29 décembre — changement pour 3 ou 4 adultes"
        descriptions = []
        for count, status in changes:
            qualifier = "DATE POTENTIELLEMENT DISPONIBLE" if status == "available" else "CHANGEMENT À VÉRIFIER"
            descriptions.append(f"- {count} adultes : {qualifier}")
        body = ("Modification détectée pour la nuit du 29 au 30 décembre 2026 :\n" +
                "\n".join(descriptions) +
                "\n\nVérifie directement sur le site avant toute réservation. "
                "Une date sélectionnable ne garantit pas une chambre réservable pour ce groupe.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    recipients = [address.strip() for address in os.environ["EMAIL_TO"].split(",") if address.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_TO vide")
    msg["To"] = ", ".join(recipients)
    msg.set_content(body + "\n\nRéservations : " + URL)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                          context=ssl.create_default_context(), timeout=25) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_APP_PASSWORD"])
        smtp.send_message(msg)


def main():
    if enabled("TEST_EMAIL"):
        if os.getenv("GITHUB_EVENT_NAME") != "workflow_dispatch":
            raise RuntimeError("TEST_EMAIL autorisé uniquement en lancement manuel")
        send_email(test=True)
        print("E-mail de TEST envoyé (sans contrôle de disponibilité).")
        return
    results = inspect_all()
    if enabled("DIAGNOSTIC"):
        print("Mode diagnostic : aucun e-mail et aucune modification d'état.")
        return
    if not enabled("ENABLE_MONITOR_34"):
        print("Surveillance non activée (ENABLE_MONITOR_34 != true) : aucun e-mail.")
        return
    issue = get_state_issue()
    previous = read_previous(issue)
    current = dict(previous)
    changes = []
    for count, item in results.items():
        status = item["status"]
        if status == "unknown":
            print(f"{count} adultes : état inconnu, on conserve l'historique sans alerte de disponibilité.")
            continue
        prior = previous.get(count)
        print(f"{count} adultes : précédent={prior}, actuel={status}")
        # First verified observation becomes a baseline, never a first-run alert.
        if prior is not None and status in ("available", "check") and status != prior:
            changes.append((count, status))
        current[count] = status
    if changes:
        send_email(changes=changes)
        print(f"Alerte envoyée pour : {changes}")
    if current != previous:
        write_state(issue, current)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        sys.exit(1)
