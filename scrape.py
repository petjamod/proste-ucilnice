"""Pobere javne urnike učilnic in oddelkov Gimnazije Kranj z eAsistenta in zapiše data.json.

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
STRAN_RAZREDI = BASE + "/urniki/" + SOLA + "/razredi/0"
PREMOR = 0.25  # sekund med zahtevki, da strežnika ne obremenjujemo

DATUM = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.")
POLN_DATUM = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
CAS = re.compile(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})")
# Vsaka celica ima skrito besedilo za bralnike zaslona, npr.
# "Torek štiriindvajseti enajsti. peta ura od 10 ur 40 do 11 ur 25." Odrežemo ga.
UVOD = re.compile(r"^.*?ura\s+od\s.*?do\s+\d+\s+ur(\s+\d+)?\.\s*", re.I)
BLOK = "ednevnik-seznam_ur_teden-blok-wrap"


def cisti(s):
    return re.sub(r"\s+", " ", s).strip()


def skrito(celica):
    """Besedilo za bralnike zaslona brez uvoda z dnevom in uro."""
    sr = celica.find("span", class_="public-urnik-sr-only")
    t = UVOD.sub("", cisti(sr.get_text(" ")) if sr else cisti(celica.get_text(" ")), count=1)
    return "" if re.match(r"brez\b", t, re.I) else t


def bloki(celica):
    """Vidni bloki v celici: en blok na sklop (naslov, podnaslov, oznake kot 'Odpadla ura')."""
    out = []
    for b in celica.find_all("div", class_=BLOK):
        naslov = b.find(class_="ednevnik-title")
        pod = b.find(class_="ednevnik-subtitle")
        oznake = [t.get("title", "") for t in b.find_all(class_=lambda c: c and "wl-tag" in c)
                  if t.get("title") and t.get("title") != "Več skupin"]
        if "odpadla" in " ".join(b.get("class", [])) and not any("odpad" in o.lower() for o in oznake):
            oznake.append("Odpadla ura")
        out.append({"naslov": cisti(naslov.get_text(" ")) if naslov else "",
                    "pod": cisti(pod.get_text(" ")) if pod else "",
                    "oznake": oznake})
    return out


def odpade(b):
    return any("odpad" in o.lower() for o in b["oznake"])


def vsebina(celica):
    """Celica v urniku učilnice: kratek opis ali '' (prosto). 'odpade: ...' pomeni, da odpadejo vsi sklopi."""
    bl = bloki(celica)
    if not bl:
        return skrito(celica)[:60]
    zive = [b for b in bl if not odpade(b)]
    if not zive:
        return ("odpade: " + ", ".join(b["naslov"] for b in bl))[:80]
    po_prof = {}
    for b in zive:  # "4.B, 4.C I. Zupan" namesto "4.B I. Zupan, 4.C I. Zupan"
        r = po_prof.setdefault(b["pod"], [])
        if b["naslov"] not in r:  # dva sklopa istega razreda pri istem profesorju
            r.append(b["naslov"])
    return "; ".join(cisti(", ".join(r) + " " + p) for p, r in po_prof.items())[:80]


def segmenti(celica):
    t = skrito(celica)
    if not t:
        return []
    return [d for d in re.split(r"(?:^|[.,]\s*)sklop\s+\d+:\s*", t) if d.strip()]


def predmet(seg):
    glava = re.split(r"[.,]\s+(?:profesor|učilnica|skupine)\s", seg, maxsplit=1)[0]
    return re.split(r"[.,]\s+", glava)[-1].strip(" .,")


def ucilnica_iz(seg):
    m = re.search(r"učilnica\s+([^,.\s]+)", seg)
    return m.group(1) if m else ""


def vsebina_razreda(celica):
    """Celica v urniku oddelka: seznam ur [predmet, kratica, učilnica, profesor, opomba]."""
    bl = bloki(celica)
    seg = segmenti(celica)
    out = []
    for k, b in enumerate(bl):
        s = seg[k] if len(seg) == len(bl) else ""
        prof, vejica, uc = b["pod"].rpartition(", ")
        if not vejica:
            prof, uc = b["pod"], ""
        out.append([predmet(s) if s else b["naslov"], b["naslov"], ucilnica_iz(s) or uc, prof,
                    ", ".join(o.lower() for o in b["oznake"])])
    if not bl:
        for s in seg:
            out.append([predmet(s), "", ucilnica_iz(s), "", ""])
    return out


def seznam(seja, url):
    r = seja.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    sel = soup.find("select", id="id_parameter")
    enote = [(o["value"], cisti(o.get_text())) for o in sel.find_all("option")] if sel else []
    m = re.search(r"var teden = '(\d+)'", r.text)
    return enote, int(m.group(1)) if m else None


def razcleni(txt, fn):
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
            "c": [fn(c) for c in cs[1:]],
        })
    return {"zacetek": zacetek, "stolpci": stolpci, "ure": ure}


def datum_stolpca(zacetek, dan, mesec):
    leto = zacetek.year + (1 if mesec < zacetek.month else 0)
    return datetime.date(leto, mesec, dan)


def poberi(seja, enote, url, fn):
    podatki = {}
    for eid, ime in enote:
        try:
            r = seja.get(url % eid, timeout=30, headers={"X-Requested-With": "XMLHttpRequest", "Referer": STRAN})
            r.raise_for_status()
            p = razcleni(r.text, fn)
        except Exception as e:
            print("Napaka pri %s: %s" % (ime, e), file=sys.stderr)
            p = None
        if p and p["ure"]:
            podatki[ime] = p
        time.sleep(PREMOR)
    return podatki


def matrike(vzorec, podatki, prazno):
    ure = [u["lab"] for u in vzorec["ure"]]
    tabela = {}
    for ime, p in podatki.items():
        po_oznaki = {u["lab"]: u for u in p["ure"]}
        stolpec = {(d, m): i for i, d, m in p["stolpci"]}
        matrika = []
        for k, lab in enumerate(ure):
            # po zaporedju samo, če ima tabela enake ure (oddelki npr. nimajo vrstice za preduro)
            vr = po_oznaki.get(lab) or (p["ure"][k] if len(p["ure"]) == len(ure) else None)
            vrstica = []
            for (i, d, m) in vzorec["stolpci"]:
                ci = stolpec.get((d, m), i) - 1  # c[] nima stolpca z oznako ure
                vrstica.append(vr["c"][ci] if vr and 0 <= ci < len(vr["c"]) else prazno)
            matrika.append(vrstica)
        tabela[ime] = matrika
    return tabela


def poberi_teden(seja, ucilnice, razredi, teden):
    url_u = "%s/urniki/ajax_urnik/%s/0/0/0/%%s/%d/0/1" % (BASE, SOLA, teden)
    url_r = "%s/urniki/ajax_urnik/%s/%%s/0/0/0/%d/0/1" % (BASE, SOLA, teden)
    pu = poberi(seja, ucilnice, url_u, vsebina)
    vzorec = next((p for p in pu.values() if p["zacetek"] and p["stolpci"]), None)
    if vzorec is None:
        return None
    pr = poberi(seja, razredi, url_r, vsebina_razreda) if razredi else {}
    dni = [datum_stolpca(vzorec["zacetek"], d, m) for _, d, m in vzorec["stolpci"]]
    return {"teden": teden, "dni": [d.isoformat() for d in dni],
            "ure": [{"lab": u["lab"], "a": u["a"], "b": u["b"]} for u in vzorec["ure"]],
            "ucilnice": matrike(vzorec, pu, ""),
            "razredi": matrike(vzorec, pr, []),
            "manjka": [ime for _, ime in ucilnice if ime not in pu] + [ime for _, ime in razredi if ime not in pr]}


def main():
    seja = requests.Session()
    seja.headers["User-Agent"] = "Mozilla/5.0 (compatible; proste-ucilnice/1.0)"
    ucilnice, teden = seznam(seja, STRAN)
    if not ucilnice or teden is None:
        sys.exit("Na strani ni seznama učilnic ali številke tedna.")
    try:
        razredi, _ = seznam(seja, STRAN_RAZREDI)
    except Exception as e:
        print("Seznama oddelkov ni bilo mogoče prebrati: %s" % e, file=sys.stderr)
        razredi = []
    tedni = [w for w in (poberi_teden(seja, ucilnice, razredi, t) for t in (teden, teden + 1)) if w]
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
    print("Zapisano: %d tednov, %d učilnic, %d oddelkov." % (len(tedni), len(tedni[0]["ucilnice"]),
                                                            len(tedni[0]["razredi"])))


if __name__ == "__main__":
    main()
