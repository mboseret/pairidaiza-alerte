"""Monitor Pairi Daiza's 29 Dec 2026 calendar date.

Run diagnostic first: it will never send an email. A date is never considered
available if its status cannot be determined safely.
"""
import json
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "https://booking.pairidaiza.eu/en/rooms/?duration=1"
TARGET_DATE = "2026-12-29"
DIAGNOSTIC = os.getenv("DIAGNOSTIC", "false").lower() == "true"
OUT = Path("diagnostic")
OUT.mkdir(exist_ok=True)

# Read status from DOM AND computed background colour. Hard fail if uncertain.
INSPECT_JS = r"""() => {
 const normalized = s => (s || '').replace(/\s+/g,' ').trim();
 const heading = [...document.querySelectorAll('body *')].filter(e =>
    e.children.length === 0 && /^(December|Décembre|Dezember)\s+2026$/i.test(normalized(e.textContent))
 );
 const el = heading[0];
 if (!el) return {status:'unknown',reason:'Titre décembre 2026 introuvable', headings:[]};
 let month = el.parentElement;
 for (let i=0;i<7 && month;i++,month=month.parentElement){
   const days = [...month.querySelectorAll('*')].filter(e => e.children.length === 0 && normalized(e.textContent)==='29' && e.getBoundingClientRect().width>0);
   if (days.length === 1) {
     const leaf=days[0];
     let node=leaf;
     let observed=[];
     for(let j=0;j<5 && node;j++,node=node.parentElement){
       const st=getComputedStyle(node), r=node.getBoundingClientRect();
       observed.push({tag:node.tagName,role:node.getAttribute('role'),className:typeof node.className==='string'?node.className.slice(0,180):'',ariaDisabled:node.getAttribute('aria-disabled'),disabled:node.hasAttribute('disabled'),title:node.getAttribute('title'),styleBackground:st.backgroundColor,styleColor:st.color,width:r.width,height:r.height});
     }
     const flags=observed.map(x=>[x.className,x.ariaDisabled,x.title,x.disabled?'disabled':''].join(' ')).join(' ').toLowerCase();
     if (/not.available|unavailable|indisponible|disabled|sold.out|fully.booked|fully-booked/.test(flags)) return {status:'unavailable',reason:'Marqueur explicite indisponible', observed};
     const colors=observed.filter(x=>x.width>=20 && x.width<=100 && x.height>=20 && x.height<=100).map(x=>x.styleBackground);
     const rgb=colors.map(c=>{const m=c.match(/^rgba?\((\d+)[ ,]+(\d+)[ ,]+(\d+)/);return m?m.slice(1,4).map(Number):null}).filter(Boolean);
     const neutral=rgb.some(a=>Math.max(...a)-Math.min(...a)<=7 && a[0]>=190 && a[0]<=242);
     const colorful=rgb.some(a=>Math.max(...a)-Math.min(...a)>=28 && Math.max(...a)>=110);
     if(neutral && !colorful) return {status:'unavailable',reason:'Cercle gris clair', observed};
     if(colorful && !neutral) return {status:'available',reason:'Cercle coloré (à valider visuellement)',observed};
     return {status:'unknown',reason:'Couleur ou état ambigu',observed};
   }
 }
 return {status:'unknown',reason:'Case 29 introuvable sans ambiguïté'};
}"""


def inspect_calendar():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, locale="en-GB", timezone_id="Europe/Brussels")
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4500)
            # Calendar opens on the current period; click its next-month arrow
            # cautiously until December 2026 appears. Never click day 29.
            for _ in range(4):
                result = page.evaluate(INSPECT_JS)
                if result.get("reason") != "Titre décembre 2026 introuvable":
                    break
                # Look for an accessible next-month button, otherwise stop safely.
                next_btn = page.locator('button[aria-label*="next" i], button[title*="next" i], button[aria-label*="suivant" i]').first
                if next_btn.count() == 0:
                    break
                next_btn.click(timeout=5000)
                page.wait_for_timeout(1200)
            result = page.evaluate(INSPECT_JS)
            page.screenshot(path=str(OUT/"calendar.png"), full_page=True)
            (OUT/"result.json").write_text(json.dumps({"target":TARGET_DATE, "url":page.url, **result},ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({"target":TARGET_DATE,"status":result['status'],"reason":result.get('reason')},ensure_ascii=False))
            return result['status']
        finally:
            browser.close()


def github_api(path, method="GET", payload=None):
    owner_repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        "https://api.github.com/repos/" + owner_repo + path,
        data=data, method=method,
        headers={"Authorization":"Bearer "+token,"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","Content-Type":"application/json","User-Agent":"pairidaiza-monitor"})
    with urllib.request.urlopen(req,timeout=20) as response:
        return json.load(response)


def get_state_issue():
    issues = github_api("/issues?state=all&per_page=100")
    return next((issue for issue in issues if issue.get("title") == "[monitor-state] Pairi Daiza 2026-12-29" and "pull_request" not in issue),None)


def update_issue(issue, status):
    payload={"body":json.dumps({"status":status,"updated_utc":datetime.now(timezone.utc).isoformat()})}
    if issue:
        github_api("/issues/"+str(issue['number']),"PATCH",payload)
    else:
        payload["title"]="[monitor-state] Pairi Daiza 2026-12-29"
        github_api("/issues","POST",payload)


def send_email():
    msg=EmailMessage()
    msg["Subject"]="Pairi Daiza : le 29 décembre 2026 semble DISPONIBLE !"
    msg["From"]=os.environ["SMTP_USER"]
    msg["To"]=os.environ["EMAIL_TO"]
    msg.set_content("La date du 29 décembre 2026 semble redevenue sélectionnable (1 nuit, 2 adultes). Vérifie et réserve immédiatement :\n"+URL+"\n\nAttention : détection automatisée, la disponibilité réelle doit être confirmée sur le site.")
    with smtplib.SMTP_SSL("smtp.gmail.com",465,context=ssl.create_default_context(),timeout=25) as smtp:
        smtp.login(os.environ["SMTP_USER"],os.environ["SMTP_APP_PASSWORD"])
        smtp.send_message(msg)


def main():
    status=inspect_calendar()
    if DIAGNOSTIC:
        print("Mode diagnostic : aucun e-mail envoyé.")
        return
    if status == "unknown":
        raise RuntimeError("Calendrier non interprétable : aucune alerte envoyée. Voir diagnostic/result.json et calendar.png")
    issue = get_state_issue()
    previous = None
    if issue:
        try: previous = json.loads(issue.get("body") or "{}").get("status")
        except json.JSONDecodeError: pass
    print(f"Statut précédent : {previous}, actuel : {status}")
    if status=="available" and previous!="available":
        send_email()  # update state only after successful email
        print("Email d'alerte envoyé.")
    if previous!=status:
        update_issue(issue,status)

if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(f"ERREUR : {exc}",file=sys.stderr)
        sys.exit(1)
