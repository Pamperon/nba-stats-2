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
def get_odds(fixture_id):
    return get_json("odds", fixtureId=fixture_id, bookmakers="bet365.it", verbosity=3)


def parse_odds(odds_data, catalog, market_label):
    """Legge linee Over/Under e soglie '20+' senza inventare Under o quote."""
    bookmaker = (odds_data.get("bookmakerOdds") or {}).get("bet365.it") or {}
    if not bookmaker.get("bookmakerIsActive", False) or bookmaker.get("suspended", False):
        return []
    types = MARKETS[market_label]
    results = {}
    for market_id, market_data in (bookmaker.get("markets") or {}).items():
        definition = catalog.get(str(market_id))
        if not definition or definition.get("marketType") not in types:
            continue
        if not market_data.get("marketActive", False):
            continue
        market_type = definition["marketType"]
        outcome_names = {str(o["outcomeId"]): o.get("outcomeName", "")
                         for o in definition.get("outcomes", [])}
        for outcome_id, outcome in (market_data.get("outcomes") or {}).items():
            name = outcome_names.get(str(outcome_id), "")
            if market_type.startswith("playertotals-"):
                if name.lower() not in ("over", "under"):
                    continue
                line = float(definition.get("handicap", 0))
                side = name.title()
                label = f"{line:g}"
            else:
                # Mercati 'players-points': es. 20+ significa almeno 20, equivalente a Over 19,5.
                match = re.fullmatch(r"\s*(\d+)\+\s*", name)
                if not match:
                    continue
                line = int(match.group(1)) - 0.5
                side = "Over"
                label = f"{line:g}"
            for player_id, player in (outcome.get("players") or {}).items():
                if not player.get("active") or not player.get("playerName"):
                    continue
                price = player.get("price")
                if not isinstance(price, (int, float)) or price <= 1:
                    continue
                key = (str(player_id), line)
                row = results.setdefault(key, {
                    "Giocatore": player["playerName"], "Linea": line,
                    "Over": None, "Under": None, "Principale": False,
                    "Tipo": "Over/Under" if market_type.startswith("playertotals-") else "Soglia +",
                })
                # In caso di duplicati, preferiamo il mercato Over/Under completo.
                if row[side] is None or market_type.startswith("playertotals-"):
                    row[side] = price
                if player.get("mainLine"):
                    row["Principale"] = True
                if market_type.startswith("playertotals-"):
                    row["Tipo"] = "Over/Under"
    return sorted(results.values(), key=lambda r: (r["Giocatore"], r["Linea"]))


st.title("🏀 NBA STATS 2.0")
st.caption("Quote Bet365 Italia tramite OddsPapi · Linee principali e alternative")
st.info("Le quote cambiano nel tempo. Sono mostrate solo quelle restituite dall'API e contrassegnate come attive.")
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
        get_odds.clear()
        st.session_state.loaded_fixture = fixture_id

if st.session_state.loaded_fixture != fixture_id:
    st.info("Seleziona una partita e premi **Carica quote Bet365**. "
            "Cambiare partita non scarica automaticamente le quote.")
    st.stop()

market_label = st.selectbox("📊 Mercato giocatore", list(MARKETS))
try:
    catalog = get_markets()
    odds_data = get_odds(fixture_id)
    rows = parse_odds(odds_data, catalog, market_label)
except (requests.RequestException, ValueError, RuntimeError) as exc:
    st.error(f"Impossibile caricare le quote: {exc}")
    st.stop()

if not rows:
    st.warning("Nessuna quota attiva Bet365 Italia per questo mercato e questa partita.")
    st.stop()

players = sorted(set(r["Giocatore"] for r in rows))
player_name = st.selectbox("🏀 Giocatore", players)
player_rows = [r for r in rows if r["Giocatore"] == player_name]

st.subheader(f"{player_name} · {market_label}")
st.caption("'Soglia +' indica un mercato solo Over (es. 20+ = almeno 20 punti = Over 19,5). "
           "L'Under non viene inventato quando Bet365 non lo fornisce.")
frame = pd.DataFrame(player_rows).sort_values("Linea")
frame["Principale"] = frame["Principale"].map({True: "Sì", False: ""})
st.dataframe(frame[["Linea", "Over", "Under", "Principale", "Tipo"]],
             hide_index=True, use_container_width=True,
             column_config={
                 "Linea": st.column_config.NumberColumn("Linea", format="%.1f"),
                 "Over": st.column_config.NumberColumn("Over", format="%.3f"),
                 "Under": st.column_config.NumberColumn("Under", format="%.3f"),
             })
st.download_button("⬇️ Scarica CSV quote", frame.to_csv(index=False).encode("utf-8-sig"),
                   file_name="nba_stats_quote.csv", mime="text/csv")
st.caption("Quote: cache 24 ore · Calendario: 24 ore · Catalogo: 7 giorni. "
           "La cache può svuotarsi quando Streamlit riavvia l’app.")
st.divider()
st.caption("NBA STATS 2.0 · Prototipo quote — statistiche storiche da integrare")
