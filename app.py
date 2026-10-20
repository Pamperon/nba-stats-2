"""NBA STATS 2.0 — quote Bet365 Italia da OddsPapi.
La chiave deve essere configurata in Streamlit Secrets, mai in GitHub.
"""
from datetime import datetime, timedelta, timezone
import re

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="NBA STATS 2.0", page_icon="🏀", layout="wide")
BASE = "https://api.oddspapi.io/v4"
MARKETS = {
    "Punti (PTS)": ("playertotals-points", "players-points"),
    "Rimbalzi (REB)": ("playertotals-rebounds", "players-rebounds"),
    "Assist (AST)": ("playertotals-assists", "players-assists"),
    "Punti + Rimbalzi + Assist (PRA)": ("playertotals-ptsassreb", "players-ptsassreb"),
}

try:
    API_KEY = st.secrets["ODDSPAPI_API_KEY"]
except (KeyError, FileNotFoundError):
    API_KEY = None


def get_json(path, **params):
    if not API_KEY:
        raise RuntimeError("Chiave ODDSPAPI_API_KEY assente nei Secrets di Streamlit.")
    try:
        response = requests.get(
            f"{BASE}/{path}",
            params={**params, "apiKey": API_KEY, "language": "en"},
            timeout=35,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        # Non mostrare mai l'URL completo: contiene la chiave API.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise RuntimeError(f"Richiesta OddsPapi non riuscita (HTTP {status or 'errore di rete'}).") from None


@st.cache_data(ttl=7 * 24 * 3600, show_spinner="Caricamento catalogo mercati…")
def get_markets():
    data = get_json("markets")
    if not isinstance(data, list):
        raise ValueError("Formato catalogo mercati inatteso")
    return {str(m["marketId"]): m for m in data if m.get("sportId") == 11}


@st.cache_data(ttl=24 * 3600, show_spinner="Caricamento calendario NBA…")
def get_fixtures(date_str):
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    data = get_json(
        "fixtures", sportId=11,
        **{"from": day.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "to": (day + timedelta(days=1) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")},
    )
    if isinstance(data, dict):
        data = data.get("fixtures", data.get("data", []))
    if not isinstance(data, list):
        raise ValueError("Formato calendario inatteso")
    return sorted([f for f in data if str(f.get("tournamentName", "")).lower() == "nba"],
                  key=lambda f: f.get("startTime", ""))


@st.cache_data(ttl=24 * 3600, show_spinner="Caricamento quote Bet365 Italia…")
def get_odds(fixture_id, revision=0):
    return get_json("odds", fixtureId=fixture_id, bookmakers="bet365.it", verbosity=3)


def market_diagnostics(odds_data, catalog, market_label, player_name=None):
    """Solo dati già scaricati: non effettua chiamate HTTP."""
    bookmaker = (odds_data.get("bookmakerOdds") or {}).get("bet365.it") or {}
    requested = set(MARKETS[market_label])
    stats = {"Mercati nel feed Bet365": len(bookmaker.get("markets") or {}),
             "Mercati del tipo richiesto": 0, "Mercati alternativi (+)": 0,
             "Mercati alternativi attivi": 0, "Mercati alternativi inattivi": 0,
             "Mercati senza definizione": 0, "Soglie alternative del giocatore": 0}
    examples = []
    for mid, market in (bookmaker.get("markets") or {}).items():
        definition = catalog.get(str(mid))
        if definition is None:
            stats["Mercati senza definizione"] += 1
            continue
        kind = definition.get("marketType", "")
        if kind not in requested:
            continue
        stats["Mercati del tipo richiesto"] += 1
        if not kind.startswith("players-"):
            continue
        stats["Mercati alternativi (+)"] += 1
        active = market.get("marketActive") is True
        stats["Mercati alternativi attivi" if active else "Mercati alternativi inattivi"] += 1
        for outcome_id, outcome in (market.get("outcomes") or {}).items():
            definition_outcome = next((x for x in definition.get("outcomes", [])
                                       if str(x.get("outcomeId")) == str(outcome_id)), {})
            label = definition_outcome.get("outcomeName", "")
            for player in (outcome.get("players") or {}).values():
                if player_name and player.get("playerName") != player_name:
                    continue
                if player.get("active") and isinstance(player.get("price"), (int, float)):
                    stats["Soglie alternative del giocatore"] += 1
                    if len(examples) < 12:
                        examples.append({"Soglia": label, "Quota": player["price"],
                                         "Mercato attivo": active})
    return stats, examples


def parse_odds(odds_data, catalog, market_label):
    """Include soglie alternative anche con marketActive=False, segnalandole.

    Non trasforma mai una quota segnalata inattiva in una quota disponibile.
    """
    bookmaker = (odds_data.get("bookmakerOdds") or {}).get("bet365.it") or {}
    if not bookmaker.get("bookmakerIsActive", False) or bookmaker.get("suspended", False):
        return []
    types = MARKETS[market_label]
    results = {}
    for market_id, market_data in (bookmaker.get("markets") or {}).items():
        definition = catalog.get(str(market_id))
        if not definition or definition.get("marketType") not in types:
            continue
        market_type = definition["marketType"]
        is_alternative = market_type.startswith("players-")
        market_active = market_data.get("marketActive") is True
        # Manteniamo il filtro originario per gli Over/Under normali.
        # Le soglie alternative inattive restano consultabili ma segnalate.
        if not market_active and not is_alternative:
            continue
        outcome_names = {str(o["outcomeId"]): o.get("outcomeName", "")
                         for o in definition.get("outcomes", [])}
        for outcome_id, outcome in (market_data.get("outcomes") or {}).items():
            name = outcome_names.get(str(outcome_id), "")
            if market_type.startswith("playertotals-"):
                if name.lower() not in ("over", "under"):
                    continue
                try:
                    line = float(definition["handicap"])
                except (KeyError, TypeError, ValueError):
                    continue
                side = name.title()
            else:
                match = re.fullmatch(r"\s*(\d+)\+\s*", name)
                if not match:
                    continue
                line = int(match.group(1)) - 0.5
                side = "Over"
            for player_id, player in (outcome.get("players") or {}).items():
                if player.get("active") is not True or not player.get("playerName"):
                    continue
                price = player.get("price")
                if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 1:
                    continue
                key = (str(player_id), line)
                row = results.setdefault(key, {
                    "Giocatore": player["playerName"], "Linea": line,
                    "Over": None, "Under": None, "Principale": False,
                    "Tipo": "Over/Under" if not is_alternative else "Soglia +",
                    "Stato API": "Attivo" if market_active else "Mercato inattivo",
                    "_side_active": {},
                })
                # Per duplicati privilegiamo la quota di un mercato attivo.
                prior_active = row["_side_active"].get(side, False)
                if row[side] is None or (market_active and not prior_active):
                    row[side] = price
                    row["_side_active"][side] = market_active
                if player.get("mainLine"):
                    row["Principale"] = True
                if not is_alternative:
                    row["Tipo"] = "Over/Under"
                if any(row["_side_active"].values()):
                    row["Stato API"] = "Attivo"
    for row in results.values():
        del row["_side_active"]
    return sorted(results.values(), key=lambda r: (r["Giocatore"], r["Linea"]))

st.title("🏀 NBA STATS 2.0")
st.caption("Quote Bet365 Italia tramite OddsPapi · Linee principali e alternative")
st.info("Le quote alternative vengono mostrate anche se OddsPapi indica il mercato come inattivo: controlla la colonna Stato API prima di utilizzarle.")
if not API_KEY:
    st.error("Configura ODDSPAPI_API_KEY nei Secrets di Streamlit prima di proseguire.")
    st.stop()

st.caption("Risparmio API: calendario 24 ore · catalogo mercati 7 giorni · quote 24 ore. "
           "Cambiare giocatore o mercato non genera nuove richieste.")

selected_date = st.date_input("Data delle partite (UTC)", value=datetime.now(timezone.utc).date())
try:
    fixtures = get_fixtures(selected_date.isoformat())
except (requests.RequestException, ValueError, RuntimeError) as exc:
    st.error(f"Impossibile caricare le partite: {exc}")
    st.stop()

if not fixtures:
    st.warning("Nessuna partita NBA trovata per questa data. Prova un altro giorno.")
    st.stop()

options = {f"{f.get('participant1Name', '?')} – {f.get('participant2Name', '?')} | {f.get('startTime', '')[:16].replace('T', ' ')} UTC": f
           for f in fixtures}
choice = st.selectbox("🏟️ Partita NBA", list(options))
fixture = options[choice]
fixture_id = str(fixture["fixtureId"])
st.caption(f"Partite NBA trovate: {len(fixtures)}")

# Nessuna richiesta alle quote finché l'utente non lo decide.
# La selezione viene mantenuta per la sessione corrente e non passa a un'altra partita.
if "loaded_fixture" not in st.session_state:
    st.session_state.loaded_fixture = None

col_load, col_refresh = st.columns([2, 1])
with col_load:
    if st.button("📥 Carica quote Bet365", type="primary"):
        st.session_state.loaded_fixture = fixture_id
with col_refresh:
    if st.button("🔄 Aggiorna quote della partita"):
        revisions = st.session_state.setdefault("quote_revisions", {})
        revisions[fixture_id] = revisions.get(fixture_id, 0) + 1
        st.session_state.loaded_fixture = fixture_id

if st.session_state.loaded_fixture != fixture_id:
    st.info("Seleziona una partita e premi **Carica quote Bet365**. "
            "Cambiare partita non scarica automaticamente le quote.")
    st.stop()

market_label = st.selectbox("📊 Mercato giocatore", list(MARKETS))
try:
    catalog = get_markets()
    odds_data = get_odds(fixture_id, st.session_state.get("quote_revisions", {}).get(fixture_id, 0))
    rows = parse_odds(odds_data, catalog, market_label)
except (requests.RequestException, ValueError, RuntimeError) as exc:
    st.error(f"Impossibile caricare le quote: {exc}")
    st.stop()

if not rows:
    st.warning("Nessuna quota Bet365 Italia disponibile nel feed per questo mercato e questa partita.")
    st.stop()

players = sorted(set(r["Giocatore"] for r in rows))
player_name = st.selectbox("🏀 Giocatore", players)
player_rows = [r for r in rows if r["Giocatore"] == player_name]

st.subheader(f"{player_name} · {market_label}")
st.caption("'Soglia +' indica un mercato solo Over (es. 20+ = Over 19,5). "
           "'Mercato inattivo' indica che OddsPapi non conferma la disponibilità della quota; "
           "verificala su Bet365. L'Under non viene inventato.")
frame = pd.DataFrame(player_rows).sort_values("Linea")
frame["Principale"] = frame["Principale"].map({True: "Sì", False: ""})
st.dataframe(frame[["Linea", "Over", "Under", "Principale", "Tipo", "Stato API"]],
             hide_index=True, use_container_width=True,
             column_config={
                 "Linea": st.column_config.NumberColumn("Linea", format="%.1f"),
                 "Over": st.column_config.NumberColumn("Over", format="%.3f"),
                 "Under": st.column_config.NumberColumn("Under", format="%.3f"),
             })
st.download_button("⬇️ Scarica CSV quote", frame.to_csv(index=False).encode("utf-8-sig"),
                   file_name="nba_stats_quote.csv", mime="text/csv")
with st.expander("🔎 Diagnostica mercati (nessuna richiesta API aggiuntiva)"):
    stats, examples = market_diagnostics(odds_data, catalog, market_label, player_name)
    st.json(stats)
    if examples:
        st.dataframe(pd.DataFrame(examples), hide_index=True, use_container_width=True)
    st.caption("Questi dati sono calcolati esclusivamente dalla risposta OddsPapi già caricata. "
               "Non vengono effettuate nuove chiamate API.")
st.caption("Quote: cache 24 ore · Calendario: 24 ore · Catalogo: 7 giorni. "
           "La cache può svuotarsi quando Streamlit riavvia l’app.")
st.divider()
st.caption("NBA STATS 2.0 · Prototipo quote — statistiche storiche da integrare")
