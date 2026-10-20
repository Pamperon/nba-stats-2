"""NBA STATS 2.0: OddsPapi Bet365 + free ESPN player game logs."""
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


ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_WEB = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"


def espn_json(url, **params):
    # Public ESPN endpoints: no account, key or OddsPapi request involved.
    response = requests.get(url, params=params, headers={
        "User-Agent": "Mozilla/5.0 (compatible; NBAStats2/2.0)",
        "Accept": "application/json",
    }, timeout=(8, 22))
    response.raise_for_status()
    return response.json()


def normalized_name(name):
    import unicodedata
    name = unicodedata.normalize("NFKD", str(name))
    name = "".join(c for c in name if not unicodedata.combining(c))
    parts = [p.strip() for p in name.split(",")]
    if len(parts) == 2:
        name = parts[1] + " " + parts[0]
    return re.sub(r"[^a-z0-9]", "", name.casefold())


@st.cache_data(ttl=7 * 86400, show_spinner=False)
def espn_teams():
    data = espn_json(f"{ESPN_SITE}/teams", limit=50)
    return (data.get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", [])


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def espn_roster(team_id, end_year=None):
    # ESPN's current roster does not need a season parameter. Some historical
    # season queries return HTTP 200 with an empty athletes list.
    params = {"season": end_year} if end_year is not None else {}
    return espn_json(f"{ESPN_SITE}/teams/{team_id}/roster", **params)


def roster_athletes(payload):
    """Traverse ESPN roster shapes, including position groups and wrappers."""
    seen = set()

    def visit(node):
        if isinstance(node, list):
            for item in node:
                yield from visit(item)
        elif isinstance(node, dict):
            # ESPN sometimes nests the athlete under an `athlete` key.
            athlete = node.get("athlete")
            if isinstance(athlete, dict):
                yield from visit(athlete)
            athlete_id = node.get("id")
            name = node.get("displayName") or node.get("fullName") or node.get("name")
            if athlete_id is not None and name and str(athlete_id).isdigit():
                key = str(athlete_id)
                if key not in seen:
                    seen.add(key)
                    yield {"id": key, "displayName": name}
            # Some responses contain groups such as {"position":...,"items":...}.
            for key, value in node.items():
                if key != "athlete" and isinstance(value, (list, dict)):
                    yield from visit(value)

    yield from visit(payload.get("athletes") or payload.get("items") or [])


def match_espn_player(name, home_team, away_team, end_year):
    target = normalized_name(name)
    teams = espn_teams()
    primary, remaining = [], []
    fixture_names = {normalized_name(home_team), normalized_name(away_team)}
    for entry in teams:
        team = entry.get("team", entry)
        team_id = team.get("id")
        if not team_id:
            continue
        names = (team.get("displayName"), team.get("name"), team.get("shortDisplayName"),
                 team.get("abbreviation"))
        matched = any(normalized_name(t) in fixture_names for t in names if t)
        (primary if matched else remaining).append(str(team_id))

    checked, failed, athletes_seen = 0, 0, 0
    sample_names = []
    # First use the current roster: it is the most reliable ESPN roster URL.
    # Only if no match is found, try the selected historical season.
    for season_value in (None, end_year):
        for team_id in primary + remaining:
            try:
                roster = espn_roster(team_id, season_value)
            except requests.RequestException:
                failed += 1
                continue
            checked += 1
            for athlete in roster_athletes(roster):
                athletes_seen += 1
                full_name = athlete["displayName"]
                if len(sample_names) < 8:
                    sample_names.append(full_name)
                if normalized_name(full_name) == target:
                    return {"id": athlete["id"], "full_name": full_name}, {
                        "Rose controllate": checked,
                        "Rose non raggiungibili": failed,
                        "Atleti letti": athletes_seen,
                        "Fonte rosa": "Attuale" if season_value is None else f"Stagione {season_value}",
                    }
    return None, {
        "Rose controllate": checked,
        "Rose non raggiungibili": failed,
        "Squadre ESPN nel catalogo": len(teams),
        "Atleti letti": athletes_seen,
        "Esempi di nomi ESPN": sample_names,
        "Controlli": "Rose attuali e storiche; nessuna corrispondenza esatta",
    }


def espn_log_to_frame(data):
    labels = [str(x).upper() for x in data.get("labels", [])]
    if not all(x in labels for x in ("PTS", "REB", "AST")):
        raise ValueError("ESPN: mancano le colonne PTS/REB/AST nello storico")
    metadata = data.get("events") or {}
    if isinstance(metadata, list):
        metadata = {str(e.get("id")): e for e in metadata if isinstance(e, dict)}
    rows = []
    for season_type in data.get("seasonTypes") or []:
        type_id = str(season_type.get("id") or season_type.get("type") or "")
        type_name = str(season_type.get("displayName") or season_type.get("name") or "").lower()
        if type_id and type_id != "2" and "regular" not in type_name:
            continue
        if not type_id and "regular" not in type_name:
            continue
        for category in season_type.get("categories") or []:
            if str(category.get("type", "")).lower() == "total":
                continue
            for event in category.get("events") or []:
                if not isinstance(event, dict):
                    continue
                values = event.get("stats") or []
                if not isinstance(values, list) or len(values) != len(labels):
                    continue
                event_id = str(event.get("eventId") or event.get("id") or "")
                info = metadata.get(event_id, {})
                if not isinstance(info, dict):
                    info = {}
                opponent = info.get("opponent") or {}
                opp = opponent.get("abbreviation") or opponent.get("displayName") or "?"
                rows.append({
                    "GAME_ID": event_id,
                    "GAME_DATE": info.get("date") or event.get("date"),
                    "MATCHUP": f"{'@' if str(info.get('atVs', '')).strip() == '@' else 'vs'} {opp}",
                    "MIN": values[labels.index("MIN")] if "MIN" in labels else None,
                    "PTS": values[labels.index("PTS")],
                    "REB": values[labels.index("REB")],
                    "AST": values[labels.index("AST")],
                })
    frame = pd.DataFrame(rows, columns=["GAME_ID", "GAME_DATE", "MATCHUP", "MIN", "PTS", "REB", "AST"])
    if not frame.empty:
        frame = frame.drop_duplicates(subset=["GAME_ID"], keep="first")
    return frame


@st.cache_data(ttl=6 * 3600, show_spinner="Recupero storico ESPN…")
def espn_game_log(athlete_id, season):
    end_year = int(season.split("-")[0]) + 1
    payload = espn_json(f"{ESPN_WEB}/athletes/{athlete_id}/gamelog", season=end_year)
    return espn_log_to_frame(payload)


st.title("🏀 NBA STATS 2.0")
st.caption("Quote Bet365 Italia tramite OddsPapi · Statistiche NBA da ESPN (fonte gratuita)")
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
st.subheader("📈 Statistiche NBA — ESPN")
st.caption("Storico delle partite tramite ESPN, senza API a pagamento e senza richieste OddsPapi aggiuntive. Solo regular season.")
season = st.selectbox("Stagione NBA", ["2026-27", "2025-26", "2024-25"])
if st.button("📊 Carica statistiche NBA del giocatore"):
    st.session_state.stats_selection = (player_name, season)
if st.session_state.get("stats_selection") == (player_name, season):
    diagnostic_stage = "Identificazione giocatore su ESPN"
    try:
        fixture = options[choice]
        nba_player, matching_diagnostics = match_espn_player(player_name, fixture.get("participant1Name", ""), fixture.get("participant2Name", ""), int(season[:4]) + 1)
        if not nba_player:
            st.warning("Giocatore non identificato nelle rose ESPN. Controlla la diagnostica: indica quanti atleti sono stati effettivamente letti.")
            with st.expander("🔎 Diagnostica abbinamento ESPN", expanded=True):
                st.json({"Giocatore cercato": player_name, "Nome normalizzato": normalized_name(player_name), **matching_diagnostics})
        else:
            diagnostic_stage = "Recupero storico partite da ESPN"
            log = espn_game_log(nba_player["id"], season)
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
                     "Fonte": "ESPN (gratuita)"})
            st.caption("Questi dettagli sono ricavati dall'errore già avvenuto: non viene eseguito alcun nuovo test di rete. Non condividere eventuali credenziali presenti in altri log.")

st.caption("Quote: cache 24 ore · Calendario: 24 ore · Catalogo: 7 giorni · Statistiche ESPN: cache 6 ore. La cache può svuotarsi ai riavvii.")
