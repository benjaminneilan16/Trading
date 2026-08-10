"""
Läser in inställningar från .env-filen till ett enkelt Settings-objekt.
Alla andra moduler importerar `settings` härifrån istället för att
läsa miljövariabler själva.
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()  # läser in .env-filen i miljövariablerna


def _split_symbols(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class Settings:
    # KuCoin
    kucoin_api_key: str = os.getenv("KUCOIN_API_KEY", "")
    kucoin_api_secret: str = os.getenv("KUCOIN_API_SECRET", "")
    kucoin_api_passphrase: str = os.getenv("KUCOIN_API_PASSPHRASE", "")

    # Databas
    db_host: str = os.getenv("DB_HOST", "localhost")
    db_port: int = int(os.getenv("DB_PORT", "5432"))
    db_name: str = os.getenv("DB_NAME", "crypto_trading")
    db_user: str = os.getenv("DB_USER", "postgres")
    db_password: str = os.getenv("DB_PASSWORD", "")

    # Symboler
    symbols: list[str] = field(
        default_factory=lambda: _split_symbols(os.getenv("SYMBOLS", "BTC/USDT"))
    )
    futures_symbols: list[str] = field(
        default_factory=lambda: _split_symbols(
            os.getenv("FUTURES_SYMBOLS", "BTC/USDT:USDT")
        )
    )

    # Intervaller (sekunder)
    ohlcv_interval: int = int(os.getenv("OHLCV_INTERVAL_SECONDS", "60"))
    orderbook_interval: int = int(os.getenv("ORDERBOOK_INTERVAL_SECONDS", "10"))
    trades_interval: int = int(os.getenv("TRADES_INTERVAL_SECONDS", "15"))
    funding_interval: int = int(os.getenv("FUNDING_INTERVAL_SECONDS", "300"))
    # Hur ofta strategin utvärderas och (ev.) handlar med låtsaspengar
    strategy_interval: int = int(os.getenv("STRATEGY_INTERVAL_SECONDS", "300"))
    paper_starting_balance: float = float(os.getenv("PAPER_STARTING_BALANCE", "10000"))

    # Momentum-scanner (jaga tokens som börjar röra sig)
    momentum_enabled: bool = os.getenv("MOMENTUM_ENABLED", "true").lower() in ("1", "true", "yes")
    # Hur ofta hela KuCoin skannas efter kandidater (sekunder)
    scan_interval: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "120"))
    # Max antal samtidiga momentum-positioner (riskspridning)
    momentum_max_positions: int = int(os.getenv("MOMENTUM_MAX_POSITIONS", "3"))
    # Hur stor andel av saldot som satsas per momentum-position
    momentum_position_size_pct: float = float(os.getenv("MOMENTUM_POSITION_SIZE_PCT", "0.05"))

    # --- Risk Manager (Fas 3) ---
    # Hur mycket kontot får förlora på en dag innan handeln stängs av (%)
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0"))
    # Max antal samtidiga öppna positioner totalt (alla strategier)
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
    # Hur stor andel av kontot som får vara i marknaden samtidigt (%)
    max_total_exposure_pct: float = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "40.0"))
    # Max andel av saldot i EN position (0.10 = 10%)
    max_position_size_pct: float = float(os.getenv("MAX_POSITION_SIZE_PCT", "0.10"))
    # Minsta affärsstorlek — under detta äter avgifterna upp vinsten
    min_trade_size_usdt: float = float(os.getenv("MIN_TRADE_SIZE_USDT", "20"))
    # Hur länge en token är spärrad efter en förlust (minuter)
    loss_cooldown_minutes: int = int(os.getenv("LOSS_COOLDOWN_MINUTES", "60"))
    # Bredaste tillåtna stop loss vid dynamisk beräkning (%)
    max_stop_loss_pct: float = float(os.getenv("MAX_STOP_LOSS_PCT", "6.0"))
    # Rug-pull-detektor
    rug_pull_drop_pct: float = float(os.getenv("RUG_PULL_DROP_PCT", "8.0"))
    rug_pull_spread_pct: float = float(os.getenv("RUG_PULL_SPREAD_PCT", "3.0"))
    # Hur ofta öppna positioner kollas för rug-pull-tecken (sekunder)
    rug_check_interval: int = int(os.getenv("RUG_CHECK_INTERVAL_SECONDS", "60"))

    # --- Social Engine (Fas 6) — Reddit ---
    # Skapa på reddit.com/prefs/apps -> "script"-typ. Gratis.
    reddit_client_id: str = os.getenv("REDDIT_CLIENT_ID", "")
    reddit_client_secret: str = os.getenv("REDDIT_CLIENT_SECRET", "")

    # --- Bot-arena: en bot per strategi, tävlar mot varandra ---
    bots_enabled: bool = os.getenv("BOTS_ENABLED", "true").lower() in ("1", "true", "yes")
    # Hur ofta varje bot utvärderar sina signaler (sekunder)
    # Höjd från 60s till 300s: att utvärdera timbaserade strategier varje
    # minut gav whipsaw och 2 694 affärer på två dygn.
    bots_interval: int = int(os.getenv("BOTS_INTERVAL_SECONDS", "300"))
    bots_starting_balance: float = float(os.getenv("BOTS_STARTING_BALANCE", "1000"))

    # Blockera köp i tillgångar som rör sig likadant som något vi redan äger
    correlation_filter_enabled: bool = os.getenv(
        "CORRELATION_FILTER_ENABLED", "true").lower() in ("1", "true", "yes")
    # Hur ofta kapitalkurvan sparas (sekunder) — ger riktiga equity-grafer
    equity_snapshot_interval: int = int(os.getenv("EQUITY_SNAPSHOT_SECONDS", "900"))
    # Daglig rapport via Telegram: timme i UTC (8 = 08:00 UTC)
    daily_report_hour_utc: int = int(os.getenv("DAILY_REPORT_HOUR_UTC", "7"))

    # --- Nya listningar ---
    track_new_listings: bool = os.getenv(
        "TRACK_NEW_LISTINGS", "true").lower() in ("1", "true", "yes")
    # Hur ofta symbolregistret synkas (sekunder). Ett API-anrop per körning.
    listing_sync_interval: int = int(os.getenv("LISTING_SYNC_SECONDS", "600"))

    # Hur ofta gammal data städas bort (sekunder). Standard: varje timme.
    cleanup_interval: int = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))

    # --- On-chain features via DexScreener (gratis, ingen nyckel) ---
    onchain_enabled: bool = os.getenv(
        "ONCHAIN_ENABLED", "true").lower() in ("1", "true", "yes")
    # Hur ofta features hämtas (sekunder). Varje symbol = ett API-anrop.
    onchain_interval: int = int(os.getenv("ONCHAIN_INTERVAL_SECONDS", "600"))

    # Telegram-notiser (se notifier.py för hur du skapar en bot)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # API-server (dashboard/app)
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("API_PORT", "8000"))
    # Enkel egen "lösenordsnyckel" så inte vem som helst på ditt nätverk
    # kan styra boten från din dashboard.
    api_key: str = os.getenv("API_KEY", "change_me")

    # Om True: startar datainsamlingen automatiskt när servern startar
    # (praktiskt vid molndrift, där du inte kan trycka "start" förrän
    # frontenden är byggd).
    auto_start: bool = os.getenv("AUTO_START", "true").lower() in ("1", "true", "yes")


settings = Settings()
