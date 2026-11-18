# Proste učilnice, Gimnazija Kranj

Stran pokaže proste učilnice (od pritličja navzgor) za izbrani dan in šolsko uro (do 10. ure) iz javnega urnika eAsistent, ter kje ima pouk izbrani razred.
GitHub Actions (`.github/workflows/posodobi.yml`) med poukom vsakih 30 minut zažene `scrape.py` in osveži `data.json`.
Učilnica brez pouka v urniku ni nujno odklenjena ali prazna.
