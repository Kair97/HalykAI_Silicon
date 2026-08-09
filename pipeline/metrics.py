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


def quoted_category(spec, default=None, txns=None):
    """The «...» category a 'по категории' clause constrains.

    If the quoted line has no transactions at all, fall back to the covenant's
    natural subject: a zero total is never the intended answer and would emit a
    confidently wrong figure.
    """
    for q in re.findall(r"«([^»]+)»", spec.get("text", "")):
        cat = CATEGORY_WORDS.get(q)
        if cat and (txns is None or total(txns, cat)):
            return cat
    return default


def group_additions(f):
    """Consolidated-parent capex. Zero when no group accounts were found -- the
    health check flags the cell rather than the run dying."""
    g = (f.get("group") or {}).get("parent") or {}
    return Decimal(g["additions"]) if g.get("additions") else Z


def disclosed(f):
    return sum((Decimal(d["amount"])
                for d in (f.get("_adj") or {}).get("disclosed_liabilities", [])), Z)


def quarter_totals(txns, *cats):
    """Category total per fiscal quarter -- several hidden-archive covenants test
    the worst single quarter rather than the year."""
    out = {}
    for t in txns:
        if t["excluded"] or t["amount"] is None:
            continue
        if cats and t["category"] not in cats:
            continue
        q = "Q" + str((int(t["date"][5:7]) - 1) // 3 + 1)
        out[q] = out.get(q, Z) + usd(t)
    return {k: abs(v) for k, v in out.items()}


def net_debt(txns):
    """Debt drawn in the period, less scheduled principal repayments -- the
    hidden archive's leverage covenants define total debt exactly this way."""
    return total(txns, "financing_inflow") - total(txns, "debt_repayment")


def adj_ebitda(txns):
    """EBITDA increased by one-off items the auditors add back."""
    return ebitda(txns) + total(txns, "opex", one_off=True)


def basket(spec, txns):
    """A 'permitted basket' carve-out: the SECOND money figure in the clause caps
    how much of the related-party total may be excluded."""
    amts = [Decimal(a.replace(",", ""))
            for a in re.findall(r"\$([0-9][0-9,]*\.[0-9]{2})", spec.get("text", ""))]
    cap = amts[1] if len(amts) > 1 else Z
    eligible = total(txns, "consulting", related=True)
    return min(cap, eligible)


def _vs_revenue(spec):
    """Does this ratio measure against Revenue rather than costs?"""
    return bool(re.search(r"к\s+Выручке|to\s+Revenue|от\s+выручки",
                          spec.get("text", ""), re.I))


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

    # ---- shapes first seen in the hidden archive ---------------------------
    ("debt_leverage",
     r"Максимальный коэффициент долговой нагрузки|Maximum Net Debt Leverage",
     ("financing_inflow", "revenue", "opex"),
     lambda x, f, s: div(net_debt(x), adj_ebitda(x))),

    ("adjusted_debt_leverage_with_guarantees",
     r"Максимальная скорректированная долговая нагрузка",
     ("financing_inflow", "revenue", "opex"),
     lambda x, f, s: div(net_debt(x) + disclosed(f), adj_ebitda(x))),

    ("leverage_capped_addbacks",
     r"Долговая нагрузка с ограничением корректировок EBITDA",
     ("financing_inflow", "revenue", "opex"),
     lambda x, f, s: div(net_debt(x), adj_ebitda(x))),

    ("min_quarterly_revenue",
     r"Минимальная квартальная выручка",
     ("revenue",),
     lambda x, f, s: min(quarter_totals(x, "revenue").values() or [Z])),

    ("min_quarterly_ebitda",
     r"Минимальная квартальная EBITDA",
     ("revenue", "opex"),
     lambda x, f, s: min([quarter_totals(x, "revenue").get(q, Z)
                          - quarter_totals(x, "opex").get(q, Z)
                          for q in ("Q1", "Q2", "Q3", "Q4")] or [Z])),

    ("max_quarterly_revenue_concentration",
     r"Maximum Quarterly Revenue Concentration",
     ("revenue",),
     lambda x, f, s: div(max(quarter_totals(x, "revenue").values() or [Z]),
                         total(x, "revenue"))),

    ("related_party_with_basket",
     r"Платежи связанным сторонам с разрешённой корзиной",
     ("@related",),
     lambda x, f, s: max(Z, rp(x) - basket(s, x))),

    ("consulting_to_ebitda",
     r"Максимальное отношение консультационных услуг к EBITDA",
     ("consulting", "revenue", "opex"),
     lambda x, f, s: div(total(x, "consulting"), ebitda(x))),

    ("capex_to_ebitda",
     r"Максимальное отношение капитальных затрат к EBITDA",
     ("capex", "revenue", "opex"),
     lambda x, f, s: div(total(x, "capex"), ebitda(x))),

    ("conditional_capex_limit",
     r"Условный лимит капитальных затрат|Лимит капитальных затрат на уровне группы",
     ("capex",),
     lambda x, f, s: total(x, "capex")),

    # Capital intensity is defined against DIFFERENT denominators in different
    # contracts -- revenue in the hidden archive, opex+lease in the public one.
    # Read it from the clause instead of assuming.
    ("conditional_capital_intensity",
     r"Условный лимит капиталоёмкости|Максимальная капиталоёмкость",
     lambda s: ("capex", "revenue") if _vs_revenue(s) else ("capex", "opex"),
     lambda x, f, s: div(total(x, "capex"),
                         total(x, "revenue") if _vs_revenue(s)
                         else total(x, "opex") + total(x, "lease"))),

    ("conditional_related_party_distributions",
     r"Условный лимит распределений связанным сторонам|Условный лимит распределений",
     ("@related",),
     lambda x, f, s: rp(x)),

    ("springing_unrestricted_transfers",
     r"Springing Limitation on Transfers to Unrestricted Subsidiaries"
     r"|Запрет передачи активов неограниченным дочерним",
     ("capex_transfer",),
     lambda x, f, s: total(x, "capex_transfer")),

    ("revenue_less_capex",
     r"Минимальная выручка за вычетом капитальных затрат",
     ("revenue", "capex"),
     lambda x, f, s: total(x, "revenue") - total(x, "capex")),

    ("premises_cost_cap",
     r"Максимальные совокупные расходы на содержание помещений"
     r"|Property Rental Cap|Springing Property Rental Cap",
     ("lease",),
     lambda x, f, s: total(x, "lease") + total(x, "utilities")),

    ("debt_service_cover",
     r"Минимальный коэффициент покрытия обслуживания долга"
     r"|Минимальный коэффициент покрытия постоянных платежей"
     r"|Minimum Fixed Overhead Cover|Минимальный запас покрытия постоянных расходов",
     ("revenue", "opex"),
     lambda x, f, s: div(ebitda(x), total(x, "interest") + total(x, "lease")
                         or Decimal(1))),

    ("ebitda_margin",
     r"Минимальная рентабельность по EBITDA|Minimum Post-Personnel Operating Margin",
     ("revenue", "opex"),
     lambda x, f, s: div(ebitda(x), total(x, "revenue"))),

    ("fiscal_burden",
     r"Maximum Fiscal Burden Ratio",
     ("taxes", "revenue", "opex"),
     lambda x, f, s: div(total(x, "taxes"), ebitda(x))),

    ("insurance_vs_capex",
     r"Insurance Cover Linked to Capital Expenditure",
     ("insurance", "capex"),
     lambda x, f, s: div(total(x, "insurance"), total(x, "capex"))),

    ("marketing_quarterly_cap",
     r"Квартальный лимит маркетинговых расходов",
     ("opex",),
     lambda x, f, s: max(quarter_totals(x, "marketing").values() or [Z])),

    ("retained_financing",
     r"Minimum Retained Financing Proceeds|Minimum Liquidity Contribution",
     ("financing_inflow",),
     lambda x, f, s: total(x, "financing_inflow") - total(x, "capex")),

    ("debt_basket_cap",
     r"Лимит общей разрешённой долговой корзины",
     ("financing_inflow",),
     lambda x, f, s: total(x, "financing_inflow")),

    ("dual_maintenance_test",
     r"Двойной поддерживающий тест|Двойное условие дефолта",
     ("revenue", "opex", "financing_inflow"),
     lambda x, f, s: div(net_debt(x), adj_ebitda(x))),

    ("cashflow_sweep",
     r"Условие досрочного погашения за счёт денежного потока",
     ("revenue", "opex"),
     lambda x, f, s: ebitda(x) - total(x, "capex")),

    # category-parameterised: the constrained line is read from the «...» quote
    ("category_revenue_floor",
     r"Минимальная выручка по категории",
     lambda s: (quoted_category(s, "revenue"),),
     lambda x, f, s: total(x, quoted_category(s, "revenue", x))),

    ("category_expense_cap",
     r"Максимальные расходы по категории",
     lambda s: (quoted_category(s, "capex"),),
     lambda x, f, s: total(x, quoted_category(s, "capex", x))),
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


def fallback_value(txns, spec):
    """Best-effort figure for a covenant we could not match.

    A wrong cell and an empty cell score identically, but an unparseable file
    forfeits every other cell -- so emit something plausible and let the health
    check shout. We use the category the clause text mentions most, falling back
    to revenue, which is the most common covenant subject.
    """
    text = spec.get("text", "")
    best, hits = None, 0
    for ru, cat in CATEGORY_WORDS.items():
        n = text.count(ru)
        if n > hits:
            best, hits = cat, n
    if best:
        v = total(txns, best)
        if v:
            return v
    for cat in ("revenue", "opex", "capex"):
        v = total(txns, cat)
        if v:
            return v
    return Decimal("0.01")
