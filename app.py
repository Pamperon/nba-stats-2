"""NBA STATS 2.0: OddsPapi Bet365 + free ESPN player game logs."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re
import logging
import traceback
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="NBA STATS 2.0", page_icon="🏀", layout="wide")
logger = logging.getLogger("nba_stats_diagnostics")
BASE = "https://api.oddspapi.io/v4"
ROME_TZ = ZoneInfo("Europe/Rome")
MARKETS = {
    "Punti (PTS)": ("playertotals-points", "players-points"),
    "Rimbalzi (REB)": ("playertotals-rebounds", "players-rebounds"),
    "Assist (AST)": ("playertotals-assists", "players-assists"),
}
STATS = {"Punti (PTS)": "PTS", "Rimbalzi (REB)": "REB", "Assist (AST)": "AST"}
# Soglie alternative Bet365 verificate dall'utente. Il mercato principale
# Over/Under può usare linee diverse e viene conservato separatamente.
BET365_ALT_LINES = {
    "PTS": {float(n) + 0.5 for n in range(4, 100, 5)},
    "REB": {2.5, 4.5, 6.5, 9.5, 12.5, 14.5, 16.5, 19.5},
    "AST": {2.5, 4.5, 6.5, 9.5, 12.5, 14.5},
}


def bet365_line_allowed(row, stat):
    # Le soglie fornite si riferiscono ai mercati alternativi (+).
    # Le linee principali Over/Under non devono essere eliminate per errore.
    if abs(float(row["Linea"]) - 0.5) < 1e-6:
        return False
    if row.get("Tipo") == "Over/Under":
        return True
    return round(float(row["Linea"]), 2) in BET365_ALT_LINES[stat]


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


@st.cache_data(ttl=86400, show_spinner="Calendario NBA…")
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

    Compare adjacent Over quotes, repeating after each removal. Preserve Under-only
    rows. A monotonic feed is not proof that Bet365 currently offers the quote.
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
    """Heuristic, regular-season only. Not a calibrated predictive model."""
    if log.empty or stat not in log.columns:
        return None
    log = log.copy()
    log[stat] = pd.to_numeric(log[stat], errors="coerce")
    log["GAME_DATE"] = pd.to_datetime(log["GAME_DATE"], errors="coerce", utc=True)
    log = log.dropna(subset=[stat, "GAME_DATE"]).sort_values("GAME_DATE", ascending=False)
    if len(log) < 15:
        return None
    values = log[stat]
    # Half-point markets have no pushes. Skip other lines to avoid settlement ambiguity.
    if abs(line % 1 - 0.5) > 1e-6:
        return None
    wins = (values > line) if side == "Over" else (values < line)
    season_rate = float(wins.mean())
    recent = wins.head(min(20, len(wins)))
    recent_rate = float(recent.mean())
    # Bayesian smoothing toward 50% plus modest recency adjustment.
    # This is a ranking heuristic, NOT a statistically calibrated probability.
    smooth = (float(wins.sum()) + 12.0) / (len(wins) + 24.0)
    estimate = 0.75 * smooth + 0.25 * recent_rate
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
                              "full_name": athlete["displayName"]})
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


def finder_scan(fixtures, catalog, season, min_games, odds_min, odds_max,
                min_value, progress_callback=None):
    """One cached OddsPapi odds request per fixture, three markets per response.

    ESPN classification is cached per athlete/season and reused for all markets.
    """
    results, issues = [], []
    players_index = finder_player_index(int(season[:4]) + 1)
    odds_rows = []
    raw_audit = []
    filter_audit = []
    for idx, fixture in enumerate(fixtures):
        fid = str(fixture["fixtureId"])
        match_name = f"{fixture.get('participant1Name', '?')} - {fixture.get('participant2Name', '?')}"
        if progress_callback:
            progress_callback(idx / max(len(fixtures), 1) * 0.25, f"Quote: {match_name}")
        try:
            odds = get_odds(fid, st.session_state.odds_refresh_versions.get(fid, 0))
            raw_audit.extend(odds_source_audit(odds, catalog, match_name))
            for market_label in MARKETS:
                parsed, _ = parse_odds(odds, catalog, market_label)
                grouped = {}
                for row in parsed:
                    grouped.setdefault(row["Giocatore"], []).append(row)
                for player_name, group in grouped.items():
                    removed_half = [r for r in group if abs(float(r["Linea"]) - 0.5) < 1e-6]
                    for row in removed_half:
                        filter_audit.append({"Partita": match_name, "Giocatore": player_name,
                                             "Mercato": STATS[market_label], "Linea": row["Linea"],
                                             "Motivo": "Linea 0,5 esclusa per scelta: non disponibile su Bet365"})
                    filtered, excluded = auto_filter_suspicious_lines(
                        [r for r in group if abs(float(r["Linea"]) - 0.5) >= 1e-6])
                    for row in excluded:
                        filter_audit.append({"Partita": match_name, "Giocatore": player_name,
                                             "Mercato": STATS[market_label], "Linea": row["Linea"],
                                             "Motivo": "Quota Over della linea inferiore maggiore della linea successiva"})
                    for row in filtered:
                        if not bet365_line_allowed(row, STATS[market_label]):
                            filter_audit.append({"Partita": match_name, "Giocatore": player_name,
                                                 "Mercato": STATS[market_label], "Linea": row["Linea"],
                                                 "Motivo": "Soglia alternativa fuori schema Bet365 verificato"})
                            continue
                        for side in ("Over", "Under"):
                            price = row.get(side)
                            if isinstance(price, (int, float)) and odds_min <= price <= odds_max:
                                odds_rows.append((match_name, player_name, market_label, row["Linea"], side, float(price)))
        except Exception as exc:
            issues.append(f"Quote non disponibili per {match_name}: {type(exc).__name__}")
    # Process each athlete only once, regardless of number of props and lines.
    grouped_props = {}
    for match_name, player_name, market_label, line, side, price in odds_rows:
        grouped_props.setdefault(player_name, []).append((match_name, market_label, line, side, price))
    total = len(grouped_props)
    for idx, (player_name, props) in enumerate(grouped_props.items()):
        if progress_callback:
            progress_callback(0.25 + 0.75 * idx / max(total, 1),
                              f"ESPN: {idx + 1}/{total} giocatori — {player_name}")
        athlete = players_index.get(normalized_name(player_name))
        if athlete is None:
            issues.append(f"Giocatore non identificato: {player_name}")
            continue
        try:
            games = selected_game_log(athlete["id"], season, "Regular season", athlete["team_id"])
            if games.empty:
                continue
            games = prepare_game_log(games, athlete["team_id"], season, require_dates=False)
        except Exception as exc:
            issues.append(f"Statistiche non disponibili: {player_name} ({type(exc).__name__})")
            continue
        if games["GAME_DATE"].isna().any() or len(games) < min_games:
            continue
        for match_name, market_label, line, side, price in props:
            stat = STATS[market_label]
            estimate = finder_estimate_probability(games, stat, float(line), side)
            if not estimate or estimate["n"] < min_games:
                continue
            ev = estimate["estimate"] * price - 1.0
            if ev < min_value / 100.0:
                continue
            results.append({
                "Partita": match_name, "Giocatore": player_name,
                "Mercato": stat, "Scelta": side, "Linea": float(line),
                "Quota": price, "Prob. stimata %": round(100 * estimate["estimate"], 1),
                "Prob. implicita %": round(100 / price, 1),
                "Value stimato %": round(100 * ev, 1),
                "Ultime 20": f"{estimate['recent_hits']}/{estimate['n_recent']}",
                "Stagione %": round(100 * estimate["season"], 1),
                "Partite": estimate["n"], "Media": round(estimate["avg"], 1),
            })
    if progress_callback:
        progress_callback(1.0, "Analisi completata")
    results.sort(key=lambda x: (x["Value stimato %"], x["Partite"]), reverse=True)
    return results, issues, len(odds_rows), total, raw_audit, filter_audit


def render_value_finder(fixtures, selected_date):
    st.subheader("🔎 NBA Value Finder — opportunità della giornata")
    st.info("Classifica sperimentale basata sui risultati passati: non è una previsione affidabile né una garanzia di valore reale. Le quote potrebbero essere cambiate.")
    st.caption("La scansione usa le quote OddsPapi delle partite selezionate e le statistiche ESPN di regular season. Una richiesta quote per partita non già in cache; nessun aggiornamento automatico.")
    season = st.selectbox("Stagione da analizzare", ["2026-27", "2025-26", "2024-25"], index=1, key="finder_season")
    c1, c2, c3 = st.columns(3)
    with c1:
        min_games = st.number_input("Partite minime", min_value=10, max_value=60, value=20, step=5)
    with c2:
        odds_min = st.number_input("Quota minima", min_value=1.01, max_value=10.0, value=1.15, step=0.05)
    with c3:
        odds_max = st.number_input("Quota massima", min_value=1.01, max_value=20.0, value=2.00, step=0.10)
    min_value = st.slider("Value stimato minimo (%)", min_value=-100, max_value=50, value=-100, step=1, help="-100% mostra anche le selezioni con Value negativo; 0% mostra solo quelle con Value non negativo.")
    st.caption("Le linee 0,5 sono escluse come richiesto. Con Value minimo -100% vengono mostrate anche le selezioni a Value negativo, purché superino gli altri controlli. La disponibilità reale su Bet365 non è certificata dal feed.")
    st.caption("Il Value è una stima euristica: usa frequenze stagionali corrette verso il 50% e andamento recente. Un Value negativo indica un rendimento teorico sfavorevole secondo il modello, non una quota da giocare. Non include infortuni, avversario, quintetti o casa/trasferta.")
    if odds_min > odds_max:
        st.warning("La quota minima deve essere inferiore o uguale alla quota massima.")
        return
    if st.button(f"🔍 Analizza tutte le {len(fixtures)} partite", type="primary", key="finder_start"):
        st.session_state.pop("finder_results", None)
        progress = st.progress(0, text="Preparazione analisi…")
        try:
            catalog = get_markets()
            results, issues, odds_count, player_count, raw_audit, filter_audit = finder_scan(
                fixtures, catalog, season, int(min_games), float(odds_min), float(odds_max),
                int(min_value), lambda p, t: progress.progress(min(1.0, max(0.0, p)), text=t))
            st.session_state.finder_results = {
                "date": str(selected_date), "season": season,
                "results": results, "issues": issues,
                "odds_count": odds_count, "player_count": player_count,
                "raw_audit": raw_audit, "filter_audit": filter_audit,
            }
        except Exception as exc:
            st.error(f"Scansione interrotta ({type(exc).__name__}). Le quote già in cache restano disponibili.")
        finally:
            progress.empty()
    saved = st.session_state.get("finder_results")
    if not saved or saved["date"] != str(selected_date) or saved["season"] != season:
        return
    st.caption(f"Quote candidate: {saved['odds_count']} · Giocatori esaminati: {saved['player_count']} · "
               f"Linee escluse dai controlli: {len(saved.get('filter_audit', []))} · "
               f"Segnalazioni: {len(saved['issues'])}")
    if saved["results"]:
        df = pd.DataFrame(saved["results"])
        st.write(f"**{len(df)} selezioni analizzate** (ordinate per Value stimato; clicca sulle intestazioni per cambiare ordine)")
        st.dataframe(df, hide_index=True, width="stretch")
        st.download_button("⬇️ Scarica risultati CSV", df.to_csv(index=False).encode("utf-8-sig"),
                           file_name="nba_value_finder.csv", mime="text/csv")
    else:
        st.warning("Nessuna selezione supera i filtri, oppure i dati ESPN disponibili sono insufficienti.")
    with st.expander("🔬 Diagnostica originale OddsPapi — linee e stati del feed"):
        st.warning("'Attivo nel feed' NON significa 'disponibile su Bet365'. "
                   "Questa diagnostica mostra esattamente quali esiti e identificativi sono stati ricevuti, "
                   "senza effettuare nuove richieste OddsPapi.")
        audit_df = pd.DataFrame(saved.get("raw_audit", []))
        if not audit_df.empty:
            suspicious = audit_df.loc[audit_df["Linea interpretata"] <= 0.5]
            st.write(f"**Esiti con linea 0,5 nel feed:** {len(suspicious)}")
            st.dataframe(suspicious, hide_index=True, width="stretch")
            st.download_button("⬇️ Scarica diagnostica completa CSV",
                               audit_df.to_csv(index=False).encode("utf-8-sig"),
                               file_name="odds_feed_diagnostica.csv", mime="text/csv")
        else:
            st.info("Nessun esito disponibile per la diagnostica.")
        if saved.get("filter_audit"):
            st.write("**Linee escluse dai controlli (coerenza e schema):**")
            st.dataframe(pd.DataFrame(saved["filter_audit"]), hide_index=True, width="stretch")
    if saved["issues"]:
        with st.expander(f"⚠️ Dettagli: {len(saved['issues'])} giocatori/partite non elaborati"):
            st.write("\n".join(f"- {issue}" for issue in saved["issues"][:100]))


st.title("🏀 NBA STATS 2.0")
st.caption("Quote Bet365 Italia tramite OddsPapi · Statistiche NBA da ESPN (fonte gratuita)")
st.info("Le quote provengono da OddsPapi. Le anomalie vengono segnalate e documentate con una diagnostica del feed; la disponibilità effettiva va verificata su Bet365.")
if not API_KEY:
    st.error("Configura ODDSPAPI_API_KEY nei Secrets di Streamlit.")
    st.stop()

selected_date = st.date_input("Data delle partite (ora italiana)", value=datetime.now(ROME_TZ).date())
try:
    fixtures = get_fixtures(selected_date.isoformat())
except (RuntimeError, ValueError) as exc:
    st.error(f"Calendario non disponibile: {exc}")
    st.stop()
if not fixtures:
    st.warning("Nessuna partita NBA trovata per questa data.")
    st.stop()
options = {f"{f.get('participant1Name', '?')} – {f.get('participant2Name', '?')} | {fixture_start_rome(f).strftime('%d/%m/%Y %H:%M')} (Italia)": f for f in fixtures}
choice = st.selectbox("🏟️ Partita NBA", list(options))
fixture_id = str(options[choice]["fixtureId"])
st.caption(f"Partite NBA trovate: {len(fixtures)} · Date e orari in Europe/Rome (ora italiana)")
section = st.radio("Sezione", ["📊 Analisi singola partita", "🔎 NBA Value Finder"], horizontal=True)
if section == "🔎 NBA Value Finder":
    if "odds_refresh_versions" not in st.session_state:
        st.session_state.odds_refresh_versions = {}
    render_value_finder(fixtures, selected_date)
    st.stop()

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
st.caption("Controllo automatico attivo: linea 0,5 sempre esclusa; se una soglia inferiore ha una quota Over maggiore della successiva, viene esclusa. La disponibilità su Bet365 non è garantita.")
removed_half = [r for r in player_rows if abs(float(r["Linea"]) - 0.5) < 1e-6]
player_rows = [r for r in player_rows if abs(float(r["Linea"]) - 0.5) >= 1e-6]
if removed_half:
    st.caption(f"Escluse {len(removed_half)} linee 0,5 (non disponibili su Bet365 secondo la verifica effettuata).")
player_rows, excluded_rows = auto_filter_suspicious_lines(player_rows)
if excluded_rows:
    st.caption(f"Escluse automaticamente {len(excluded_rows)} linee con quote Over incoerenti.")
    with st.expander("🔎 Linee escluse per quote incoerenti"):
        st.dataframe(pd.DataFrame(excluded_rows)[["Linea", "Over", "Under", "Tipo"]], hide_index=True, width="stretch")
out_of_schema = [r for r in player_rows if not bet365_line_allowed(r, STATS[market_label])]
player_rows = [r for r in player_rows if bet365_line_allowed(r, STATS[market_label])]
if out_of_schema:
    with st.expander(f"📋 {len(out_of_schema)} soglie alternative fuori schema Bet365"):
        st.dataframe(pd.DataFrame(out_of_schema)[["Giocatore", "Linea", "Over", "Under", "Tipo"]],
                     hide_index=True, width="stretch")
        st.caption("Le soglie principali Over/Under rimangono visibili anche se diverse dalle soglie alternative.")
if not player_rows:
    st.warning("Nessuna linea rimasta dopo i controlli automatici.")
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
