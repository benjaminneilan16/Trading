"""
Social Engine (Fas 6) — Hype Score.

STATUS: förberedd struktur, INGEN riktig datakälla inkopplad än.

Anledningen är praktisk, inte teknisk: varje källa kräver egna nycklar,
egna kostnader och egna regler:

  X/Twitter   — API kostar ca $200/mån för meningsfull åtkomst (Basic-nivå).
                Gratisnivån räcker inte för att söka nämnvärt.
  Reddit      — gratis API, men kräver registrerad app + OAuth. Fullt görbart.
                Troligen den billigaste startpunkten.
  Telegram    — kräver att du är med i grupperna + en user-bot. Gråzon mot
                Telegrams regler beroende på hur det används.
  Discord     — samma sak; att skrapa servrar du inte äger bryter mot ToS.
  YouTube     — Data API är gratis upp till en kvot, funkar för titlar/kommentarer.

Tills en källa är inkopplad returnerar hype_score() ett neutralt värde,
så att strategin fungerar exakt likadant som utan modulen — inget
låtsas-värde som lurar dig att tro att signalen finns.

VÄRT ATT VETA om hype som signal: när en token syns tydligt på X har
rörelsen ofta redan startat, och en stor del av "hypen" i småtokens är
koordinerade grupper eller botar. Volymspikar (scanner.py) är i praktiken
en snabbare och ärligare tidig signal. Se hype som en bekräftelse, inte
som huvudsignalen.
"""
import logging

logger = logging.getLogger("social")

NEUTRAL_SCORE = 0.0


def hype_score(symbol: str) -> dict:
    """
    Returnerar en hype-score för en token.

    Returformat (samma oavsett källa, så strategin inte behöver ändras
    när du kopplar in något):
        {
          "score": float,      # -1.0 (negativt) .. +1.0 (kraftig hype)
          "sources": dict,     # per källa, för felsökning
          "available": bool,   # False = ingen källa inkopplad
        }
    """
    return {
        "score": NEUTRAL_SCORE,
        "sources": {},
        "available": False,
        "note": "Ingen social datakälla inkopplad — se social.py för hur du lägger till en.",
    }


# ---------------------------------------------------------------------------
# Så här kopplar du in Reddit (billigaste startpunkten) när du vill:
#
# 1. Gå till reddit.com/prefs/apps -> "create app" -> välj "script"
# 2. Du får client_id och client_secret. Lägg i .env:
#       REDDIT_CLIENT_ID=...
#       REDDIT_CLIENT_SECRET=...
# 3. pip install praw  (lägg till i requirements.txt)
# 4. Ersätt hype_score() ovan med något i stil med:
#
#       import praw
#       reddit = praw.Reddit(client_id=..., client_secret=..., user_agent="cryptobot")
#       base = symbol.split("/")[0]          # "PEPE/USDT" -> "PEPE"
#       posts = list(reddit.subreddit("CryptoMoonShots+CryptoCurrency")
#                          .search(base, time_filter="day", limit=50))
#       # Räkna antal omnämnanden nu vs snitt senaste veckan ->
#       # det är förändringen som är signalen, inte absoluta antalet.
#
# Nyckeln: mät FÖRÄNDRING i omnämnanden, inte totalt antal. BTC nämns
# alltid tusen gånger; en okänd token som går från 2 till 60 omnämnanden
# på en timme är den signal du faktiskt letar efter.
# ---------------------------------------------------------------------------
