"""NBA STATS 2.0: OddsPapi Bet365 + official NBA player game logs."""
from datetime import datetime, timedelta, timezone
import re
import logging
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="NBA STATS 2.0", page_icon="🏀", layout="wide")
logger = logging.getLogger("nba_stats_diagnostics")
BASE = "https://api.oddspapi.io/v4"
MARKETS = {
    "Punti (PTS)": ("playertotals-points", "players-points"),
    "Rimbalzi (REB)": ("playertotals-rebounds", "players-rebounds"),
    "Assist (AST)": ("playertotals-assists", "players-assists"),
    "Punti + Rimbalzi + Assist (PRA)": ("playertotals-ptsassreb", "players-ptsassreb"),
}
STATS = {"Punti (PTS)": "PTS", "Rimbalzi (REB)": "REB", "Assist (AST)": "AST", "Punti + Rimbalzi + Assist (PRA)": "PRA"}
try:
    API_KEY = st.secrets["ODDSPAPI_API_KEY"]
except (KeyError, FileNotFoundError):
    API_KEY = None


def get_json(path, **params):
    if not API_KEY:
        raise RuntimeError("Chiave OddsPapi assente in Streamlit Secrets.")
    try:
        resp = requests.get(f"{BASE}/{path}", params={**params, "apiKey": API_KEY, "language": "en"}, timeout=40)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        # Never reveal exception's request URL, which contains the API key.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise RuntimeError(f"OddsPapi: errore HTTP {status or 'di connessione'}.") from None


@st.cache_data(ttl=7 * 86400, show_spinner="Catalogo mercati…")
def get_markets():
    data = get_json("markets")
    if not isinstance(data, list):
        raise ValueError("Formato catalogo mercati inatteso")
    return {str(m["marketId"]): m for m in data if str(m.get("sportId")) == "11"}


@st.cache_data(ttl=86400, show_spinner="Calendario NBA…")
def get_fixtures(day_string):
    start = datetime.strptime(day_string, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    data = get_json("fixtures", sportId=11, **{
        "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": (start + timedelta(days=1) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    if isinstance(data, dict):
        data = data.get("fixtures", data.get("data", []))
    if not isinstance(data, list):
        raise ValueError("Formato calendario inatteso")
    return sorted((f for f in data if str(f.get("tournamentName", "")).casefold() == "nba"), key=lambda f: str(f.get("startTime", "")))


@st.cache_data(ttl=86400, show_spinner="Quote Bet365…")
def get_odds(fixture_id, refresh_version=0):
    return get_json("odds", fixtureId=fixture_id, bookmakers="bet365.it", verbosity=3)


def parse_odds(data, catalog, market_label):
    bookmaker = (data.get("bookmakerOdds") or {}).get("bet365.it") or {}
    if not bookmaker.get("bookmakerIsActive", False) or bookmaker.get("suspended", False):
        return [], {"Bookmaker": "Non attivo o sospeso"}
    rows = {}
    diag = {"Mercati nel feed Bet365": len(bookmaker.get("markets") or {}), "Mercati del tipo richiesto": 0,
            "Mercati alternativi (+)": 0, "Mercati alternativi attivi": 0,
            "Mercati alternativi inattivi": 0, "Mercati senza definizione": 0}
    for market_id, market_data in (bookmaker.get("markets") or {}).items():
        definition = catalog.get(str(market_id))
        if not definition:
            diag["Mercati senza definizione"] += 1
            continue
        kind = definition.get("marketType", "")
        if kind not in MARKETS[market_label]:
            continue
        diag["Mercati del tipo richiesto"] += 1
        is_alternative = kind.startswith("players-")
        active_market = market_data.get("marketActive") is True
        if is_alternative:
            diag["Mercati alternativi (+)"] += 1
            diag["Mercati alternativi attivi" if active_market else "Mercati alternativi inattivi"] += 1
        elif not active_market:
            continue  # Keep strict filtering for main O/U markets.
        names = {str(o.get("outcomeId")): str(o.get("outcomeName", "")) for o in definition.get("outcomes", [])}
        for outcome_id, outcome in (market_data.get("outcomes") or {}).items():
            name = names.get(str(outcome_id), "")
            if is_alternative:
                match = re.fullmatch(r"\s*(\d+)\+\s*", name)
                if not match:
                    continue
                line, side = int(match.group(1)) - 0.5, "Over"
            else:
                if name.lower() not in ("over", "under"):
                    continue
                try:
                    line = float(definition["handicap"])
                except (ValueError, KeyError, TypeError):
                    continue
                side = name.title()
            for player_id, player in (outcome.get("players") or {}).items():
                if not player.get("active") or not player.get("playerName"):
                    continue
                price = player.get("price")
                if not isinstance(price, (int, float)) or price <= 1:
                    continue
                key = (str(player_id), line)
                row = rows.setdefault(key, {"Giocatore": player["playerName"], "Linea": line,
                    "Over": None, "Under": None, "Principale": False, "Tipo": "Soglia +" if is_alternative else "Over/Under",
                    "Stato API": "Mercato inattivo" if not active_market else "Attivo"})
                # Main O/U takes precedence when both markets describe the same line.
                if row[side] is None or not is_alternative:
                    row[side] = price
                if not is_alternative:
                    row["Tipo"] = "Over/Under"
                    row["Stato API"] = "Attivo"
                if player.get("mainLine"):
                    row["Principale"] = True
    return sorted(rows.values(), key=lambda r: (r["Giocatore"], r["Linea"])), diag


@st.cache_data(ttl=7 * 86400, show_spinner=False)
def nba_player_directory():
    from nba_api.stats.static import players
    return players.get_players()


@st.cache_data(ttl=6 * 3600, show_spinner="Recupero statistiche ufficiali NBA…")
def nba_game_log(player_id, season):
    from nba_api.stats.endpoints import playergamelog
    # A timeout can be transient. Retry once with a slightly longer timeout.
    # Both requests go directly to stats.nba.com; neither uses OddsPapi.
    for attempt, timeout_seconds in enumerate((15, 35), start=1):
        try:
            return playergamelog.PlayerGameLog(
                player_id=player_id,
                season=season,
                season_type_all_star="Regular Season",
                timeout=timeout_seconds,
            ).get_data_frames()[0]
        except requests.exceptions.Timeout:
            logger.warning("NBA game log timed out: attempt=%s timeout=%ss", attempt, timeout_seconds)
            if attempt == 2:
                raise


def match_nba_player(odds_name, directory):
    # OddsPapi: "Cunningham, Cade"; nba_api: "Cade Cunningham".
    parts = [p.strip() for p in odds_name.split(",")]
    normalized = " ".join(reversed(parts)) if len(parts) == 2 else odds_name
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.casefold())
    matches = [p for p in directory if norm(p["full_name"]) == norm(normalized)]
    return matches[0] if len(matches) == 1 else None


st.title("🏀 NBA STATS 2.0")
st.caption("Quote Bet365 Italia tramite OddsPapi · Statistiche giocatori dal sito NBA ufficiale")
st.info("Le quote alternative segnalate come inattive dall'API sono visibili per consultazione, ma la loro disponibilità va verificata su Bet365.")
if not API_KEY:
    st.error("Configura ODDSPAPI_API_KEY nei Secrets di Streamlit.")
    st.stop()

selected_date = st.date_input("Data delle partite (UTC)", value=datetime.now(timezone.utc).date())
try:
    fixtures = get_fixtures(selected_date.isoformat())
except (RuntimeError, ValueError) as exc:
    st.error(f"Calendario non disponibile: {exc}")
    st.stop()
if not fixtures:
    st.warning("Nessuna partita NBA trovata per questa data.")
    st.stop()
options = {f"{f.get('participant1Name', '?')} – {f.get('participant2Name', '?')} | {str(f.get('startTime', ''))[:16].replace('T', ' ')} UTC": f for f in fixtures}
choice = st.selectbox("🏟️ Partita NBA", list(options))
fixture_id = str(options[choice]["fixtureId"])
st.caption(f"Partite NBA trovate: {len(fixtures)}")
if "loaded_fixture" not in st.session_state:
    st.session_state.loaded_fixture = None
if "odds_refresh_versions" not in st.session_state:
    st.session_state.odds_refresh_versions = {}
left, right = st.columns([2, 1])
with left:
    if st.button("📥 Carica quote Bet365", type="primary"):
        st.session_state.loaded_fixture = fixture_id
with right:
    if st.button("🔄 Aggiorna quote della partita"):
        versions = st.session_state.odds_refresh_versions
        versions[fixture_id] = versions.get(fixture_id, 0) + 1
        st.session_state.loaded_fixture = fixture_id
if st.session_state.loaded_fixture != fixture_id:
    st.info("Premi **Carica quote Bet365** per la partita selezionata. Il calendario non carica automaticamente le quote.")
    st.stop()
market_label = st.selectbox("📊 Mercato giocatore", list(MARKETS))
try:
    catalog = get_markets()
    odds = get_odds(fixture_id, st.session_state.odds_refresh_versions.get(fixture_id, 0))
    rows, diag = parse_odds(odds, catalog, market_label)
except (RuntimeError, ValueError) as exc:
    st.error(f"Quote non disponibili: {exc}")
    st.stop()
if not rows:
    st.warning("Nessuna quota disponibile nel feed per questo mercato.")
    st.stop()
player_name = st.selectbox("🏀 Giocatore", sorted({r["Giocatore"] for r in rows}))
player_rows = [r for r in rows if r["Giocatore"] == player_name]
st.subheader(f"{player_name} · {market_label}")
st.caption("'Soglia +' indica solo Over (20+ = Over 19,5). Nessuna quota Under viene inventata.")
frame = pd.DataFrame(player_rows).sort_values("Linea")
frame["Principale"] = frame["Principale"].map({True: "Sì", False: ""})
st.dataframe(frame[["Linea", "Over", "Under", "Principale", "Tipo", "Stato API"]],
    hide_index=True, width="stretch",
    column_config={"Linea": st.column_config.NumberColumn(format="%.1f"),
                   "Over": st.column_config.NumberColumn(format="%.3f"),
                   "Under": st.column_config.NumberColumn(format="%.3f")})
st.download_button("⬇️ Scarica CSV quote", frame.to_csv(index=False).encode("utf-8-sig"),
                   file_name="nba_stats_quote.csv", mime="text/csv")
with st.expander("🔎 Diagnostica mercati (nessuna richiesta OddsPapi aggiuntiva)"):
    st.json(diag)
    st.caption("Dati ricavati dalla risposta OddsPapi già caricata.")

st.divider()
st.subheader("📈 Statistiche ufficiali NBA — prima integrazione")
st.caption("Le statistiche utilizzano nba_api e non consumano richieste OddsPapi. In caso di timeout, viene effettuato un solo tentativo aggiuntivo.")
season = st.selectbox("Stagione NBA", ["2026-27", "2025-26", "2024-25"])
if st.button("📊 Carica statistiche NBA del giocatore"):
    st.session_state.stats_selection = (player_name, season)
if st.session_state.get("stats_selection") == (player_name, season):
    diagnostic_stage = "Caricamento anagrafica giocatori NBA"
    try:
        directory = nba_player_directory()
        diagnostic_stage = "Abbinamento del nome del giocatore"
        nba_player = match_nba_player(player_name, directory)
        if not nba_player:
            st.warning("Nome giocatore non abbinato automaticamente all'anagrafica NBA. Non sono stati utilizzati dati di un altro giocatore.")
        else:
            diagnostic_stage = "Recupero storico partite da stats.nba.com"
            log = nba_game_log(nba_player["id"], season)
            diagnostic_stage = "Elaborazione e visualizzazione statistiche"
            if log.empty:
                st.info("Nessuna partita di regular season disponibile per questo giocatore e questa stagione.")
            else:
                log = log.copy()
                log["GAME_DATE"] = pd.to_datetime(log["GAME_DATE"], errors="coerce")
                log = log.sort_values("GAME_DATE", ascending=False)
                for col in ("PTS", "REB", "AST"):
                    log[col] = pd.to_numeric(log[col], errors="coerce")
                log["PRA"] = log["PTS"] + log["REB"] + log["AST"]
                stat = STATS[market_label]
                st.write(f"**{nba_player['full_name']} — {season} — {len(log)} partite**")
                line = st.selectbox("Linea da analizzare", sorted(frame["Linea"].unique()), index=0)
                columns = st.columns(3)
                for column, count in zip(columns, (5, 10, 20)):
                    subset = log.head(count)[stat].dropna()
                    hits = int((subset > line).sum())
                    column.metric(f"Ultime {count} (disponibili: {len(subset)})", f"{hits}/{len(subset)}", f"{hits / len(subset):.0%} Over" if len(subset) else None)
                st.write(f"Media {stat} stagionale: **{log[stat].mean():.1f}**")
                visible = log[["GAME_DATE", "MATCHUP", "MIN", "PTS", "REB", "AST", "PRA"]].copy()
                visible["GAME_DATE"] = visible["GAME_DATE"].dt.strftime("%d/%m/%Y")
                st.dataframe(visible, hide_index=True, width="stretch")
    except Exception as exc:
        # Never display or log raw exception text: request exceptions can contain URLs.
        # Only publish the stage, exception class, and numeric HTTP status if present.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if not isinstance(status, int):
            status = None
        error_type = type(exc).__name__
        logger.error("NBA diagnostics: stage=%s type=%s http_status=%s", diagnostic_stage, error_type, status)
        st.error("Impossibile caricare le statistiche NBA. Le quote OddsPapi non sono coinvolte.")
        with st.expander("🔎 Diagnostica connessione NBA (nessuna richiesta aggiuntiva)", expanded=True):
            st.json({"Fase": diagnostic_stage, "Tipo errore": error_type,
                     "Stato HTTP": status if status is not None else "Non disponibile",
                     "Giocatore": player_name, "Stagione": season,
                     "Tentativi previsti in caso di timeout": 2})
            st.caption("Questi dettagli sono ricavati dall'errore già avvenuto: non viene eseguito alcun nuovo test di rete. Non condividere eventuali credenziali presenti in altri log.")

st.caption("Quote: cache 24 ore · Calendario: 24 ore · Catalogo: 7 giorni · Statistiche NBA: cache 6 ore. La cache può svuotarsi ai riavvii.")
