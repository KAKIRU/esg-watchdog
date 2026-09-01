from esg_watchdog.parsers.pdf import PageText

GOAL_PLAN_KEYWORDS = (
    "목표",
    "계획",
)


def select_goal_plan_pages(
    pages: list[PageText],
) -> list[PageText]:
    return [
        page
        for page in pages
        if any(
            keyword in page.text
            for keyword in GOAL_PLAN_KEYWORDS
        )
    ]