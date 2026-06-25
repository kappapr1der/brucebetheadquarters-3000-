from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

from .analytics import (
    compare_participants,
    compute_standings,
    field_summary,
    hq_summary,
    match_header,
    match_dossier,
    prediction_is_eligible,
    prediction_views_for_match,
    recommend_match,
    risk_map,
    round_deadlines,
    strategy_summary,
    team_profile,
)
from .scoring import is_standard_score, normalize_score, parse_datetime, parse_score
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
    import_predictions,
    import_team_form,
    import_team_match_factors,
    import_teams,
    init_db,
    reset_db,
)
from .vk_parser import parse_file as parse_vk_file


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
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    conn = open_db(args)
    print_risk_map(risk_map(conn, args.round))
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

    hq = sub.add_parser("hq", help="Show headquarters summary for the active round.")
    hq.set_defaults(func=cmd_hq)

    risk = sub.add_parser("risk", help="Show the risk map for a round.")
    risk.add_argument("round", nargs="?")
    risk.set_defaults(func=cmd_risk)

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

    dossier = sub.add_parser("dossier", help="Show match variables: context, odds, factors, absences.")
    dossier.add_argument("query")
    dossier.set_defaults(func=cmd_dossier)

    audit = sub.add_parser("audit", help="Show missing, invalid, and late prediction issues.")
    audit.set_defaults(func=cmd_audit)

    copy_examples = sub.add_parser("copy-examples", help="Copy CSV templates to a folder.")
    copy_examples.add_argument("target")
    copy_examples.set_defaults(func=cmd_copy_examples)

    parse_vk = sub.add_parser("parse-vk", help="Parse a Forecasters Club VK pasted-text export.")
    parse_vk.add_argument("source")
    parse_vk.add_argument("--out-dir", required=True)
    parse_vk.set_defaults(func=cmd_parse_vk)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
