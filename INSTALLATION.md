# Installation från grunden

Följ dessa steg i ordning. Tar cirka 10 minuter.

---

## 1. Ladda upp koden till GitHub

Gå till ditt repo → mappen `market_data_engine`.

**Add file → Upload files** → dra in **hela innehållet** från zip-filens
`market_data_engine`-mapp (alla `.py`, `.sql`, `.txt`, `Procfile`).

GitHub skriver över det som ändrats och lämnar resten. Att dra in allt är
säkrare än att välja ut enskilda filer — den vanligaste felkällan har varit
att en fil glömts bort, vilket får hela appen att krascha vid start med
`ModuleNotFoundError`.

Sedan: klicka in i mappen **`collectors`** och ladda upp dess fem filer
separat (`__init__.py`, `exchange.py`, `funding.py`, `ohlcv.py`,
`orderbook.py`).

**Commit changes** efter varje uppladdning.

---

## 2. Skapa databasen

Railway → **Postgres** → **Console**-fliken.

Kopiera hela innehållet i `schema.sql` och kör det.

Det skapar alla 17 tabeller. Säkert att köra flera gånger — allt är
`IF NOT EXISTS`, så inget skrivs över.

> De gamla `migration_*.sql`-filerna behövs inte längre. `schema.sql`
> innehåller allt.

---

## 3. Miljövariabler

Railway → **web**-tjänsten → **Variables**.

### Måste finnas

```
DB_HOST=${{Postgres.PGHOST}}
DB_PORT=${{Postgres.PGPORT}}
DB_NAME=${{Postgres.PGDATABASE}}
DB_USER=${{Postgres.PGUSER}}
DB_PASSWORD=${{Postgres.PGPASSWORD}}
API_KEY=ditt-hemliga-lösenord
AUTO_START=true
SYMBOLS=BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,AVAX/USDT,LINK/USDT,DOT/USDT
```

### För Telegram-notiser

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### Allt annat är valfritt

Alla övriga inställningar har fungerande standardvärden i koden. Se
`.env.example` för hela listan med förklaringar. Lägg bara till dem i
Railway om du vill ändra något.

---

## 4. Vänta på bygget

Railway → **web** → **Deployments**. Vänta tills senaste raden är grön.

Blir den röd: klicka på den, välj **View Logs**, och sök efter
`ModuleNotFoundError`. Det betyder att en fil saknas på GitHub.

---

## 5. Starta bot-arenan

Gå till `https://din-app.up.railway.app/docs` och kör med din API-nyckel:

| Ordning | Endpoint | Vad det gör |
|---|---|---|
| 1 | `GET /api/health` | Kontrollera att motorn kör och datan är färsk |
| 2 | `POST /api/bots/seed` | Skapa de tolv bottarna |
| 3 | `POST /api/bots/reset-all` | Nollställ (om de fanns sedan tidigare) |
| 4 | `GET /api/bots` | Bekräfta att tolv bottar finns |

Efter cirka 15 minuter:

| Endpoint | Vad du ser |
|---|---|
| `GET /api/bots/diagnostics` | Vad varje bot tycker just nu |
| `GET /api/bots/leaderboard` | Ställningen |

---

## 6. Fyll listningsregistret (valfritt)

Kör `POST /api/listings/check-ages` fem till tio gånger. Varje anrop
åldersbestämmer 15 symboler. Följ förloppet med `GET /api/listings/stats`
och fältet `age_pending`.

Motorn gör det också automatiskt varannan minut, men manuellt går
snabbare i början.

---

## Sedan: låt det vara

Dygnsrapporten kommer till Telegram varje morgon. Databasen städas varje
timme. Bottarna handlar var femte minut.

**Vad du tittar på om två veckor:** inte topplistan, utan
`GET /api/bots/analysis` och fältet `avg_gross_move_pct`. Ligger det kring
noll för alla bottar finns ingen kant — och då är svaret givet oavsett vad
avgifterna gör.

**Rör inte inställningarna under tiden.** Varje ändring nollställer i
praktiken mätperioden.
