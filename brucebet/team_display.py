from __future__ import annotations


# Internal fixture names follow provider identifiers. VK-facing messages use the
# familiar short Russian names from the Forecasters Club template.
RUSSIAN_TEAM_NAMES = {
    "Arsenal": "Арсенал",
    "Aston Villa": "Астон Вилла",
    "Bournemouth": "Борнмут",
    "Brentford": "Брентфорд",
    "Brighton": "Брайтон",
    "Brighton & Hove Albion": "Брайтон",
    "Brighton and Hove Albion": "Брайтон",
    "Burnley": "Бернли",
    "Chelsea": "Челси",
    "Coventry": "Ковентри",
    "Coventry City": "Ковентри",
    "Crystal Palace": "Кристал Пэлас",
    "Everton": "Эвертон",
    "Fulham": "Фулхэм",
    "Hull": "Халл",
    "Hull City": "Халл",
    "Ipswich": "Ипсвич",
    "Ipswich Town": "Ипсвич",
    "Leeds": "Лидс",
    "Leeds United": "Лидс",
    "Liverpool": "Ливерпуль",
    "Manchester City": "Манчестер Сити",
    "Manchester United": "Манчестер Юнайтед",
    "Newcastle": "Ньюкасл",
    "Newcastle United": "Ньюкасл",
    "Nottingham Forest": "Ноттингем Форест",
    "Sunderland": "Сандерленд",
    "Tottenham": "Тоттенхэм",
    "Tottenham Hotspur": "Тоттенхэм",
    "West Ham": "Вест Хэм",
    "West Ham United": "Вест Хэм",
    "Wolves": "Вулверхэмптон",
    "Wolverhampton": "Вулверхэмптон",
    "Wolverhampton Wanderers": "Вулверхэмптон",
}


def russian_team_name(name: str) -> str:
    """Return the VK display name while leaving unknown teams untouched."""

    value = name.strip()
    return RUSSIAN_TEAM_NAMES.get(value, value)


def russian_match_label(home: str, away: str, *, separator: str = " - ") -> str:
    return f"{russian_team_name(home)}{separator}{russian_team_name(away)}"
