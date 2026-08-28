from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import sys

from .analytics import (
    calendar_matches,
    capture_model_forecasts,
    compare_participants,
    compute_standings,
    edge_map,
    field_summary,
    finalize_completed_rounds,
    hq_summary,
    intelligence_readiness,
    match_header,
    missing_forecasts_summary,
    model_calibration_summary,
    match_dossier,
    next_calendar_match,
    player_status_summary,
    prediction_is_eligible,
    prediction_views_for_match,
    ready_summary,
    recommend_match,
    risk_map,
    round_deadlines,
    round_review,
    strategy_summary,
    team_profile,
)
from .forecast_import import ForecastImportReport, import_forecast_block
from .scoring import is_standard_score, normalize_score, parse_datetime, parse_score
from .odds_api import (
    DEFAULT_ODDS_BOOKMAKER,
    DEFAULT_ODDS_MARKETS,
    DEFAULT_ODDS_REGIONS,
    DEFAULT_ODDS_SPORT,
    OddsApiError,
    TheOddsApiClient,
    sync_odds_to_db,
)
from .pl_fixtures import (
    DEFAULT_PL_COMPSEASON_ID,
    DEFAULT_PL_SEASON_LABEL,
    PremierLeagueApiError,
    sync_pl_fixtures_to_db,
    sync_pl_results_to_db,
)
from .rehearsal import run_rehearsal
from .sources import SourceConfig, check_all_sources
from .snapshot import export_snapshot
from .storage import (
    activate_profile,
    active_season,
    connect,
    find_match,
    import_absences,
    import_match_assessments,
    import_match_contexts,
    import_match_odds,
    import_matches,
    import_participants,
    import_player_statuses,
    import_predictions,
    import_team_form,
    import_team_match_factors,
    import_teams,
    init_db,
    manual_result_history,
    reset_db,
    set_manual_match_result,
    upsert_absence,
)
from .variable_sync import VariableSyncResult, sync_match_assessments, sync_match_contexts_and_factors, sync_match_variables
from .vk_parser import parse_file as parse_vk_file
from .vk_board import VkBrowserError, probe_public_group_topics
from .vk_dry_run import read_public_topic_dry_run, render_dry_run_report


DEFAULT_DB = "brucebet.sqlite"


def open_db(args: argparse.Namespace, reset: bool = False):
    conn = connect(args.db)
    if reset:
        reset_db(conn)
    else:
        init_db(conn)
    activate_profile(
        conn,
        competition_code=args.competition,
        season_name=args.season,
        season_display_name=args.season_display,
        lock_minutes=args.lock_minutes,
    )
    return conn


def print_rows(headers: list[str], rows: list[list[object]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))
    fmt = "  ".join(f"{{:<{width}}}" for width in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * width for width in widths]))
    for row in rows:
        print(fmt.format(*[str(value) for value in row]))


def clean(value: object) -> str:
    return "" if value is None else str(value)


def print_key_values(items: list[tuple[str, object]]) -> None:
    rows = [[key, clean(value)] for key, value in items if value is not None and clean(value) != ""]
    if rows:
        print_rows(["field", "value"], rows)


def odds_api_key() -> str:
    return os.getenv("THE_ODDS_API_KEY", "").strip()


def env_default(name: str, fallback: str) -> str:
    return os.getenv(name, fallback).strip() or fallback


def print_odds_quota(remaining: int | None, used: int | None, last: int | None) -> None:
    print_key_values(
        [
            ("requests_remaining", remaining),
            ("requests_used", used),
            ("requests_last", last),
        ]
    )


def variable_sync_rows(result: VariableSyncResult) -> list[tuple[str, object]]:
    rows: list[tuple[str, object]] = [
        ("updated_at", result.updated_at),
        ("fpl_players_seen", result.fpl_players_seen),
        ("fpl_players_imported", result.fpl_players_imported),
        ("fpl_teams_matched", result.fpl_teams_matched),
        ("form_matches_seen", result.form_matches_seen),
        ("form_rows_upserted", result.form_rows_upserted),
        ("form_teams_matched", result.form_teams_matched),
        ("elo_teams_checked", result.elo_teams_checked),
        ("elo_teams_updated", result.elo_teams_updated),
        ("contexts_upserted", result.contexts_upserted),
        ("factors_upserted", result.factors_upserted),
        ("weather_checked", result.weather_checked),
        ("weather_updated", result.weather_updated),
        ("weather_skipped", result.weather_skipped),
        ("assessments_upserted", result.assessments_upserted),
    ]
    if result.fpl_unmatched_teams:
        rows.append(("fpl_unmatched_teams", ", ".join(result.fpl_unmatched_teams[:12])))
    if result.form_fallback_teams:
        rows.append(("form_fallback_teams", ", ".join(result.form_fallback_teams[:12])))
    if result.form_unmatched_teams:
        rows.append(("form_unmatched_teams", ", ".join(result.form_unmatched_teams[:12])))
    if result.elo_unmatched_teams:
        rows.append(("elo_unmatched_teams", ", ".join(result.elo_unmatched_teams[:12])))
    if result.errors:
        rows.append(("errors", " | ".join(result.errors)))
    return rows


def source_config_from_env(timeout: int = 20) -> SourceConfig:
    return SourceConfig(
        the_odds_api_key=os.getenv("THE_ODDS_API_KEY", "").strip(),
        api_football_key=os.getenv("API_FOOTBALL_KEY", "").strip(),
        football_data_token=os.getenv("FOOTBALL_DATA_TOKEN", "").strip(),
        thesportsdb_key=os.getenv("THESPORTSDB_KEY", "123").strip() or "123",
        timeout=timeout,
    )


def print_risk_map(item: dict[str, object]) -> None:
    labels = [("safe", "Safe"), ("slippery", "Slippery"), ("risk", "Risk"), ("unknown", "Unknown")]
    print(f"Round: {clean(item.get('round_name'))}")
    for key, title in labels:
        rows = [
            [
                row["position"],
                row["label"],
                row["top_outcome"],
                row["top_share"],
                row["predictions"],
                row["suggested_score"],
            ]
            for row in item.get(key, [])
        ]
        print()
        print(f"{title}:")
        print_rows(["#", "match", "top", "share", "n", "base"], rows)


def print_calendar_items(items: list[object]) -> None:
    print_rows(
        ["round", "#", "match", "kickoff", "deadline", "status", "mine", "field", "result"],
        [
            [
                item.round_name,
                item.position,
                item.label,
                clean(item.kickoff_at.isoformat() if item.kickoff_at else None),
                clean(item.deadline_at.isoformat() if item.deadline_at else None),
                item.status,
                "yes" if item.my_prediction_count else "no",
                item.prediction_count,
                clean(item.result),
            ]
            for item in items
        ],
    )


def freshness_rows(items: dict[str, dict[str, object]]) -> list[list[object]]:
    return [
        [key, clean(value["updated_at"]), clean(value["age_minutes"])]
        for key, value in items.items()
    ]


def cmd_init(args: argparse.Namespace) -> int:
    conn = open_db(args, reset=args.reset)
    season = active_season(conn)
    print(f"Database ready: {args.db}")
    print(f"Active profile: {season['competition_code']} {season['name']}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    conn = open_db(args, reset=args.reset)
    totals = []
    if args.participants:
        totals.append(f"participants={import_participants(conn, args.participants)}")
    if args.teams:
        totals.append(f"teams={import_teams(conn, args.teams)}")
    if args.matches:
        totals.append(f"matches={import_matches(conn, args.matches)}")
    if args.predictions:
        totals.append(f"predictions={import_predictions(conn, args.predictions)}")
    if args.team_form:
        totals.append(f"team_form={import_team_form(conn, args.team_form)}")
    if args.absences:
        totals.append(f"absences={import_absences(conn, args.absences)}")
    if args.player_statuses:
        totals.append(f"player_statuses={import_player_statuses(conn, args.player_statuses)}")
    if args.contexts:
        totals.append(f"contexts={import_match_contexts(conn, args.contexts)}")
    if args.odds:
        totals.append(f"odds={import_match_odds(conn, args.odds)}")
    if args.factors:
        totals.append(f"factors={import_team_match_factors(conn, args.factors)}")
    if args.assessments:
        totals.append(f"assessments={import_match_assessments(conn, args.assessments)}")
    print("Imported " + ", ".join(totals))
    return 0


def cmd_load_sample(args: argparse.Namespace) -> int:
    base = Path(__file__).resolve().parents[1] / "examples"
    conn = open_db(args, reset=True)
    import_participants(conn, base / "participants.csv")
    import_teams(conn, base / "teams.csv")
    import_matches(conn, base / "matches.csv")
    import_predictions(conn, base / "predictions.csv")
    import_team_form(conn, base / "team_form.csv")
    import_absences(conn, base / "absences.csv")
    import_player_statuses(conn, base / "player_statuses.csv")
    import_match_contexts(conn, base / "match_contexts.csv")
    import_match_odds(conn, base / "match_odds.csv")
    import_team_match_factors(conn, base / "team_match_factors.csv")
    import_match_assessments(conn, base / "match_assessments.csv")
    print(f"Sample data loaded into {args.db}")
    return 0


def cmd_table(args: argparse.Namespace) -> int:
    conn = open_db(args)
    standings = compute_standings(conn, entry_fee_rub=args.entry_fee, lock_minutes=args.lock_minutes)
    rows = [
        [
            item.rank,
            item.name,
            item.total,
            item.exact_hits,
            item.diff_hits,
            item.outcome_hits,
            item.late,
            "yes" if item.paid else "no",
            item.prize_rub,
        ]
        for item in standings
    ]
    print_rows(
        ["#", "name", "pts", "exact", "diff", "outcome", "late", "paid", "prize"],
        rows,
    )
    return 0


def cmd_match(args: argparse.Namespace) -> int:
    conn = open_db(args)
    match = find_match(conn, args.query)
    print(match_header(match))
    views = prediction_views_for_match(conn, int(match["id"]), lock_minutes=args.lock_minutes)
    rows = [[view.participant, view.score, view.category, view.points] for view in views]
    print_rows(["participant", "score", "category", "pts"], rows)
    return 0


def cmd_field(args: argparse.Namespace) -> int:
    conn = open_db(args)
    match = find_match(conn, args.query)
    print(match_header(match))
    summary = field_summary(conn, int(match["id"]))
    print("Outcomes:")
    print_rows(["outcome", "count"], [[key, value] for key, value in summary["outcomes"].most_common()])
    print()
    print("Scores:")
    print_rows(["score", "count"], [[key, value] for key, value in summary["scores"].most_common()])
    return 0


def cmd_recommend(args: argparse.Namespace) -> int:
    conn = open_db(args)
    match = find_match(conn, args.query)
    item = recommend_match(conn, int(match["id"]))
    print(match_header(item["match"]))
    print_key_values(
        [
            ("suggested_score", item["suggested_score"]),
            ("risk_level", item["risk_level"]),
            ("confidence", item["confidence"]),
            ("top_outcome_share", item["top_outcome_share"]),
            ("consensus_note", item["consensus_note"]),
            ("contrarian_note", item["contrarian_note"]),
        ]
    )
    print()
    print("Outcomes:")
    print_rows(["outcome", "count"], [[key, value] for key, value in item["outcomes"].most_common()])
    print()
    print("Popular scores:")
    print_rows(["score", "count"], [[key, value] for key, value in item["scores"].most_common(8)])
    return 0


def cmd_deadlines(args: argparse.Namespace) -> int:
    conn = open_db(args)
    rows = []
    for item in round_deadlines(conn, lock_minutes=args.lock_minutes):
        rows.append(
            [
                item.round_name,
                clean(item.first_kickoff_at.isoformat() if item.first_kickoff_at else None),
                clean(item.stored_deadline_at.isoformat() if item.stored_deadline_at else None),
                clean(item.computed_deadline_at.isoformat() if item.computed_deadline_at else None),
                clean(item.effective_deadline_at.isoformat() if item.effective_deadline_at else None),
            ]
        )
    print_rows(["round", "first_kickoff", "stored_deadline", "computed_deadline", "effective"], rows)
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    conn = open_db(args)
    items = calendar_matches(
        conn,
        days=args.days,
        user_participant=args.user,
        lock_minutes=args.lock_minutes,
        round_name=args.round,
        limit=args.limit,
        include_unknown_kickoff=args.include_unknown,
    )
    print_calendar_items(items)
    return 0


def cmd_today(args: argparse.Namespace) -> int:
    conn = open_db(args)
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    items = calendar_matches(
        conn,
        days=1,
        user_participant=args.user,
        lock_minutes=args.lock_minutes,
        start_at=today,
        limit=args.limit,
    )
    print_calendar_items(items)
    return 0


def cmd_week(args: argparse.Namespace) -> int:
    conn = open_db(args)
    items = calendar_matches(
        conn,
        days=7,
        user_participant=args.user,
        lock_minutes=args.lock_minutes,
        limit=args.limit,
    )
    print_calendar_items(items)
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    conn = open_db(args)
    item = next_calendar_match(conn, user_participant=args.user, lock_minutes=args.lock_minutes)
    if item is None:
        print("No upcoming matches with kickoff_at.")
        return 0
    print_key_values(
        [
            ("round", item.round_name),
            ("position", item.position),
            ("match", item.label),
            ("kickoff", item.kickoff_at.isoformat() if item.kickoff_at else None),
            ("deadline", item.deadline_at.isoformat() if item.deadline_at else None),
            ("status", item.status),
            ("your_forecast", "yes" if item.my_prediction_count else "no"),
            ("field_predictions", item.prediction_count),
            ("result", item.result),
        ]
    )
    return 0


def cmd_round_calendar(args: argparse.Namespace) -> int:
    conn = open_db(args)
    start = datetime(1900, 1, 1).astimezone()
    items = calendar_matches(
        conn,
        days=60000,
        user_participant=args.user,
        lock_minutes=args.lock_minutes,
        round_name=args.round,
        start_at=start,
        limit=args.limit,
        include_unknown_kickoff=True,
    )
    print_calendar_items(items)
    return 0


def cmd_hq(args: argparse.Namespace) -> int:
    conn = open_db(args)
    item = hq_summary(conn, user_participant=args.user, lock_minutes=args.lock_minutes)
    season = item["season"]
    deadline = item["deadline"]
    effective = deadline.effective_deadline_at.isoformat() if deadline and deadline.effective_deadline_at else ""
    print(f"BruceBet Headquarters: {season['display_name'] or season['name']}")
    print_key_values(
        [
            ("round", item["round_name"]),
            ("deadline", effective),
            ("matches", item["match_count"]),
            ("participants", item["participant_count"]),
            ("paid", item["paid_count"]),
            ("bank_rub", item["bank_rub"]),
            ("your_forecast", f"{item['predictions']['mine']}/{item['match_count']}"),
            (
                "field_loaded",
                f"{item['predictions']['participants']}/{item['participant_count']} participants, "
                f"{item['predictions']['rows']} rows",
            ),
        ]
    )
    print()
    print("Risk focus:")
    focus = item["risk"].get("risk", [])[:3] + item["risk"].get("slippery", [])[:3]
    print_rows(
        ["#", "match", "top", "share", "base"],
        [[row["position"], row["label"], row["top_outcome"], row["top_share"], row["suggested_score"]] for row in focus],
    )
    print()
    print("Data freshness:")
    print_rows(["source", "updated_at", "age_minutes"], freshness_rows(item["freshness"]))
    return 0


def cmd_ready(args: argparse.Namespace) -> int:
    conn = open_db(args)
    item = ready_summary(conn, user_participant=args.user, lock_minutes=args.lock_minutes)
    print_key_values(
        [
            ("status", item["status"]),
            ("round", item["round_name"]),
            ("deadline", item["deadline"].isoformat() if item["deadline"] else None),
            ("minutes_to_deadline", item["minutes_to_deadline"]),
            ("matches", item["match_count"]),
            ("your_predictions", item["your_predictions"]),
            ("missing_your_predictions", item["missing_your_predictions"]),
            ("field_predictions", f"{item['field_predictions']}/{item['expected_field_predictions']}"),
            ("model_forecasts", f"{item['model_forecasts']}/{item['match_count']}"),
        ]
    )
    if item["blockers"]:
        print("\nBlockers:")
        for row in item["blockers"]:
            print(f"- {row}")
    if item["warnings"]:
        print("\nWarnings:")
        for row in item["warnings"]:
            print(f"- {row}")
    print("\nData freshness:")
    print_rows(["source", "updated_at", "age_minutes"], freshness_rows(item["freshness"]))
    return 0


def cmd_intel(args: argparse.Namespace) -> int:
    conn = open_db(args)
    item = intelligence_readiness(conn, args.round, lock_minutes=args.lock_minutes)
    if not item["items"]:
        print("No matches found for the selected round.")
        return 0
    print_key_values(
        [
            ("round", item["round_name"]),
            ("ready", item["ready_count"]),
            ("attention", item["attention_count"]),
            ("blocked", item["blocked_count"]),
        ]
    )
    print()
    print_rows(
        ["#", "match", "status", "signals", "priority"],
        [
            [
                row["match"]["position"],
                f"{row['match']['home']} - {row['match']['away']}",
                row["status"],
                f"{row['ready_signals']}/{row['total_signals']}",
                ", ".join(signal["title"] for signal in row["follow_up"][:3]) or "-",
            ]
            for row in item["items"]
        ],
    )
    return 0


def cmd_absence(args: argparse.Namespace) -> int:
    if args.impact is not None and not 0 <= args.impact <= 1:
        print("impact must be between 0 and 1", file=sys.stderr)
        return 2
    conn = open_db(args)
    now = datetime.now().astimezone()
    absence_id = upsert_absence(
        conn,
        {
            "team": args.team,
            "player": args.player,
            "status": args.status,
            "impact_rating": "" if args.impact is None else str(args.impact),
            "source": args.source,
            "notes": args.note or "",
            "updated_at": now.isoformat(),
        },
    )
    sync_match_contexts_and_factors(
        conn,
        now=now,
        days_ahead=args.days,
        weather_days=0,
        timezone_name=args.timezone,
    )
    assessments = sync_match_assessments(
        conn,
        now.isoformat(),
        now=now,
        days_ahead=args.days,
        timezone_name=args.timezone,
    )
    conn.commit()
    print_key_values(
        [
            ("action", "cleared" if absence_id == 0 else "saved"),
            ("team", args.team),
            ("player", args.player),
            ("status", args.status),
            ("assessments_recalculated", assessments),
        ]
    )
    return 0


def cmd_missing(args: argparse.Namespace) -> int:
    conn = open_db(args)
    item = missing_forecasts_summary(conn, args.round, lock_minutes=args.lock_minutes)
    if not item["round_name"]:
        print("No active round found.")
        return 0
    print_key_values(
        [
            ("round", item["round_name"]),
            ("deadline", item["deadline"].isoformat() if item["deadline"] else None),
            ("matches", item["match_count"]),
            ("complete", f"{item['complete_count']}/{item['participant_count']}"),
        ]
    )
    if not item["incomplete"]:
        print("All participants have a complete forecast block.")
        return 0
    print()
    print_rows(
        ["participant", "saved", "missing positions"],
        [
            [
                row["participant"],
                f"{row['submitted_count']}/{item['match_count']}",
                ",".join(str(position) for position in row["missing_positions"]),
            ]
            for row in item["incomplete"]
        ],
    )
    return 0


def cmd_set_result(args: argparse.Namespace) -> int:
    conn = open_db(args)
    match = find_match(conn, args.query)
    previous, current = set_manual_match_result(conn, int(match["id"]), args.score, reason=args.reason)
    reviews = finalize_completed_rounds(conn, lock_minutes=args.lock_minutes)
    print_key_values(
        [
            ("match", match_header(match)),
            ("previous_result", previous),
            ("manual_result", current),
            ("reason", args.reason),
            ("completed_round_reviews", len(reviews)),
        ]
    )
    return 0


def cmd_result_history(args: argparse.Namespace) -> int:
    conn = open_db(args)
    match = find_match(conn, args.query)
    print(match_header(match))
    print_rows(
        ["previous", "new", "changed_at", "chat_id", "reason"],
        [
            [clean(row["previous_result"]), row["new_result"], row["changed_at"], clean(row["actor_chat_id"]), clean(row["reason"])]
            for row in manual_result_history(conn, int(match["id"]))
        ],
    )
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    conn = open_db(args)
    print_risk_map(risk_map(conn, args.round))
    return 0


def print_edge_map(item: dict[str, object]) -> None:
    print(f"Round: {clean(item['round_name'])}")
    rows = item["opportunities"]
    if rows:
        print("\nDisagreement opportunities:")
        print_rows(
            ["#", "match", "model", "market", "field", "edge", "signals"],
            [
                [
                    row["position"],
                    row["label"],
                    f"{row['model_score']} {row['model_outcome']}".strip(),
                    f"{row['market_outcome']} {clean(row['market_share'])}",
                    f"{row['field_outcome']} {clean(row['field_share'])}",
                    row["edge_score"],
                    ",".join(row["signals"]),
                ]
                for row in rows
            ],
        )
    missing = item["needs_data"]
    if missing:
        print("\nNeeds data before it can be ranked:")
        print_rows(
            ["#", "match", "missing"],
            [[row["position"], row["label"], ",".join(row["missing"])] for row in missing],
        )


def cmd_edge(args: argparse.Namespace) -> int:
    conn = open_db(args)
    print_edge_map(edge_map(conn, args.round))
    return 0


def print_forecast_import_report(participant: str, round_name: str, report: ForecastImportReport) -> None:
    print(
        f"Forecast import: participant={participant}, round={round_name}, "
        f"parsed={report.accepted_count}/{report.expected_count}, stored={report.stored_count}/{report.expected_count}"
    )
    if report.normalized:
        print("Normalized:")
        print_rows(
            ["position", "raw", "stored"],
            [[item.position, item.raw_score, item.score] for item in report.normalized],
        )
    if report.missing_positions:
        print("Missing positions: " + ", ".join(str(item) for item in report.missing_positions))
    if report.duplicate_positions:
        print("Duplicate positions skipped: " + ", ".join(str(item) for item in report.duplicate_positions))
    if report.invalid_lines:
        print("Invalid lines:")
        for item in report.invalid_lines:
            print(f"- {item}")
    if report.extra_lines:
        print("Extra lines:")
        for item in report.extra_lines:
            print(f"- {item}")
    if report.protected_positions:
        print("Deadline-protected positions: " + ", ".join(str(item) for item in report.protected_positions))
    if report.rejected_positions:
        print("Rejected positions: " + ", ".join(str(item) for item in report.rejected_positions))
    if report.quarantined_positions:
        print("Quarantined positions: " + ", ".join(str(item) for item in report.quarantined_positions))


def cmd_import_forecast(args: argparse.Namespace) -> int:
    conn = open_db(args)
    text = Path(args.source).read_text(encoding="utf-8-sig")
    submitted_at = parse_datetime(args.submitted_at) if args.submitted_at else datetime.now().astimezone()
    report = import_forecast_block(
        conn,
        participant=args.participant,
        round_name=args.round,
        text=text,
        submitted_at=submitted_at,
        source="cli-forecast",
        lock_minutes=args.lock_minutes,
    )
    print_forecast_import_report(args.participant, args.round, report)
    return 0


def cmd_strategy(args: argparse.Namespace) -> int:
    conn = open_db(args)
    item = strategy_summary(conn, user_participant=args.user, lock_minutes=args.lock_minutes)
    me = item["me"]
    leader = item["leader"]
    print_key_values(
        [
            ("user", item["user"]),
            ("mode", item["mode"]),
            ("your_rank", me.rank if me else None),
            ("your_points", me.total if me else None),
            ("leader", leader.name if leader else None),
            ("leader_points", leader.total if leader else None),
            ("gap", item["gap"]),
            ("advice", item["advice"]),
        ]
    )
    print()
    print("Risk map:")
    print_risk_map(item["risk"])
    return 0


def cmd_scenario(args: argparse.Namespace) -> int:
    conn = open_db(args)
    match = find_match(conn, args.query)
    scenario = parse_score(args.score)
    if scenario is None:
        raise SystemExit("Scenario score must look like 2:0, with one digit per side.")
    print(match_header(match))
    print(f"Scenario: {scenario.label()}")
    views = prediction_views_for_match(conn, int(match["id"]), scenario=scenario, lock_minutes=args.lock_minutes)
    rows = sorted(
        [[view.participant, view.score, view.category, view.points] for view in views],
        key=lambda row: (-int(row[3]), str(row[0]).lower()),
    )
    print_rows(["participant", "score", "category", "pts"], rows)
    return 0


def cmd_vs(args: argparse.Namespace) -> int:
    conn = open_db(args)
    comparison = compare_participants(conn, args.me, args.opponent, lock_minutes=args.lock_minutes)
    rows = [
        [
            row["round"],
            row["position"],
            row["match"],
            row["result"],
            row["mine"],
            row["opponent"],
            "" if row["delta"] is None else row["delta"],
        ]
        for row in comparison
    ]
    print_rows(["round", "#", "match", "result", args.me, args.opponent, "delta"], rows)
    return 0


def cmd_team(args: argparse.Namespace) -> int:
    conn = open_db(args)
    profile = team_profile(conn, args.query)
    team = profile["team"]
    print(f"Team: {team['name']}")
    print_key_values(
        [
            ("short_name", team["short_name"]),
            ("country", team["country"]),
            ("confederation", team["confederation"]),
            ("fifa_rank", team["fifa_rank"]),
            ("elo_rating", team["elo_rating"]),
            ("market_value_m_eur", team["market_value_m_eur"]),
            ("manager", team["manager"]),
            ("formation", team["preferred_formation"]),
            ("attack", team["attack_rating"]),
            ("defense", team["defense_rating"]),
            ("transition", team["transition_rating"]),
            ("set_pieces", team["set_piece_rating"]),
            ("goalkeeper", team["goalkeeper_rating"]),
            ("style", team["style_tags"]),
            ("notes", team["notes"]),
            ("updated_at", team["updated_at"]),
        ]
    )
    print()
    print("Recent form:")
    print_rows(
        ["date", "opponent", "venue", "gf", "ga", "xgf", "xga", "result"],
        [
            [
                row["match_date"],
                row["opponent"],
                clean(row["venue"]),
                clean(row["goals_for"]),
                clean(row["goals_against"]),
                clean(row["xg_for"]),
                clean(row["xg_against"]),
                clean(row["result"]),
            ]
            for row in profile["form"]
        ],
    )
    print()
    print("Absences:")
    print_rows(
        ["player", "role", "status", "severity", "impact", "return", "source"],
        [
            [
                row["player"],
                clean(row["role"]),
                row["status"],
                clean(row["severity"]),
                clean(row["impact_rating"]),
                clean(row["expected_return"]),
                clean(row["source"]),
            ]
            for row in profile["absences"]
        ],
    )
    return 0


def cmd_variables(args: argparse.Namespace) -> int:
    conn = open_db(args)
    rows = player_status_summary(conn, args.team, limit=args.limit)
    print_rows(
        ["team", "player", "role", "status", "avail", "form", "min5", "starts5", "source", "updated"],
        [
            [
                row["team"],
                row["player"],
                clean(row["role"]),
                clean(row["status"]),
                clean(row["availability_pct"]),
                clean(row["form_rating"]),
                clean(row["minutes_last_5"]),
                clean(row["starts_last_5"]),
                clean(row["source"]),
                clean(row["updated_at"]),
            ]
            for row in rows
        ],
    )
    return 0


def cmd_dossier(args: argparse.Namespace) -> int:
    conn = open_db(args)
    match = find_match(conn, args.query)
    dossier = match_dossier(conn, int(match["id"]))
    print(match_header(dossier["match"]))
    print()
    print("Teams:")
    print_rows(
        ["side", "team", "fifa", "elo", "attack", "defense", "style"],
        [
            [
                "home",
                dossier["home"]["name"],
                clean(dossier["home"]["fifa_rank"]),
                clean(dossier["home"]["elo_rating"]),
                clean(dossier["home"]["attack_rating"]),
                clean(dossier["home"]["defense_rating"]),
                clean(dossier["home"]["style_tags"]),
            ],
            [
                "away",
                dossier["away"]["name"],
                clean(dossier["away"]["fifa_rank"]),
                clean(dossier["away"]["elo_rating"]),
                clean(dossier["away"]["attack_rating"]),
                clean(dossier["away"]["defense_rating"]),
                clean(dossier["away"]["style_tags"]),
            ],
        ],
    )

    if dossier["context"]:
        print()
        print("Context:")
        ctx = dossier["context"]
        print_key_values(
            [
                ("venue", ctx["venue"]),
                ("city", ctx["city"]),
                ("neutral_site", ctx["neutral_site"]),
                ("home_rest_days", ctx["home_rest_days"]),
                ("away_rest_days", ctx["away_rest_days"]),
                ("weather", ctx["weather"]),
                ("temperature_c", ctx["temperature_c"]),
                ("referee", ctx["referee"]),
                ("home_motivation", ctx["home_motivation"]),
                ("away_motivation", ctx["away_motivation"]),
                ("home_rotation_risk", ctx["home_rotation_risk"]),
                ("away_rotation_risk", ctx["away_rotation_risk"]),
                ("notes", ctx["notes"]),
            ]
        )

    if dossier["odds"]:
        print()
        print("Odds:")
        print_rows(
            ["bookmaker", "captured_at", "home", "draw", "away", "u2.5", "o2.5"],
            [
                [
                    row["bookmaker"],
                    row["captured_at"],
                    clean(row["home_win"]),
                    clean(row["draw"]),
                    clean(row["away_win"]),
                    clean(row["under_2_5"]),
                    clean(row["over_2_5"]),
                ]
                for row in dossier["odds"]
            ],
        )

    if dossier["factors"]:
        print()
        print("Team factors:")
        print_rows(
            ["side", "team", "lineup", "absences", "fatigue", "morale", "tactical", "motivation"],
            [
                [
                    row["side"],
                    row["team"],
                    clean(row["expected_lineup_confidence"]),
                    clean(row["absences_impact"]),
                    clean(row["fatigue"]),
                    clean(row["morale"]),
                    clean(row["tactical_fit"]),
                    clean(row["motivation"]),
                ]
                for row in dossier["factors"]
            ],
        )

    if dossier["absences"]:
        print()
        print("Absences:")
        print_rows(
            ["team", "player", "role", "status", "impact", "source"],
            [
                [
                    row["team"],
                    row["player"],
                    clean(row["role"]),
                    row["status"],
                    clean(row["impact_rating"]),
                    clean(row["source"]),
                ]
                for row in dossier["absences"]
            ],
        )

    if dossier["assessment"]:
        print()
        print("Assessment:")
        item = dossier["assessment"]
        print_key_values(
            [
                ("suggested_score", item["suggested_score"]),
                ("risk_level", item["risk_level"]),
                ("confidence", item["confidence"]),
                ("home_edge", item["home_edge"]),
                ("draw_edge", item["draw_edge"]),
                ("away_edge", item["away_edge"]),
                ("volatility", item["volatility"]),
                ("consensus_note", item["consensus_note"]),
                ("contrarian_note", item["contrarian_note"]),
                ("notes", item["notes"]),
            ]
        )
    return 0


def cmd_odds(args: argparse.Namespace) -> int:
    conn = open_db(args)
    match = find_match(conn, args.query)
    dossier = match_dossier(conn, int(match["id"]))
    print(match_header(dossier["match"]))
    if not dossier["odds"]:
        print("No stored odds snapshots.")
        return 0
    print_rows(
        ["bookmaker", "captured_at", "home", "draw", "away", "u2.5", "o2.5", "btts_y", "btts_n"],
        [
            [
                row["bookmaker"],
                row["captured_at"],
                clean(row["home_win"]),
                clean(row["draw"]),
                clean(row["away_win"]),
                clean(row["under_2_5"]),
                clean(row["over_2_5"]),
                clean(row["btts_yes"]),
                clean(row["btts_no"]),
            ]
            for row in dossier["odds"][: args.limit]
        ],
    )
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    key = odds_api_key()
    if not key:
        print("THE_ODDS_API_KEY is not set.", file=sys.stderr)
        return 2
    try:
        check = TheOddsApiClient(key).sports(all_sports=args.all)
    except OddsApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print_key_values(
        [
            ("ok", check.ok),
            ("sports_count", check.sports_count),
            ("sport_present", args.sport in check.sport_keys),
        ]
    )
    print_odds_quota(check.quota.requests_remaining, check.quota.requests_used, check.quota.requests_last)
    return 0


def cmd_sync_odds(args: argparse.Namespace) -> int:
    key = odds_api_key()
    if not key:
        print("THE_ODDS_API_KEY is not set.", file=sys.stderr)
        return 2
    conn = open_db(args)
    try:
        result = sync_odds_to_db(
            conn,
            api_key=key,
            sport=args.sport,
            regions=args.regions,
            markets=args.markets,
            bookmaker=args.bookmaker,
            days_ahead=args.days,
        )
    except OddsApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print_key_values(
        [
            ("sport", result.sport),
            ("regions", result.regions),
            ("markets", result.markets),
            ("bookmaker", result.bookmaker),
            ("captured_at", result.captured_at),
            ("events_seen", result.events_seen),
            ("matched", result.matched),
            ("inserted", result.inserted),
            ("unmatched", len(result.unmatched)),
        ]
    )
    print_odds_quota(result.quota.requests_remaining, result.quota.requests_used, result.quota.requests_last)
    if result.unmatched:
        print()
        print("Unmatched events:")
        for item in result.unmatched[: args.show_unmatched]:
            print(f"- {item}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    checks = check_all_sources(source_config_from_env(timeout=args.timeout))
    print_rows(
        ["source", "ok", "configured", "detail"],
        [[item.name, "yes" if item.ok else "no", "yes" if item.configured else "no", item.detail] for item in checks],
    )
    return 0 if all(item.ok for item in checks if item.configured) else 1


def cmd_sync_fixtures(args: argparse.Namespace) -> int:
    conn = open_db(args)
    try:
        result = sync_pl_fixtures_to_db(
            conn,
            compseason_id=args.compseason_id,
            season_label=args.season_label,
            timezone_name=args.timezone,
        )
    except PremierLeagueApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print_key_values(
        [
            ("source", result.source),
            ("compseason_id", result.compseason_id),
            ("season_label", result.season_label),
            ("fetched", result.fetched),
            ("imported", result.imported),
            ("rounds", result.rounds),
            ("created", result.created),
            ("updated", result.updated),
            ("moved", result.moved),
            ("unmatched", result.unmatched),
            ("stale_factors_removed", result.stale_factors_removed),
            ("before_hash", result.before_hash),
            ("after_hash", result.after_hash),
            ("first_kickoff", result.first_kickoff),
            ("last_kickoff", result.last_kickoff),
        ]
    )
    return 0


def cmd_sync_variables(args: argparse.Namespace) -> int:
    conn = open_db(args)
    result = sync_match_variables(
        conn,
        days_ahead=args.days,
        weather_days=args.weather_days,
        timeout=args.timeout,
        timezone_name=args.timezone,
        include_fpl=not args.skip_fpl,
        include_elo=not args.skip_elo,
        include_context=not args.skip_context,
        include_assessments=not args.skip_assessments,
        football_data_token=os.getenv("FOOTBALL_DATA_TOKEN", "").strip(),
    )
    print_key_values(variable_sync_rows(result))
    captured = capture_model_forecasts(conn)
    print(f"model_forecasts_captured={captured}")
    return 0 if not result.errors else 1


def print_calibration(item: dict[str, object]) -> None:
    print_key_values(
        [
            ("model", item["model_key"]),
            ("forecasts", item["forecasts"]),
            ("scored", item["scored"]),
            ("pending", item["pending"]),
            ("exact", item["exact"]),
            ("diff", item["diff"]),
            ("outcome", item["outcome"]),
            ("miss", item["miss"]),
            ("points", item["points"]),
            ("points_per_match", item["points_per_match"]),
        ]
    )
    print()
    print("Confidence buckets:")
    print_rows(
        ["bucket", "forecasts", "exact", "diff", "outcome", "miss", "points"],
        [
            [bucket, value["forecasts"], value["exact"], value["diff"], value["outcome"], value["miss"], value["points"]]
            for bucket, value in item["buckets"].items()
        ],
    )


def print_round_review(item: dict[str, object]) -> None:
    print_key_values(
        [
            ("round", item["round_name"]),
            ("finished", f"{item['finished_count']}/{item['match_count']}"),
            ("complete", item["complete"]),
        ]
    )
    print()
    print("Round standings:")
    print_rows(
        ["participant", "points", "exact", "diff", "outcome", "miss", "late"],
        [
            [row["participant"], row["points"], row["exact"], row["diff"], row["outcome"], row["miss"], row["late"]]
            for row in item["participants"]
        ],
    )
    if item["swings"]:
        print()
        print("Main swings:")
        print_rows(
            ["#", "match", "result", "spread", "max", "min"],
            [
                [row["position"], row["match"], row["result"], row["spread"], row["max_points"], row["min_points"]]
                for row in item["swings"]
            ],
        )
    print()
    print("Model calibration:")
    print_calibration(item["calibration"])


def cmd_sync_results(args: argparse.Namespace) -> int:
    conn = open_db(args)
    try:
        result = sync_pl_results_to_db(
            conn,
            compseason_id=args.compseason_id,
            season_label=args.season_label,
        )
    except PremierLeagueApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    reviews = finalize_completed_rounds(conn, lock_minutes=args.lock_minutes)
    print_key_values(
        [
            ("source", result.source),
            ("fetched", result.fetched),
            ("finished_seen", result.finished_seen),
            ("matched", result.matched),
            ("updated", result.updated),
            ("unmatched", len(result.unmatched)),
            ("completed_round_reviews", len(reviews)),
        ]
    )
    if result.unmatched:
        print("Unmatched completed fixtures:")
        for item in result.unmatched[:10]:
            print(f"- {item}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    conn = open_db(args)
    print_round_review(round_review(conn, args.round, lock_minutes=args.lock_minutes))
    return 0


def cmd_calibration(args: argparse.Namespace) -> int:
    conn = open_db(args)
    print_calibration(model_calibration_summary(conn, round_name=args.round or None))
    return 0


def cmd_rehearse(args: argparse.Namespace) -> int:
    item = run_rehearsal(lock_minutes=args.lock_minutes)
    print("Rehearsal completed without touching the live database.")
    print_key_values([("model_forecasts_captured", item["captured"]), ("reviews_saved", item["reviews_saved"])])
    print()
    print("Checks:")
    print_rows(
        ["status", "check", "detail"],
        [["OK" if row["passed"] else "FAIL", row["name"], row["detail"]] for row in item["checks"]],
    )
    print()
    print("Final standings:")
    print_rows([["#", "participant", "points"][index] for index in range(3)], [[row["rank"], row["name"], row["points"]] for row in item["standings"]])
    print()
    print_round_review(item["review"])
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    conn = open_db(args)
    result = export_snapshot(conn, args.out_dir, label=args.label)
    print_key_values(
        [
            ("out_dir", result.out_dir),
            ("manifest", result.manifest_path),
            ("generated_at", result.generated_at),
            ("season", result.season),
            ("files", len(result.tables)),
            ("rows", sum(result.tables.values())),
        ]
    )
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    conn = open_db(args)
    season_id = int(active_season(conn)["id"])

    missing = list(
        conn.execute(
            """
            SELECT
                p.name AS participant,
                r.name AS round_name,
                GROUP_CONCAT(m.position, ',') AS positions,
                COUNT(*) AS count
            FROM season_participants sp
            JOIN participants p ON p.id = sp.participant_id
            CROSS JOIN matches m
            JOIN rounds r ON r.id = m.round_id
            LEFT JOIN predictions pr ON pr.participant_id = p.id AND pr.match_id = m.id
            WHERE sp.season_id = ?
              AND sp.active = 1
              AND r.season_id = ?
              AND pr.id IS NULL
            GROUP BY p.id, r.id
            ORDER BY r.sort_order, p.name
            """,
            (season_id, season_id),
        )
    )
    print("Missing predictions:")
    print_rows(
        ["round", "participant", "count", "positions"],
        [[row["round_name"], row["participant"], row["count"], row["positions"]] for row in missing],
    )

    invalid_rows = list(
        conn.execute(
            """
            SELECT p.name AS participant, r.name AS round_name, pr.score, m.position
            FROM predictions pr
            JOIN participants p ON p.id = pr.participant_id
            JOIN matches m ON m.id = pr.match_id
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ?
            ORDER BY r.sort_order, p.name, m.position
            """,
            (season_id,),
        )
    )
    invalid_grouped: dict[tuple[str, str], list[str]] = {}
    nonstandard_grouped: dict[tuple[str, str], list[str]] = {}
    for row in invalid_rows:
        score = parse_score(row["score"])
        if score is not None:
            if not is_standard_score(row["score"]):
                key = (row["round_name"], row["participant"])
                nonstandard_grouped.setdefault(key, []).append(
                    f"{row['position']}={row['score']}->{normalize_score(row['score'])}"
                )
            continue
        key = (row["round_name"], row["participant"])
        invalid_grouped.setdefault(key, []).append(f"{row['position']}={row['score']}")
    print()
    print("Unreadable scores:")
    print_rows(
        ["round", "participant", "count", "examples"],
        [
            [round_name, participant, len(values), "; ".join(values[:6])]
            for (round_name, participant), values in sorted(invalid_grouped.items())
        ],
    )
    print()
    print("Non-standard but accepted scores:")
    print_rows(
        ["round", "participant", "count", "examples"],
        [
            [round_name, participant, len(values), "; ".join(values[:6])]
            for (round_name, participant), values in sorted(nonstandard_grouped.items())
        ],
    )

    late_grouped: dict[tuple[str, str], list[str]] = {}
    late_rows = list(
        conn.execute(
            """
            SELECT
                p.name AS participant,
                r.name AS round_name,
                r.deadline_at AS round_deadline_at,
                m.position,
                m.kickoff_at,
                pr.submitted_at
            FROM predictions pr
            JOIN participants p ON p.id = pr.participant_id
            JOIN matches m ON m.id = pr.match_id
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ?
            ORDER BY r.sort_order, p.name, m.position
            """,
            (season_id,),
        )
    )
    for row in late_rows:
        submitted_at = parse_datetime(row["submitted_at"])
        kickoff_at = parse_datetime(row["kickoff_at"])
        round_deadline_at = parse_datetime(row["round_deadline_at"])
        if prediction_is_eligible(submitted_at, kickoff_at, round_deadline_at, args.lock_minutes):
            continue
        key = (row["round_name"], row["participant"])
        late_grouped.setdefault(key, []).append(str(row["position"]))
    print()
    print("Late / needs kickoff check:")
    print_rows(
        ["round", "participant", "count", "positions"],
        [
            [round_name, participant, len(values), ",".join(values)]
            for (round_name, participant), values in sorted(late_grouped.items())
        ],
    )
    return 0


def cmd_copy_examples(args: argparse.Namespace) -> int:
    target = Path(args.target)
    target.mkdir(parents=True, exist_ok=True)
    base = Path(__file__).resolve().parents[1] / "examples"
    for filename in [
        "participants.csv",
        "teams.csv",
        "matches.csv",
        "predictions.csv",
        "team_form.csv",
        "absences.csv",
        "player_statuses.csv",
        "match_contexts.csv",
        "match_odds.csv",
        "team_match_factors.csv",
        "match_assessments.csv",
    ]:
        shutil.copy2(base / filename, target / filename)
    print(f"Examples copied to {target}")
    return 0


def cmd_parse_vk(args: argparse.Namespace) -> int:
    templates, records = parse_vk_file(Path(args.source), Path(args.out_dir))
    print(
        f"Parsed rounds={len(templates)}, "
        f"matches={sum(len(template.matches) for template in templates)}, "
        f"predictions={len(records)}"
    )
    for template in templates:
        participants = {record.participant for record in records if record.round_name == template.round_name}
        print(f"Round {template.round_name}: matches={len(template.matches)}, participants={len(participants)}")
    return 0


def cmd_vk_dry_run(args: argparse.Namespace) -> int:
    try:
        report = read_public_topic_dry_run(
            args.topic_url,
            args.kind,
            chromium_bin=args.chromium,
            wait_ms=args.wait_ms,
            timeout=args.timeout,
        )
    except (ValueError, VkBrowserError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(render_dry_run_report(report, limit=args.limit))
    return 0


def cmd_vk_discover(args: argparse.Namespace) -> int:
    try:
        result = probe_public_group_topics(
            args.group_id,
            chromium_bin=args.chromium,
            virtual_time_ms=args.wait_ms,
            timeout=args.timeout,
        )
    except VkBrowserError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"VK group: {result.group_id}")
    print(f"Source URL: {result.source_url}")
    print(f"Topics found: {len(result.topics)}")
    for topic in result.topics:
        print(f"- [{topic.league_hint}/{topic.topic_kind}] {topic.title}: {topic.url}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brucebet", description="BruceBet 3000 contest toolkit")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path, default: {DEFAULT_DB}")
    parser.add_argument("--lock-minutes", type=int, default=90, help="Prediction lock time before kickoff.")
    parser.add_argument("--competition", default="epl", help="Competition profile code, default: epl.")
    parser.add_argument("--season", default="2026/27", help="Season profile name, default: 2026/27.")
    parser.add_argument("--season-display", default="EPL 2026/27", help="Human-readable active season name.")
    parser.add_argument("--user", default="Bruce Wayne", help="Your participant name for strategy commands.")
    sub = parser.add_subparsers(required=True)

    init = sub.add_parser("init", help="Create an empty database.")
    init.add_argument("--reset", action="store_true")
    init.set_defaults(func=cmd_init)

    imp = sub.add_parser("import", help="Import CSV files.")
    imp.add_argument("--participants")
    imp.add_argument("--teams")
    imp.add_argument("--matches")
    imp.add_argument("--predictions")
    imp.add_argument("--team-form")
    imp.add_argument("--absences")
    imp.add_argument("--player-statuses")
    imp.add_argument("--contexts")
    imp.add_argument("--odds")
    imp.add_argument("--factors")
    imp.add_argument("--assessments")
    imp.add_argument("--reset", action="store_true")
    imp.set_defaults(func=cmd_import)

    sample = sub.add_parser("load-sample", help="Load sample contest data.")
    sample.set_defaults(func=cmd_load_sample)

    table = sub.add_parser("table", help="Show standings.")
    table.add_argument("--entry-fee", type=int, default=300)
    table.set_defaults(func=cmd_table)

    match = sub.add_parser("match", help="Show predictions for one match.")
    match.add_argument("query")
    match.set_defaults(func=cmd_match)

    field = sub.add_parser("field", help="Show field consensus for one match.")
    field.add_argument("query")
    field.set_defaults(func=cmd_field)

    recommend = sub.add_parser("recommend", help="Show a structured recommendation for one match.")
    recommend.add_argument("query")
    recommend.set_defaults(func=cmd_recommend)

    deadlines = sub.add_parser("deadlines", help="Show round deadlines.")
    deadlines.set_defaults(func=cmd_deadlines)

    calendar = sub.add_parser("calendar", help="Show upcoming match calendar.")
    calendar.add_argument("--days", type=int, default=14)
    calendar.add_argument("--limit", type=int, default=30)
    calendar.add_argument("--round")
    calendar.add_argument("--include-unknown", action="store_true")
    calendar.set_defaults(func=cmd_calendar)

    today = sub.add_parser("today", help="Show today's matches.")
    today.add_argument("--limit", type=int, default=30)
    today.set_defaults(func=cmd_today)

    week = sub.add_parser("week", help="Show the next seven days.")
    week.add_argument("--limit", type=int, default=30)
    week.set_defaults(func=cmd_week)

    next_match = sub.add_parser("next", help="Show the next scheduled match.")
    next_match.set_defaults(func=cmd_next)

    round_calendar = sub.add_parser("round", help="Show one round calendar.")
    round_calendar.add_argument("round")
    round_calendar.add_argument("--limit", type=int, default=40)
    round_calendar.set_defaults(func=cmd_round_calendar)

    hq = sub.add_parser("hq", help="Show headquarters summary for the active round.")
    hq.set_defaults(func=cmd_hq)

    ready = sub.add_parser("ready", help="Run a preflight check for the active round.")
    ready.set_defaults(func=cmd_ready)

    intel = sub.add_parser("intel", help="Show per-match analytical readiness and missing signals.")
    intel.add_argument("round", nargs="?")
    intel.set_defaults(func=cmd_intel)

    absence = sub.add_parser("absence", help="Save a confirmed absence and recalculate relevant variables.")
    absence.add_argument("team")
    absence.add_argument("player")
    absence.add_argument("status", help="injured, doubtful, suspended, fit, or available")
    absence.add_argument("--impact", type=float, help="importance from 0 to 1")
    absence.add_argument("--source", default="manual CLI")
    absence.add_argument("--note")
    absence.add_argument("--days", type=int, default=int(env_default("BRUCEBET_VARIABLE_DAYS_AHEAD", "365")))
    absence.add_argument("--timezone", default=env_default("BRUCEBET_TIMEZONE", "Europe/Moscow"))
    absence.set_defaults(func=cmd_absence)

    missing = sub.add_parser("missing", help="List participants with missing forecasts for a round.")
    missing.add_argument("round", nargs="?")
    missing.set_defaults(func=cmd_missing)

    risk = sub.add_parser("risk", help="Show the risk map for a round.")
    risk.add_argument("round", nargs="?")
    risk.set_defaults(func=cmd_risk)

    edge = sub.add_parser("edge", help="Rank matches where model, market, and field disagree.")
    edge.add_argument("round", nargs="?")
    edge.set_defaults(func=cmd_edge)

    strategy = sub.add_parser("strategy", help="Show season strategy against the table.")
    strategy.set_defaults(func=cmd_strategy)

    scenario = sub.add_parser("scenario", help="Score one match under a hypothetical result.")
    scenario.add_argument("query")
    scenario.add_argument("score")
    scenario.set_defaults(func=cmd_scenario)

    vs = sub.add_parser("vs", help="Show prediction differences between two participants.")
    vs.add_argument("me")
    vs.add_argument("opponent")
    vs.set_defaults(func=cmd_vs)

    team = sub.add_parser("team", help="Show team variables, form, and absences.")
    team.add_argument("query")
    team.set_defaults(func=cmd_team)

    variables = sub.add_parser("variables", help="Show player availability/form snapshots.")
    variables.add_argument("team", nargs="?")
    variables.add_argument("--limit", type=int, default=30)
    variables.set_defaults(func=cmd_variables)

    dossier = sub.add_parser("dossier", help="Show match variables: context, odds, factors, absences.")
    dossier.add_argument("query")
    dossier.set_defaults(func=cmd_dossier)

    odds = sub.add_parser("odds", help="Show stored odds snapshots for one match.")
    odds.add_argument("query")
    odds.add_argument("--limit", type=int, default=10)
    odds.set_defaults(func=cmd_odds)

    quota = sub.add_parser("quota", help="Check The Odds API key and remaining credits.")
    quota.add_argument("--sport", default=env_default("THE_ODDS_API_SPORT", DEFAULT_ODDS_SPORT))
    quota.add_argument("--all", action="store_true", help="Ask The Odds API for active and inactive sports.")
    quota.set_defaults(func=cmd_quota)

    sync_odds = sub.add_parser("sync-odds", help="Fetch The Odds API snapshots into match_odds.")
    sync_odds.add_argument("--sport", default=env_default("THE_ODDS_API_SPORT", DEFAULT_ODDS_SPORT))
    sync_odds.add_argument("--regions", default=env_default("THE_ODDS_API_REGIONS", DEFAULT_ODDS_REGIONS))
    sync_odds.add_argument("--markets", default=env_default("THE_ODDS_API_MARKETS", DEFAULT_ODDS_MARKETS))
    sync_odds.add_argument("--bookmaker", default=env_default("THE_ODDS_API_BOOKMAKER", DEFAULT_ODDS_BOOKMAKER))
    sync_odds.add_argument("--days", type=int, default=int(env_default("THE_ODDS_API_DAYS_AHEAD", "30")))
    sync_odds.add_argument("--show-unmatched", type=int, default=10)
    sync_odds.set_defaults(func=cmd_sync_odds)

    sources = sub.add_parser("sources", help="Check configured/free data sources.")
    sources.add_argument("--timeout", type=int, default=20)
    sources.set_defaults(func=cmd_sources)

    sync_fixtures = sub.add_parser("sync-fixtures", help="Fetch official Premier League fixtures into matches.")
    sync_fixtures.add_argument("--compseason-id", type=int, default=int(env_default("PREMIER_LEAGUE_COMPSEASON_ID", str(DEFAULT_PL_COMPSEASON_ID))))
    sync_fixtures.add_argument("--season-label", default=env_default("PREMIER_LEAGUE_SEASON_LABEL", DEFAULT_PL_SEASON_LABEL))
    sync_fixtures.add_argument("--timezone", default=env_default("BRUCEBET_TIMEZONE", "Europe/Moscow"))
    sync_fixtures.set_defaults(func=cmd_sync_fixtures)

    sync_variables = sub.add_parser("sync-variables", help="Fetch FPL/ClubElo/weather/context variables into the database.")
    sync_variables.add_argument("--days", type=int, default=int(env_default("BRUCEBET_VARIABLE_DAYS_AHEAD", "365")))
    sync_variables.add_argument("--weather-days", type=int, default=int(env_default("BRUCEBET_WEATHER_DAYS_AHEAD", "16")))
    sync_variables.add_argument("--timeout", type=int, default=30)
    sync_variables.add_argument("--timezone", default=env_default("BRUCEBET_TIMEZONE", "Europe/Moscow"))
    sync_variables.add_argument("--skip-fpl", action="store_true")
    sync_variables.add_argument("--skip-elo", action="store_true")
    sync_variables.add_argument("--skip-context", action="store_true")
    sync_variables.add_argument("--skip-assessments", action="store_true")
    sync_variables.set_defaults(func=cmd_sync_variables)

    sync_results = sub.add_parser("sync-results", help="Fetch completed PL results and finalize complete round reviews.")
    sync_results.add_argument("--compseason-id", type=int, default=int(env_default("PREMIER_LEAGUE_COMPSEASON_ID", str(DEFAULT_PL_COMPSEASON_ID))))
    sync_results.add_argument("--season-label", default=env_default("PREMIER_LEAGUE_SEASON_LABEL", DEFAULT_PL_SEASON_LABEL))
    sync_results.set_defaults(func=cmd_sync_results)

    set_result = sub.add_parser("set-result", help="Manually set a fallback final result and record an audit entry.")
    set_result.add_argument("query")
    set_result.add_argument("score")
    set_result.add_argument("--reason", default="manual fallback")
    set_result.set_defaults(func=cmd_set_result)

    result_history = sub.add_parser("result-history", help="Show manual result override history for one match.")
    result_history.add_argument("query")
    result_history.set_defaults(func=cmd_result_history)

    review = sub.add_parser("review", help="Show a post-round review with score swings and model results.")
    review.add_argument("round")
    review.set_defaults(func=cmd_review)

    calibration = sub.add_parser("calibration", help="Show BruceBet model calibration from frozen pre-kickoff forecasts.")
    calibration.add_argument("round", nargs="?")
    calibration.set_defaults(func=cmd_calibration)

    rehearse = sub.add_parser("rehearse", help="Run an isolated end-to-end test round without touching live data.")
    rehearse.set_defaults(func=cmd_rehearse)

    snapshot = sub.add_parser("snapshot", help="Export a safe CSV/JSON snapshot of the active season.")
    snapshot.add_argument(
        "--out-dir",
        default=env_default("BRUCEBET_SNAPSHOT_OUT_DIR", "data/snapshots/current"),
        help="Output directory for stable snapshot files.",
    )
    snapshot.add_argument("--label", default=env_default("BRUCEBET_SNAPSHOT_LABEL", "manual"))
    snapshot.set_defaults(func=cmd_snapshot)

    audit = sub.add_parser("audit", help="Show missing, invalid, and late prediction issues.")
    audit.set_defaults(func=cmd_audit)

    copy_examples = sub.add_parser("copy-examples", help="Copy CSV templates to a folder.")
    copy_examples.add_argument("target")
    copy_examples.set_defaults(func=cmd_copy_examples)

    parse_vk = sub.add_parser("parse-vk", help="Parse a Forecasters Club VK pasted-text export.")
    parse_vk.add_argument("source")
    parse_vk.add_argument("--out-dir", required=True)
    parse_vk.set_defaults(func=cmd_parse_vk)

    vk_dry_run = sub.add_parser("vk-dry-run", help="Read and structurally parse one public VK topic without SQLite writes.")
    vk_dry_run.add_argument("--topic-url", required=True, help="Public VK discussion URL.")
    vk_dry_run.add_argument("--kind", choices=("registration", "predictions"), required=True)
    vk_dry_run.add_argument("--chromium", default=os.getenv("VK_CHROMIUM_BIN", "chromium").strip() or "chromium")
    vk_dry_run.add_argument("--wait-ms", type=int, default=int(env_default("VK_BROWSER_WAIT_MS", "8000")))
    vk_dry_run.add_argument("--timeout", type=int, default=45)
    vk_dry_run.add_argument("--limit", type=int, default=60)
    vk_dry_run.set_defaults(func=cmd_vk_dry_run)

    vk_discover = sub.add_parser("vk-discover", help="List public Forecasters Club discussion topics without SQLite writes.")
    vk_discover.add_argument("--group-id", type=int, default=int(env_default("VK_GROUP_ID", "217130885")))
    vk_discover.add_argument("--chromium", default=os.getenv("VK_CHROMIUM_BIN", "chromium").strip() or "chromium")
    vk_discover.add_argument("--wait-ms", type=int, default=int(env_default("VK_BROWSER_WAIT_MS", "8000")))
    vk_discover.add_argument("--timeout", type=int, default=45)
    vk_discover.set_defaults(func=cmd_vk_discover)

    import_forecast = sub.add_parser("import-forecast", help="Import one participant's pasted forecast block.")
    import_forecast.add_argument("participant")
    import_forecast.add_argument("round")
    import_forecast.add_argument("source", help="UTF-8 text file with scores in match order or labelled lines.")
    import_forecast.add_argument("--submitted-at", help="ISO timestamp; defaults to now in the local timezone.")
    import_forecast.set_defaults(func=cmd_import_forecast)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
