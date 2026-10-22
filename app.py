"""NBA STATS 2.0: OddsPapi Bet365 + free ESPN player game logs."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re
import logging
import traceback
import pandas as pd
import altair as alt
import requests
import streamlit as st

st.set_page_config(page_title="NBA STATS 3.6", page_icon="🏀", layout="wide")
logger = logging.getLogger("nba_stats_diagnostics")
BASE = "https://api.oddspapi.io/v4"
ROME_TZ = ZoneInfo("Europe/Rome")
MARKETS = {
    "Punti (PTS)": ("players-points",),
    "Rimbalzi (REB)": ("players-rebounds",),
    "Assist (AST)": ("players-assists",),
}
STATS = {"Punti (PTS)": "PTS", "Rimbalzi (REB)": "REB", "Assist (AST)": "AST"}
# Solo mercati alternativi Bet365 (es. 20+ = Over 19,5).
BET365_ALT_LINES = {
    "PTS": {float(n) + 0.5 for n in range(4, 100, 5)},
    "REB": {2.5, 4.5, 6.5, 9.5, 12.5, 14.5, 16.5, 19.5},
    "AST": {2.5, 4.5, 6.5, 9.5, 12.5, 14.5},
}


def bet365_line_allowed(row, stat):
    return (row.get("Tipo") == "Soglia +" and
            round(float(row["Linea"]), 2) in BET365_ALT_LINES[stat] and
            isinstance(row.get("Over"), (int, float)))


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


def fixture_start_rome(fixture):
    """Convert an OddsPapi ISO timestamp to Italian local time."""
    raw = fixture.get("startTime")
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            timestamp = float(raw)
            if timestamp > 1e11:  # Unix milliseconds
                timestamp /= 1000
            instant = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            value = str(raw).strip()
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            instant = datetime.fromisoformat(value)
            # OddsPapi UTC timestamps may omit their explicit Z suffix.
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
        return instant.astimezone(ROME_TZ)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


@st.cache_data(ttl=1800, show_spinner="Calendario NBA…")
def get_fixtures(day_string):
    # Calendar day is Europe/Rome, including daylight-saving transitions.
    local_start = datetime.strptime(day_string, "%Y-%m-%d").replace(tzinfo=ROME_TZ)
    local_end = (local_start + timedelta(days=1))
    utc_start = local_start.astimezone(timezone.utc)
    utc_end = local_end.astimezone(timezone.utc)
    data = get_json("fixtures", sportId=11, **{
        "from": utc_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": (utc_end - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    if isinstance(data, dict):
        data = data.get("fixtures", data.get("data", []))
    if not isinstance(data, list):
        raise ValueError("Formato calendario inatteso")
    matches = []
    for fixture in data:
        if str(fixture.get("tournamentName", "")).casefold() != "nba":
            continue
        start_rome = fixture_start_rome(fixture)
        if start_rome is not None and start_rome.date().isoformat() == day_string:
            matches.append(fixture)
    return sorted(matches, key=lambda f: fixture_start_rome(f))


def fixture_is_completed(fixture):
    """Hide completed games, not games merely underway."""
    status = str(fixture.get("statusName") or "").strip().casefold()
    if status in {"finished", "final", "completed", "ended", "after overtime", "after extra time"}:
        return True
    if fixture.get("trueEndTime"):
        return True
    return False


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
        # Never combine alternative '+' quotes with main Over/Under prices.
        active_market = market_data.get("marketActive") is True
        diag["Mercati alternativi (+)"] += 1
        diag["Mercati alternativi attivi" if active_market else "Mercati alternativi inattivi"] += 1
        if not active_market:
            diag.setdefault("Mercati inattivi esclusi", 0)
            diag["Mercati inattivi esclusi"] += 1
            continue
        names = {str(o.get("outcomeId")): str(o.get("outcomeName", "")) for o in definition.get("outcomes", [])}
        for outcome_id, outcome in (market_data.get("outcomes") or {}).items():
            name = names.get(str(outcome_id), "")
            match = re.fullmatch(r"\s*(\d+)\+\s*", name)
            if not match:
                continue
            line = int(match.group(1)) - 0.5
            for player_id, player in (outcome.get("players") or {}).items():
                if not player.get("active") or not player.get("playerName"):
                    continue
                price = player.get("price")
                if not isinstance(price, (int, float)) or price <= 1:
                    continue
                key = (str(player_id), line)
                rows[key] = {"Giocatore": player["playerName"], "Linea": line,
                             "Over": float(price), "Tipo": "Soglia +",
                             "Stato API": "Attivo"}
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
    return log.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)


def analysis_row(title, data, stat, line):
    values = data[stat].dropna()
    n = len(values)
    over = int((values > line).sum())
    return {"Periodo": title, "Partite": n,
            "Over": f"{over}/{n}" if n else "—",
            "% Over": f"{over/n:.1%}" if n else "—",
            "Media": f"{values.mean():.1f}" if n else "—",
            "Media - linea": f"{values.mean()-line:+.1f}" if n else "—"}


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
    """Classify using ESPN game-log groups first, then independent evidence.

    An unclassified game is never silently called a playoff game.
    """
    end_year = int(season[:4]) + 1
    frames = []
    membership = {2: set(), 3: set()}
    for requested_type in (None, 3):
        params = {"season": end_year}
        if requested_type is not None:
            params["seasonType"] = requested_type
        payload = espn_json(f"{ESPN_WEB}/athletes/{athlete_id}/gamelog", **params)
        frame = espn_log_to_frame(payload)
        if not frame.empty:
            frames.append(frame)
            if requested_type in (2, 3):
                membership[requested_type].update(frame["GAME_ID"].astype(str))
    columns = ["GAME_ID", "SEASON_TYPE", "GAME_DATE", "MATCHUP", "MIN", "PTS", "REB", "AST"]
    if not frames:
        return pd.DataFrame(columns=columns)

    # Prefer explicit season type attached to the individual log row.
    games = pd.concat(frames, ignore_index=True)
    explicit = {}
    for _, row in games.iterrows():
        event_id = str(row["GAME_ID"])
        kind = row["SEASON_TYPE"]
        if pd.notna(kind) and int(kind) in (2, 3):
            explicit.setdefault(event_id, set()).add(int(kind))
    games = games.drop_duplicates(subset=["GAME_ID"], keep="first").reset_index(drop=True)
    known = {event_id: next(iter(types)) for event_id, types in explicit.items() if len(types) == 1}

    # Schedules provide corroboration when the game-log group lacks type info.
    try:
        schedule_types = espn_schedule_competition_ids(team_id, end_year) if team_id else {}
    except (requests.RequestException, ValueError, TypeError):
        schedule_types = {}
    for event_id, kind in schedule_types.items():
        if event_id not in known and kind in (2, 3):
            known[event_id] = kind

    # Last resort: ask for an event summary, but only for unresolved games.
    for event_id in games["GAME_ID"].astype(str):
        if event_id in known:
            continue
        try:
            kind = espn_event_competition_type(event_id)
        except (requests.RequestException, ValueError, TypeError):
            kind = None
        if kind in (2, 3):
            known[event_id] = kind

    games["SEASON_TYPE"] = games["GAME_ID"].astype(str).map(known)
    # Do not raise for unknown classification: expose the uncertainty and exclude
    # those games from mode-specific analyses to avoid false Over/Under results.
    games.attrs["unclassified"] = int(games["SEASON_TYPE"].isna().sum())
    return games


def selected_game_log(athlete_id, season, mode, team_id=None):
    games = espn_classified_game_log(athlete_id, season, team_id)
    if mode == "Regular season":
        return games.loc[games["SEASON_TYPE"] == 2].copy()
    if mode == "Playoff":
        return games.loc[games["SEASON_TYPE"] == 3].copy()
    if mode == "Regular season + Playoff":
        return games.loc[games["SEASON_TYPE"].isin([2, 3])].copy()
    raise ValueError("Modalità competizione non riconosciuta")


def auto_filter_suspicious_lines(player_rows):
    """Remove a lower threshold if its Over price exceeds the next higher Over.

    Compare adjacent alternative Over quotes, repeating after each removal.
    A monotonic feed is not proof that Bet365 currently offers the quote.
    """
    ordered = sorted(player_rows, key=lambda r: r["Linea"])
    kept = list(ordered)
    excluded = []
    while True:
        over_rows = [r for r in kept if isinstance(r.get("Over"), (int, float))]
        bad = next((prev for prev, following in zip(over_rows, over_rows[1:])
                    if prev["Over"] > following["Over"] + 0.005), None)
        if bad is None:
            break
        kept.remove(bad)
        excluded.append(bad)
    return kept, excluded


def finder_estimate_probability(log, stat, line, side):
    """Stima empirica regolarizzata per ciascuna soglia; NON calibrata.

    Prior di Jeffreys Beta(0.5, 0.5), anziche' 24 pseudopartite al 50%.
    Una frequenza 0/N resta bassa, N/N resta alta; nessuna garanzia predittiva.
    La finestra recente e' parte della stagione: il suo peso (20%) e' una
    scelta provvisoria, da validare su giornate future senza riottimizzarla.
    """
    if log.empty or stat not in log.columns:
        return None
    log = log.copy()
    log[stat] = pd.to_numeric(log[stat], errors="coerce")
    log["GAME_DATE"] = pd.to_datetime(log["GAME_DATE"], errors="coerce", utc=True)
    log = log.dropna(subset=[stat, "GAME_DATE"]).sort_values("GAME_DATE", ascending=False)
    if len(log) < 15 or abs(line % 1 - 0.5) > 1e-6:
        return None
    values = log[stat]
    wins = (values > line) if side == "Over" else (values < line)
    recent = wins.head(min(20, len(wins)))
    season_rate = float(wins.mean())
    recent_rate = float(recent.mean())
    # Jeffreys smoothing: 0/20 -> 2.38%, 20/20 -> 97.62%.
    season_smoothed = (float(wins.sum()) + 0.5) / (len(wins) + 1.0)
    recent_smoothed = (float(recent.sum()) + 0.5) / (len(recent) + 1.0)
    estimate = 0.80 * season_smoothed + 0.20 * recent_smoothed
    return {"estimate": estimate, "season": season_rate, "recent": recent_rate,
            "n": len(wins), "n_recent": len(recent), "recent_hits": int(recent.sum()),
            "avg": float(values.mean())}


def finder_player_index(end_year):
    """Read all current ESPN rosters once; return normalized-name lookup."""
    index = {}
    for entry in espn_teams():
        team = entry.get("team", entry)
        team_id = str(team.get("id") or "")
        if not team_id:
            continue
        try:
            roster = espn_roster(team_id, None)
        except requests.RequestException:
            continue
        for athlete in roster_athletes(roster):
            index.setdefault(normalized_name(athlete["displayName"]),
                             {"id": athlete["id"], "team_id": team_id,
                              "full_name": athlete["displayName"],
                              "team_name": team.get("displayName") or team.get("name") or f"Squadra ESPN {team_id}"})
    return index


def odds_source_audit(odds, catalog, match_name):
    """Read bookmaker source fields already downloaded; NO extra API requests.

    'Attivo nel feed' is not a guarantee that Bet365 offers the selection.
    """
    bookmaker = (odds.get("bookmakerOdds") or {}).get("bet365.it") or {}
    result = []
    for market_id, market_data in (bookmaker.get("markets") or {}).items():
        definition = catalog.get(str(market_id))
        if not definition:
            continue
        kind = str(definition.get("marketType") or "")
        if kind not in {t for kinds in MARKETS.values() for t in kinds}:
            continue
        names = {str(o.get("outcomeId")): str(o.get("outcomeName", ""))
                 for o in definition.get("outcomes") or []}
        for outcome_id, outcome in (market_data.get("outcomes") or {}).items():
            label = names.get(str(outcome_id), "")
            if kind.startswith("players-"):
                match = re.fullmatch(r"\s*(\d+)\+\s*", label)
                if not match:
                    continue
                line = float(int(match.group(1)) - 0.5)
            else:
                if label.strip().lower() not in ("over", "under"):
                    continue
                try:
                    line = float(definition["handicap"])
                except (ValueError, TypeError, KeyError):
                    continue
            for player_id, player in (outcome.get("players") or {}).items():
                price = player.get("price")
                if not isinstance(price, (int, float)) or price <= 1:
                    continue
                result.append({
                    "Partita": match_name,
                    "Giocatore": str(player.get("playerName") or ""),
                    "Mercato API": kind,
                    "Nome mercato API": str(definition.get("marketName") or ""),
                    "Esito originale": label,
                    "Linea interpretata": line,
                    "Quota": price,
                    "Mercato attivo feed": market_data.get("marketActive"),
                    "Giocatore attivo feed": player.get("active"),
                    "Bookmaker attivo feed": bookmaker.get("bookmakerIsActive"),
                    "Mercato ID": str(market_id),
                    "Esito ID": str(outcome_id),
                    "Bookmaker market ID": str(market_data.get("bookmakerMarketId") or ""),
                    "Bookmaker outcome ID": str(player.get("bookmakerOutcomeId") or ""),
                    "Ultima variazione feed": str(player.get("changedAt") or ""),
                })
    return result


def finder_detailed_stats(log, stat, line):
    """Descrittori osservati, non un modello probabilistico validato."""
    frame = log.copy()
    frame[stat] = pd.to_numeric(frame[stat], errors="coerce")
    frame["GAME_DATE"] = pd.to_datetime(frame["GAME_DATE"], errors="coerce", utc=True)
    frame = frame.dropna(subset=[stat, "GAME_DATE"]).sort_values("GAME_DATE", ascending=False)
    if frame.empty:
        return {}
    vals = frame[stat]
    def hits(n):
        sub = vals.head(n)
        return f"{int((sub > line).sum())}/{len(sub)}"
    minutes = pd.to_numeric(frame["MIN"], errors="coerce") if "MIN" in frame else pd.Series(dtype=float)
    return {"Ultime 5": hits(5), "Ultime 10": hits(10), "Ultime 20": hits(20),
            "Stagione %": round(100 * float((vals > line).mean()), 1),
            "Partite": len(vals), "Media": round(float(vals.mean()), 1),
            "Scarto medio": round(float(vals.mean() - line), 1),
            "Dev. standard": round(float(vals.std(ddof=1)), 1) if len(vals) > 1 else None,
            "Minuti medi": round(float(minutes.mean()), 1) if minutes.notna().any() else None,
            "Minuti ultime 5": round(float(minutes.head(5).mean()), 1) if minutes.head(5).notna().any() else None}


def finder_scan(fixtures, catalog, season, min_games, odds_min, odds_max,
                min_value, progress_callback=None):
    """Esporta tutte le linee alternative attive ricevute, incluse non valutabili.

    Un'unica risposta OddsPapi per partita; se già presente in sessione la riusa.
    I filtri sono soltanto per la vista, mai per il CSV completo.
    """
    results, issues, raw_audit, filter_audit = [], [], [], []
    index = finder_player_index(int(season[:4]) + 1)
    props_by_player = {}
    player_logs = {}
    quote_count = 0
    for idx, fixture in enumerate(fixtures):
        fid = str(fixture["fixtureId"])
        match_name = f"{fixture.get('participant1Name', '?')} - {fixture.get('participant2Name', '?')}"
        if progress_callback:
            progress_callback(0.25 * idx / max(len(fixtures), 1), f"Quote: {match_name}")
        try:
            stored = st.session_state.get("odds_by_fixture", {}).get(fid)
            odds = stored if stored is not None else get_odds(fid, st.session_state.get("odds_refresh_versions", {}).get(fid, 0))
            st.session_state.setdefault("odds_by_fixture", {})[fid] = odds
            raw_audit.extend(odds_source_audit(odds, catalog, match_name))
            for market_label in MARKETS:
                parsed, _ = parse_odds(odds, catalog, market_label)
                grouped = {}
                for row in parsed:
                    grouped.setdefault(row["Giocatore"], []).append(row)
                for player_name, group in grouped.items():
                    # Il controllo di coerenza non deve cancellare righe dall'esportazione.
                    eligible = [r for r in group if bet365_line_allowed(r, STATS[market_label])]
                    _, excluded = auto_filter_suspicious_lines(eligible)
                    bad_keys = {(r["Giocatore"], float(r["Linea"])) for r in excluded}
                    for row in group:
                        line = float(row["Linea"])
                        if not bet365_line_allowed(row, STATS[market_label]):
                            reason = "Soglia fuori schema configurato" if line != 0.5 else "Linea 0,5 esclusa dalla classifica"
                        elif (player_name, line) in bad_keys:
                            reason = "Quote incoerenti tra soglie: verificare su Bet365"
                        else:
                            reason = ""
                        if reason:
                            filter_audit.append({"Partita": match_name, "Giocatore": player_name,
                                                 "Mercato": STATS[market_label], "Linea": line,
                                                 "Motivo": reason})
                        price = float(row["Over"])
                        quote_count += 1
                        props_by_player.setdefault(player_name, []).append((match_name, market_label, line, price, reason))
        except Exception as exc:
            issues.append(f"Quote non disponibili per {match_name}: {type(exc).__name__}")
    for idx, (player_name, props) in enumerate(props_by_player.items()):
        if progress_callback:
            progress_callback(0.25 + 0.75 * idx / max(len(props_by_player), 1),
                              f"ESPN: {idx + 1}/{len(props_by_player)} — {player_name}")
        athlete = index.get(normalized_name(player_name))
        games = None
        error = "Giocatore non identificato su ESPN" if athlete is None else ""
        if athlete is not None:
            try:
                previous_season = f"{int(season[:4])-1}-{str(int(season[:4]))[-2:]}"
                parts = []
                for reference_season in (season, previous_season):
                    part = selected_game_log(athlete["id"], reference_season, "Regular season", athlete["team_id"])
                    if not part.empty:
                        part = prepare_game_log(part, athlete["team_id"], reference_season, require_dates=False)
                        part["ANNO_STAGIONE"] = reference_season
                        parts.append(part)
                if not parts:
                    error = "Nessuna partita ESPN nelle ultime due stagioni"
                else:
                    games = pd.concat(parts, ignore_index=True)
                    games["GAME_DATE"] = pd.to_datetime(games["GAME_DATE"], errors="coerce", utc=True)
                    if games["GAME_DATE"].isna().any():
                        error = "Date ESPN incomplete: cronologia non verificabile"
                        games = None
                    else:
                        games = (games.drop_duplicates(subset=["GAME_ID"], keep="first")
                                      .sort_values("GAME_DATE", ascending=False).reset_index(drop=True))
                        player_logs[player_name] = games
            except Exception as exc:
                error = f"Dati ESPN non disponibili ({type(exc).__name__})"
                games = None
        if error:
            issues.append(f"{player_name}: {error}")
        for match_name, market_label, line, price, reason in props:
            stat = STATS[market_label]
            row = {"Partita": match_name, "Giocatore": player_name,
                   "Squadra": athlete.get("team_name", "Squadra non identificata") if athlete else "Squadra non identificata",
                   "Mercato": stat,
                   "Scelta": "Over", "Linea": line, "Quota": price,
                   "Prob. stimata %": None, "Prob. implicita %": round(100 / price, 1),
                   "Value stimato %": None, "Ultime 5": "—", "Ultime 10": "—",
                   "Ultime 20": "—", "Stagione %": None, "Partite": 0, "Stagione attuale (n)": 0, "Stagione precedente (n)": 0,
                   "Media": None, "Scarto medio": None, "Dev. standard": None,
                   "Minuti medi": None, "Minuti ultime 5": None,
                   "Stato analisi": "Non valutabile", "Nota": reason or error}
            if games is not None and not games.empty:
                details = finder_detailed_stats(games, stat, line)
                row.update(details)
                row["Stagione attuale (n)"] = int((games["ANNO_STAGIONE"] == season).sum())
                row["Stagione precedente (n)"] = int((games["ANNO_STAGIONE"] != season).sum())
                if len(games) < min_games:
                    row["Nota"] = "; ".join(filter(None, [reason, f"Solo {len(games)} partite ESPN (minimo {min_games})"]))
                elif not reason:
                    estimate = finder_estimate_probability(games, stat, line, "Over")
                    if estimate:
                        prob = estimate["estimate"]
                        row["Prob. stimata %"] = round(prob * 100, 1)
                        row["Value stimato %"] = round((prob * price - 1) * 100, 1)
                        row["Stato analisi"] = "Valutabile (stima empirica)"
                        row["Nota"] = "Stima empirica NON calibrata; verifica quota e disponibilità"
            results.append(row)
    if progress_callback:
        progress_callback(1.0, "Analisi completata")
    results.sort(key=lambda r: (r["Prob. stimata %"] is not None,
                                r["Prob. stimata %"] if r["Prob. stimata %"] is not None else -1), reverse=True)
    return results, issues, quote_count, len(props_by_player), raw_audit, filter_audit, player_logs




def _ratio_hits(value):
    """Parse observed hit count; leave missing/malformed values unavailable."""
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", str(value))
    if not match:
        return None
    hits, total = map(int, match.groups())
    return (hits, total) if total > 0 and 0 <= hits <= total else None


def rank_single_candidates(frame, minimum_odds=1.50, maximum_odds=2.10):
    """Transparent screening, NOT a validated predictive model."""
    if frame.empty:
        return frame.copy()
    data = frame[frame["Stato analisi"] == "Valutabile (stima empirica)"].copy()
    for column in ("Quota", "Prob. stimata %", "Partite", "Stagione %", "Minuti medi", "Minuti ultime 5"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["Quota", "Prob. stimata %"])
    data = data[data["Quota"].between(minimum_odds, maximum_odds)].copy()
    if data.empty:
        return data
    for window in (5, 10, 20):
        data[f"_h{window}"] = data[f"Ultime {window}"].map(_ratio_hits)
        data[f"_p{window}"] = data[f"_h{window}"].map(lambda x: x[0]/x[1] if x and x[1] == window else float('nan'))
    data["Prob. pareggio %"] = (100 / data["Quota"]).round(1)
    data["Margine stimato pp"] = (data["Prob. stimata %"] - data["Prob. pareggio %"]).round(1)
    data["Minuti stabili"] = (
        data["Minuti medi"].notna() & (data["Minuti medi"] >= 20) &
        data["Minuti ultime 5"].notna() &
        (data["Minuti ultime 5"] >= 0.8 * data["Minuti medi"])
    )
    data["Criteri rispettati"] = (
        (data["Prob. stimata %"] >= 70).astype(int) +
        (data["_p5"] >= 0.8).astype(int) +
        (data["_p10"] >= 0.8).astype(int) +
        (data["_p20"] >= 0.8).astype(int) +
        (data["Stagione %"] >= 70).astype(int) +
        (data["Partite"] >= 20).astype(int) +
        data["Minuti stabili"].astype(int) +
        (data["Margine stimato pp"] >= 3).astype(int)
    )
    data["Esito screening"] = data.apply(
        lambda row: "Candidata da verificare" if (
            row["Criteri rispettati"] == 8 and row["Prob. stimata %"] >= 75
        ) else "Non supera tutti i criteri", axis=1
    )
    return data.sort_values(
        ["Criteri rispettati", "Prob. stimata %", "Margine stimato pp", "Partite"],
        ascending=[False, False, False, False]
    ).drop(columns=["_h5", "_h10", "_h20", "_p5", "_p10", "_p20"])


def csv_excel_friendly(frame):
    """Prevent Excel interpreting 5/5 as a date in exported CSV."""
    result = frame.copy()
    for column in ("Ultime 5", "Ultime 10", "Ultime 20"):
        if column in result:
            result[column] = result[column].map(
                lambda value: str(value).replace("/", " su ") if _ratio_hits(value) else value
            )
    return result.to_csv(index=False).encode("utf-8-sig")

FINDER_MODEL_VERSION = "2.1-empirica-jeffreys-20261021"

def render_value_finder(fixtures, selected_date):
    st.subheader("🔎 NBA Value Finder — tutte le linee Over alternative")
    st.caption(f"Versione modello: **{FINDER_MODEL_VERSION}**")
    old = st.session_state.get("finder_results")
    if old and old.get("model_version") != FINDER_MODEL_VERSION:
        st.session_state.pop("finder_results", None)
        st.warning("Le analisi della versione precedente sono state invalidate. Premi Analizza per ricalcolare con la nuova formula; le quote già caricate in sessione vengono riutilizzate.")
    st.info("Il CSV completo include tutte le quote Over alternative attive ricevute dal feed, anche se non rispettano i filtri o non hanno statistiche ESPN sufficienti. Le probabilità sono **stime euristiche non calibrate**, non garanzie.")
    st.caption("La scansione riutilizza le quote delle partite già caricate in sessione. Per le altre può richiedere una chiamata OddsPapi per partita; non aggiorna automaticamente le quote.")
    season = st.selectbox("Stagione da analizzare", ["2026-27", "2025-26", "2024-25"], index=0, key="finder_season")
    c1, c2, c3 = st.columns(3)
    with c1:
        min_games = st.number_input("Partite minime per la stima", min_value=10, max_value=60, value=15, step=5)
    with c2:
        odds_min = st.number_input("Quota minima (solo classifica)", min_value=1.01, max_value=10.0, value=1.15, step=0.05)
    with c3:
        odds_max = st.number_input("Quota massima (solo classifica)", min_value=1.01, max_value=20.0, value=2.00, step=0.10)
    min_value = st.slider("Value minimo (solo classifica)", -100, 50, -100, 1)
    if odds_min > odds_max:
        st.warning("La quota minima deve essere inferiore o uguale alla massima.")
        return
    if st.button(f"🔍 Analizza tutte le {len(fixtures)} partite", type="primary", key="finder_start"):
        st.session_state.pop("finder_results", None)
        progress = st.progress(0, text="Preparazione analisi…")
        try:
            catalog = get_markets()
            results, issues, odds_count, player_count, raw_audit, filter_audit, player_logs = finder_scan(
                fixtures, catalog, season, int(min_games), float(odds_min), float(odds_max),
                int(min_value), lambda p, t: progress.progress(min(1.0, max(0.0, p)), text=t))
            st.session_state.finder_results = {
                "date": str(selected_date), "season": season, "model_version": FINDER_MODEL_VERSION,
                "results": results, "issues": issues, "odds_count": odds_count,
                "player_count": player_count, "raw_audit": raw_audit,
                "filter_audit": filter_audit}
        except Exception as exc:
            st.error(f"Scansione interrotta ({type(exc).__name__}). Quote già in sessione preservate.")
        finally:
            progress.empty()
    saved = st.session_state.get("finder_results")
    if not saved or saved["date"] != str(selected_date) or saved["season"] != season:
        return
    rows = saved["results"]
    st.caption(f"Modello {FINDER_MODEL_VERSION} · Linee nel CSV: {len(rows)} · Giocatori: {saved['player_count']} · Segnalazioni: {len(saved['issues'])}")
    if not rows:
        st.warning("Nessuna quota alternativa attiva restituita per le partite selezionate.")
        return
    df = pd.DataFrame(rows)
    st.download_button("⬇️ Scarica CSV COMPLETO — tutte le linee",
                       csv_excel_friendly(df),
                       file_name="nba_value_finder_tutte_le_linee_modello_2_1.csv", mime="text/csv")
    st.divider()
    st.subheader("🎯 Singola del giorno — screening statistico")
    st.caption("Tutti e tre i mercati: PTS, REB, AST. La classifica non garantisce vincite e NON verifica automaticamente infortuni, quintetti o quote live.")
    a, b = st.columns(2)
    with a:
        single_min = st.number_input("Quota minima singola", min_value=1.01, max_value=10.0, value=1.50, step=0.05, key="single_min")
    with b:
        single_max = st.number_input("Quota massima singola", min_value=1.01, max_value=20.0, value=2.10, step=0.05, key="single_max")
    if single_min <= single_max:
        ranked = rank_single_candidates(df, float(single_min), float(single_max))
        shortlist = ranked[ranked["Esito screening"] == "Candidata da verificare"] if not ranked.empty else ranked
        if shortlist.empty:
            st.warning("Nessuna singola supera tutti i criteri oggi. Non forzare una selezione.")
        else:
            pick = shortlist.iloc[0]
            st.success(f"Prima candidata da verificare: {pick['Giocatore']} · {pick['Scelta']} {pick['Linea']} {pick['Mercato']} · quota {pick['Quota']:.2f}")
            st.write(f"{pick['Partita']} · probabilità modello **{pick['Prob. stimata %']:.1f}%** (NON calibrata) · quota di pareggio **{pick['Prob. pareggio %']:.1f}%**")
            st.caption("Verifica disponibilità, minutaggio previsto, quote effettive e data del campione storico prima di considerare la selezione.")
            st.dataframe(shortlist.head(10), hide_index=True, width="stretch")
        if not ranked.empty:
            with st.expander(f"Classifica completa delle {len(ranked)} linee nella fascia quota scelta"):
                st.dataframe(ranked, hide_index=True, width="stretch")
            st.download_button("⬇️ Scarica screening singole CSV", csv_excel_friendly(ranked),
                               file_name="nba_stats_screening_singole.csv", mime="text/csv")
    else:
        st.warning("La quota minima deve essere inferiore o uguale alla massima.")
    st.divider()
    valid = df[df["Stato analisi"] == "Valutabile (stima empirica)"].copy()
    valid = valid[(valid["Quota"] >= odds_min) & (valid["Quota"] <= odds_max) &
                  (valid["Value stimato %"] >= min_value)]
    mode = st.radio("Ordina la classifica", ["Probabilità stimata", "Value stimato"], horizontal=True)
    key = "Prob. stimata %" if mode == "Probabilità stimata" else "Value stimato %"
    valid = valid.sort_values([key, "Partite"], ascending=False)
    st.write(f"**Top 10 — {mode.lower()}** (filtri applicati solo alla classifica)")
    st.dataframe(valid.head(10), hide_index=True, width="stretch")
    st.write(f"**Tutte le {len(df)} linee ricevute**, incluse quelle non valutabili")
    st.dataframe(df, hide_index=True, width="stretch")
    st.caption("Ultime 5/10/20 e stagione sono frequenze osservate. La probabilità è una stima empirica NON calibrata: 80% frequenza stagionale e 20% ultime 20, con regolarizzazione Beta(0,5; 0,5). Le ultime 20 sono incluse nella stagione. Minuti e variabilità sono mostrati ma NON modificano ancora la stima: serve validazione su più partite.")
    with st.expander("🔬 Diagnostica OddsPapi e linee segnalate"):
        audit = pd.DataFrame(saved.get("raw_audit", []))
        if not audit.empty:
            st.download_button("⬇️ Scarica feed diagnostico", audit.to_csv(index=False).encode("utf-8-sig"),
                               file_name="odds_feed_diagnostica.csv", mime="text/csv")
        if saved.get("filter_audit"):
            st.dataframe(pd.DataFrame(saved["filter_audit"]), hide_index=True, width="stretch")
    if saved["issues"]:
        with st.expander(f"⚠️ {len(saved['issues'])} segnalazioni ESPN/feed"):
            st.write("\n".join(f"- {item}" for item in saved["issues"][:100]))



# NBA STATS 3.0 — Presentation layer. The data collectors and probability estimator
# above are unchanged from 2.0. Screening is heuristic and not calibrated.
from io import BytesIO


def show_legacy_player(fixtures, selected_date):
    """Retain the existing player/market/ESPN diagnostics workflow."""
    options = {f"{f.get('participant1Name', '?')} – {f.get('participant2Name', '?')} | {fixture_start_rome(f).strftime('%d/%m/%Y %H:%M')} (Italia)": f for f in fixtures}
    choice = st.selectbox("Partita", list(options), key="player_fixture")
    fixture_id = str(options[choice]["fixtureId"])
    if "odds_by_fixture" not in st.session_state:
        st.session_state.odds_by_fixture = {}
    if "odds_refresh_versions" not in st.session_state:
        st.session_state.odds_refresh_versions = {}

    already_loaded = fixture_id in st.session_state.odds_by_fixture
    button_label = "🔄 Aggiorna quote" if already_loaded else "📥 Carica quote"
    refresh_clicked = st.button(button_label, type="primary")
    if not already_loaded and not refresh_clicked:
        st.info("Premi **Carica quote** per questa partita. Se torni su una partita già caricata, le quote ricompariranno automaticamente senza nuove chiamate API.")
        st.stop()

    market_label = st.selectbox("📊 Mercato giocatore", list(MARKETS))
    try:
        catalog = get_markets()
    except (RuntimeError, ValueError) as exc:
        st.error(f"Catalogo mercati non disponibile: {exc}")
        st.stop()

    if refresh_clicked:
        # Solo un clic esplicito genera una nuova versione della richiesta API.
        versions = st.session_state.odds_refresh_versions
        versions[fixture_id] = versions.get(fixture_id, 0) + 1
        try:
            incoming = get_odds(fixture_id, versions[fixture_id])
            # Una risposta valida per almeno uno dei tre mercati viene salvata.
            has_any_market = any(parse_odds(incoming, catalog, m)[0] for m in MARKETS)
            if has_any_market:
                st.session_state.odds_by_fixture[fixture_id] = incoming
                st.success("Quote caricate e memorizzate per questa partita.")
            elif already_loaded:
                st.warning("Il nuovo aggiornamento non contiene quote alternative utilizzabili. Mantengo le quote precedenti della partita.")
            else:
                st.warning("La risposta OddsPapi non contiene quote alternative utilizzabili. Puoi riprovare più tardi.")
        except (RuntimeError, ValueError, TypeError, AttributeError) as exc:
            if already_loaded:
                st.warning(f"Aggiornamento non riuscito ({exc}). Mantengo le quote precedenti.")
            else:
                st.error(f"Caricamento non riuscito: {exc}")

    odds = st.session_state.odds_by_fixture.get(fixture_id)
    if odds is None:
        st.stop()
    if not refresh_clicked:
        st.caption("Quote già caricate in questa sessione · Nessuna nuova chiamata API. Premi **Aggiorna quote** per richiederne di nuove.")
    try:
        rows, diag = parse_odds(odds, catalog, market_label)
    except (RuntimeError, ValueError, TypeError, AttributeError) as exc:
        st.error(f"Impossibile interpretare le quote salvate: {exc}")
        st.stop()
    if not rows:
        st.warning("Nessuna quota disponibile nel feed per questo mercato.")
        st.stop()
    player_name = st.selectbox("🏀 Giocatore", sorted({r["Giocatore"] for r in rows}))
    player_rows = [r for r in rows if r["Giocatore"] == player_name]
    st.subheader(f"{player_name} · {market_label}")
    st.caption("Solo mercati alternativi Over (+): 20+ equivale a Over 19,5. Mercati principali e Under esclusi.")
    st.caption("Controllo automatico attivo: linea 0,5 sempre esclusa; se una soglia inferiore ha una quota Over maggiore della successiva, viene esclusa. La disponibilità su Bet365 non è garantita.")
    removed_half = [r for r in player_rows if abs(float(r["Linea"]) - 0.5) < 1e-6]
    player_rows = [r for r in player_rows if abs(float(r["Linea"]) - 0.5) >= 1e-6]
    if removed_half:
        st.caption(f"Escluse {len(removed_half)} linee 0,5 (non disponibili su Bet365 secondo la verifica effettuata).")
    out_of_schema = [r for r in player_rows if not bet365_line_allowed(r, STATS[market_label])]
    player_rows = [r for r in player_rows if bet365_line_allowed(r, STATS[market_label])]
    player_rows, excluded_rows = auto_filter_suspicious_lines(player_rows)
    if excluded_rows:
        st.caption(f"Escluse automaticamente {len(excluded_rows)} linee con quote Over incoerenti.")
        with st.expander("🔎 Linee escluse per quote incoerenti"):
            st.dataframe(pd.DataFrame(excluded_rows)[["Linea", "Over", "Tipo"]], hide_index=True, width="stretch")
    if out_of_schema:
        with st.expander(f"📋 {len(out_of_schema)} soglie alternative fuori schema Bet365"):
            st.dataframe(pd.DataFrame(out_of_schema)[["Giocatore", "Linea", "Over", "Tipo"]],
                         hide_index=True, width="stretch")
            st.caption("Le soglie non previste dallo schema alternativo sono escluse.")
    if not player_rows:
        st.warning("Nessuna linea rimasta dopo i controlli automatici.")
        st.stop()
    frame = pd.DataFrame(player_rows).sort_values("Linea")
    st.dataframe(frame[["Linea", "Over"]],
        hide_index=True, width="stretch",
        column_config={"Linea": st.column_config.NumberColumn(format="%.1f"),
                       "Over": st.column_config.NumberColumn(format="%.3f")})
    st.download_button("⬇️ Scarica CSV quote", frame[["Giocatore", "Linea", "Over"]].to_csv(index=False).encode("utf-8-sig"),
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
                uncertain_games = espn_classified_game_log(nba_player["id"], season, nba_player.get("team_id")).attrs.get("unclassified", 0)
                if uncertain_games:
                    st.warning(f"ESPN non permette di identificare la competizione di {uncertain_games} partite. Queste partite sono escluse dall’analisi per evitare statistiche errate.")
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
                    st.caption(f"Linea {line:g} · Over: risultato > {line:g}")
                    for period_title, period_log in periods:
                        values = period_log[stat].dropna() if not period_log.empty else pd.Series(dtype=float)
                        count = len(values)
                        over = int((values > line).sum())
                        with st.container(border=True):
                            first, second = st.columns([2, 1])
                            with first:
                                st.write(f"**{period_title}** · {count} partite")
                                if count:
                                    st.markdown(
                                        f'<span style="display:inline-block;background:#dcfce7;color:#166534;padding:5px 12px;border-radius:20px;margin-right:8px;font-weight:600">'
                                        f'↑ {over/count:.0%} Over ({over}/{count})</span>',
                                        unsafe_allow_html=True,
                                    )
                                else:
                                    st.caption("Nessuna partita disponibile")
                            with second:
                                if count:
                                    st.metric(f"Media {stat}", f"{values.mean():.1f}",
                                              f"{values.mean()-line:+.1f} rispetto alla linea")
                    st.subheader(f"Dettaglio partite — {season} · {competition_mode}")
                    detail_columns = ["GAME_DATE", "MATCHUP", "MIN", "PTS", "REB", "AST"]
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


def get_saved_analysis(selected_date, season, min_games):
    saved = st.session_state.get("finder_results")
    if saved and (saved.get("date") != str(selected_date) or saved.get("season") != season or saved.get("model_version") != FINDER_MODEL_VERSION or saved.get("min_games") != int(min_games)):
        return None
    return saved


def start_scan(fixtures, selected_date, season, min_games):
    st.session_state.pop("finder_results", None)
    progress = st.progress(0, text="Preparazione analisi...")
    try:
        catalog = get_markets()
        # Pass broad bounds so the full unfiltered archive is preserved.
        results, issues, odds_count, player_count, raw_audit, filter_audit, player_logs = finder_scan(
            fixtures, catalog, season, int(min_games), 1.01, 20.0, -100,
            lambda p, t: progress.progress(min(1.0, max(0.0, p)), text=t))
        st.session_state.finder_results = {
            "date": str(selected_date), "season": season, "model_version": FINDER_MODEL_VERSION,
            "results": results, "issues": issues, "odds_count": odds_count,
            "player_count": player_count, "raw_audit": raw_audit, "filter_audit": filter_audit,
            "player_logs": player_logs, "scanned_at": datetime.now(ROME_TZ).strftime("%d/%m/%Y %H:%M"),
            "min_games": int(min_games)}
    except Exception as exc:
        # Avoid printing raw requests errors that might contain an API key.
        st.error(f"Analisi non completata ({type(exc).__name__}). Riprova.")
    finally:
        progress.empty()


def get_filtered_frame(saved, odd_min, odd_max, market, minimum_probability):
    df = pd.DataFrame(saved["results"])
    if df.empty:
        return df
    q = pd.to_numeric(df["Quota"], errors="coerce")
    p = pd.to_numeric(df["Prob. stimata %"], errors="coerce")
    mask = q.between(odd_min, odd_max) & p.ge(minimum_probability)
    if market != "Tutti":
        mask &= df["Mercato"].eq(market)
    return df.loc[mask].sort_values(["Prob. stimata %", "Partite"], ascending=False)


def show_pick(saved, odds_min, odds_max, market="Tutti", minimum_probability=0):
    df = pd.DataFrame(saved["results"])
    ranked = rank_single_candidates(df, odds_min, odds_max)
    if not ranked.empty:
        if market != "Tutti":
            ranked = ranked[ranked["Mercato"].eq(market)]
        ranked = ranked[ranked["Prob. stimata %"].ge(minimum_probability)]
    shortlist = ranked[ranked["Esito screening"] == "Candidata da verificare"] if not ranked.empty else ranked
    if shortlist.empty:
        st.info("Nessuna singola supera tutti gli 8 criteri dello screening. Le altre selezioni restano consultabili nella Classifica.")
        return
    pick = shortlist.iloc[0]
    with st.container(border=True, key="daily_pick_card"):
        st.caption("PRIMA CANDIDATA DA VERIFICARE · NON È UNA RACCOMANDAZIONE AUTOMATICA")
        st.subheader(f"{pick['Giocatore']} · {pick['Scelta']} {pick['Linea']:g} {pick['Mercato']}")
        st.caption(str(pick["Partita"]))
        a,b,c=st.columns(3)
        a.metric("Quota feed", f"{pick['Quota']:.2f}")
        b.metric("Stima statistica*", f"{pick['Prob. stimata %']:.1f}%")
        c.metric("Ultime 20", str(pick["Ultime 20"]))
        hits = _ratio_hits(pick.get("Ultime 20", ""))
        n = int(pick.get("Partite", 0) or 0)
        current_n = int(pick.get("Stagione attuale (n)", 0) or 0)
        if hits:
            st.caption(f"Frequenza osservata ultime 20: {hits[0]}/{hits[1]} ({100*hits[0]/hits[1]:.1f}%).")
        st.caption(f"Base statistica: {n} partite complessive, di cui {current_n} nella stagione attuale. Affidabilità predittiva: NON ancora validata.")
        with st.expander("Come leggere la stima statistica"):
            st.write("La stima combina l'80% della frequenza Over complessiva e il 20% della frequenza delle ultime 20 partite, entrambe regolarizzate con Jeffreys Beta(0,5; 0,5). Le ultime 20 sono già comprese nello storico complessivo.")
            st.write("Non è una probabilità reale verificata: non integra automaticamente avversario, infortuni, variazioni di ruolo o minutaggio previsto. Un campione più grande non garantisce una previsione accurata.")
        st.caption("*Stima euristica non calibrata. Verifica squadra, disponibilità, minutaggio e quota reale.")
    if len(shortlist)>1:
        st.caption("Altre candidate che superano lo screening")
        columns=[x for x in ["Giocatore","Mercato","Linea","Quota","Prob. stimata %","Ultime 20","Partita"] if x in shortlist]
        st.dataframe(shortlist.iloc[1:6][columns],hide_index=True,use_container_width=True)


def show_history():
    st.subheader("Storico delle giocate")
    st.caption("Registro locale alla sessione. Scarica il CSV per conservare i dati: Streamlit non garantisce memoria permanente tra riavvii.")
    if "bets_history" not in st.session_state:
        st.session_state.bets_history = [
            {"Data":"2026-10-21","Giocatore":"Tre Jones","Mercato":"PTS","Linea":9.5,"Quota":1.83,"Puntata":10.0,"Esito":"Vinta"},
            {"Data":"2026-10-22","Giocatore":"Jaylen Brown","Mercato":"PTS","Linea":19.5,"Quota":1.80,"Puntata":10.0,"Esito":"In attesa"}]
    with st.expander("Aggiungi una giocata"):
        with st.form("new_bet",clear_on_submit=True):
            a,b=st.columns(2)
            date=a.date_input("Data giocata",value=datetime.now(ROME_TZ).date())
            player=b.text_input("Giocatore")
            market=st.selectbox("Mercato",["PTS","REB","AST"])
            a,b,c=st.columns(3)
            line=a.number_input("Linea",min_value=0.5,step=1.0,value=9.5)
            odd=b.number_input("Quota",min_value=1.01,step=0.01,value=1.80)
            stake=c.number_input("Puntata (€)",min_value=0.01,step=1.0,value=10.0)
            outcome=st.selectbox("Esito",["In attesa","Vinta","Persa","Annullata"])
            if st.form_submit_button("Salva in questa sessione") and player.strip():
                st.session_state.bets_history.append({"Data":str(date),"Giocatore":player.strip(),"Mercato":market,"Linea":line,"Quota":odd,"Puntata":stake,"Esito":outcome})
                st.rerun()
    with st.expander("Importa registro CSV"):
        upload=st.file_uploader("CSV esportato da NBA STATS 3.0",type="csv",key="history_import")
        if upload and st.button("Importa registro"):
            try:
                incoming=pd.read_csv(upload)
                required={"Data","Giocatore","Mercato","Linea","Quota","Puntata","Esito"}
                if not required.issubset(incoming.columns):
                    st.error("Il CSV non contiene tutte le colonne richieste.")
                else:
                    st.session_state.bets_history=incoming[list(required)].to_dict("records")
                    st.rerun()
            except (ValueError,UnicodeDecodeError,pd.errors.ParserError):
                st.error("Impossibile leggere il CSV.")
    bets=pd.DataFrame(st.session_state.bets_history)
    bets["Quota"]=pd.to_numeric(bets["Quota"],errors="coerce")
    bets["Puntata"]=pd.to_numeric(bets["Puntata"],errors="coerce")
    bets["Profitto €"]=bets.apply(lambda r: round(r["Puntata"]*(r["Quota"]-1),2) if r["Esito"]=="Vinta" else (-r["Puntata"] if r["Esito"]=="Persa" else 0.0),axis=1)
    settled=bets[bets["Esito"].isin(["Vinta","Persa"])]
    a,b,c=st.columns(3)
    a.metric("Chiuse",len(settled))
    b.metric("Bilancio realizzato",f"{settled['Profitto €'].sum():+.2f} €")
    c.metric("ROI chiuse",f"{100*settled['Profitto €'].sum()/settled['Puntata'].sum():+.1f}%" if settled['Puntata'].sum() else "—")
    st.dataframe(bets,hide_index=True,use_container_width=True)
    st.download_button("Scarica registro CSV",csv_excel_friendly(bets),file_name="nba_stats_registro.csv",mime="text/csv")
    st.caption("Le due giocate iniziali sono inserite dal registro discusso in chat; gli esiti non sono verificati automaticamente dal sito.")



# NBA STATS 3.1 — layout e navigazione centralizzata
st.markdown("""
<style>
/* NBA STATS 3.2: tema coerente, alto contrasto e controlli leggibili. */
:root { color-scheme: dark; }
html, body, [data-testid="stAppViewContainer"], .stApp,
[data-testid="stMain"], [data-testid="stMainBlockContainer"] {
    background: #0b1220 !important; color: #edf4ff !important;
}
/* Header retained for sidebar navigation on desktop and mobile. */
[data-testid="stHeader"] { background: #0b1220 !important; min-height: 2.6rem !important; }
#MainMenu, [data-testid="stToolbar"], [data-testid="stDecoration"],
[data-testid="stFooter"], footer, [data-testid="manage-app-button"],
[data-testid="stStatusWidget"] { display: none !important; }
[data-testid="stDecoration"] { display: none !important; }
/* Preserve the sidebar toggle and leave room for the NBA STATS title. */
.block-container, [data-testid="stMainBlockContainer"] {
    max-width: 1390px !important;
    padding: 4rem 2.2rem 3rem !important;
}
[data-testid="stSidebar"], [data-testid="stSidebarContent"] {
    background: #142238 !important; border-right: 1px solid #34445d !important;
}
[data-testid="stSidebar"] { min-width: 292px !important; }
[data-testid="stSidebar"] *:not(svg):not(path),
[data-testid="stSidebar"] label, [data-testid="stSidebar"] p,
[data-testid="stSidebar"] span, [data-testid="stSidebar"] small,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    color: #e2eaf7 !important;
}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p { color: #b8c8de !important; }
[data-testid="stSidebar"] hr { border-color: #33445f !important; }
[data-testid="stSidebar"] [data-testid="stExpander"] {
    background: #1a2c46 !important; border: 1px solid #3b5271 !important;
    border-radius: 10px !important;
}
/* Tipografia: contrasto e gerarchia visiva */
h1, h2, h3, h4, h5, h6 { color: #f8fbff !important; letter-spacing: -.025em !important; }
h1 { font-size: clamp(1.75rem, 2.8vw, 2.4rem) !important; }
h2 { font-size: 1.6rem !important; }
h3 { font-size: 1.28rem !important; }
.stApp p, .stApp label, .stApp li, .stApp span, .stApp div[data-testid="stMarkdownContainer"] {
    color: #e2eaf7;
}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p,
[data-testid="stCaptionContainer"] span { color: #b9c9de !important; opacity: 1 !important; }
/* Inputs: background scuro anche con tema Streamlit chiaro configurato */
[data-testid="stDateInput"] input,
[data-testid="stNumberInput"] input,
[data-testid="stTextInput"] input,
[data-testid="stSelectbox"] [data-baseweb="select"],
[data-testid="stSelectbox"] [data-baseweb="select"] > div,
[data-testid="stNumberInput"] [data-baseweb="input"],
[data-testid="stDateInput"] [data-baseweb="input"] {
    background: #233753 !important; color: #fff !important;
    border-color: #506888 !important; border-radius: 8px !important;
}
[data-testid="stDateInput"] input, [data-testid="stNumberInput"] input,
[data-testid="stTextInput"] input, [data-testid="stSelectbox"] input,
[data-testid="stSelectbox"] span { color: #fff !important; -webkit-text-fill-color: #fff !important; }
[data-testid="stNumberInput"] button, [data-testid="stDateInput"] button {
    background: #2b4262 !important; color: #fff !important;
}
[data-testid="stSelectbox"] svg, [data-testid="stDateInput"] svg,
[data-testid="stNumberInput"] svg { fill: #e2eaf7 !important; color: #e2eaf7 !important; }
/* NBA STATS 3.3: campi e popup a sfondo chiaro con testo SCURO, senza combinazioni invisibili. */
[data-testid="stSidebar"] [data-baseweb="input"],
[data-testid="stSidebar"] [data-baseweb="input"] > div,
[data-testid="stSidebar"] [data-baseweb="select"],
[data-testid="stSidebar"] [data-baseweb="select"] > div,
[data-testid="stSidebar"] input,
[data-testid="stMain"] [data-baseweb="select"],
[data-testid="stMain"] [data-baseweb="select"] > div,
[data-testid="stMain"] [data-baseweb="input"],
[data-testid="stMain"] [data-baseweb="input"] > div,
[data-testid="stMain"] input {
    background: #f8fafc !important; color: #12243b !important;
    -webkit-text-fill-color: #12243b !important; border-color: #b7c6d8 !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] *,
[data-testid="stMain"] [data-baseweb="select"] *,
[data-testid="stSidebar"] [data-baseweb="input"] input,
[data-testid="stMain"] [data-baseweb="input"] input {
    color: #12243b !important; -webkit-text-fill-color: #12243b !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] svg,
[data-testid="stMain"] [data-baseweb="select"] svg,
[data-testid="stDateInput"] svg { fill: #334e70 !important; color: #334e70 !important; }
[data-testid="stNumberInput"] button { background: #243e60 !important; color: #fff !important; }
[data-testid="stNumberInput"] button svg { color: #fff !important; fill: #fff !important; }
/* Calendario e menu a comparsa BaseWeb (renderizzati fuori dal contenitore Streamlit). */
[data-baseweb="popover"], [data-baseweb="popover"] *,
[data-baseweb="calendar"], [data-baseweb="calendar"] *,
[data-baseweb="menu"], [data-baseweb="menu"] * {
    color: #14243b !important; -webkit-text-fill-color: #14243b !important;
}
[data-baseweb="popover"], [data-baseweb="calendar"], [data-baseweb="menu"],
[data-baseweb="popover"] [role="listbox"], [data-baseweb="calendar"] > div {
    background: #f8fafc !important;
}
[data-baseweb="calendar"] button, [data-baseweb="calendar"] select {
    color: #14243b !important; -webkit-text-fill-color: #14243b !important;
}
[data-baseweb="calendar"] [aria-selected="true"],
[data-baseweb="calendar"] [aria-selected="true"] * {
    background: #2563eb !important; color: #fff !important; -webkit-text-fill-color: #fff !important;
}
[data-baseweb="menu"] li:hover, [data-baseweb="menu"] [role="option"]:hover { background: #dbeafe !important; }
[data-testid="stSidebar"] [data-testid="stExpander"] summary,
[data-testid="stSidebar"] [data-testid="stExpander"] summary * {
    background: #243c5b !important; color: #f8fafc !important; -webkit-text-fill-color: #f8fafc !important;
}
/* Migliore leggibilità del testo nelle tabelle Streamlit. */
[data-testid="stDataFrame"] { background: #fff !important; }
/* Slider: indicazioni sempre leggibili */
[data-testid="stSlider"] [data-testid="stTickBar"] { color: #e2eaf7 !important; }
/* Metriche e card */
[data-testid="stMetric"] {
    background: #172a43 !important; border: 1px solid #334e70 !important;
    border-radius: 14px !important; padding: 1rem 1.15rem !important;
}
[data-testid="stMetricLabel"] *, [data-testid="stMetricLabel"] { color: #b8c9df !important; }
[data-testid="stMetricValue"] *, [data-testid="stMetricValue"] { color: #fff !important; }
[data-testid="stVerticalBlockBorderWrapper"] > div { border-color: #385272 !important; }
/* Pulsanti e azioni */
.stButton button[kind="primary"], [data-testid="stBaseButton-primary"] {
    background: #2874ed !important; border: 1px solid #5192ff !important;
    color: #fff !important; font-weight: 700 !important;
    border-radius: 10px !important; min-height: 2.8rem !important;
}
.stButton button[kind="primary"] *, [data-testid="stBaseButton-primary"] * { color: #fff !important; }
.stButton button:hover { filter: brightness(1.13); }
.stDownloadButton button, [data-testid="stBaseButton-secondary"] {
    border-radius: 9px !important; background: #203654 !important;
    border: 1px solid #4c6687 !important; color: #f3f7ff !important;
}
.stDownloadButton button * { color: #f3f7ff !important; }
/* Tabelle e pannelli di messaggio */
[data-testid="stDataFrame"], [data-testid="stTable"] { border: 1px solid #324b69; border-radius: 10px; }
[data-testid="stAlert"] { border-radius: 10px !important; }
[data-testid="stAlert"] p, [data-testid="stAlert"] span { color: #edf4ff !important; }
/* Navigazione orizzontale più compatta */
[data-testid="stRadio"] [role="radiogroup"] { gap: .65rem !important; flex-wrap: wrap !important; }
[data-testid="stRadio"] [role="radiogroup"] label {
    padding: .45rem .85rem !important; background: #1b304d !important;
    border: 1px solid #3c5577 !important; border-radius: 9px !important;
}
[data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) {
    background: #234d8c !important; border-color: #5c9bff !important;
}
[data-testid="stRadio"] [role="radiogroup"] label p { color: #f5f9ff !important; }
@media (max-width: 760px) {
    .block-container, [data-testid="stMainBlockContainer"] { padding: 3.5rem .9rem 2.5rem !important; }
    h1 { font-size: 1.65rem !important; }
}
/* NBA STATS 3.4: calendario con testo leggibile, anche nei giorni non selezionati. */
[data-baseweb="calendar"], [data-baseweb="calendar"] > div,
[data-baseweb="calendar"] [role="grid"],
[data-baseweb="calendar"] [role="gridcell"],
[data-baseweb="calendar"] [role="columnheader"] {
    background-color: #f8fafc !important; color: #14243b !important;
    -webkit-text-fill-color: #14243b !important;
}
[data-baseweb="calendar"] button,
[data-baseweb="calendar"] button span,
[data-baseweb="calendar"] [role="gridcell"] *,
[data-baseweb="calendar"] [role="columnheader"] * {
    color: #14243b !important; -webkit-text-fill-color: #14243b !important;
}
[data-baseweb="calendar"] [aria-selected="true"],
[data-baseweb="calendar"] [aria-selected="true"] *,
[data-baseweb="calendar"] button[aria-selected="true"],
[data-baseweb="calendar"] button[aria-selected="true"] * {
    background: #2563eb !important; color: #ffffff !important;
    -webkit-text-fill-color: #ffffff !important;
}
[data-baseweb="calendar"] [aria-disabled="true"],
[data-baseweb="calendar"] [aria-disabled="true"] * { color: #77869b !important; -webkit-text-fill-color: #77869b !important; }
/* Rimuove l'effetto bianco su bianco causato dai vecchi selettori generici. */
[data-testid="stSidebar"] [data-testid="stSelectbox"] [data-baseweb="select"] span,
[data-testid="stSidebar"] [data-testid="stDateInput"] input,
[data-testid="stMain"] [data-testid="stSelectbox"] [data-baseweb="select"] span {
    color: #14243b !important; -webkit-text-fill-color: #14243b !important;
}
/* Evidenzia solo la card della singola del giorno, non tutte le tabelle. */
.st-key-daily_pick_card, [data-testid="stVerticalBlockBorderWrapper"]:has(.st-key-daily_pick_card) {
    border: 2px solid #60a5fa !important;
    border-radius: 16px !important;
    background: #14243b !important;
    box-shadow: 0 0 0 1px rgba(96,165,250,.15), 0 10px 32px rgba(0,0,0,.22) !important;
}
/* NBA STATS 3.5: override finale e SPECIFICO del campo data.
   Evita che i selettori generici della sidebar prevalgano sul valore del calendario. */
[data-testid="stSidebar"] [data-testid="stDateInput"] [data-baseweb="input"],
[data-testid="stSidebar"] [data-testid="stDateInput"] [data-baseweb="input"] > div,
[data-testid="stSidebar"] [data-testid="stDateInput"] input,
[data-testid="stSidebar"] [data-testid="stDateInput"] input:focus,
[data-testid="stSidebar"] [data-testid="stDateInput"] input:disabled {
    background-color: #ffffff !important;
    color: #14243b !important;
    -webkit-text-fill-color: #14243b !important;
    opacity: 1 !important;
    caret-color: #14243b !important;
}
[data-testid="stSidebar"] [data-testid="stDateInput"] input::placeholder {
    color: #475569 !important;
    -webkit-text-fill-color: #475569 !important;
    opacity: 1 !important;
}
[data-testid="stSidebar"] [data-testid="stDateInput"] button,
[data-testid="stSidebar"] [data-testid="stDateInput"] button svg {
    color: #14243b !important;
    fill: #14243b !important;
}
/* Popover calendario BaseWeb: contrasto e selezione, anche fuori dalla sidebar. */
[data-baseweb="calendar"] button:not([aria-selected="true"]),
[data-baseweb="calendar"] button:not([aria-selected="true"]) span {
    color: #14243b !important;
    -webkit-text-fill-color: #14243b !important;
}
[data-baseweb="calendar"] [aria-selected="true"],
[data-baseweb="calendar"] [aria-selected="true"] * {
    color: #ffffff !important;
    -webkit-text-fill-color: #ffffff !important;
}
</style>
""", unsafe_allow_html=True)

if not API_KEY:
    st.error("Configura ODDSPAPI_API_KEY nei Secrets di Streamlit.")
    st.stop()

with st.sidebar:
    st.markdown("### ⚙️ Impostazioni")
    selected_date = st.date_input("Data delle partite (Italia)", value=datetime.now(ROME_TZ).date())
    st.caption("Storico ESPN automatico: stagione attuale + precedente, con ultime 5/10/20 combinate.")
    st.divider()
    st.markdown("**Filtri delle selezioni**")
    odds_min = st.number_input("Quota minima", min_value=1.01, max_value=20.0, value=1.60, step=0.05)
    odds_max = st.number_input("Quota massima", min_value=1.01, max_value=100.0, value=2.00, step=0.05)
    market = st.selectbox("Mercato", ["Tutti", "PTS", "REB", "AST"])
    minimum_probability = st.slider("Probabilità minima mostrata (%)", 0, 100, 0, 1)
    # Soglia interna fissa: evita stime su campioni troppo piccoli senza appesantire la UI.
    min_games = 15
    st.caption("I filtri non eliminano quote dal CSV completo.")
    st.caption(f"Modello {FINDER_MODEL_VERSION} · Stime non calibrate")

if odds_min > odds_max:
    st.error("La quota minima non può superare quella massima.")
    st.stop()

# NBA regular season starts in autumn: choose the end year of the current NBA season.
def current_nba_season(day):
    start = day.year if day.month >= 10 else day.year - 1
    return f"{start}-{str(start + 1)[-2:]}"

season = current_nba_season(selected_date)

st.markdown("# 🏀 NBA STATS | PRO ANALYTICS")
st.caption("VALUE FINDER 3.4  ·  OddsPapi Bet365 + ESPN  ·  Analisi NBA")
page = st.radio("Sezioni", ["🏠 Dashboard", "🎯 Value Finder", "📊 Player Stats"], horizontal=True, label_visibility="collapsed")

# Recupero calendario condiviso; la scansione delle quote avviene SOLO al clic.
try:
    fixtures = [f for f in get_fixtures(selected_date.isoformat()) if not fixture_is_completed(f)]
except (RuntimeError, ValueError):
    fixtures = []
    st.warning("Calendario temporaneamente non disponibile.")

st.markdown("### 🔎 Centro di analisi")
left, middle, right = st.columns([1.7, 2, 2])
with left:
    scan_clicked = st.button("🔍 ANALIZZA LE PARTITE", type="primary", use_container_width=True)
with middle:
    st.caption(f"Partite: {selected_date.strftime('%d/%m/%Y')} · Stagioni ESPN: {season} + {int(season[:4])-1}-{str(int(season[:4]))[-2:]}")
with right:
    st.caption("Una scansione per Dashboard, Value Finder e Player Stats")

if scan_clicked:
    if not fixtures:
        st.warning("Non ci sono partite future disponibili per la data selezionata.")
    else:
        start_scan(fixtures, selected_date, season, min_games)

saved = get_saved_analysis(selected_date, season, min_games)
if saved:
    st.caption(f"✅ Analisi disponibile · acquisita il {saved.get('scanned_at', '—')} (Italia). Cambiare i filtri non richiama OddsPapi.")
else:
    st.info("Premi ANALIZZA LE PARTITE per caricare una volta sola quote e statistiche nelle tre sezioni.")

st.divider()
if page == "🏠 Dashboard":
    st.subheader("La singola del giorno")
    st.caption("Il modello mostra una candidata solo se supera lo screening. Può anche non proporne nessuna.")
    if saved:
        df = pd.DataFrame(saved["results"])
        a, b, c, d = st.columns(4)
        a.metric("Partite in programma", len(fixtures))
        b.metric("Linee totali", len(df))
        c.metric("Giocatori", saved["player_count"])
        d.metric("Segnalazioni", len(saved["issues"]))
        if not df.empty:
            show_pick(saved, odds_min, odds_max, market, minimum_probability)
            st.caption("Per tutte le quote e gli export apri Value Finder.")

elif page == "🎯 Value Finder":
    st.subheader("Classifica delle linee Over")
    st.caption("PTS, REB e AST in un'unica tabella. I filtri si applicano solo alla vista e al CSV filtrato.")
    if saved:
        df = pd.DataFrame(saved["results"])
        if not df.empty:
            filtered = get_filtered_frame(saved, odds_min, odds_max, market, minimum_probability)
            st.caption(f"{len(filtered)} selezioni visibili su {len(df)} linee complessive")
            cols = [c for c in ["Giocatore", "Squadra", "Partita", "Mercato", "Linea", "Quota", "Prob. stimata %", "Ultime 5", "Ultime 10", "Ultime 20", "Partite", "Stagione attuale (n)", "Stagione precedente (n)", "Value stimato %"] if c in filtered.columns]
            st.dataframe(filtered[cols], hide_index=True, use_container_width=True, height=490)
            a,b = st.columns(2)
            a.download_button("⬇️ CSV filtrato", csv_excel_friendly(filtered), file_name="nba_stats_3_1_filtrato.csv", mime="text/csv", disabled=filtered.empty, use_container_width=True)
            b.download_button("⬇️ CSV completo", csv_excel_friendly(df), file_name="nba_stats_3_1_completo.csv", mime="text/csv", use_container_width=True)
            with st.expander("🎯 Screening avanzato delle singole"):
                ranked = rank_single_candidates(df, odds_min, odds_max)
                if not ranked.empty:
                    st.dataframe(ranked, hide_index=True, use_container_width=True)
                    st.download_button("⬇️ CSV screening", csv_excel_friendly(ranked), file_name="nba_stats_3_1_screening.csv", mime="text/csv")
                else:
                    st.info("Nessuna candidata valutabile nella fascia quota.")
            with st.expander("🔬 Diagnostica tecnica"):
                if saved.get("issues"):
                    st.write("\n".join(f"- {issue}" for issue in saved["issues"][:100]))
                if saved.get("filter_audit"):
                    st.dataframe(pd.DataFrame(saved["filter_audit"]), hide_index=True, use_container_width=True)
                if saved.get("raw_audit"):
                    st.download_button("Scarica diagnostica feed", pd.DataFrame(saved["raw_audit"]).to_csv(index=False).encode("utf-8-sig"), file_name="nba_stats_diagnostica.csv", mime="text/csv")
        else:
            st.warning("La scansione non ha restituito linee.")

else:
    st.subheader("Player Stats")
    st.caption("Statistiche e grafici ottenuti dalla stessa scansione: nessuna nuova chiamata API cambiando giocatore o linea.")
    if saved:
        logs = saved.get("player_logs", {})
        if not logs:
            st.info("Nessun log ESPN disponibile dalla scansione.")
        else:
            # La squadra deriva dai roster ESPN già scaricati nella scansione; nessuna nuova chiamata API.
            player_team = {}
            for item in saved["results"]:
                name = item.get("Giocatore")
                team = item.get("Squadra") or "Squadra non identificata"
                if name in logs:
                    player_team[name] = team
            teams = sorted(set(player_team.values()))
            team_choice = st.selectbox("Squadra", ["Tutte le squadre"] + teams, key="player_team_filter")
            players = sorted(name for name in logs if team_choice == "Tutte le squadre" or player_team.get(name) == team_choice)
            if not players:
                st.info("Nessun giocatore disponibile per questa squadra nella scansione.")
                st.stop()
            player = st.selectbox("Giocatore", players, key="player_name_filter")
            stat = st.selectbox("Statistica", ["PTS", "REB", "AST"])
            related = pd.DataFrame(saved["results"])
            related = related[(related["Giocatore"] == player) & (related["Mercato"] == stat)]
            choices = sorted(pd.to_numeric(related["Linea"], errors="coerce").dropna().unique().tolist())
            line = st.selectbox("Linea Over", choices) if choices else st.number_input("Linea Over", min_value=0.5, value=9.5, step=1.0)
            log = logs[player].copy().sort_values("GAME_DATE", ascending=False)
            if not log.empty:
                previous_season = f"{int(season[:4])-1}-{str(int(season[:4]))[-2:]}"
                current_log = log[log["ANNO_STAGIONE"] == season]
                previous_log = log[log["ANNO_STAGIONE"] == previous_season]
                periods = [
                    ("Ultime 5 (combinate)", log.head(5)),
                    ("Ultime 10 (combinate)", log.head(10)),
                    ("Ultime 20 (combinate)", log.head(20)),
                    (f"Stagione {season}", current_log),
                    (f"Stagione {previous_season}", previous_log),
                ]
                summary = pd.DataFrame([analysis_row(title, data, stat, float(line)) for title, data in periods])
                st.dataframe(summary, hide_index=True, use_container_width=True)
                st.caption("Le percentuali Over della tabella sono frequenze osservate, non probabilità predittive validate.")
                estimate = finder_estimate_probability(log, stat, float(line), "Over")
                if estimate:
                    st.metric("Stima statistica modello (non calibrata)", f"{100*estimate['estimate']:.1f}%")
                    with st.expander("Come viene calcolata questa stima?"):
                        st.write("80% frequenza Over complessiva + 20% frequenza ultime 20, con regolarizzazione Beta(0,5; 0,5). La stima non tiene automaticamente conto del prossimo avversario o del ruolo attuale.")
                        st.write(f"Partite considerate: {estimate['n']}; ultime partite nella componente recente: {estimate['n_recent']}. Affidabilità predittiva: non ancora validata.")
                else:
                    st.caption("Stima modello non disponibile: servono almeno 15 partite valide e una linea Over a metà punto.")
                a,b = st.columns(2)
                a.metric(f"Partite {season}", len(current_log))
                b.metric(f"Partite {previous_season}", len(previous_log))
                st.markdown("#### Ultime 20 partite · Over/Under")
                chart = log.head(20).iloc[::-1].copy().reset_index(drop=True)
                chart["Partita"] = chart["GAME_DATE"].dt.strftime("%d/%m")
                chart["Indice"] = range(1, len(chart) + 1)
                chart["Esito"] = chart[stat].apply(lambda x: "OVER" if x > float(line) else "UNDER")
                chart["Etichetta"] = chart[stat].map(lambda x: f"{x:g}")
                chart["Stagione"] = chart["ANNO_STAGIONE"].astype(str)
                base = alt.Chart(chart).encode(
                    x=alt.X("Indice:O", title="Partite dalla meno recente alla più recente", axis=alt.Axis(labelAngle=0)),
                    tooltip=[alt.Tooltip("Partita:N", title="Data"), alt.Tooltip("Stagione:N"),
                             alt.Tooltip(f"{stat}:Q", title=stat), alt.Tooltip("Esito:N")],
                )
                bars = base.mark_bar(size=26, cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
                    y=alt.Y(f"{stat}:Q", title=stat, scale=alt.Scale(zero=True)),
                    color=alt.Color("Esito:N", scale=alt.Scale(domain=["OVER", "UNDER"], range=["#19c99a", "#f06a79"]),
                                    legend=alt.Legend(title="Risultato")),
                )
                labels = base.mark_text(dy=-10, color="#eaf2ff", fontSize=12, fontWeight="bold").encode(
                    y=alt.Y(f"{stat}:Q"), text="Etichetta:N"
                )
                threshold = alt.Chart(pd.DataFrame({"Soglia": [float(line)]})).mark_rule(
                    color="#f5b942", strokeDash=[6, 4], strokeWidth=2.5
                ).encode(y="Soglia:Q")
                st.altair_chart((bars + threshold + labels).properties(height=340), use_container_width=True)
                st.caption("Verde: Over · Rosa: Under · Linea gialla tratteggiata: soglia selezionata. Ultime 20 combinate in ordine cronologico.")
                with st.expander("Dettaglio delle partite ESPN"):
                    columns = [c for c in ["GAME_DATE", "ANNO_STAGIONE", "MATCHUP", "MIN", "PTS", "REB", "AST"] if c in log.columns]
                    st.dataframe(log[columns], hide_index=True, use_container_width=True)
                st.caption("Il grafico usa le ultime 20 partite disponibili delle due stagioni. La linea è una soglia, non una previsione.")

st.divider()
st.caption("Quote e statistiche possono cambiare. Le probabilità sono euristiche non calibrate: verifica sempre disponibilità e quota reale.")
