from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    AliasSource,
    MerchantAlias,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesStatus,
    Transaction,
)
from moneta.pipelines.normalize import (
    looks_clean,
    merge_targets,
    normalize_merchants,
    renormalize_merchants,
    rule_normalize,
)
from moneta.pipelines.recurring import detect_recurring
from tests.factories import make_account, make_series, make_txn


class FakeLLM:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        self.calls.append(prompt)
        for needle, merchant in self.answers.items():
            if needle in prompt:
                return {"merchant": merchant}
        return None


def test_rule_normalize() -> None:
    assert rule_normalize("NETFLIX.COM") == "Netflix.Com"
    assert rule_normalize("SQ *BLUE BOTTLE #1234") == "Blue Bottle"
    assert rule_normalize("TST* JOES  DINER 0042") == "Joes Diner"
    assert rule_normalize("PAYPAL *SPOTIFY") == "Spotify"


def test_rule_normalize_keeps_hyphen_joined_digit_runs() -> None:
    # a hyphen-joined digit run (e.g. a vanity phone number) is not a store number
    # and must survive normalization intact.
    assert rule_normalize("1-800-FLOWERS.COM") == "1-800-Flowers.Com"
    assert rule_normalize("BLUE BOTTLE #1234") == "Blue Bottle"
    assert rule_normalize("STORE 4521") == "Store"


def test_looks_clean() -> None:
    assert looks_clean("Netflix.Com")
    assert not looks_clean("X4529182")
    assert not looks_clean("")


async def test_normalize_uses_rules_and_caches(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="NETFLIX.COM")
    await make_txn(session, acct, description="NETFLIX.COM")
    n = await normalize_merchants(session, llm=None)
    assert n == 2
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert all(t.merchant == "Netflix.Com" for t in txns)
    alias = (await session.execute(select(MerchantAlias))).scalar_one()
    assert alias.raw_descriptor == "NETFLIX.COM" and alias.source == "rule"


async def test_normalize_falls_back_to_llm(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="X4529182 84756")
    llm = FakeLLM({"X4529182": "Mystery Gym"})
    await normalize_merchants(session, llm=llm)
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.merchant == "Mystery Gym"
    alias = (await session.execute(select(MerchantAlias))).scalar_one()
    assert alias.source == "llm"


async def test_normalize_opens_review_without_llm(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="X4529182 84756")
    await normalize_merchants(session, llm=None)
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.merchant is not None  # rule fallback still applied
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == "merchant"


class BlankLLM:
    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        return {"merchant": "  "}


class MalformedLLM:
    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        return {"merchant": {"name": "X"}}  # truthy non-str must degrade, never crash sync


async def test_normalize_rejects_non_string_llm_merchant(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="X4529182 84756")
    await normalize_merchants(session, llm=MalformedLLM())
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.merchant == rule_normalize("X4529182 84756")  # rule fallback, not a dict
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == "merchant"
    alias = (await session.execute(select(MerchantAlias))).scalar_one()
    assert alias.source == "rule"


async def test_normalize_rejects_empty_llm_merchant(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="X4529182 84756")
    await normalize_merchants(session, llm=BlankLLM())
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.merchant == rule_normalize("X4529182 84756")  # rule fallback, not blank
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == "merchant"
    alias = (await session.execute(select(MerchantAlias))).scalar_one()
    assert alias.source == "rule"


async def test_alias_cache_skips_llm(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="X4529182 84756")
    llm = FakeLLM({"X4529182": "Mystery Gym"})
    await normalize_merchants(session, llm=llm)
    await make_txn(session, acct, description="X4529182 84756")
    await normalize_merchants(session, llm=llm)
    assert len(llm.calls) == 1  # second run served from MerchantAlias


def test_rule_normalize_strips_reference_tokens() -> None:
    # payment-reference tokens (8+ chars containing a digit) must not fragment merchants
    variants = [
        "BILT PAYMENT BILTRENT f3d9bb17a1f74fa WEB ID: 9999918544",
        "BILT PAYMENT BILTRENT 262f2feddc034a5 WEB ID: 9999918544",
        "BILT PAYMENT BILTRENT 311233ba29b5469 WEB ID: 9999918544",
    ]
    names = {rule_normalize(v) for v in variants}
    assert len(names) == 1
    assert names.pop() == "Bilt Payment Biltrent"


async def test_renormalize_merges_fragmented_merchants(session: AsyncSession) -> None:
    from moneta.models import RecurringSeries

    acct = await make_account(session)
    raws = [
        "BILT PAYMENT BILTRENT f3d9bb17a1f74fa WEB ID: 9999918544",
        "BILT PAYMENT BILTRENT 262f2feddc034a5 WEB ID: 9999918544",
        "BILT PAYMENT BILTRENT 311233ba29b5469 WEB ID: 9999918544",
    ]
    old_names = ["Bilt Payment Biltrent F3D9Bb17A1F74Fa Web Id:", "Bilt 262F2F", "Bilt Rent"]
    for raw, old in zip(raws, old_names, strict=True):
        await make_txn(session, acct, description=raw, merchant=old, amount_cents=-249004)
        session.add(
            MerchantAlias(
                raw_descriptor=raw,
                merchant=old,
                source=AliasSource.llm if old == "Bilt Rent" else AliasSource.rule,
            )
        )
    # a manual alias must never be touched
    await make_txn(session, acct, description="SOME RAW 12345678x", merchant="My Custom Name")
    session.add(
        MerchantAlias(
            raw_descriptor="SOME RAW 12345678x",
            merchant="My Custom Name",
            source=AliasSource.manual,
        )
    )
    await session.flush()

    changed = await renormalize_merchants(session)
    assert changed == 3
    txns = (await session.execute(select(Transaction))).scalars().all()
    bilt_names = {t.merchant for t in txns if t.description.startswith("BILT")}
    assert bilt_names == {"Bilt Payment Biltrent"}
    manual = next(t for t in txns if t.description == "SOME RAW 12345678x")
    assert manual.merchant == "My Custom Name"
    assert (await session.execute(select(RecurringSeries))).scalars().all() == []


def test_rule_normalize_strips_wallets_legal_ach_address_and_state() -> None:
    assert rule_normalize("APLPAY TST* ACME GRILL 0ELMTON TX") == "Acme Grill 0Elmton"
    assert rule_normalize("ACME FITNESS LLC RIVERTON TX") == "Acme Fitness Riverton"
    assert rule_normalize("ACME FITNESS LLC 500 EAST MAIN ROAD RIVERTON TX USA") == "Acme Fitness"
    assert rule_normalize("STREAMCO *SUBSCRIPTION 3RD STREET SAN CA USA") == (
        "Streamco Subscription"
    )
    assert rule_normalize("DD *STREAMCOPASS SAN FRANCISCO CA") == "Streamcopass San Francisco"
    assert rule_normalize("GOOGLE PAY ACME CO") == "Acme"
    assert rule_normalize("ACME PAYROLL PPD ID: 1234567890") == "Acme Payroll"
    assert rule_normalize("TX") == "Tx"  # a lone token is never stripped to nothing


def test_rule_normalize_requires_word_boundaries_on_bare_wallets() -> None:
    # bare-word wallets need word boundaries to avoid matching partial words
    assert rule_normalize("APPLE PAYROLL SVC") == "Apple Payroll Svc"
    assert rule_normalize("GOOGLE PAYMENT 1234") == "Google Payment"
    # optional space before * in symbol prefixes
    assert rule_normalize("SQ*ACME GRILL") == "Acme Grill"


_PLACES = ["Kroger", "Kroger Elmton", "Target", "Target Elmton", "Lidl", "Lidl Elmton"]


def test_multi_token_name_absorbs_longer_names() -> None:
    assert merge_targets(
        ["Acme Fitness", "Acme Fitness Riverton", "Acme Fitness Of Texlakeside"]
    ) == {
        "Acme Fitness Riverton": "Acme Fitness",
        "Acme Fitness Of Texlakeside": "Acme Fitness",
    }


def test_single_token_name_absorbs_only_a_place() -> None:
    names = [*_PLACES, "Streamco", "Streamco Elmton", "Streamco Plus", "Streamco Energy Ven"]
    targets = merge_targets(names)
    assert targets["Streamco Elmton"] == "Streamco"
    assert "Streamco Plus" not in targets  # brand + product stays its own merchant
    assert "Streamco Energy Ven" not in targets
    assert merge_targets([*_PLACES, "Shyco", "Shyco 0Elmton"])["Shyco 0Elmton"] == "Shyco"


def test_merge_needs_four_letters_and_resolves_chains() -> None:
    assert "Abc Fitness" not in merge_targets(["Abc", "Abc Fitness"])
    chained = merge_targets([*_PLACES, "Shyco", "Shyco Elmton", "Shyco Elmton Hifi"])
    assert chained["Shyco Elmton Hifi"] == "Shyco"


async def test_renormalize_merges_names_and_renames_series_and_decisions(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for raw in ("ACME FITNESS LLC RIVERTON TX", "APLPAY ACME FITNESS"):
        await make_txn(session, acct, description=raw)
    await make_txn(session, acct, description="ACME FITNESS ELMTON")
    session.add(
        MerchantAlias(
            raw_descriptor="ACME FITNESS ELMTON", merchant="My Gym", source=AliasSource.manual
        )
    )
    await session.flush()
    await normalize_merchants(session, llm=None)  # "Acme Fitness Riverton", "Acme Fitness"
    series = await make_series(session, merchant="Acme Fitness Riverton")
    session.add(
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            question="Is 'Acme Fitness Riverton' a recurring bill?",
            payload={"merchant": "Acme Fitness Riverton", "direction": "outflow"},
        )
    )
    await session.commit()

    changed = await renormalize_merchants(session)

    assert changed == 1
    txns = (await session.execute(select(Transaction))).scalars().all()
    by_raw = {t.description: t.merchant for t in txns}
    assert by_raw["ACME FITNESS LLC RIVERTON TX"] == "Acme Fitness"
    assert by_raw["ACME FITNESS ELMTON"] == "My Gym"  # manual aliases are never rewritten
    await session.refresh(series)
    assert series.merchant == "Acme Fitness"
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload["merchant"] == "Acme Fitness"


async def test_renormalize_is_idempotent(session: AsyncSession) -> None:
    acct = await make_account(session)
    for raw in ("ACME FITNESS LLC RIVERTON TX", "ACME FITNESS"):
        await make_txn(session, acct, description=raw)
    await normalize_merchants(session, llm=None)
    assert await renormalize_merchants(session) == 1
    assert await renormalize_merchants(session) == 0


def test_prefix_shared_by_many_payees_absorbs_nothing() -> None:
    payees = [
        "Zelle Payment",
        "Zelle Payment To Alice",
        "Zelle Payment To Bob",
        "Zelle Payment To Carol",
    ]
    assert merge_targets(payees) == {}
    # a place variant of the shared prefix still merges
    assert merge_targets([*_PLACES, *payees, "Zelle Payment Elmton"]) == {
        "Kroger Elmton": "Kroger",
        "Target Elmton": "Target",
        "Lidl Elmton": "Lidl",
        "Zelle Payment Elmton": "Zelle Payment",
    }


async def test_unclean_llm_name_neither_merges_nor_teaches_a_place(session: AsyncSession) -> None:
    acct = await make_account(session)
    for raw in ("ALPHA", "ALPHA ELMTON", "BRAVO", "BRAVO ELMTON", "CHARLIE"):
        await make_txn(session, acct, description=raw)
    await normalize_merchants(session, llm=None)
    await make_txn(session, acct, description="X4529182 84756")  # rule output is unclean
    await normalize_merchants(session, llm=FakeLLM({"X4529182": "Charlie Elmton"}))
    bravo = await make_series(session, merchant="Bravo")
    item = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="?",
        payload={"merchant": "Bravo", "direction": "outflow"},
    )
    session.add(item)
    await session.commit()

    await renormalize_merchants(session)
    assert await renormalize_merchants(session) == 0  # nothing flips back

    await session.refresh(bravo)
    await session.refresh(item)
    assert bravo.merchant == "Bravo"
    assert item.payload == {"merchant": "Bravo", "direction": "outflow"}
    llm_alias = (
        await session.execute(
            select(MerchantAlias).where(MerchantAlias.raw_descriptor == "X4529182 84756")
        )
    ).scalar_one()
    assert llm_alias.merchant == "Charlie Elmton" and llm_alias.source == "llm"


async def test_name_a_manual_alias_still_carries_is_not_renamed(session: AsyncSession) -> None:
    acct = await make_account(session)
    for raw in ("ACME FITNESS LLC RIVERTON TX", "ACME FITNESS", "ACME FIT RIVERTON"):
        await make_txn(session, acct, description=raw)
    session.add(
        MerchantAlias(
            raw_descriptor="ACME FIT RIVERTON",
            merchant="Acme Fitness Riverton",
            source=AliasSource.manual,
        )
    )
    await session.flush()
    await normalize_merchants(session, llm=None)
    series = await make_series(session, merchant="Acme Fitness Riverton")
    item = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="?",
        payload={"merchant": "Acme Fitness Riverton", "direction": "outflow"},
    )
    session.add(item)
    await session.commit()

    assert await renormalize_merchants(session) == 1  # the rule alias leaves the name

    await session.refresh(series)
    await session.refresh(item)
    assert series.merchant == "Acme Fitness Riverton"
    assert item.payload["merchant"] == "Acme Fitness Riverton"


async def test_llm_alias_with_clean_rule_output_becomes_a_rule_alias(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="ACME FITNESS")
    await make_txn(session, acct, description="ACME FITNESS LLC RIVERTON TX", merchant="Acme Gym")
    session.add(
        MerchantAlias(
            raw_descriptor="ACME FITNESS LLC RIVERTON TX",
            merchant="Acme Gym",
            source=AliasSource.llm,
        )
    )
    await session.flush()
    await normalize_merchants(session, llm=None)

    await renormalize_merchants(session)

    alias = (
        await session.execute(
            select(MerchantAlias).where(
                MerchantAlias.raw_descriptor == "ACME FITNESS LLC RIVERTON TX"
            )
        )
    ).scalar_one()
    assert alias.merchant == "Acme Fitness" and alias.source == "rule"


async def test_merged_away_decision_keeps_to_its_own_bill(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month in range(1, 7):
        await make_txn(
            session,
            acct,
            description="ACME FITNESS",
            amount_cents=-4999,
            posted_on=date(2026, month, 3),
        )
    for posted_on, cents in (
        (date(2026, 2, 9), -2350),
        (date(2026, 3, 20), -1720),
        (date(2026, 5, 2), -3100),
    ):
        await make_txn(
            session,
            acct,
            description="ACME FITNESS LLC RIVERTON TX",
            amount_cents=cents,
            posted_on=posted_on,
        )
    await normalize_merchants(session, llm=None)  # "Acme Fitness", "Acme Fitness Riverton"
    series = await make_series(session, merchant="Acme Fitness", expected_cents=-4999)
    shop = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="?",
        payload={"merchant": "Acme Fitness Riverton", "direction": "outflow"},
        status=ReviewStatus.resolved,
        resolution={"is_recurring": False, "resolved_by": "manual"},
    )
    session.add(shop)
    await session.commit()

    await renormalize_merchants(session)

    await session.refresh(shop)
    # the "not a bill" answer now names the shop charges' median, not every bill
    assert shop.payload == {
        "merchant": "Acme Fitness",
        "direction": "outflow",
        "amount_cents": 2350,
    }

    await detect_recurring(session, llm=None, today=date(2026, 7, 1))

    await session.refresh(series)
    assert series.status == SeriesStatus.active
    membership = (
        await session.execute(select(Transaction).where(Transaction.amount_cents == -4999))
    ).scalars()
    assert {t.series_id for t in membership} == {series.id}


async def test_llm_group_whose_rule_outputs_diverge_keeps_its_name(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    raws = (
        "ACME PARKINGMONT RIVERTON TX",
        "Acme Parking Monthly",
        "ACME PARKING MONTH 1300 ELM STREET RIVERTON 75000 TX USA",
    )
    outputs = {rule_normalize(raw) for raw in raws}
    assert len(outputs) == 3 and all(looks_clean(o) for o in outputs)
    assert merge_targets(outputs) == {}  # the rules cannot reunite them
    for raw in raws:
        await make_txn(session, acct, description=raw, merchant="Acme Parking")
        session.add(
            MerchantAlias(raw_descriptor=raw, merchant="Acme Parking", source=AliasSource.llm)
        )
    await session.flush()
    series = await make_series(session, merchant="Acme Parking")
    item = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="?",
        payload={"merchant": "Acme Parking", "direction": "outflow"},
        status=ReviewStatus.resolved,
        resolution={"is_recurring": True, "resolved_by": "manual"},
    )
    session.add(item)
    await session.commit()

    assert await renormalize_merchants(session) == 0
    assert await renormalize_merchants(session) == 0

    await session.refresh(series)
    await session.refresh(item)
    assert series.merchant == "Acme Parking"
    assert item.payload == {"merchant": "Acme Parking", "direction": "outflow"}
    aliases = (await session.execute(select(MerchantAlias))).scalars().all()
    assert {(a.merchant, a.source) for a in aliases} == {("Acme Parking", "llm")}
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert {t.merchant for t in txns} == {"Acme Parking"}


async def test_llm_group_whose_rule_outputs_land_on_one_name_flips_to_rules(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    raws = ("ACME FITNESS", "ACME FITNESS RIVERTON")
    assert merge_targets({rule_normalize(raw) for raw in raws}) == {
        "Acme Fitness Riverton": "Acme Fitness"
    }
    for raw in raws:
        await make_txn(session, acct, description=raw, merchant="Acme Gym")
        session.add(MerchantAlias(raw_descriptor=raw, merchant="Acme Gym", source=AliasSource.llm))
    await session.commit()

    assert await renormalize_merchants(session) == 2
    assert await renormalize_merchants(session) == 0

    aliases = (await session.execute(select(MerchantAlias))).scalars().all()
    assert {(a.merchant, a.source) for a in aliases} == {("Acme Fitness", "rule")}


async def test_pure_rename_keeps_decisions_merchant_level(session: AsyncSession) -> None:
    acct = await make_account(session)
    raw = "ACME PAYROLL PPD ID: 123456"
    assert rule_normalize(raw) == "Acme Payroll"
    for month, cents in enumerate((250000, 310000, 180000), start=1):
        await make_txn(
            session,
            acct,
            description=raw,
            merchant="Acme Payroll Ppd Id",
            amount_cents=cents,
            posted_on=date(2026, month, 5),
        )
    session.add(
        MerchantAlias(raw_descriptor=raw, merchant="Acme Payroll Ppd Id", source=AliasSource.rule)
    )
    item = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="?",
        payload={"merchant": "Acme Payroll Ppd Id", "direction": "inflow"},
        status=ReviewStatus.resolved,
        resolution={"is_recurring": True, "resolved_by": "manual"},
    )
    session.add(item)
    await session.commit()

    assert await renormalize_merchants(session) == 1

    await session.refresh(item)
    assert item.payload == {"merchant": "Acme Payroll", "direction": "inflow"}


async def test_kept_llm_group_is_not_a_phantom_merge_target(session: AsyncSession) -> None:
    acct = await make_account(session)
    group = ("Acme Parking Monthly", "ACME PARKINGMONT RIVERTON TX")
    plus = "ACME PARKING MONTHLY RIVERTON PLUS"
    assert rule_normalize(plus) == "Acme Parking Monthly Riverton Plus"
    for raw in group:
        await make_txn(session, acct, description=raw, merchant="Acme Garage")
        session.add(
            MerchantAlias(raw_descriptor=raw, merchant="Acme Garage", source=AliasSource.llm)
        )
    await make_txn(session, acct, description=plus, merchant="Acme Parking Monthly Riverton Plus")
    session.add(
        MerchantAlias(
            raw_descriptor=plus,
            merchant="Acme Parking Monthly Riverton Plus",
            source=AliasSource.rule,
        )
    )
    await session.commit()

    assert await renormalize_merchants(session) == 0  # the group keeps "Acme Garage"
    assert await renormalize_merchants(session) == 0

    aliases = (await session.execute(select(MerchantAlias))).scalars().all()
    assert {a.raw_descriptor: a.merchant for a in aliases} == {
        group[0]: "Acme Garage",
        group[1]: "Acme Garage",
        plus: "Acme Parking Monthly Riverton Plus",
    }


def test_rule_normalize_strips_expressway_addresses() -> None:
    assert rule_normalize("ACME DINER 4500 OAK HOLLOW EXPY ELMTON TX") == "Acme Diner"
    assert rule_normalize("ACME DINER 4500 OAK HOLLOW EXPWY ELMTON TX") == "Acme Diner"


def test_glued_places_fold_into_the_spaced_name() -> None:
    assert merge_targets(
        ["Acme University", "Acme Universityelmton", "Acme Universityriverton"]
    ) == {
        "Acme Universityelmton": "Acme University",
        "Acme Universityriverton": "Acme University",
    }
    # a one-word name absorbs only a glued place learned from other names
    assert merge_targets([*_PLACES, "Shyco", "Shycoelmton"])["Shycoelmton"] == "Shyco"
    assert merge_targets(["Shyco", "Shycobrook", "Shycofield"]) == {}


def test_one_longer_word_is_not_a_glued_place() -> None:
    # one glued variant is as likely a different word as a place
    assert merge_targets(["Corner Store", "Corner Storehouse"]) == {}
    # a short tail is a word ending, not a place, however many there are
    names = ["Acme Parking Month", "Acme Parking Monthly", "Acme Parking Months"]
    assert merge_targets(names) == {}


async def test_renormalize_folds_glued_place_descriptors(session: AsyncSession) -> None:
    acct = await make_account(session)
    for raw in ("ACME UNIVERSITY CA", "ACME UNIVERSITYELMTON CA", "ACME UNIVERSITYRIVERTON CA"):
        await make_txn(session, acct, description=raw)
    await normalize_merchants(session, llm=None)

    assert await renormalize_merchants(session) == 2
    assert await renormalize_merchants(session) == 0

    txns = (await session.execute(select(Transaction))).scalars().all()
    assert {t.merchant for t in txns} == {"Acme University"}


def test_rule_normalize_keeps_a_letters_only_code_for_the_merge_pass() -> None:
    # the address strips; a letters-only code stays, since one alone may be a real name
    assert rule_normalize("ACMEAPP-QWERTYUI 12 ELM AVENUE ELMTON 99999 TX USA") == (
        "Acmeapp-Qwertyui"
    )
    # a code with a digit is a reference token and was always stripped
    assert rule_normalize("ACMEAPP-QW3RTYUI 12 ELM AVENUE ELMTON 99999 TX USA") == "Acmeapp"


def test_per_charge_codes_fold_into_their_merchant() -> None:
    assert merge_targets(["Acmeapp-Qwertyui", "Acmeapp-Asdfghjk"]) == {
        "Acmeapp-Qwertyui": "Acmeapp",
        "Acmeapp-Asdfghjk": "Acmeapp",
    }
    # beside the name a digit code's charges already carry
    assert merge_targets(["Acmeapp", "Acmeapp-Qwertyui", "Acmeapp-Asdfghjk"]) == {
        "Acmeapp-Qwertyui": "Acmeapp",
        "Acmeapp-Asdfghjk": "Acmeapp",
    }


def test_one_hyphenated_name_is_not_a_code() -> None:
    assert merge_targets(["Corner-Sandwich"]) == {}
    assert merge_targets(["Corner", "Corner-Sandwich"]) == {}
    # the same second word twice is one name, not two codes
    assert merge_targets(["Corner-Sandwich", "Corner-Sandwich Elmton"]) == {}


def test_a_charge_code_is_exactly_eight_letters() -> None:
    # 7 or 9 letters are not codes, however many different ones there are
    assert merge_targets(["Acmeapp-Qwertyu", "Acmeapp-Asdfghj"]) == {}
    assert merge_targets(["Acmeapp-Qwertyuio", "Acmeapp-Asdfghjkl"]) == {}
    # an 8-letter code beside a 7-letter one is one code, and one code is not enough
    assert merge_targets(["Acmeapp-Qwertyui", "Acmeapp-Asdfghj"]) == {}


def test_a_charge_code_needs_a_base_word_of_four_letters() -> None:
    assert merge_targets(["Acme-Qwertyui", "Acme-Asdfghjk"]) == {
        "Acme-Qwertyui": "Acme",
        "Acme-Asdfghjk": "Acme",
    }
    assert merge_targets(["Abc-Qwertyui", "Abc-Asdfghjk"]) == {}


async def test_per_charge_codes_form_one_subscription(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, code in ((4, "QWERTYUI"), (5, "ASDFGHJK"), (6, "ZXCVBNMQ")):
        await make_txn(
            session,
            acct,
            description=f"ACMEAPP-{code} 12 ELM AVENUE ELMTON 99999 TX USA",
            amount_cents=-1500,
            posted_on=date(2026, month, 3),
        )
    await normalize_merchants(session, llm=None)

    assert await renormalize_merchants(session) == 3
    assert await renormalize_merchants(session) == 0

    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    series = (await session.execute(select(RecurringSeries))).scalar_one()
    assert series.merchant == "Acmeapp" and series.expected_cents == -1500
