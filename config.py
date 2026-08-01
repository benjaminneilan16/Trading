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
