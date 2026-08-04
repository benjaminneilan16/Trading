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

## Paper trading — automatisk handel med låtsaspengar (nytt)

Motorn har nu en **regelbaserad automatisk strategi** som körs var 5:e
minut (justerbart via `STRATEGY_INTERVAL_SECONDS`) för varje symbol:

1. **Technical Engine** (`technical.py`) räknar ut EMA12/EMA26, RSI14, MACD
2. **Decision Engine** (`strategy.py`) väger samman dessa till Köp/Sälj/Avvakta
3. **Paper Trading Engine** (`paper_trading.py`) exekverar automatiskt mot
   ett låtsaskonto (start: 10 000 låtsas-USDT, justerbart via
   `PAPER_STARTING_BALANCE`) — riktiga priser, inga riktiga pengar

**Viktigt att förstå:** detta är enkla, kända tekniska regler som röstar
mot varandra — inte en AI som lär sig eller hittar på egna strategier.
Det är precis hur riktiga kvant-strategier ofta börjar: transparent och
justerbart. Du ser exakt varför varje beslut togs i `/api/paper/signals`.

### Nya endpoints

| Endpoint | Metod | Vad den gör |
|---|---|---|
| `/api/paper/portfolio` | GET | Saldo, öppna positioner, orealiserad vinst/förlust |
| `/api/paper/trades?limit=50` | GET | Historik över alla simulerade köp/sälj |
| `/api/paper/signals?symbol=BTC/USDT&limit=20` | GET | Varje beslut (inkl. "avvakta") med motivering |
| `/api/paper/reset` | POST | Nollställ papperskontot till startbeloppet |

### Justera strategin

Öppna `strategy.py` — `BUY_THRESHOLD`, `SELL_THRESHOLD`, `RSI_OVERSOLD`,
`RSI_OVERBOUGHT` styr när den handlar. Vill du lägga till fler
indikatorer (Bollinger, VWAP, ATR — resten av Fas 4), lägg till dem i
`technical.py` och ge dem en röst i `strategy.py`.

### Kom ihåg innan du går till riktiga pengar

- Låt strategin gå ett bra tag (dagar/veckor) och titta på `total_pnl` i
  `/api/paper/portfolio` innan du ens funderar på riktig exekvering
- Fas 3 (Risk Manager — stop loss, positionsgränser, daglig förlustgräns)
  är fortfarande inte byggd — bygg den innan riktiga pengar är inblandade
- Paper trading har ingen slippage/avgifter inräknat, så resultatet i
  verkligheten blir alltid något sämre

## Momentum-scanner — fånga tokens som börjar röra sig (nytt)

Detta är en andra, helt separat strategi som körs parallellt med den lugna
EMA/RSI/MACD-strategin. Den letar bland **alla** USDT-par på KuCoin, inte
bara dina tre valda symboler.

### Så här tänker den

**Entry** (`scanner.py`): letar inte efter "hög volym" — då hittar man bara
BTC varje gång. Den letar efter **ovanligt hög volym för just den token**:
senaste 5m-candlens volym jämförd med tokenens egen normala volym. En token
som plötsligt gör 4x sin vanliga volym är intressant; BTC som gör sin vanliga
miljardvolym är det inte.

Filter som körs innan något ens övervägs:
- 24h-volym mellan 200k och 50M USDT (för litet = du kommer inte ur;
  för stort = rör sig inte nog)
- Spread under 1% (bred spread äter upp vinsten på snabba affärer)
- Redan upp mer än 15% på 15 min → **straffas**, då är tåget borta

**Exit** (`momentum_strategy.py`): fyra regler, den som slår först vinner:

| Regel | Standard | Varför |
|---|---|---|
| Take profit | +4% | Ta hem vinsten innan den försvinner |
| Stop loss | −2% | Mindre än vinstmålet = positiv risk/reward |
| Trailing stop | −2% från topp | Låser in vinst när rörelsen vänder |
| Time exit | 45 min | Dog rörelsen ut är pengarna bättre använda någon annanstans |

Exit-reglerna kollas **var 30:e sekund** — hela poängen med strategin är att
komma ut snabbt.

### Riskspridning

Max 3 samtidiga positioner, 5% av saldot per position. Det betyder att även
om allt går fel samtidigt riskerar du 15% av kontot, inte allt.

### Nya endpoints

| Endpoint | Metod | Vad den gör |
|---|---|---|
| `/api/scanner/hits?limit=50` | GET | Allt scannern hittat, inkl. det den valde bort |
| `/api/momentum/config` | GET | Alla nuvarande trösklar och inställningar |

### Justera strategin

Trösklarna sitter överst i `scanner.py` och `momentum_strategy.py`, med
kommentarer om vad varje siffra gör. Antal positioner och positionsstorlek
sätts som miljövariabler i Railway (`MOMENTUM_MAX_POSITIONS`,
`MOMENTUM_POSITION_SIZE_PCT`).

## Social hype (Fas 6) — förberedd, inte inkopplad

`social.py` finns med rätt struktur men returnerar ett neutralt värde tills
du kopplar in en riktig datakälla. Det är medvetet: hellre ingen signal än
en påhittad som lurar dig att tro att den fungerar.

Anledningen är kostnad och praktik, inte teknik:
- **X/Twitter**: ca $200/mån för meningsfull API-åtkomst
- **Reddit**: gratis API, kräver bara registrering — billigaste startpunkten,
  instruktioner finns i kommentarerna i `social.py`
- **Telegram/Discord**: kräver medlemskap i grupperna och rör sig i gråzon
  mot deras användarvillkor

Värt att veta innan du investerar i det: när en token syns tydligt på X har
rörelsen ofta redan startat, och mycket "hype" i småtokens är koordinerade
grupper eller botar. Volymspiken i `scanner.py` är i praktiken en snabbare
och ärligare tidig signal. Se hype som bekräftelse, inte huvudsignal.

## Innan riktiga pengar — läs detta

Momentum-strategin på småtokens är den mest riskfyllda delen av hela
projektet. Saker paper trading INTE fångar:

- **Slippage**: på illikvida par kan du få betydligt sämre pris än du ser.
  Detta är oftast det som förvandlar en "lönsam" paper-strategi till en
  förlust i verkligheten.
- **Avgifter**: KuCoin tar ca 0,1% per affär. Vid många snabba affärer
  äter det snabbt upp en tunn marginal.
- **Pump-and-dump**: plötslig volym i en liten token är exakt vad
  koordinerade grupper skapar. Du kan vara den som köper toppen.
- **Fas 3 (Risk Manager) är fortfarande inte byggd** — daglig förlustgräns
  och total exponeringskontroll saknas.

Låt den gå på papper i veckor, inte dagar, och titta på `/api/paper/trades`
för att se hur ofta exits faktiskt triggar med vinst kontra förlust.

## Risk Manager (Fas 3) — skyddsnätet

Detta är den viktigaste modulen om du någonsin tänker köra riktiga pengar.
Principen: **strategierna bestämmer VAD som ska köpas, Risk Manager
bestämmer OM det får köpas.** Ingen strategi kan gå förbi den.

Anledningen till att det är byggt som en grindvakt istället för regler
inbakade i varje strategi: när du lägger till fler strategier senare vill
du inte behöva komma ihåg att kopiera in riskreglerna i var och en.

### Fem lager av skydd

| Lager | Standard | Vad det gör |
|---|---|---|
| Positionsstorlek | 5% per affär | Ingen enskild affär kan göra stor skada |
| Exponeringstak | 40% av kontot | Resten ligger säkert i USDT |
| Max positioner | 5 samtidigt | Riskspridning |
| Daglig förlustgräns | −5% | Stänger av handeln resten av dagen |
| Cooldown | 60 min | Hindrar loop: köp → stoppas ut → köp igen |

Om exponeringstaket nästan är nått **skalas köpet ner** istället för att
blockeras — hellre en mindre position än ingen alls.

### Dynamisk stop loss

En token som normalt svänger 6% i timmen stoppas ut av en 2%-stop av rent
brus, långt innan något faktiskt gått fel. Risk Manager mäter därför
tokenens volatilitet (ATR) och sätter stoppen till ca 1,5x dess normala
rörelse:

| Tokenens ATR | Stop loss blir |
|---|---|
| 0,5% (lugn) | −2,0% (basvärdet) |
| 3,0% | −4,5% |
| 8,0% (vild) | −6,0% (taket) |

### Partial take profit

Vid +2% säljs **halva** positionen automatiskt. Resten ligger kvar och
jagar +4%. Anledningen: de flesta av dessa rörelser når +2% men bara en
del når +4%. Genom att säkra halva blir affären svår att förlora på.

### Rug-pull-detektor (nödutgång)

Kollas var 30:e sekund på alla öppna positioner. Går före alla vanliga
exit-regler:

1. **Prisras** — mer än 8% ner på 3 minuter
2. **Spread-explosion** — spread över 3% (likviditeten försvinner, du
   riskerar att inte komma ut alls)
3. **Volymkollaps** — volymen under 15% av toppen och positionen i minus
   (köparna är borta)

### Kill switch

Den stora röda knappen. Kan aktiveras manuellt från appen, och aktiveras
**automatiskt** när den dagliga förlustgränsen nås. Läget sparas i
databasen så det överlever omstarter — en bot som förlorat för mycket
ska inte börja handla igen bara för att Railway startade om den.

### Nya endpoints

| Endpoint | Metod | Vad den gör |
|---|---|---|
| `/api/risk/status` | GET | Hela riskläget + hur mycket utrymme som finns kvar |
| `/api/risk/daily` | GET | Dagens resultat, antal affärer, vinstandel |
| `/api/risk/kill-switch/activate` | POST | Nödstopp — ingen ny handel |
| `/api/risk/kill-switch/deactivate` | POST | Släpp på handeln igen |
| `/api/risk/close-all` | POST | Stäng ALLA positioner + aktivera kill switch |

### Justera gränserna

Alla gränser sätts som miljövariabler i Railway (se `.env.example`).
Du behöver **inte** ändra i koden — ändra variabeln, Railway startar om,
klart.

## Backtesting (Fas 5)

Kör strategierna mot historisk data för att se hur de HADE presterat —
svar på minuter istället för veckor av paper trading.

**Ingen databasmigration behövs för detta.** Backtesten läser historik
direkt från KuCoin och returnerar resultatet live.

### Två endpoints

| Endpoint | Vad den gör |
|---|---|
| `/api/backtest/run?symbol=BTC/USDT&strategy_name=momentum&timeframe=5m&limit=1000` | En symbol, full detalj + affärslista |
| `/api/backtest/compare?symbols=BTC/USDT,ETH/USDT,SOL/USDT` | Flera symboler, jämförelse + omdöme |

`strategy_name` kan vara `momentum` eller `technical`.

### Vad som gör den ärlig

**Samma strategikod som liveboten.** Backtesten anropar
`momentum_strategy.check_exit()` och `strategy.decide()` — samma
funktioner som körs live. Bygger man en separat "backtest-version"
testar man i praktiken något annat än det som faktiskt körs.

**Ingen lookahead bias.** Loopen ger strategin bara `candles[:i+1]` vid
varje steg — den kan aldrig se priset den ska handla på.

**Avgifter och slippage inräknade:** 0,1% avgift + 0,15% slippage, per
sida. Det låter lite, men på en strategi med tunna marginaler är det
ofta skillnaden mellan vinst och förlust. En strategi som ser lönsam ut
utan dessa avdrag är oftast en förlustaffär i verkligheten.

**Buy & hold som jämförelse.** Varje resultat visar `buy_and_hold_pct`
och `beat_buy_and_hold`. Detta är den jämförelse som oftast avslöjar
att en strategi inte är värd sin komplexitet — slår den inte "köp och
vänta", varför köra den?

### Så tolkar du siffrorna

| Nyckeltal | Vad det betyder |
|---|---|
| `profit_factor` | Bruttovinst ÷ bruttoförlust. Under 1,0 = förlustsystem |
| `max_drawdown_pct` | Största fallet från en topp. Ofta viktigare än avkastningen — en strategi som tjänar 40% men tappar 30% på vägen är svår att faktiskt orka köra |
| `win_rate_pct` | Andel vinnande affärer. **Låg win rate är inte automatiskt dåligt** om vinsterna är större än förlusterna — kolla `avg_win_pct` mot `avg_loss_pct` |
| `exit_reasons` | Vilka exit-regler som faktiskt triggade. Dominerar TIME EXIT betyder det att strategin sällan når sina mål |

### Fallgropar värda att känna till

**Under ~30 affärer säger resultatet ingenting.** `/api/backtest/compare`
säger uttryckligen ifrån när underlaget är för tunt. Fem lyckade affärer
är slump, inte en edge.

**Lönsam på en symbol men inte andra = troligen tur.** Därför finns
`compare`-endpointen. En riktig edge fungerar oftast på flera marknader.

**Överanpassning är den stora fällan.** Om du justerar trösklarna tills
backtesten ser bra ut har du inte hittat en strategi — du har
memorerat historien. Testa alltid ändringar på en tidsperiod du inte
justerade mot.

**Backtesten kan inte simulera:** att din order flyttar priset i en
illikvid token, att börsen ligger nere, eller att just den token du
köpte var en pump-and-dump där du är exit-likviditeten.

## Strategy Lab (Fas 5+) — testa många metoder systematiskt

Kör **alla strategier × alla parameterkombinationer × alla symboler** och
rangordnar dem. Just nu 68 kombinationer per symbol, från 9 strategifamiljer.

### Den viktigaste designidén: skydd mot överanpassning

Testar du 500 kombinationer mot samma data kommer några se lysande ut av
ren slump. Att sedan välja den bästa och köra den live är ett av de
vanligaste sätten att förlora pengar på algoritmisk handel.

Skyddet är inbyggt: datan delas i två delar.

| Del | Andel | Vad som händer |
|---|---|---|
| Träning | 65% | Alla kombinationer söks igenom här |
| Test | 35% | Vinnaren utvärderas här — data den aldrig sett |

Resultatlistan sorteras på **testresultatet**, aldrig träningsresultatet.
Fältet `overfit_gap` (träning minus test) avslöjar direkt när något såg
bra ut på gammal data men inte fungerar på ny.

### Strategier i biblioteket

| Strategi | Idé |
|---|---|
| `ema_cross` | Klassisk trendföljning |
| `rsi_mean_reversion` | Köp det som fallit "för mycket" |
| `macd_cross` | Momentum |
| `bollinger_breakout` | Utbrott ur normalt prisspann |
| `bollinger_reversion` | Motsatsen: köp vid nedre bandet |
| `donchian_breakout` | Ny högsta nivå på N barer (Turtle Traders-metoden) |
| `volume_spike_momentum` | Din nuvarande scanner-logik |
| `trend_filtered_momentum` | Momentum, men bara i medvind |
| `buy_and_hold` | **Referensen.** Slår en strategi inte denna är den inte värd sin risk |

### Endpoints

| Endpoint | Vad den gör |
|---|---|
| `POST /api/lab/start?symbols=BTC/USDT,ETH/USDT&timeframe=5m` | Startar körning i bakgrunden (1-5 min) |
| `GET /api/lab/status` | Status + hela resultatet när klart |
| `GET /api/lab/strategies` | Vilka strategier och hur många kombinationer |

Du får en Telegram-notis när körningen är klar.

### Hur du läser resultatet

Kolla i denna ordning:
1. **`verdict.summary`** — den ärliga sammanfattningen
2. **`verdict.warnings`** — varje varning är en anledning att tveka
3. **`beats_buy_and_hold`** — är false, sluta där
4. **`consistency_pct`** — under 60% betyder att det troligen är tur på enstaka symboler
5. **`test_trades`** — under 30 säger siffrorna ingenting

### Effektivitet

- Candles hämtas **en gång per symbol** och återanvänds för alla 68 kombinationer
- Indikatorer beräknas **en gång per kombination**, inte per bar (O(n) istället för O(n²), ca 100x snabbare)
- Körningen sker i bakgrunden så HTTP-anropet inte timear ut

## Social Engine (Fas 6) — Reddit hype

Nu på riktigt inkopplad, inte längre en platshållare.

### Uppsättning (gratis, ca 3 minuter)

1. Gå till **reddit.com/prefs/apps** → "create another app..."
2. Välj typen **script**. Redirect URI: `http://localhost:8080`
3. Kopiera **client_id** (står under appnamnet) och **client_secret**
4. Lägg i Railway Variables: `REDDIT_CLIENT_ID` och `REDDIT_CLIENT_SECRET`
5. Modulen aktiverar sig själv när nycklarna finns

### Designidén: förändring, inte antal

BTC nämns tusen gånger om dagen — det säger ingenting. En okänd token som
går från 2 till 60 omnämnanden på sex timmar är signalen. Modulen jämför
därför senaste 6 timmarna mot snittet för föregående vecka.

Resultatet begränsas till −0,5 till +1,0 så att den sociala signalen
aldrig kan dominera över volymsignalen.

### Ärlig begränsning

När en token syns tydligt på Reddit har rörelsen ofta redan börjat, och
mycket "hype" i småtokens är koordinerade grupper eller botar. Använd det
som **bekräftelse** på en volymsignal, inte som huvudsignal. Det är därför
vikten är medvetet begränsad.

Testa med: `GET /api/social/hype?symbol=PEPE/USDT`

## Bot-arena — en bot per strategi, tydlig statistik

Nio bottar handlar samtidigt på samma marknad, var och en med sin egen
plånbok, sina egna positioner och samma startkapital. Den som presterar
bäst vinner — och ingen kan skylla på att den fick en dålig period.

### Varför detta slår backtesting

Backtest visar hur en strategi HADE gått på data där facit redan är känt.
En bot som handlar framåt i tiden kan inte fuska: den ser samma marknad
som alla andra, i samma sekund, utan att veta vad som händer härnäst.
Det kallas forward testing och är den ärligaste utvärderingen som finns
utan riktiga pengar.

### Kom igång

1. Kör migrationen `migration_bots.sql` i Railway
2. `POST /api/bots/seed` — skapar en bot per strategi
3. Vänta. Kolla `GET /api/bots/leaderboard` efter några dagar

### Endpoints

| Endpoint | Vad den gör |
|---|---|
| `GET /api/bots/leaderboard` | Topplistan + ärlig bedömning |
| `GET /api/bots` | Alla bottar och deras inställningar |
| `GET /api/bots/{id}` | Full statistik för en bot |
| `GET /api/bots/{id}/trades` | Affärshistorik för en bot |
| `POST /api/bots/seed` | Skapa en bot per strategi |
| `POST /api/bots/create` | Skapa egen bot med valfria parametrar |
| `POST /api/bots/{id}/enable?enabled=false` | Pausa en bot |
| `POST /api/bots/{id}/reset` | Nollställ en bot |
| `POST /api/bots/reset-all` | Nollställ alla — starta ny mätperiod |

### Statistik per bot

| Fält | Vad det betyder |
|---|---|
| `return_pct` | Total avkastning, inkl. orealiserade positioner |
| `win_rate_pct` | Andel vinnande affärer |
| `profit_factor` | Bruttovinst ÷ bruttoförlust. Under 1,0 = förlustsystem |
| `max_drawdown_pct` | Största fallet från en topp — **titta på detta innan du blir förtjust i avkastningen** |
| `total_fees_paid` | Avgifter kostar. En bot som handlar ofta betalar mycket |
| `trades_per_day` | Hur aktiv boten är |
| `confidence` | **Hur mycket siffrorna är värda än** |

### `confidence` — läs detta först

Fältet finns för att en topplista efter två dagar frestar till slutsatser
som datan inte bär:

| Avslutade affärer | Nivå | Innebörd |
|---|---|---|
| under 10 | ingen | Brus, inte resultat |
| 10–29 | låg | Enskilda affärer dominerar fortfarande |
| 30–99 | medel | Rimligt underlag, men marknadsläget kan ha gynnat en stil |
| 100+ | hyfsad | Statistiskt användbart |

`buy_and_hold` finns med som referensbot. Slår ingen strategi den är det
ett fullt normalt utfall — det betyder bara att komplexiteten inte lönar
sig i detta marknadsläge.

### Effektivitet

Candles hämtas en gång per symbol och delas av alla bottar. Med 9 bottar
× 3 symboler blir det 3 databasfrågor per cykel istället för 27.

## Order flow — vad köpare och säljare faktiskt gör

Detta är en fundamentalt annan sorts signal än resten. EMA, RSI och MACD
räknar på **priset**, som är resultatet av handeln. Order flow tittar på
**orsaken**: vem som köper, hur mycket, och hur aggressivt.

All data kommer från tabeller du redan samlar in (`trades` och
`orderbook_snapshots`). Ingen ny datakälla, inga API-nycklar.

### Sex signaler

| Signal | Vad den mäter |
|---|---|
| **CVD** | Aggressiva köp minus aggressiva säljningar. En "buy" i trades-tabellen betyder att någon tog priset från säljsidan — de ville in NU. Otålighet är information. |
| **Orderboksobalans** | Mer volym på köp- eller säljsidan? Tung köpsida = stöd under priset. |
| **Valprintar** | Enstaka affärer 8x över medianen. Kommer inte från privatpersoner. |
| **Absorption** | Hög volym men priset rör sig knappt. Någon köper allt som säljs utan att jaga priset. Ofta den starkaste signalen som finns. |
| **Storleksfördelning** | Många små affärer = privatpersoner. Få stora = institutioner. |
| **Aggressionsratio** | Hur stor andel av volymen som var aggressiva köp. |

Allt vägs samman till en score mellan −1 och +1.

### Två nya bottar i arenan

| Bot | Vad den gör |
|---|---|
| `order_flow_pressure` | Köper när sammanvägd score passerar tröskeln, säljer när den vänder |
| `whale_follow` | Ignorerar allt utom stora affärer och absorption på köpsidan |

De tävlar direkt mot TA-bottarna med samma startkapital. Det gör arenan
till ett rent test av frågan: **slår order flow glidande medelvärden?**

### Endpoints

| Endpoint | Vad den gör |
|---|---|
| `GET /api/orderflow?symbol=BTC/USDT&window_minutes=15` | Full analys för en symbol |
| `GET /api/orderflow/all` | Alla bevakade symboler, rankade på score |

### Viktigt: order flow kan inte backtestas

Orderboksdjup och affärsriktning sparas bara framåt i tiden — historiken
finns inte att hämta i efterhand. Labbet (`/api/lab/start`) hoppar därför
över dessa två strategier och säger uttryckligen ifrån om det i fältet
`skipped_strategies`.

Det betyder att bot-arenan är enda ärliga sättet att utvärdera dem. Vilket
i sin tur betyder att du behöver vänta — inga genvägar via historisk data.

## Om copy trading-listor

Du frågade om att följa kända traders via KuCoins topplista. Det går inte
att bygga ärligt, av två skäl:

**Tekniskt:** KuCoins CopyTrading-API är till för att lead traders ska
kunna *lägga ordrar*. Det finns inget publikt endpoint för topplistan över
mest kopierade traders. Den finns bara på webbsidan, och att skrapa den
vore skört och troligen mot deras villkor.

**Statistiskt:** topplistor lider av survivorship bias. De som ligger högst
har oftast kört hög hävstång och haft tur under mätperioden. De som blåste
kontot syns inte på listan alls. Att välja utifrån en sådan lista är att
välja den som råkat ha mest tur nyligen.

Order flow är den ärliga versionen av samma idé: istället för att lita på
vem som *säger* att de är bra, mäter du vad kapitalet faktiskt gör just nu.

## Batch 2 — regim, korrelation, beslutsmotor, rapporter

### Marknadsregim (`/api/regime`)

Trendar marknaden eller går den sidledes? Detta förklarar **varför** vissa
bottar leder, inte bara vilka.

Huvudmåttet är Kaufmans Efficiency Ratio: nettorörelse delat med summan av
alla enskilda rörelser.

| ER | Betyder |
|---|---|
| nära 1,0 | Priset gick rakt från A till B — stark trend |
| nära 0,0 | Priset rörde sig mycket men kom ingenstans — sidledes |

Svaret innehåller `favors` och `avoid`: vilka strategifamiljer som brukar
passa respektive missgynnas av regimen. Trendföljare (EMA, Donchian,
breakouts) behöver hög ER. Mean reversion (RSI, Bollinger-reversion)
fungerar bäst vid låg.

### Korrelationsfilter (`/api/correlation`)

**Problemet det löser:** fem positioner känns som riskspridning. Men om alla
fem rör sig likadant har du i praktiken EN position med fem gånger
storleken. BTC, ETH och SOL korrelerar historiskt runt 0,8–0,9.

Risk Manager blockerar nu köp i tillgångar som korrelerar över 0,75 med
något du redan håller. Korrelationen mäts på procentuella förändringar,
inte på priserna själva — det senare ger falskt höga värden för allt som
trendar åt samma håll.

Stäng av med `CORRELATION_FILTER_ENABLED=false` om du vill.

### AI Decision Engine (`/api/decision?symbol=BTC/USDT`)

Fas 8 på riktigt: väger samman fyra källor till ett beslut.

| Källa | Vikt | Varför |
|---|---|---|
| Order flow | 0,40 | Mäter vad kapital faktiskt gör. Svårast att manipulera |
| Teknisk | 0,30 | Välkänd — vilket också betyder att kanten är bortarbetad |
| Regim | 0,20 | Avgör om övriga signaler ska tas på allvar |
| Social | 0,10 | Lägst medvetet: när hypen syns har rörelsen ofta börjat |

**Vikterna är inte optimerade, och det är avsiktligt.** Att söka fram de
vikter som gett bäst resultat historiskt vore överanpassning i renaste
form. De är satta efter hur pålitlig varje källa är i princip. Ändra dem
gärna — men inte baserat på gårdagens resultat.

Svaret visar `contributions`: exakt hur mycket varje källa bidrog. Ingen
svart låda.

**Två finesser värda att känna till:**

*MACD-dödband:* i jämna trender konvergerar MACD- och signallinjen och kan
flimra över/under varandra på tusendelar. Gap under 0,02% av priset räknas
därför som neutralt istället för som en full säljröst.

*Regimmedveten RSI:* "överköpt" betyder olika saker i olika marknader. I en
sidledes marknad är RSI 75 en säljsignal. I en stark trend kan något vara
överköpt i veckor medan priset fortsätter upp — där straffas det bara lätt,
istället för att motarbeta trendsignalen.

Den finns också som bot i arenan: `ensemble_ai`.

### Kapitalkurvor (`/api/bots/{id}/equity`)

Tidigare räknades drawdown bara på **stängda** affärer. Det missade det som
faktiskt gör ont: en position som ligger 20% back men inte är såld syntes
inte alls. Nu sparas hela portföljvärdet var 15:e minut, så kurvan visar
sanningen — inklusive orealiserade förluster.

Svaret innehåller både kurvan och `drawdown` med max, nuvarande, samt när
toppen och botten inträffade.

### Dygnsrapport

Skickas automatiskt till Telegram varje morgon (`DAILY_REPORT_HOUR_UTC`,
standard 07:00 UTC). Innehåller marknadsregim, dygnets affärer per bot,
ställningen mot buy & hold, och riskläget.

Rapporten visar medvetet också vad som gick **sämst** — en rapport som bara
visar vinster är marknadsföring, inte information.

- `GET /api/report/daily` — läs rapporten nu
- `POST /api/report/send` — skicka till Telegram direkt

### VWAP

Sista pusselbiten i Fas 4. Skiljer sig från vanliga glidande medelvärden
genom att volymvikta: en bar med stor volym påverkar mycket mer än en tunn.
Det gör den till en bättre bild av var handeln faktiskt skett, vilket är
varför institutionella traders använder den som riktmärke.

## Nya listningar

Nya tokens rör sig mer än etablerade: ingen historik att förankra priset i,
färre som redan äger, och listningen i sig drar uppmärksamhet.

**Samma egenskaper gör dem farligast.** Listningspumpar vänder ofta hårt
inom timmar, spreadarna är bredare, likviditeten tunnare — och det är här
pump-and-dump är vanligast. Du kan vara den som köper toppen av någon som
fick tokens gratis före listningen.

### Hur åldern mäts utan att KuCoin berättar den

KuCoins API säger inte när ett par listades. Men dagliga candles finns bara
från listningsdagen och framåt. Hämtar man 400 dagscandles och får tillbaka
18, är token ungefär 18 dagar gammal.

Fungerar retroaktivt — du behöver inte ha lyssnat sedan listningen.

### Två sätt att hitta nya

| Metod | Vad den ger |
|---|---|
| **Åldersmätning** | Hittar unga tokens direkt, även sådana som listades innan vi började |
| **Symbolregistret** | Dyker en symbol upp som inte finns i registret listades den just nu — snabbaste signalen, men bara framåt |

Vid genuint nya listningar får du en Telegram-notis.

### Bonuspoäng i scannern

| Ålder | Bonus |
|---|---|
| under 7 dagar | +1,0 |
| 7–30 dagar | +0,5 |
| över 30 dagar | 0 |

**Ungdom är ingen köpsignal i sig** — bara en förstärkning av en signal som
redan finns. En ny token utan volymspik är fortfarande ointressant. Det är
därför bonusen är begränsad istället för avgörande.

### Strängare krav för nya tokens

| Krav | Vanliga tokens | Nya tokens |
|---|---|---|
| Min 24h-volym | 200 000 USDT | **500 000 USDT** |
| Max spread | 1,0% | **0,8%** |

Skälet: slippage på en illikvid ny listning kan äta hela vinsten. En token
som ser lovande ut men har 2% spread kostar 4% bara att gå in och ut ur.

### Endpoints

| Endpoint | Vad den gör |
|---|---|
| `GET /api/listings/new?max_age_days=30` | Unga tokens, yngst först |
| `GET /api/listings/stats` | Registrets status |
| `POST /api/listings/sync` | Synka registret nu |
| `POST /api/listings/check-ages` | Åldersbestäm nästa 15 symboler |

### Efter uppsättning

Registret börjar tomt. Kör `POST /api/listings/check-ages` **några gånger**
i rad — varje anrop åldersbestämmer 15 symboler, och registret innehåller
hundratals. Motorn gör det också automatiskt varannan minut, men manuellt
går snabbare i början.

Följ förloppet med `GET /api/listings/stats` och fältet `age_pending`.

## Max positioner per bot

Med fler bevakade symboler behövs ett tak. Utan det kan en bot öppna en
position i varje symbol och betala avgift på alla — då blir den en
indexfond med extra steg, och jämförelsen mellan bottar meningslös.

Standard: 4 samtidiga positioner per bot (`MAX_POSITIONS_PER_BOT`).

## Säkerhetsnät i bot-arenan

Tre hål som täpptes innan simuleringen fick löpa på riktigt.

### 1. Kill switch gäller nu alla bottar

Tidigare stoppade nödstoppet bara momentum-scannern — bottarna handlade
vidare som om inget hänt. Nu slutar alla tolv köpa när kill switch är aktiv.
Befintliga positioner får ligga kvar och stängas av sina vanliga regler.

### 2. Hård stop loss för alla strategier

**Detta var det allvarligaste problemet.** Sex bottar har exit-läge
"signal" och säljer bara när strategin ger säljsignal. Kommer den aldrig
rider positionen ner hur långt som helst — en RSI-bot som köper vid 25 och
token faller 70% sitter kvar för alltid.

Utan detta hade arenan mätt fel sak: inte vilken strategi som är bäst, utan
vilken som hade turen att slippa en katastrof.

| Regel | Standard | Gäller |
|---|---|---|
| Hård stop loss | −15% | Alla utom `buy_and_hold` |
| Maxtid för position | 72 timmar | Alla utom `buy_and_hold` |

Nivåerna är medvetet vida. De ska fånga haverier, inte ersätta
strategiernas egna exits. `buy_and_hold` är undantagen eftersom den per
definition ska hålla oavsett vad som händer — det är hela poängen med en
referens.

### 3. Färskhetskoll på data

Slutar insamlingen fungera fryser sista candlen i databasen, och bottarna
skulle fortsätta handla på ett pris som inte finns längre. Nu hoppas
symboler med data äldre än 15 minuter över, med varning i loggen.

### Hälsokontroll

`GET /api/health` visar om motorn kör och hur färsk datan är per symbol.
Fältet `healthy` är false om något ligger efter.

Värt att kolla då och då — ett tyst dataavbrott är svårare att upptäcka än
en krasch, eftersom allt ser ut att fungera.

## Dynamisk bevakningslista — bottarna jagar småtokens

Tidigare handlade bot-arenan bara på de tio fasta symbolerna, medan
momentum-scannern skannade alla ~800 USDT-par. Det gjorde arenan orättvis:
`volume_spike_momentum` testades på BTC och ETH, som är byggda för att
vara stabila — precis de par där den är designad att inte fungera.

Nu bygger arenan en kandidatlista varje femte minut och lägger den ovanpå
de fasta symbolerna.

### Urvalet

| Filter | Gräns |
|---|---|
| 24h-volym | 200 000 – 50 000 000 USDT |
| Spread | max 1,0% |
| Antal per cykel | 15 (`MAX_DYNAMIC_SYMBOLS`) |

Rankas på dagsrörelse, med bonus för unga tokens.

**Urvalet är medvetet enklare än momentum-scannerns.** Skulle vi filtrera
hårt här mätte arenan scannerns urval istället för strategierna. Bottarna
ska få ett rimligt urval att välja bland, inte ett förhandsvalt.

### Fyra saker som löstes på vägen

**Positioner utanför listan.** En bot som köpt en småtoken måste kunna
sälja den även när token åkt ur kandidatlistan. Utan detta hade positioner
fastnat för alltid och säkerhetsnätet aldrig kunnat lösa ut. Priser hämtas
därför separat för alla öppna positioner, och exit-reglerna körs på dem
oavsett bevakningsstatus.

**Referensboten stannar på de stora paren.** `buy_and_hold` handlar bara
fasta symboler. Annars hade den mätt "vad hade hänt om jag köpt
slumpmässiga småtokens" istället för "vad hade hänt om jag bara väntat" —
och referensen hade blivit meningslös.

**API-belastning.** Kandidatlistan byggs var 5:e minut och delas av alla
bottar, inte en gång per bot. Ett anrop för alla tickers plus ett per
kandidat.

**Order flow fungerar bara på fasta symboler.** `orderflow` läser från
databasen, och vi samlar inte in trades/orderbok för hundratals par. De två
order flow-bottarna returnerar "hold" på dynamiska symboler istället för
att gissa.

### Risknivå

Småtokens är farligare, och säkerhetsnätet gäller fullt ut: hård stop loss
på −15%, maxtid 72 timmar, max 4 positioner per bot, korrelationsfilter.

Stäng av med `DYNAMIC_WATCHLIST_ENABLED=false` om du vill tillbaka till
bara fasta symboler.

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
