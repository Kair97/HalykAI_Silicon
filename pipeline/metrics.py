"""Covenant metric library, keyed by the covenant's own wording.

The 36 cells use only 19 distinct metric shapes; four of those recur across
borrowers. Selecting the formula from the *clause text* rather than from a
(scenario, clause) key means a regenerated archive with different borrower ids
still resolves, as long as the contract language is drawn from the same set.

A clause whose wording matches nothing raises -- silence here would mean emitting
a number computed by the wrong formula, which is the worst failure available.
"""
import re
from decimal import Decimal

Z = Decimal(0)

# Russian category names as they appear in the clauses -> internal category
CATEGORY_WORDS = {
    "Капитальные затраты": "capex",
    "Выручка": "revenue",
    "Операционные расходы": "opex",
    "Расходы на оплату труда": "labour",
    "Коммунальные услуги": "utilities",
    "Страховые премии": "insurance",
    "Процентные расходы": "interest",
    "Налоги": "taxes",
    "Арендные платежи": "lease",
}


# --------------------------------------------------------------------------- helpers
def usd(t):
    if t["amount"] is None:
        return Z
    return t.get("amount_usd", t["amount"])


def total(txns, *cats, related=None, one_off=None, q4=False):
    s = Z
    for t in txns:
        if t["excluded"] or t["amount"] is None:
            continue
        if cats and t["category"] not in cats:
            continue
        if related is not None and t["related_party"] != related:
            continue
        if one_off is not None and t["one_off"] != one_off:
            continue
        if q4 and t["date"][5:7] not in ("10", "11", "12"):
            continue
        s += usd(t)
    return abs(s)


def ebitda(x):
    return total(x, "revenue") - total(x, "opex")


def rp(x):
    """Related-party payments, counted in full regardless of expense category."""
    return total(x, related=True)


def div(a, b):
    return a / b if b else Z


def quoted_category(spec, default=None):
    """The «...» category a 'по категории' clause constrains."""
    for q in re.findall(r"«([^»]+)»", spec["text"]):
        if q in CATEGORY_WORDS:
            return CATEGORY_WORDS[q]
    return default


def group_additions(f):
    return Decimal(f["group"]["parent"]["additions"])


def disclosed(f):
    return sum((Decimal(d["amount"]) for d in f["_adj"]["disclosed_liabilities"]), Z)


# --------------------------------------------------------------------------- library
# (name, wording pattern, requires, metric)
# `requires` may be a tuple or a callable(spec) -> tuple.
LIBRARY = [
    ("interest_coverage",
     r"Минимальный коэффициент покрытия процентов",
     ("revenue", "opex", "interest"),
     lambda x, f, s: div(ebitda(x), total(x, "interest"))),

    ("individual_overhead_ceiling",
     r"Individual Overhead Line Ceiling",
     ("labour", "utilities"),
     lambda x, f, s: max(total(x, "labour"), total(x, "utilities"))),

    ("q4_revenue_floor",
     r"Минимальная выручка за четвёртый квартал",
     ("revenue",),
     lambda x, f, s: total(x, "revenue", q4=True)),

    ("capital_intensity",
     r"Maximum Capital Intensity Ratio",
     ("capex", "opex", "lease"),
     lambda x, f, s: div(total(x, "capex"), total(x, "opex") + total(x, "lease"))),

    ("sources_cover_applications",
     r"Minimum Cover of Applications by Sources",
     ("revenue", "financing_inflow", "opex", "capex"),
     lambda x, f, s: div(total(x, "revenue") + total(x, "financing_inflow"),
                         total(x, "opex") + total(x, "capex"))),

    ("springing_leverage",
     r"Springing Drawdown Leverage Test",
     ("financing_inflow", "revenue", "opex"),
     lambda x, f, s: div(total(x, "financing_inflow"), ebitda(x))),

    ("adjusted_ebitda_margin",
     r"Минимальная скорректированная рентабельность по EBITDA",
     ("revenue", "opex"),
     lambda x, f, s: div(ebitda(x) + total(x, "opex", one_off=True), total(x, "revenue"))),

    ("group_capex_to_ebitda",
     r"Максимальное отношение капитальных затрат Группы к EBITDA",
     ("@group", "revenue", "opex"),
     lambda x, f, s: div(group_additions(f), ebitda(x))),

    ("related_party_share_of_opex",
     r"Максимальная доля платежей связанным сторонам в операционных расходах",
     ("@related", "opex"),
     lambda x, f, s: div(rp(x), total(x, "opex"))),

    ("revenue_cover_of_payroll_and_utilities",
     r"Минимальное покрытие расходов на персонал и коммунальные услуги выручкой",
     ("revenue", "labour", "utilities"),
     lambda x, f, s: div(total(x, "revenue"), total(x, "labour") + total(x, "utilities"))),

    ("tax_and_utility_burden",
     r"Максимальное отношение налоговой и коммунальной нагрузки к EBITDA",
     ("taxes", "utilities", "revenue", "opex"),
     lambda x, f, s: div(total(x, "taxes") + total(x, "utilities"), ebitda(x))),

    ("total_personnel_obligations",
     r"Максимальные совокупные обязательства по персоналу",
     ("labour", "@disclosed"),
     lambda x, f, s: total(x, "labour") + disclosed(f)),

    ("unrestricted_subsidiary_transfers",
     r"Максимальная доля активов, переданных неограниченным дочерним",
     ("capex_transfer", "capex"),
     lambda x, f, s: div(total(x, "capex_transfer"),
                         total(x, "capex") + total(x, "capex_transfer"))),

    ("insurance_cover_of_premises_costs",
     r"Минимальное страховое покрытие расходов на содержание помещений",
     ("insurance", "lease", "utilities"),
     lambda x, f, s: div(total(x, "insurance"), total(x, "lease") + total(x, "utilities"))),

    ("revenue_less_largest_overhead",
     r"Минимальная выручка за вычетом наибольшей статьи накладных расходов",
     ("revenue", "labour", "taxes"),
     lambda x, f, s: total(x, "revenue") - max(total(x, "labour"), total(x, "taxes"))),

    ("related_party_ratio_of_revenue",
     r"Maximum Related-Party Payments as a Proportion of Revenue",
     ("@related", "revenue"),
     lambda x, f, s: div(rp(x), total(x, "revenue"))),

    ("related_party_cap",
     r"Максимальные платежи связанным сторонам",
     ("@related",),
     lambda x, f, s: rp(x)),

    # category-parameterised: the constrained line is read from the «...» quote
    ("category_revenue_floor",
     r"Минимальная выручка по категории",
     lambda s: (quoted_category(s, "revenue"),),
     lambda x, f, s: total(x, quoted_category(s, "revenue"))),

    ("category_expense_cap",
     r"Максимальные расходы по категории",
     lambda s: (quoted_category(s, "capex"),),
     lambda x, f, s: total(x, quoted_category(s, "capex"))),
]


class UnmatchedCovenant(Exception):
    pass


def resolve(spec):
    """clause text -> (name, requires, metric). Raises if nothing matches."""
    text = re.sub(r"\s+", " ", spec["text"])
    hits = [e for e in LIBRARY if re.search(e[1], text)]
    if not hits:
        raise UnmatchedCovenant(
            f"no metric in the library matches this covenant wording; a formula must "
            f"be added to metrics.LIBRARY before this cell can be computed:\n  "
            f"{text[:220]}")
    # longest pattern wins, so a specific wording beats a generic one
    name, _pat, requires, fn = max(hits, key=lambda e: len(e[1]))
    return name, (requires(spec) if callable(requires) else requires), fn
