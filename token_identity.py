"""
Token Identity & Transferability Layer.

TRE SEPARATA IDENTITETER, som du beskrev:

  1. ASSET IDENTITY      — vilken ekonomisk token är detta?
  2. NETWORK IDENTITY    — på vilken kedja och vilket kontrakt finns den?
  3. MARKET IDENTITY     — vilket KuCoin-instrument kan jag faktiskt handla?

VARFÖR SEPARATIONEN BEHÖVS: "PEPE" är inte en identitet. Det finns dussintals
tokens med den tickern på olika kedjor, med helt olika kontraktsadresser.
Första försöket att matcha på ticker gav BTC/USDT mot ett Solana-token med
adressen BTCEDZwA... och 8 miljarder i påstådd likviditet.

Kontraktsadressen är den enda riktiga identiteten.

TRANSFERABILITY ÄR VIKTIGARE ÄN IDENTITET:

En perfekt matchning på ett nätverk KuCoin inte tar emot är värdelös. Därför
sparar vi per kedja om insättning och uttag är påslaget, vad uttaget kostar,
och hur många bekräftelser som krävs.

Att uttag är pausat är dessutom en varningssignal i sig — det är ofta det
första tecknet på att något är fel med en token, långt innan priset visar det.

Allt kommer från KuCoins egen currencies-endpoint. De vet vilket kontrakt de
listar och under vilka villkor det kan flyttas — det är per definition rätt
svar, eftersom det är den token du handlar.
"""
import logging
from datetime import datetime, timezone

import requests

from db import get_cursor

logger = logging.getLogger("identity")

KUCOIN_CURRENCIES_URL = "https://api.kucoin.com/api/v3/currencies"
TIMEOUT = 15


def ensure_tables():
    with get_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS token_networks (
                id                  BIGSERIAL PRIMARY KEY,
                currency            TEXT        NOT NULL,
                full_name           TEXT,
                chain_name          TEXT        NOT NULL,
                chain_id            TEXT,
                contract_address    TEXT,
                deposit_enabled     BOOLEAN,
                withdraw_enabled    BOOLEAN,
                withdrawal_min_fee  NUMERIC,
                withdrawal_min_size NUMERIC,
                confirms            INT,
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (currency, chain_name)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_networks_currency "
            "ON token_networks (currency)"
        )


def sync_kucoin_currencies() -> dict:
    """
    Hämtar KuCoins fullständiga valutaförteckning med nätverksinfo.

    Ett anrop ger allt: kontraktsadresser, insättnings- och uttagsstatus,
    avgifter och bekräftelsekrav för varje kedja varje token finns på.
    """
    ensure_tables()

    try:
        r = requests.get(KUCOIN_CURRENCIES_URL, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error("Kunde inte hämta KuCoin-valutor: %s", e)
        return {"error": str(e)}

    currencies = data.get("data") or []
    rows = 0

    def num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    with get_cursor() as cur:
        for c in currencies:
            code = (c.get("currency") or "").upper()
            full_name = c.get("fullName") or c.get("name")
            for ch in c.get("chains") or []:
                cur.execute(
                    """
                    INSERT INTO token_networks
                        (currency, full_name, chain_name, chain_id, contract_address,
                         deposit_enabled, withdraw_enabled, withdrawal_min_fee,
                         withdrawal_min_size, confirms)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (currency, chain_name) DO UPDATE SET
                        contract_address = EXCLUDED.contract_address,
                        deposit_enabled  = EXCLUDED.deposit_enabled,
                        withdraw_enabled = EXCLUDED.withdraw_enabled,
                        withdrawal_min_fee = EXCLUDED.withdrawal_min_fee,
                        withdrawal_min_size = EXCLUDED.withdrawal_min_size,
                        confirms = EXCLUDED.confirms,
                        updated_at = now()
                    """,
                    (
                        code, full_name,
                        ch.get("chainName") or ch.get("chainId") or "unknown",
                        ch.get("chainId"),
                        (ch.get("contractAddress") or "").strip() or None,
                        ch.get("isDepositEnabled"),
                        ch.get("isWithdrawEnabled"),
                        num(ch.get("withdrawalMinFee")),
                        num(ch.get("withdrawalMinSize")),
                        ch.get("confirms"),
                    ),
                )
                rows += 1

    logger.info("Synkade %d valutor / %d nätverk från KuCoin", len(currencies), rows)
    return {"currencies": len(currencies), "networks": rows}


def get_networks(currency: str) -> list[dict]:
    """Alla kedjor en token finns på hos KuCoin, med transferstatus."""
    ensure_tables()
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT chain_name, chain_id, contract_address, deposit_enabled, "
            "withdraw_enabled, withdrawal_min_fee, withdrawal_min_size, confirms "
            "FROM token_networks WHERE currency = %s ORDER BY chain_name",
            (currency.upper(),),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def check_transferability(symbol: str) -> dict:
    """
    Din beslutskedja, implementerad:

        TOKEN MATCH -> NETWORK COMPATIBLE? -> DEPOSIT ENABLED?
        -> WITHDRAW ENABLED? -> KANDIDAT

    Returnerar vilka kedjor som klarar hela vägen, och varför de andra
    föll bort. Att veta VARFÖR något inte går är lika användbart som att
    veta att det går.
    """
    currency = symbol.split("/")[0].split(":")[0].upper()
    networks = get_networks(currency)

    if not networks:
        return {
            "currency": currency,
            "transferable": False,
            "reason": "Okänd valuta hos KuCoin — kör sync först",
            "chains": [],
        }

    usable, blocked = [], []
    for n in networks:
        if not n["contract_address"]:
            blocked.append({**n, "blocked_by": "ingen kontraktsadress (nativ kedja)"})
        elif not n["deposit_enabled"]:
            blocked.append({**n, "blocked_by": "insättning avstängd"})
        elif not n["withdraw_enabled"]:
            blocked.append({**n, "blocked_by": "uttag avstängt"})
        else:
            usable.append(n)

    # Uttag avstängt är en varningssignal i sig, inte bara ett hinder
    withdrawals_off = [n for n in networks if n["withdraw_enabled"] is False]

    return {
        "currency": currency,
        "transferable": bool(usable),
        "usable_chains": usable,
        "blocked_chains": blocked,
        "warning": (
            f"Uttag är avstängt på {len(withdrawals_off)} av {len(networks)} kedjor. "
            "Det är ofta första tecknet på problem med en token — långt innan "
            "priset visar något."
        ) if withdrawals_off else None,
    }


# ---------------------------------------------------------------------------
# Confidence score
# ---------------------------------------------------------------------------

def match_confidence(symbol: str, dex_chain: str, dex_token_address: str) -> dict:
    """
    Hur säkra är vi på att DEX-tokenen är samma som KuCoin listar?

    Poängsättningen är medvetet konservativ, och den viktar kontraktsadressen
    tungt av ett skäl: allt annat går att förfalska. Vem som helst kan skapa
    ett token med tickern PEPE, kalla det "Pepe" och lägga likviditet i det.
    Ingen kan skapa ett token på KuCoins registrerade kontraktsadress.

    Notera att vi INTE använder CoinGecko som extra källa. Det skulle höja
    poängen från hög till mycket hög på ett problem som redan är löst av
    adressmatchningen — och lägga till ett beroende som kan gå sönder.
    """
    currency = symbol.split("/")[0].split(":")[0].upper()
    networks = get_networks(currency)

    score = 0
    checks = []

    exact = [n for n in networks
             if n["contract_address"]
             and n["contract_address"].lower() == (dex_token_address or "").lower()]

    if exact:
        score += 60
        checks.append(("Kontraktsadressen finns hos KuCoin", 60))
        n = exact[0]
        chain_norm = (n["chain_name"] or "").lower()
        if dex_chain and (dex_chain.lower() in chain_norm or chain_norm in dex_chain.lower()):
            score += 20
            checks.append(("Kedjan stämmer", 20))
        else:
            checks.append((f"Kedja skiljer sig ({dex_chain} mot {n['chain_name']})", 0))
        if n["deposit_enabled"] and n["withdraw_enabled"]:
            score += 20
            checks.append(("Insättning och uttag påslaget", 20))
        else:
            checks.append(("Insättning eller uttag avstängt", 0))
    else:
        checks.append(("Kontraktsadressen finns INTE hos KuCoin", 0))

    if score >= 80:
        level, note = "hög", "Samma token, verifierat via kontraktsadress."
    elif score >= 60:
        level, note = "medel", "Rätt kontrakt men något villkor saknas — granska."
    else:
        level, note = "ingen", ("Kontraktsadressen matchar inte KuCoins register. "
                                "Behandla som en annan token.")

    return {"score": score, "level": level, "note": note, "checks": checks}


def coverage() -> dict:
    ensure_tables()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT COUNT(DISTINCT currency) FROM token_networks")
        currencies = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM token_networks WHERE contract_address IS NOT NULL")
        with_contract = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM token_networks WHERE deposit_enabled AND withdraw_enabled "
            "AND contract_address IS NOT NULL"
        )
        fully_transferable = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM token_networks WHERE withdraw_enabled = FALSE")
        withdrawals_off = cur.fetchone()[0]
    return {
        "currencies_known": currencies,
        "networks_with_contract": with_contract,
        "fully_transferable_networks": fully_transferable,
        "networks_with_withdrawals_off": withdrawals_off,
    }
