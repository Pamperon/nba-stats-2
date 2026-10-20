"""NBA STATS 2.0: OddsPapi Bet365 + free ESPN player game logs."""
from datetime import datetime, timedelta, timezone
import re
import logging
import traceback
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
        if not active_market:
            diag.setdefault("Mercati inattivi esclusi", 0)
            diag["Mercati inattivi esclusi"] += 1
            continue  # Apply active-market requirement to BOTH main and alternative markets.
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
                    return {"id": athlete["id"], "full_name": full_name, "team_id": team_id}, {
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


def espn_event_date(info):
    """Extract a real date only; never infer a date from row position."""
    if not isinstance(info, dict):
        return None
    for key in ("date", "gameDate", "startDate", "startTime"):
        if info.get(key):
            return info[key]
    for competition in info.get("competitions") or []:
        if isinstance(competition, dict):
            found = espn_event_date(competition)
            if found:
                return found
    return None


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def espn_team_schedule(team_id, end_year, season_type=2):
    """One ESPN schedule request supplies game dates for an entire season."""
    payload = espn_json(f"{ESPN_SITE}/teams/{team_id}/schedule", season=end_year, seasontype=season_type)
    dates = {}
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("id") or "")
        date = espn_event_date(event)
        if event_id and date:
            dates[event_id] = date
    return dates


def espn_log_to_frame(data, season_type=2):
    """Parse ESPN games, keeping their *actual* competition when identifiable."""
    labels = [str(x).strip().upper() for x in data.get("labels") or []]
    metadata = data.get("events") or {}
    if isinstance(metadata, list):
        metadata = {str(e.get("id")): e for e in metadata if isinstance(e, dict)}
    if not isinstance(metadata, dict):
        metadata = {}
    rows = []
    for group in data.get("seasonTypes") or []:
        if not isinstance(group, dict):
            continue
        raw_type = group.get("id") or group.get("type")
        if isinstance(raw_type, dict):
            raw_type = raw_type.get("id") or raw_type.get("value")
        group_name = str(group.get("displayName") or group.get("name") or "").lower()
        group_type = None
        if str(raw_type) in ("2", "3"):
            group_type = int(raw_type)
        elif "regular" in group_name:
            group_type = 2
        elif any(word in group_name for word in ("postseason", "post-season", "playoff")):
            group_type = 3
        # ESPN can ignore seasonType query. Never blindly label games as the requested type.
        for category in group.get("categories") or []:
            if not isinstance(category, dict) or str(category.get("type", "")).lower() == "total":
                continue
            local_labels = [str(x).strip().upper() for x in (category.get("labels") or labels)]
            if not all(x in local_labels for x in ("PTS", "REB", "AST")):
                continue
            for event in category.get("events") or []:
                if not isinstance(event, dict):
                    continue
                values = event.get("stats") or []
                if not isinstance(values, list) or len(values) != len(local_labels):
                    continue
                event_id = str(event.get("eventId") or event.get("id") or "")
                if not event_id:
                    continue
                info = metadata.get(event_id) or {}
                if not isinstance(info, dict):
                    info = {}
                opponent = info.get("opponent") or {}
                if not isinstance(opponent, dict):
                    opponent = {}
                opp = opponent.get("abbreviation") or opponent.get("displayName") or "?"
                rows.append({
                    "GAME_ID": event_id,
                    "SEASON_TYPE": group_type,
                    "GAME_DATE": espn_event_date(info) or espn_event_date(event),
                    "MATCHUP": f"{'@' if str(info.get('atVs', '')).strip() == '@' else 'vs'} {opp}",
                    "MIN": values[local_labels.index("MIN")] if "MIN" in local_labels else None,
                    "PTS": values[local_labels.index("PTS")],
                    "REB": values[local_labels.index("REB")],
                    "AST": values[local_labels.index("AST")],
                })
    frame = pd.DataFrame(rows, columns=["GAME_ID", "SEASON_TYPE", "GAME_DATE", "MATCHUP", "MIN", "PTS", "REB", "AST"])
    if not frame.empty:
        frame = frame.drop_duplicates(subset=["GAME_ID"], keep="first")
    return frame


def prepare_game_log(log, team_id, season, require_dates=True):
    """Complete and verify dates before calculating last-N statistics."""
    log = log.copy()
    if log.empty:
        return log
    dates = pd.to_datetime(log["GAME_DATE"], errors="coerce", utc=True)
    if dates.isna().any() and team_id:
        schedule = {}
        for kind in sorted(set(log["SEASON_TYPE"].astype(int))) if "SEASON_TYPE" in log.columns else [2]:
            schedule.update(espn_team_schedule(team_id, int(season[:4]) + 1, kind))
        replacement = pd.to_datetime(log["GAME_ID"].astype(str).map(schedule), errors="coerce", utc=True)
        dates = dates.fillna(replacement)
    log["GAME_DATE"] = dates
    # Do not invent chronology. Missing dates do not prevent season-wide stats;
    # last-N comparisons are hidden until the complete order is known.
    for col in ("PTS", "REB", "AST"):
        log[col] = pd.to_numeric(log[col], errors="coerce")
    if log[["PTS", "REB", "AST"]].isna().any().any():
        raise ValueError("Statistiche ESPN incomplete o non numeriche: analisi sospesa.")
    if (log[["PTS", "REB", "AST"]] < 0).any().any():
        raise ValueError("Statistiche ESPN non valide: valori negativi.")
    log["PRA"] = log["PTS"] + log["REB"] + log["AST"]
    return log.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)


def analysis_row(title, data, stat, line):
    values = data[stat].dropna()
    n = len(values)
    over = int((values > line).sum())
    under = int((values < line).sum())
    push = n - over - under
    row = {"Periodo": title, "Partite": n, "Over": f"{over}/{n}" if n else "—",
           "% Over": f"{over/n:.1%}" if n else "—",
           "Under": f"{under}/{n}" if n else "—",
           "% Under": f"{under/n:.1%}" if n else "—",
           "Media": f"{values.mean():.1f}" if n else "—",
           "Media - linea": f"{values.mean()-line:+.1f}" if n else "—"}
    if push:
        row["Push"] = push
    return row


@st.cache_data(ttl=6 * 3600, show_spinner="Recupero storico ESPN…")
def espn_game_log(athlete_id, season, season_type=2):
    end_year = int(season.split("-")[0]) + 1
    # Explicit seasonType is necessary: ESPN defaults to regular-season logs.
    payload = espn_json(f"{ESPN_WEB}/athletes/{athlete_id}/gamelog", season=end_year, seasonType=season_type)
    return espn_log_to_frame(payload, season_type=season_type)


def _espn_competition_type(event):
    """Read competition type from explicit ESPN event metadata only."""
    if not isinstance(event, dict):
        return None
    candidates = [event.get("season"), event.get("seasonType"),
                  event.get("season_type"), event.get("type")]
    for comp in event.get("competitions") or []:
        if isinstance(comp, dict):
            candidates.extend([comp.get("season"), comp.get("seasonType")])
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("type") or candidate.get("id") or candidate.get("value")
        if str(candidate) in ("2", "3"):
            return int(candidate)
        if isinstance(candidate, str):
            value = candidate.lower()
            if "regular" in value:
                return 2
            if "playoff" in value or "postseason" in value:
                return 3
    return None


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def espn_schedule_competition_ids(team_id, end_year):
    """Resolve event types from ESPN schedules; ignore season-type query if ESPN ignores it."""
    sources = {}
    for requested_type in (2, 3):
        payload = espn_json(f"{ESPN_SITE}/teams/{team_id}/schedule", season=end_year, seasontype=requested_type)
        for event in payload.get("events") or []:
            if not isinstance(event, dict) or not event.get("id"):
                continue
            event_id = str(event["id"])
            explicit_type = _espn_competition_type(event)
            record = sources.setdefault(event_id, {"explicit": set(), "requests": set()})
            if explicit_type:
                record["explicit"].add(explicit_type)
            record["requests"].add(requested_type)
    result = {}
    for event_id, record in sources.items():
        if len(record["explicit"]) == 1:
            result[event_id] = next(iter(record["explicit"]))
        elif not record["explicit"] and len(record["requests"]) == 1:
            # Trust schedule membership only if the requested filters disagree.
            result[event_id] = next(iter(record["requests"]))
    return result


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def espn_event_competition_type(event_id):
    """Fallback: one free ESPN event summary when schedule lacks type metadata."""
    payload = espn_json(f"{ESPN_SITE}/summary", event=event_id)
    header = payload.get("header") or {}
    return _espn_competition_type(header) or _espn_competition_type(payload.get("gameInfo"))


@st.cache_data(ttl=6 * 3600, show_spinner="Classificazione partite ESPN…")
def espn_classified_game_log(athlete_id, season, team_id):
    """Use event metadata to distinguish regular season from playoffs for any player."""
    end_year = int(season[:4]) + 1
    raw = []
    for requested_type in (None, 3):
        params = {"season": end_year}
        if requested_type is not None:
            params["seasonType"] = requested_type
        payload = espn_json(f"{ESPN_WEB}/athletes/{athlete_id}/gamelog", **params)
        frame = espn_log_to_frame(payload)
        if not frame.empty:
            raw.append(frame)
    columns = ["GAME_ID", "SEASON_TYPE", "GAME_DATE", "MATCHUP", "MIN", "PTS", "REB", "AST"]
    if not raw:
        return pd.DataFrame(columns=columns)
    games = pd.concat(raw, ignore_index=True).drop_duplicates(subset=["GAME_ID"], keep="first").reset_index(drop=True)
    # The roster team may be the current team; for players who moved teams,
    # game summaries can resolve event IDs missing from this team's schedule.
    known_types = espn_schedule_competition_ids(team_id, end_year) if team_id else {}
    unresolved = [event_id for event_id in games["GAME_ID"].astype(str) if event_id not in known_types]
    for event_id in unresolved:
        try:
            found = espn_event_competition_type(event_id)
        except (requests.RequestException, ValueError, TypeError):
            found = None
        if found in (2, 3):
            known_types[event_id] = found
    games["SEASON_TYPE"] = games["GAME_ID"].astype(str).map(known_types)
    missing = int(games["SEASON_TYPE"].isna().sum())
    if missing:
        raise ValueError(f"ESPN non consente di classificare {missing} partite su {len(games)}: impossibile distinguere regular season e playoff senza inventare dati.")
    games["SEASON_TYPE"] = games["SEASON_TYPE"].astype(int)
    return games


def selected_game_log(athlete_id, season, mode, team_id=None):
    games = espn_classified_game_log(athlete_id, season, team_id)
    if mode == "Regular season":
        return games.loc[games["SEASON_TYPE"] == 2].copy()
    if mode == "Playoff":
        return games.loc[games["SEASON_TYPE"] == 3].copy()
    if mode == "Regular season + Playoff":
        return games.copy()
    raise ValueError("Modalità competizione non riconosciuta")


def auto_filter_suspicious_lines(player_rows):
    """Exclude a stale low-line prefix only when odds show a clear price reset.

    Higher points thresholds should not have materially lower Over prices.
    This is a heuristic, not proof of availability at the bookmaker.
    """
    ordered = sorted(player_rows, key=lambda r: r["Linea"])
    last_price = None
    reset_at = None
    for index, row in enumerate(ordered):
        price = row.get("Over")
        if not isinstance(price, (int, float)):
            continue
        if last_price is not None and last_price >= 1.35 and price < last_price * 0.75 and last_price - price >= 0.30:
            reset_at = index
        last_price = price
    if reset_at is None:
        return ordered, []
    return ordered[reset_at:], ordered[:reset_at]


st.title("🏀 NBA STATS 2.0")
st.caption("Quote Bet365 Italia tramite OddsPapi · Statistiche NBA da ESPN (fonte gratuita)")
st.info("Le quote provengono da OddsPapi. Il filtro automatico esclude alcune linee sospette, ma la disponibilità effettiva va verificata su Bet365.")
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
st.caption("Le soglie '+' mostrano soltanto la quota Over (20+ = Over 19,5). Nessuna quota Under viene inventata.")
st.caption("Il filtro automatico segnala anomalie nelle quote, ma non garantisce che ogni linea sia realmente presente su Bet365.")
automatic_filter = st.checkbox("Filtro automatico linee sospette", value=True,
                               help="Esclude gruppi di soglie con una forte inversione delle quote Over. Non verifica in tempo reale il sito Bet365.")
if automatic_filter:
    player_rows, excluded_rows = auto_filter_suspicious_lines(player_rows)
    if excluded_rows:
        st.caption(f"Filtro automatico: escluse {len(excluded_rows)} linee con andamento delle quote incoerente "
                   f"(fino a {excluded_rows[-1]['Linea']:g}). Puoi disattivarlo per rivederle.")
min_line_enabled = st.checkbox("Imposta manualmente la prima linea disponibile", value=False)
if min_line_enabled:
    min_line = st.number_input("Prima linea disponibile su Bet365", min_value=0.5, max_value=200.5, value=14.5, step=1.0, format="%.1f")
    player_rows = [r for r in player_rows if r["Linea"] >= min_line]
if not player_rows:
    st.warning("Nessuna linea rimasta dopo i filtri. Disattivali o abbassa la soglia.")
    st.stop()
frame = pd.DataFrame(player_rows).sort_values("Linea")
frame["Principale"] = frame["Principale"].map({True: "Sì", False: ""})
st.dataframe(frame[["Linea", "Over", "Under", "Principale"]],
    hide_index=True, width="stretch",
    column_config={"Linea": st.column_config.NumberColumn(format="%.1f"),
                   "Over": st.column_config.NumberColumn(format="%.3f"),
                   "Under": st.column_config.NumberColumn(format="%.3f")})
st.download_button("⬇️ Scarica CSV quote", frame[["Giocatore", "Linea", "Over", "Under", "Principale"]].to_csv(index=False).encode("utf-8-sig"),
                   file_name="nba_stats_quote.csv", mime="text/csv")
with st.expander("🔎 Diagnostica mercati (nessuna richiesta OddsPapi aggiuntiva)"):
    st.json(diag)
    st.caption("Dati ricavati dalla risposta OddsPapi già caricata.")

st.divider()
st.subheader("📈 Statistiche NBA — ESPN")
st.caption("Storico delle partite tramite ESPN, senza API a pagamento e senza richieste OddsPapi aggiuntive.")
season = st.selectbox("Stagione NBA", ["2026-27", "2025-26", "2024-25"])
competition_mode = st.radio("Tipo di competizione", ["Regular season", "Playoff", "Regular season + Playoff"], horizontal=True)

if st.button("📊 Carica statistiche NBA del giocatore"):
    st.session_state.stats_selection = (player_name, season, competition_mode)
if st.session_state.get("stats_selection") == (player_name, season, competition_mode):
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
            log = selected_game_log(nba_player["id"], season, competition_mode, nba_player.get("team_id"))
            diagnostic_stage = "Elaborazione e visualizzazione statistiche"
            if log.empty:
                st.info(f"Nessuna partita disponibile in modalità {competition_mode} per questo giocatore e questa stagione.")
            else:
                log = prepare_game_log(log, nba_player.get("team_id"), season)
                stat = STATS[market_label]
                st.write(f"**{nba_player['full_name']} — {season} — {competition_mode} — {len(log)} partite**")
                if competition_mode != "Regular season":
                    st.caption("Nei playoff il campione può essere ridotto. Le percentuali sono calcolate solo sulle partite effettivamente disponibili.")
                line = st.selectbox("Linea da analizzare", sorted(frame["Linea"].unique()), index=0)
                # The previous season is loaded only when the user requests the analysis.
                previous_season = f"{int(season[:4])-1}-{str(int(season[:4]))[-2:]}"
                previous_log = selected_game_log(nba_player["id"], previous_season, competition_mode, nba_player.get("team_id"))
                if not previous_log.empty:
                    previous_log = prepare_game_log(previous_log, nba_player.get("team_id"), previous_season, require_dates=False)
                missing_dates = int(log["GAME_DATE"].isna().sum())
                periods = []
                if missing_dates == 0:
                    periods.extend([("Ultime 5", log.head(5)), ("Ultime 10", log.head(10)),
                                    ("Ultime 20", log.head(20))])
                else:
                    st.warning(f"ESPN non fornisce una data verificabile per {missing_dates} partite su {len(log)}. "
                               "Le analisi ultime 5/10/20 sono sospese per evitare risultati inesatti; "
                               "le statistiche dell'intera stagione restano disponibili.")
                periods.extend([(f"{competition_mode} {season}", log),
                                (f"{competition_mode} {previous_season}", previous_log)])
                st.caption(f"Linea {line:g} · Over: risultato > {line:g} · Under: risultato < {line:g}")
                for period_title, period_log in periods:
                    values = period_log[stat].dropna() if not period_log.empty else pd.Series(dtype=float)
                    count = len(values)
                    over = int((values > line).sum())
                    under = int((values < line).sum())
                    pushes = count - over - under
                    with st.container(border=True):
                        first, second = st.columns([2, 1])
                        with first:
                            st.write(f"**{period_title}** · {count} partite")
                            if count:
                                st.markdown(
                                    f'<span style="display:inline-block;background:#dcfce7;color:#166534;padding:5px 12px;border-radius:20px;margin-right:8px;font-weight:600">'
                                    f'↑ {over/count:.0%} Over ({over}/{count})</span>'
                                    f'<span style="display:inline-block;background:#fee2e2;color:#991b1b;padding:5px 12px;border-radius:20px;font-weight:600">'
                                    f'↓ {under/count:.0%} Under ({under}/{count})</span>',
                                    unsafe_allow_html=True,
                                )
                                if pushes:
                                    st.caption(f"Push: {pushes} (risultato esattamente uguale alla linea)")
                            else:
                                st.caption("Nessuna partita disponibile")
                        with second:
                            if count:
                                st.metric(f"Media {stat}", f"{values.mean():.1f}",
                                          f"{values.mean()-line:+.1f} rispetto alla linea")
                st.subheader(f"Dettaglio partite — {season} · {competition_mode}")
                detail_columns = ["GAME_DATE", "MATCHUP", "MIN", "PTS", "REB", "AST", "PRA"]
                if competition_mode == "Regular season + Playoff":
                    detail_columns.insert(1, "SEASON_TYPE")
                visible = log[detail_columns].copy()
                if "SEASON_TYPE" in visible:
                    visible["SEASON_TYPE"] = visible["SEASON_TYPE"].map({2: "Regular season", 3: "Playoff"})
                visible["GAME_DATE"] = visible["GAME_DATE"].dt.strftime("%d/%m/%Y")
                st.dataframe(visible, hide_index=True, width="stretch")
                with st.expander("📊 Riepilogo comparativo"):
                    st.dataframe(pd.DataFrame([analysis_row(title, games, stat, line)
                                               for title, games in periods]), hide_index=True, width="stretch")

    except Exception as exc:
        # Never display or log raw exception text: request exceptions can contain URLs.
        # Only publish the stage, exception class, and numeric HTTP status if present.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if not isinstance(status, int):
            status = None
        error_type = type(exc).__name__
        # Show the exact failing source line, without exposing request URLs or secrets.
        frames_trace = traceback.extract_tb(exc.__traceback__)
        own_frames = [f for f in frames_trace if f.filename.endswith("app.py") or "app_nba_stats" in f.filename]
        failure = own_frames[-1] if own_frames else (frames_trace[-1] if frames_trace else None)
        failure_location = f"Riga {failure.lineno} · funzione {failure.name}" if failure else "Non disponibile"
        logger.error("NBA diagnostics: stage=%s type=%s http_status=%s location=%s", diagnostic_stage, error_type, status, failure_location)
        st.error("Impossibile completare l'analisi ESPN. Le quote OddsPapi non sono coinvolte.")
        st.caption(f"Punto del problema: **{failure_location}**")
        if isinstance(exc, TypeError):
            st.warning(f"Dettaglio TypeError: {str(exc)[:250]}")
        if isinstance(exc, ValueError):
            # Only locally constructed validation errors, never raw request URLs.
            st.warning(f"Dettaglio verifica dati: {str(exc)[:350]}")
        with st.expander("🔎 Diagnostica connessione NBA (nessuna richiesta aggiuntiva)", expanded=True):
            st.json({"Fase": diagnostic_stage, "Tipo errore": error_type,
                     "Stato HTTP": status if status is not None else "Non disponibile",
                     "Giocatore": player_name, "Stagione": season,
                     "Fonte": "ESPN (gratuita)", "Punto errore": failure_location})
            st.caption("Questi dettagli sono ricavati dall'errore già avvenuto: non viene eseguito alcun nuovo test di rete. Non condividere eventuali credenziali presenti in altri log.")

st.caption("Quote: cache 24 ore · Calendario: 24 ore · Catalogo: 7 giorni · Statistiche ESPN: cache 6 ore. La cache può svuotarsi ai riavvii.")
