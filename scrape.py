"""Pobere javne urnike učilnic Gimnazije Kranj z eAsistenta in zapiše data.json.

Teče na GitHub Actions; deluje tudi lokalno (Python 3.8+):
    pip install requests beautifulsoup4
    python scrape.py
"""
import datetime
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

SOLA = "56149a5b3eb5cbefdaf9c9ded39a80c8c31d8f33"
PRVA_UCILNICA = "1539875"
BASE = "https://urniki.easistent.com"
STRAN = BASE + "/urniki/" + SOLA + "/ucilnice/" + PRVA_UCILNICA
PREMOR = 0.25  # sekund med zahtevki, da strežnika ne obremenjujemo

DATUM = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.")
POLN_DATUM = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
CAS = re.compile(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})")
# Vsaka celica ima skrito besedilo za bralnike zaslona, npr.
# "Torek štiriindvajseti enajsti. peta ura od 10 ur 40 do 11 ur 25." Odrežemo ga.
UVOD = re.compile(r"^.*?ura\s+od\s.*?do\s+\d+\s+ur(\s+\d+)?\.\s*", re.I)


def cisti(s):
    return re.sub(r"\s+", " ", s).strip()


def vsebina(celica):
    t = UVOD.sub("", cisti(celica.get_text(" ")), count=1)
    return "" if re.match(r"brez\b", t, re.I) else t[:60]


def preberi_stran(seja):
    r = seja.get(STRAN, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    sel = soup.find("select", id="id_parameter")
    ucilnice = [(o["value"], cisti(o.get_text())) for o in sel.find_all("option")] if sel else []
    m = re.search(r"var teden = '(\d+)'", r.text)
    return ucilnice, int(m.group(1)) if m else None


def razcleni(txt):
    deli = txt.split(chr(31))
    html = max((d for d in deli if "<" in d), key=len, default="")
    zacetek = None
    for d in deli:
        if "<" in d:
            continue
        m = POLN_DATUM.search(d)
        if m:
            zacetek = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            break
    soup = BeautifulSoup(html, "html.parser")
    tabele = soup.find_all("table")
    if not tabele:
        return None
    tabela = max(tabele, key=lambda t: len(t.find_all("tr")))
    # samo vrstice glavne tabele, ne vgnezdenih tabel sklopov v celicah
    vrstice = [tr for tr in tabela.find_all("tr") if tr.find_parent("table") is tabela]

    def celice(tr):
        return tr.find_all(["td", "th"], recursive=False)

    glava = next((i for i, tr in enumerate(vrstice)
                  if sum(1 for c in celice(tr) if DATUM.search(c.get_text(" "))) >= 3), -1)
    stolpci = []
    if glava >= 0:
        for i, c in enumerate(celice(vrstice[glava])):
            m = DATUM.search(cisti(c.get_text(" ")))
            if m:
                stolpci.append((i, int(m.group(1)), int(m.group(2))))
    ure = []
    for tr in vrstice[glava + 1:]:
        cs = celice(tr)
        if len(cs) < 2:
            continue
        oznaka = cisti(cs[0].get_text(" "))
        m = CAS.search(oznaka)
        ure.append({
            "lab": oznaka,
            "a": int(m.group(1)) * 60 + int(m.group(2)) if m else None,
            "b": int(m.group(3)) * 60 + int(m.group(4)) if m else None,
            "c": [vsebina(c) for c in cs],
        })
    return {"zacetek": zacetek, "stolpci": stolpci, "ure": ure}


def datum_stolpca(zacetek, dan, mesec):
    leto = zacetek.year + (1 if mesec < zacetek.month else 0)
    return datetime.date(leto, mesec, dan)


def poberi_teden(seja, ucilnice, teden):
    vzorec, podatki = None, {}
    for rid, ime in ucilnice:
        url = "%s/urniki/ajax_urnik/%s/0/0/0/%s/%d/0/1" % (BASE, SOLA, rid, teden)
        try:
            r = seja.get(url, timeout=30, headers={"X-Requested-With": "XMLHttpRequest", "Referer": STRAN})
            r.raise_for_status()
            p = razcleni(r.text)
        except Exception as e:
            print("Napaka pri učilnici %s: %s" % (ime, e), file=sys.stderr)
            p = None
        if p and p["ure"]:
            podatki[ime] = p
            if vzorec is None and p["zacetek"] and p["stolpci"]:
                vzorec = p
        time.sleep(PREMOR)
    if vzorec is None:
        return None

    dni = [datum_stolpca(vzorec["zacetek"], d, m) for _, d, m in vzorec["stolpci"]]
    ure = [{"lab": u["lab"], "a": u["a"], "b": u["b"]} for u in vzorec["ure"]]
    tabela = {}
    for ime, p in podatki.items():
        po_oznaki = {u["lab"]: u for u in p["ure"]}
        stolpec = {(d, m): i for i, d, m in p["stolpci"]}
        matrika = []
        for k, u in enumerate(ure):
            vr = po_oznaki.get(u["lab"]) or (p["ure"][k] if k < len(p["ure"]) else None)
            vrstica = []
            for (i, d, m) in vzorec["stolpci"]:
                ci = stolpec.get((d, m), i)
                vrstica.append(vr["c"][ci] if vr and ci < len(vr["c"]) else "")
            matrika.append(vrstica)
        tabela[ime] = matrika
    return {"teden": teden, "dni": [d.isoformat() for d in dni], "ure": ure, "ucilnice": tabela,
            "manjka": [ime for _, ime in ucilnice if ime not in tabela]}


def main():
    seja = requests.Session()
    seja.headers["User-Agent"] = "Mozilla/5.0 (compatible; proste-ucilnice/1.0)"
    ucilnice, teden = preberi_stran(seja)
    if not ucilnice or teden is None:
        sys.exit("Na strani ni seznama učilnic ali številke tedna.")
    tedni = [w for w in (poberi_teden(seja, ucilnice, t) for t in (teden, teden + 1)) if w]
    if not tedni:
        sys.exit("eAsistent ni vrnil nobenega urnika.")

    pot = "data.json"
    if os.path.exists(pot):
        with open(pot, encoding="utf-8") as f:
            try:
                if json.load(f).get("tedni") == tedni:
                    print("Brez sprememb.")
                    return
            except ValueError:
                pass
    zdaj = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="minutes")
    with open(pot, "w", encoding="utf-8") as f:
        json.dump({"spremenjeno": zdaj, "tedni": tedni}, f, ensure_ascii=False, separators=(",", ":"))
    print("Zapisano: %d tednov, %d učilnic." % (len(tedni), len(tedni[0]["ucilnice"])))


if __name__ == "__main__":
    main()
