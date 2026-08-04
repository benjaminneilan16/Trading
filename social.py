"""
Social Engine (Fas 6) — Hype Score via Reddit.

VARFÖR REDDIT: det är den enda stora källan med ett gratis, lagligt API.
X/Twitter kostar ca $200/mån för meningsfull åtkomst. Telegram och Discord
kräver att man skrapar servrar man inte äger, vilket bryter mot deras
villkor.

DEN VIKTIGASTE DESIGNIDÉN: vi mäter FÖRÄNDRING i omnämnanden, inte
antalet. BTC nämns tusen gånger om dagen — det säger ingenting. En okänd
token som går från 2 till 60 omnämnanden på en timme är signalen du letar
efter. Absoluta tal skulle bara ranka de största tokens högst, varje gång.

ÄRLIG BEGRÄNSNING: när en token syns tydligt på Reddit har rörelsen ofta
redan börjat. Mycket "hype" i småtokens är dessutom koordinerade grupper
eller botar. Använd detta som BEKRÄFTELSE på en volymsignal, inte som
huvudsignal. Vikten i strategin är därför medvetet begränsad.

UPPSÄTTNING (gratis, ca 3 minuter):
1. Gå till reddit.com/prefs/apps -> "create another app..."
2. Välj typen "script". Namn: valfritt. Redirect URI: http://localhost:8080
3. Du får ett client_id (under appnamnet) och ett client_secret
4. Lägg i Railway Variables:
       REDDIT_CLIENT_ID=...
       REDDIT_CLIENT_SECRET=...
5. Klart — modulen aktiverar sig själv när nycklarna finns
"""
import logging
import time
from datetime import datetime, timezone, timedelta

from config import settings

logger = logging.getLogger("social")

# Subreddits att söka i. De två första är där småtokens diskuteras mest,
# de sista ger en bredare bild.
SUBREDDITS = "CryptoCurrency+CryptoMoonShots+SatoshiStreetBets+altcoin"

# Hype-score begränsas till detta intervall, så en social signal aldrig
# kan dominera över den tekniska/volymbaserade signalen.
MAX_SCORE = 1.0
MIN_SCORE = -0.5

# Cache: Reddits API har rate limits, och vi vill inte fråga om samma
# token varje gång scannern kör.
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 900  # 15 min

_reddit_client = None


def _get_client():
    """Skapar Reddit-klienten en gång. Returnerar None om ej konfigurerad."""
    global _reddit_client
    if _reddit_client is not None:
        return _reddit_client

    if not settings.reddit_client_id or not settings.reddit_client_secret:
        return None

    try:
        import praw
        _reddit_client = praw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent="crypto-momentum-bot/1.0",
            check_for_async=False,
        )
        # read_only räcker — vi postar aldrig något
        _reddit_client.read_only = True
        logger.info("Reddit-klient initierad")
        return _reddit_client
    except ImportError:
        logger.warning("praw är inte installerat — lägg till det i requirements.txt")
        return None
    except Exception as e:
        logger.error("Kunde inte initiera Reddit-klient: %s", e)
        return None


def _base_symbol(symbol: str) -> str:
    """'PEPE/USDT' -> 'PEPE'"""
    return symbol.split("/")[0].split(":")[0].upper()


def hype_score(symbol: str) -> dict:
    """
    Returnerar hype-score för en token.

    {
      "score": float,      # MIN_SCORE .. MAX_SCORE
      "available": bool,   # False = ingen källa konfigurerad
      "sources": dict,     # rådata, för felsökning i appen
    }
    """
    client = _get_client()
    if client is None:
        return {
            "score": 0.0, "available": False, "sources": {},
            "note": "Reddit ej konfigurerat — sätt REDDIT_CLIENT_ID och "
                    "REDDIT_CLIENT_SECRET i Railway Variables.",
        }

    token = _base_symbol(symbol)

    cached = _cache.get(token)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        result = _measure_mention_change(client, token)
    except Exception as e:
        logger.error("Reddit-sökning misslyckades för %s: %s", token, e)
        return {"score": 0.0, "available": False, "sources": {}, "error": str(e)}

    _cache[token] = (time.time(), result)
    return result


def _measure_mention_change(client, token: str) -> dict:
    """
    Jämför omnämnanden senaste 6 timmarna mot snittet för föregående vecka.

    Söker på veckans träffar i en enda fråga och delar upp dem i tidsfack
    lokalt — betydligt snällare mot API:t än flera separata sökningar.
    """
    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(hours=6)
    week_cutoff = now - timedelta(days=7)

    posts = list(
        client.subreddit(SUBREDDITS).search(
            token, time_filter="week", limit=200, sort="new"
        )
    )

    recent_count = 0
    older_count = 0
    recent_score_sum = 0

    for p in posts:
        created = datetime.fromtimestamp(p.created_utc, tz=timezone.utc)
        if created < week_cutoff:
            continue
        if created >= recent_cutoff:
            recent_count += 1
            recent_score_sum += getattr(p, "score", 0)
        else:
            older_count += 1

    # Baslinje: genomsnittligt antal per 6-timmarsfönster under veckan.
    # Veckan har 28 sådana fönster, minus det senaste = 27.
    baseline_per_window = older_count / 27 if older_count else 0.0

    if baseline_per_window < 0.2:
        # För lite historik att jämföra mot. Många färska inlägg om en token
        # som knappt nämnts alls är dock i sig intressant.
        ratio = float(recent_count) if recent_count else 0.0
    else:
        ratio = recent_count / baseline_per_window

    # Omvandla ratio till score.
    #   ratio 1.0  = normalt      -> 0.0
    #   ratio 3.0  = 3x normalt   -> ~0.5
    #   ratio 6.0+ = kraftig hype -> 1.0 (tak)
    if ratio <= 1.0:
        score = -0.2 if recent_count == 0 else 0.0
    else:
        score = min((ratio - 1.0) / 5.0, MAX_SCORE)

    score = max(min(score, MAX_SCORE), MIN_SCORE)

    return {
        "score": round(score, 3),
        "available": True,
        "sources": {
            "reddit": {
                "recent_posts_6h": recent_count,
                "posts_prior_week": older_count,
                "baseline_per_6h": round(baseline_per_window, 2),
                "ratio": round(ratio, 2),
                "recent_upvotes": recent_score_sum,
            }
        },
    }


def clear_cache():
    _cache.clear()
