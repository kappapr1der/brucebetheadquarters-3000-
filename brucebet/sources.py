from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = "BruceBetHQ/0.1 (+https://github.com/kappapr1der/brucebetheadquarters-3000-)"


@dataclass(frozen=True)
class SourceConfig:
    the_odds_api_key: str = ""
    api_football_key: str = ""
    football_data_token: str = ""
    thesportsdb_key: str = "123"
    timeout: int = 20


@dataclass(frozen=True)
class SourceCheck:
    name: str
    ok: bool
    detail: str
    configured: bool = True


def _request_json(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> tuple[object, object]:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
        return payload, response.headers


def _request_text(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(4096).decode("utf-8", errors="replace")


def _safe_check(name: str, configured: bool, func) -> SourceCheck:
    if not configured:
        return SourceCheck(name=name, ok=False, detail="key/env is missing", configured=False)
    try:
        return func()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:180].replace("\n", " ")
        return SourceCheck(name=name, ok=False, detail=f"HTTP {exc.code}: {body}", configured=configured)
    except urllib.error.URLError as exc:
        return SourceCheck(name=name, ok=False, detail=f"network: {exc.reason}", configured=configured)
    except (TimeoutError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return SourceCheck(name=name, ok=False, detail=f"{type(exc).__name__}: {exc}", configured=configured)


def check_the_odds_api(config: SourceConfig) -> SourceCheck:
    def run() -> SourceCheck:
        url = "https://api.the-odds-api.com/v4/sports/?" + urllib.parse.urlencode({"apiKey": config.the_odds_api_key})
        payload, headers = _request_json(url, timeout=config.timeout)
        count = len(payload) if isinstance(payload, list) else 0
        remaining = headers.get("x-requests-remaining")
        used = headers.get("x-requests-used")
        return SourceCheck("The Odds API", True, f"sports={count}; remaining={remaining}; used={used}")

    return _safe_check("The Odds API", bool(config.the_odds_api_key), run)


def check_api_football(config: SourceConfig) -> SourceCheck:
    def run() -> SourceCheck:
        payload, _ = _request_json(
            "https://v3.football.api-sports.io/status",
            headers={"x-apisports-key": config.api_football_key},
            timeout=config.timeout,
        )
        response = payload.get("response", {}) if isinstance(payload, dict) else {}
        requests = response.get("requests", {}) if isinstance(response, dict) else {}
        current = requests.get("current") if isinstance(requests, dict) else None
        limit_day = requests.get("limit_day") if isinstance(requests, dict) else None
        return SourceCheck("API-Football", True, f"requests={current}/{limit_day}")

    return _safe_check("API-Football", bool(config.api_football_key), run)


def check_football_data_org(config: SourceConfig) -> SourceCheck:
    def run() -> SourceCheck:
        payload, _ = _request_json(
            "https://api.football-data.org/v4/competitions/PL",
            headers={"X-Auth-Token": config.football_data_token},
            timeout=config.timeout,
        )
        name = payload.get("name") if isinstance(payload, dict) else None
        code = payload.get("code") if isinstance(payload, dict) else None
        return SourceCheck("football-data.org", True, f"{code}: {name}")

    return _safe_check("football-data.org", bool(config.football_data_token), run)


def check_thesportsdb(config: SourceConfig) -> SourceCheck:
    def run() -> SourceCheck:
        key = config.thesportsdb_key or "123"
        url = f"https://www.thesportsdb.com/api/v1/json/{urllib.parse.quote(key)}/searchteams.php?" + urllib.parse.urlencode({"t": "Arsenal"})
        payload, _ = _request_json(url, timeout=config.timeout)
        teams = payload.get("teams") if isinstance(payload, dict) else None
        count = len(teams) if isinstance(teams, list) else 0
        return SourceCheck("TheSportsDB", True, f"teams={count}; key={'custom' if key != '123' else 'free-123'}")

    return _safe_check("TheSportsDB", bool(config.thesportsdb_key or "123"), run)


def check_fpl(config: SourceConfig) -> SourceCheck:
    def run() -> SourceCheck:
        payload, _ = _request_json("https://fantasy.premierleague.com/api/bootstrap-static/", timeout=config.timeout)
        if not isinstance(payload, dict):
            return SourceCheck("FPL", False, "unexpected payload")
        teams = len(payload.get("teams") or [])
        players = len(payload.get("elements") or [])
        return SourceCheck("FPL", True, f"teams={teams}; players={players}")

    return _safe_check("FPL", True, run)


def check_football_data_csv(config: SourceConfig) -> SourceCheck:
    def run() -> SourceCheck:
        text = _request_text("https://www.football-data.co.uk/mmz4281/2425/E0.csv", timeout=config.timeout)
        header = text.splitlines()[0] if text else ""
        columns = len(header.split(",")) if header else 0
        return SourceCheck("Football-Data.co.uk", bool(columns), f"columns={columns}")

    return _safe_check("Football-Data.co.uk", True, run)


def check_clubelo(config: SourceConfig) -> SourceCheck:
    def run() -> SourceCheck:
        text = _request_text("http://api.clubelo.com/Arsenal", timeout=config.timeout)
        lines = [line for line in text.splitlines() if line.strip()]
        return SourceCheck("ClubElo", len(lines) > 1, f"rows={max(0, len(lines) - 1)}")

    return _safe_check("ClubElo", True, run)


def check_open_meteo(config: SourceConfig) -> SourceCheck:
    def run() -> SourceCheck:
        params = {
            "latitude": "51.5549",
            "longitude": "-0.1084",
            "hourly": "temperature_2m,precipitation,wind_speed_10m",
            "forecast_days": "1",
        }
        payload, _ = _request_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params), timeout=config.timeout)
        hourly = payload.get("hourly", {}) if isinstance(payload, dict) else {}
        points = len(hourly.get("time") or []) if isinstance(hourly, dict) else 0
        return SourceCheck("Open-Meteo", bool(points), f"hourly_points={points}")

    return _safe_check("Open-Meteo", True, run)


def check_wikidata(config: SourceConfig) -> SourceCheck:
    def run() -> SourceCheck:
        query = "ASK { wd:Q9617 wdt:P31 ?type . }"
        url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": query, "format": "json"})
        payload, _ = _request_json(url, timeout=config.timeout)
        value = payload.get("boolean") if isinstance(payload, dict) else None
        return SourceCheck("Wikidata", value is True, f"ask={value}")

    return _safe_check("Wikidata", True, run)


def check_all_sources(config: SourceConfig) -> list[SourceCheck]:
    return [
        check_the_odds_api(config),
        check_api_football(config),
        check_football_data_org(config),
        check_thesportsdb(config),
        check_fpl(config),
        check_football_data_csv(config),
        check_clubelo(config),
        check_open_meteo(config),
        check_wikidata(config),
    ]
