# Sajtvakt

Din egen ersättare för ManageWP: backup, uppdateringar och återställning av alla dina WordPress-sajter från en panel på sajtvakt.se.

**Vad Sajtvakt gör:**
- **Backup varje natt.** Backuperna sparas i 30 dagar. Bara ändrade filer tar ny plats.
- **Uppdateringar direkt efter backupen.** Går sajten sönder lägger Sajtvakt tillbaka den gamla versionen automatiskt, också om WordPress har kraschat helt.
- **Skyldigt tillägg spärras.** Bryter en av flera uppdateringar sajten testas de en i taget. Det tillägg som orsakade felet återställs och låses, och de övriga går igenom.
- **Push av backup.** Du kan återställa en sajt eller flytta och klona den till en annan. Adressbytet görs automatiskt, även i Elementor-data.
- **Larm via e-post.** Du får ett mejl när en sajt går ner, en backup misslyckas eller en uppdatering har återställts.

Systemet består av två delar:

| Del | Var |
|---|---|
| `sajtvakt.zip` – WordPress-tillägget | installeras på varje sajt |
| den här mappen – servern med panelen | körs på en egen liten server (VPS) |

---

## 1. Det här behöver du

1. **En VPS** med Ubuntu 24.04, minst 2 GB RAM och ungefär 40 GB disk för 15 sajter. Exempel: Hetzner CX22, eller motsvarande hos ett svenskt bolag. Kostnaden ligger runt 5 €/månad.
2. **Domänen sajtvakt.se.** Lägg en DNS **A-post** för `sajtvakt.se`, och gärna `www`, som pekar på serverns IP-adress.
3. **Ett e-postkonto för utskick (SMTP).** Ett konto från ditt webbhotell räcker.

## 2. Installera servern (en gång, ca 15 min)

Logga in på servern med SSH och kör:

```bash
# Docker
curl -fsSL https://get.docker.com | sh

# Hämta Sajtvakt från GitHub
apt-get install -y git
git clone https://github.com/daystudioab/sajtvakt.git /opt/sajtvakt && cd /opt/sajtvakt/server

# Inställningar
cp .env.example .env
nano .env        # fyll i lösenord, e-post och domän

# Datamappen ägs av tjänstens egen användare (den kör inte som root)
mkdir -p data && chown 10001:10001 data

# Starta
docker compose up -d --build
```

Öppna sedan **https://sajtvakt.se** och logga in med lösenordet från `.env`. Byt det därefter under Inställningar. HTTPS-certifikatet ordnas automatiskt.

## 3. Lägg till en sajt

1. På WordPress-sajten går du till **Tillägg → Lägg till nytt → Ladda upp tillägg**, väljer `sajtvakt.zip` och aktiverar.
2. Gå till **Verktyg → Sajtvakt** och klicka **Kopiera**.
3. I panelen klickar du **+ Lägg till sajt** och klistrar in nyckeln.

Första backupen tas direkt. Från natten därpå körs backup och uppdateringar automatiskt.

**Byta från ManageWP:** lägg till sajterna i Sajtvakt först och låt det gå ett par nätter. När allt ser bra ut kopplar du bort sajterna i ManageWP och avinstallerar ManageWP Worker.

## 4. Bra att veta

- **Backuperna ligger på servern.** Slå på hostens backupfunktion för servern (hos Hetzner kostar den 20 % extra) så att du har en kopia på annan plats. Det som behöver skyddas är mappen `data/`.
- **Skärmdumpar.** Efter nattens körning fotograferar servern varje startsida (med en inbyggd webbläsare). Före och efter en uppdatering jämförs bilderna; har mer än en fjärdedel av sidan ändrats fast den svarar normalt, visas en varning i översikten. Ingenting återställs då automatiskt – det kan vara ett bildspel som bytt bild.
- **Kvittera varningar.** Varningar visas i rutan överst i översikten tills du kvitterar dem.
- **Låsta tillägg.** Ett tillägg som har kraschat en sajt låses så att Sajtvakt inte provar igen varje natt. Lås upp det på sajtens sida när en rättad version finns.
- **Större WordPress-versioner** (till exempel 6.8 → 6.9) installeras inte automatiskt. Du kör dem med en knapp i panelen. Mindre säkerhetsuppdateringar sker automatiskt.
- **Premiumtillägg** som Elementor Pro uppdateras bara om licensen är aktiv på sajten. Annars visar panelen det.
- **Panelen visar en varning** om en sajts nödingång inte går att nå, eller om webbservern (nginx) visar Sajtvakts datamapp utifrån. Be då webbhotellet spärra `wp-content/sajtvakt-data`.
- **Ta bort `SAJTVAKT_ADMIN_PASSWORD` ur `.env`** när du har loggat in och bytt lösenord. Den används bara första gången.
- **Nyckeln** ger full kontroll över sajten och ska behandlas som ett lösenord. Har den läckt skapar du en ny under Verktyg → Sajtvakt och klistrar in den på sajtens sida i panelen.

## 5. Underhåll

```bash
cd /opt/sajtvakt/server
docker compose logs -f sajtvakt                        # se vad som händer
git pull && docker compose up -d --build                # hämta och starta senaste versionen
docker compose exec sajtvakt python -m sajtvakt_server set-password   # glömt lösenordet
```
