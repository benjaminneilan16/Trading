# Market Data Engine (Fas 1) — KuCoin

Samlar in marknadsdata från KuCoin och sparar i PostgreSQL:
- **OHLCV** (candles) — spot
- **Orderbok** (snapshot, topp 20 nivåer) — spot
- **Trades** (senaste avslut) — spot
- **Funding rate** och **Open interest** — KuCoin Futures (perpetuals)

> **Liquidations** är inte med än — KuCoin exponerar det via websocket, inte REST.
> Bra nästa steg när du vill bygga vidare (se "Nästa steg" nedan).

## 1. Installera PostgreSQL

Om du inte redan har det, enklast med Docker:

```bash
docker run --name crypto-postgres -e POSTGRES_PASSWORD=change_me \
  -e POSTGRES_DB=crypto_trading -p 5432:5432 -d postgres:16
```

## 2. Skapa tabellerna

```bash
psql -h localhost -U postgres -d crypto_trading -f schema.sql
# (lösenordet du satte ovan, eller ange -W för att bli tillfrågad)
```

## 3. Python-miljö

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 4. Konfiguration

```bash
cp .env.example .env
```

Öppna `.env` och fyll i:
- `DB_PASSWORD` — samma som du satte i steg 1
- `SYMBOLS` — vilka spot-par du vill samla data för
- `FUTURES_SYMBOLS` — vilka futures-kontrakt för funding/OI
- API-nycklar behövs **inte** för marknadsdata, bara för framtida handel

## 5. Kör

```bash
python main.py
```

Du bör se loggrader som:
```
2026-07-28 ... [INFO] main: Startar loop: ohlcv (var 60s)
2026-07-28 ... [INFO] ohlcv: OHLCV BTC/USDT: 5 candles sparade
```

Avbryt med `Ctrl+C`.

## 6. Verifiera i databasen

```sql
SELECT * FROM ohlcv ORDER BY ts DESC LIMIT 10;
SELECT * FROM orderbook_snapshots ORDER BY ts DESC LIMIT 5;
SELECT * FROM funding_rates ORDER BY ts DESC LIMIT 5;
```

## Mappstruktur (matchar din projektplan)

```
market_data_engine/
  collectors/
    exchange.py     # ccxt-klienter (spot + futures)
    ohlcv.py
    orderbook.py
    trades.py
    funding.py
  config.py         # läser .env
  db.py             # alla databas-inserts
  main.py           # startar alla insamlingsloopar
  schema.sql
```

När du går vidare till Fas 2 (Execution Engine) lägger du den i en
egen `/execution`-mapp bredvid den här, och återanvänder `config.py`
och samma databas.

## 7. Koppla till din iPhone (som en egen app, utan App Store)

Detta är en **PWA** (webbsida som beter sig som en app). Ingen App Store behövs.

### Starta API-servern (istället för `main.py` — den gör samma jobb men går att styra via appen)

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

### Hitta datorns IP-adress på ditt wifi

- Mac: `ipconfig getifaddr en0`
- Windows: `ipconfig` (leta efter IPv4-adress)

Du får något i stil med `192.168.1.42`.

### På din iPhone (samma wifi som datorn)

1. Öppna **Safari** och gå till `http://192.168.1.42:8000` (din IP + port)
2. Tryck på **Dela**-ikonen (fyrkant med pil upp)
3. Välj **"Lägg till på hemskärmen"**
4. Ge den ett namn, t.ex. "Crypto Bot" — klart!

Nu har du en app-ikon på hemskärmen. Öppnar du den går den i fullskärm
utan Safaris adressfält, precis som en riktig app.

Första gången frågar dashboarden efter din **API-nyckel** (samma som
`API_KEY` i `.env`) — det är för att bara du ska kunna starta/stoppa
boten, inte vem som helst på samma wifi.

### Vill du komma åt den även när du INTE är hemma?

Då räcker inte lokal IP. Enklaste gratislösningen är **Tailscale**
(en app du installerar på både datorn och iPhone — finns i App Store,
men är bara ett VPN, inte "din" app). Då kan du nå datorns Tailscale-IP
varifrån som helst, lika säkert som hemma på wifi.

### Om Telegram-notiser

Se instruktionerna högst upp i `notifier.py` för att skapa en bot på
2 minuter. När du har token + chat_id i `.env`, fungerar
testnotis-knappen i appen direkt. Riktiga köp/sälj-notiser kopplas in
när Execution Engine (Fas 2) är byggd.

## 8. Koppla en Lovable-frontend till backend

Backend är nu en fristående REST-API, redo att drivas av vilken frontend
som helst — inklusive en du bygger i Lovable.

### Ge Lovable API-specen

Starta servern (`uvicorn api:app --host 0.0.0.0 --port 8000`) och öppna:

```
http://localhost:8000/docs
```

Det är en interaktiv Swagger-sida med alla endpoints, parametrar och
svarsformat. Maskinläsbar version finns på `http://localhost:8000/openapi.json`
— den kan du klistra in eller länka i Lovable så den förstår hela API:t
direkt, istället för att du behöver beskriva varje endpoint för hand.

### Viktiga endpoints för frontend att bygga UI mot

| Endpoint | Metod | Vad den gör |
|---|---|---|
| `/api/status` | GET | Är motorn igång, senaste körningar, fel |
| `/api/start` / `/api/stop` | POST | Starta/stoppa insamlingen |
| `/api/settings` | GET | Vilka symboler/intervaller som är konfigurerade |
| `/api/symbols` | GET | Lista över bevakade spot/futures-symboler |
| `/api/ohlcv?symbol=BTC/USDT&timeframe=1m&limit=200` | GET | Candle-data för grafer |
| `/api/orderbook?symbol=BTC/USDT` | GET | Senaste orderboks-snapshot |
| `/api/trades?symbol=BTC/USDT&limit=50` | GET | Senaste avslut |
| `/api/funding?symbol=BTC/USDT:USDT` | GET | Senaste funding rate |
| `/api/open-interest?symbol=BTC/USDT:USDT` | GET | Senaste open interest |
| `/api/notify` | POST | Skicka egen Telegram-notis, body: `{"message": "..."}` |

**Alla endpoints kräver headern** `X-API-Key: <ditt API_KEY från .env>`.
Säg åt Lovable att lägga till den headern på varje anrop.

### Ett viktigt "men" — publik åtkomst

Lovable hostar sin frontend på internet (t.ex. `dittprojekt.lovable.app`).
Den kan **inte** nå `localhost:8000` eller din lokala wifi-IP — det är bara
nåbart från nätverk i ditt hem. För att Lovable-frontenden ska kunna prata
med din backend behöver den vara nåbar publikt, t.ex. via:

- **Cloudflare Tunnel** (gratis): `brew install cloudflared` →
  `cloudflared tunnel --url http://localhost:8000` ger dig en publik
  `https://....trycloudflare.com`-adress som pekar mot din Mac
- Eller senare: flytta backend till en billig molnserver (Railway,
  Render, Fly.io, DigitalOcean) så den alltid är nåbar

Den gamla dashboarden (`dashboard/index.html`) finns kvar i projektet
som en enkel fallback-app om du vill jämföra, men den behövs inte längre
— Lovable-frontenden ersätter den.

## Köra i molnet istället (rekommenderas om din Mac krånglar)

Det här sättet kräver **ingen terminal, ingen Homebrew, ingen lokal
Python** — allt sker i webbläsaren. Bra om din Mac har ett äldre
macOS (t.ex. Catalina) som gör lokala installationer krångliga.

### Steg 1 — Lägg upp koden på GitHub (utan git-kommandon)

1. Gå till **github.com**, skapa ett gratis konto om du inte har ett
2. Klicka **"New repository"** → ge det ett namn, t.ex. `crypto-bot` → **Create repository**
3. Klicka **"uploading an existing file"** (länk mitt i sidan)
4. Dra in **alla filer och mappar** från din uppackade `market_data_engine`-zip
5. Klicka **Commit changes**

### Steg 2 — Skapa konto på Railway

1. Gå till **railway.app** → **Login** → logga in med GitHub
2. Klicka **New Project** → **Deploy from GitHub repo** → välj `crypto-bot`
3. Railway upptäcker automatiskt att det är ett Python-projekt och bygger det

### Steg 3 — Lägg till en databas (två klick, inget kommando)

1. I ditt Railway-projekt: klicka **New** → **Database** → **Add PostgreSQL**
2. Klart — Railway skapar en databas och kopplar ihop den automatiskt

### Steg 4 — Koppla databasen till din app

Klicka på din app-service → fliken **Variables** → lägg till dessa
(Railway föreslår dem automatiskt när du skriver `$` — välj referens
till Postgres-tjänsten istället för att skriva egna värden):

```
DB_HOST=${{Postgres.PGHOST}}
DB_PORT=${{Postgres.PGPORT}}
DB_NAME=${{Postgres.PGDATABASE}}
DB_USER=${{Postgres.PGUSER}}
DB_PASSWORD=${{Postgres.PGPASSWORD}}
```

Lägg också till dina egna inställningar i samma Variables-flik:

```
SYMBOLS=BTC/USDT,ETH/USDT,SOL/USDT
FUTURES_SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT
API_KEY=sätt-ett-eget-hemligt-lösenord-här
AUTO_START=true
```

(Telegram-variabler också om du vill ha notiser: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)

### Steg 5 — Skapa tabellerna i databasen (ingen terminal behövs)

1. Klicka på Postgres-tjänsten i Railway → fliken **Data** → **Query**
2. Öppna `schema.sql` (från zip-filen) i valfri textredigerare, kopiera hela innehållet
3. Klistra in i Query-rutan i Railway → kör den

### Steg 6 — Gör appen publikt nåbar

1. Klicka på din app-service → fliken **Settings** → **Networking**
2. Klicka **Generate Domain**
3. Du får en adress typ `crypto-bot-production.up.railway.app` — **det är din publika backend-URL**, redan med https, ingen tunnel behövs

### Steg 7 — Testa

Gå till `https://din-app.up.railway.app/docs` i webbläsaren. Ser du
API-dokumentationen? Då körs allt, dygnet runt, utan att din Mac
behöver vara påslagen.

### Steg 8 — Ge URL:en till Lovable

Använd exakt samma Lovable-prompt som tidigare, men byt bara ut
tunnel-adressen mot din nya `https://din-app.up.railway.app`.

> **Kostnad:** Railway ger nya konton gratis startkrediter (räcker en
> bra stund för ett litet projekt som detta). Efter det kostar det
> normalt någon dollar i månaden för ett litet projekt som körs dygnet
> runt — betydligt billigare än besväret med lokal drift.

## Nästa steg (förslag)

1. **Liquidations via websocket** — KuCoin Futures har en
   `/contractMarket/snapshot` / liquidation-ws-kanal. Kräver
   `ccxt.pro` eller en egen websocket-klient (`websockets`-biblioteket).
2. **Fler timeframes** — kör `collect_ohlcv` även för `5m`/`1h`/`1d`.
3. **Data-kvalitet** — lägg till ett litet script som kollar luckor
   (missade candles) i `ohlcv`-tabellen.
4. **Docker Compose** — paketera Postgres + detta script tillsammans
   så det är en `docker compose up` för hela Fas 1.

Säg till när du vill bygga något av detta, eller gå vidare till
**Fas 2: Execution Engine**.

---
**Viktigt om risk:** detta är infrastruktur för datainsamling, inte
handelsråd. När du senare bygger Execution Engine och Risk Manager,
testa alltid grundligt i paper trading innan riktiga pengar sätts in.
