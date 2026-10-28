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


def test_a_lone_letters_code_folds_beside_a_digit_code_of_its_merchant() -> None:
    # the rules strip a code with a digit, so only the raw descriptors show that this
    # merchant bills under per-charge codes: the digit code is the second code
    descriptors = [
        "ACMEAPP 12 ELM AVENUE ELMTON 99999 TX USA",
        "ACMEAPP-QW3RTYUI 12 ELM AVENUE ELMTON 99999 TX USA",
        "ACMEAPP-ASDFGHJK 12 ELM AVENUE ELMTON 99999 TX USA",
    ]
    names = {rule_normalize(raw) for raw in descriptors}
    assert names == {"Acmeapp", "Acmeapp-Asdfghjk"}
    assert merge_targets(names, descriptors=descriptors) == {"Acmeapp-Asdfghjk": "Acmeapp"}
    assert merge_targets(names) == {}  # without the descriptors one code is one name


def test_a_hyphenated_name_beside_its_first_word_stays_apart() -> None:
    descriptors = ["CORNER", "CORNER-SANDWICH"]
    names = {rule_normalize(raw) for raw in descriptors}
    assert merge_targets(names, descriptors=descriptors) == {}


async def test_a_letters_code_beside_digit_codes_forms_one_subscription(
    session: AsyncSession,
) -> None:
    acct = await make_account(session)
    for month, code in ((4, ""), (5, "-QW3RTYUI"), (6, "-ASDFGHJK")):
        await make_txn(
            session,
            acct,
            description=f"ACMEAPP{code} 12 ELM AVENUE ELMTON 99999 TX USA",
            amount_cents=-1500,
            posted_on=date(2026, month, 3),
        )
    await normalize_merchants(session, llm=None)

    assert await renormalize_merchants(session) == 1
    assert await renormalize_merchants(session) == 0

    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    series = (await session.execute(select(RecurringSeries))).scalar_one()
    assert series.merchant == "Acmeapp" and series.expected_cents == -1500


def test_short_codes_fold_once_two_differ() -> None:
    assert merge_targets(["Acme Cloud Q1W2E3", "Acme Cloud 4R5T6Y"]) == {
        "Acme Cloud Q1W2E3": "Acme Cloud",
        "Acme Cloud 4R5T6Y": "Acme Cloud",
    }
    # inside the name, not only at its end
    assert merge_targets(["Acme Energy K1234 Auto", "Acme Energy K5678 Auto"]) == {
        "Acme Energy K1234 Auto": "Acme Energy Auto",
        "Acme Energy K5678 Auto": "Acme Energy Auto",
    }
    # onto a one-word brand, which otherwise absorbs only places
    assert merge_targets(["Streamco Q1W2E3A", "Streamco R4T5Y6U"]) == {
        "Streamco Q1W2E3A": "Streamco",
        "Streamco R4T5Y6U": "Streamco",
    }


def test_one_short_code_stays_part_of_the_name() -> None:
    assert merge_targets(["Streamco", "Streamco Q1W2E3"]) == {}


def test_letter_digit_words_that_are_not_codes_stay() -> None:
    # too short to be a code: a name's own letter-digit word
    assert merge_targets(["Acme O1", "Acme O2"]) == {}
    # an ordinal is a street, not a code
    assert merge_targets(["Acme Diner 21St", "Acme Diner 33Rd"]) == {}
    # a masked account number names an account: two are two payees
    assert merge_targets(["Acme Transfer To Xxx1234", "Acme Transfer To Xxx5678"]) == {}
    # names that differ in more than one word are different names
    assert merge_targets(["Acme Q1W2 Alpha", "Acme R3T4 Bravo"]) == {}
    # the rest of the name needs four letters, like any merge target
    assert merge_targets(["Abc Q1W2E3", "Abc R4T5Y6"]) == {}


def test_a_payment_rail_keeps_its_codes() -> None:
    rail = ["Payrail Xfer Alpha", "Payrail Xfer Bravo", "Payrail Xfer Charlie"]
    # right behind a prefix shared by 3+ different names, a code may be all that names
    # the payee
    assert merge_targets([*rail, "Payrail Xfer Q1W2E3", "Payrail Xfer R4T5Y6"]) == {}
    # a merchant behind the rail still sheds its codes
    assert merge_targets(
        [*rail, "Payrail Xfer Streamco Q1W2E3", "Payrail Xfer Streamco R4T5Y6"]
    ) == {
        "Payrail Xfer Streamco Q1W2E3": "Payrail Xfer Streamco",
        "Payrail Xfer Streamco R4T5Y6": "Payrail Xfer Streamco",
    }


def test_shed_codes_never_turn_a_payment_rail_into_a_merchant() -> None:
    # three different tails make the bare rail generic; once one payee's two codes fold,
    # only two payees remain behind it, but the rail must still not absorb them
    names = ["Payrail Xfer", "Payrail Xfer Alpha Q1W2E3", "Payrail Xfer Alpha R4T5Y6"]
    assert merge_targets([*names, "Payrail Xfer Bravo"]) == {
        "Payrail Xfer Alpha Q1W2E3": "Payrail Xfer Alpha",
        "Payrail Xfer Alpha R4T5Y6": "Payrail Xfer Alpha",
    }


async def test_short_codes_form_one_subscription(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, code in ((4, "Q1W2E3A"), (5, "R4T5Y6U"), (6, "Z7X8C9V")):
        await make_txn(
            session,
            acct,
            description=f"PAYRAIL XFER STREAMCO*{code} WEB ID: PAYRAIL77",
            amount_cents=-1199,
            posted_on=date(2026, month, 3),
        )
    await normalize_merchants(session, llm=None)

    assert await renormalize_merchants(session) == 3
    assert await renormalize_merchants(session) == 0

    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    series = (await session.execute(select(RecurringSeries))).scalar_one()
    assert series.merchant == "Payrail Xfer Streamco"


def test_rule_normalize_strips_streets_with_hyphens_initials_and_abbreviations() -> None:
    # a hyphenated street name
    assert rule_normalize("ACME AI 17 DU NORD-EST AVE O SUITE 9 ELMTON QC CAN") == "Acme Ai"
    # initials and a suffix: four words, one with a period, before the street type
    assert rule_normalize("ACMEHUB INC. 12 ELM P OAK JR. STREET ELMTON99999 CA USA") == "Acmehub"
    # an abbreviated direction before an ordinal street
    assert rule_normalize("STREAMCO USA 12 E. 3RD STREET ELMTON 99999 NY USA") == "Streamco"
    assert rule_normalize("ACME DINER 0042 77 N. ELM AVE ELMTON 99999 TX USA") == "Acme Diner"


def test_rule_normalize_strips_a_house_number_glued_onto_the_name() -> None:
    # a fixed-width name field runs straight into the house number; its last, truncated
    # word goes with the address, as the reference-token rule drops a long one
    assert rule_normalize("ACME.AI SUBSCRIPTION12 ELM STREET PMB 4321 ELMTON99999 CA USA") == (
        "Acme.Ai"
    )
    assert rule_normalize("ACME DINER RESTAURA4100 OAK LINE RD STE 7 ELMTON 99999 TX USA") == (
        "Acme Diner"
    )
    assert rule_normalize("ACME *CHATBOT SUBSCR5 ELM STREET ELMTON CA USA") == "Acme Chatbot"
    # a glued word that starts the name is the name
    assert rule_normalize("SQ *ACMEHUB12 ELM STREET ELMTON CA USA") == "Acmehub"


def test_a_glued_word_right_after_a_processor_star_stays() -> None:
    # behind a processor's star the first word names the merchant it bills for; dropping
    # it would leave only the processor, and two of its merchants would share that name
    assert rule_normalize("ZAPPROC.NET* ACMESOUND12 ELM BLVD 9 ELMTON 99999 NY USA") == (
        "Zapproc.Net Acmesound"
    )
    assert rule_normalize("ZAPPROC.NET*BETAAPP34 OAK ST ELMTON NY USA") == "Zapproc.Net Betaapp"


def test_a_glued_name_word_keeps_the_name_other_descriptors_start_with() -> None:
    raws = ("ACME *CHATBOT SUBSCR5 ELM STREET ELMTON CA USA", "ACME *CHATBOT SUBSELMTON CA")
    assert merge_targets({rule_normalize(raw) for raw in raws}) == {
        "Acme Chatbot Subselmton": "Acme Chatbot"
    }


def test_rule_normalize_strips_more_street_types() -> None:
    assert rule_normalize("ACME ONLINE 4400 OAK PARKWAY ELMTON CA USA") == "Acme Online"
    assert rule_normalize("ACME SUSHI 3100 ELM BOULEVARD S ELMTON 99999 KS USA") == "Acme Sushi"
    assert rule_normalize("ACMEHUB 1234 OAK COURT ELMTON99999 CA USA") == "Acmehub"
    assert rule_normalize("ACME CARE 86 ELM CT E STE 4 ELMTON GA USA") == "Acme Care"
    assert rule_normalize("ACME HARDWARE 2468 HWY 12 ELMTON TX") == "Acme Hardware"
    assert rule_normalize("ACME LABS 1357 OAK LINE STE 40 ELMTON 99999 TX USA") == "Acme Labs"


def test_rule_normalize_reads_a_trailing_ct_as_the_state() -> None:
    # "CT" is a state tail, not a court: the store number before the city is no house number
    assert rule_normalize("ACME PHARMACY #05436 ELMTON CT") == "Acme Pharmacy Elmton"
    assert rule_normalize("ACME 24 HOUR GYM ELMTON CT") == "Acme 24 Hour Gym Elmton"
    assert rule_normalize("ACME 5 STAR DINER ELMTON CT") == "Acme 5 Star Diner Elmton"


def test_rule_normalize_never_strands_a_store_hash_before_an_address() -> None:
    assert rule_normalize("ACME PHARMACY #1234 ST ELMTON MO") == "Acme Pharmacy"


def test_rule_normalize_strips_a_numbered_building_before_the_street_address() -> None:
    assert rule_normalize("STREAMCO 7 ACME TRADE CENTER 100 ELM STREET ELMTON 99999 NY USA") == (
        "Streamco"
    )
    # a number in the merchant's own name is not a building
    assert rule_normalize("ACME 24 HOUR GYM 100 ELM STREET ELMTON TX") == "Acme 24 Hour Gym"
    assert rule_normalize("24 HOUR ACME CENTER 100 ELM STREET ELMTON TX") == "24 Hour Acme Center"


async def test_address_variants_form_one_subscription(session: AsyncSession) -> None:
    acct = await make_account(session)
    raws = (
        "STREAMCO USA 12 E. 3RD STREET ELMTON 99999 NY USA",
        "STREAMCO X1Y2Z3W4V5 7 ACME TRADE CENTER 100 ELM STREET ELMTON 99999 NY USA",
        "STREAMCO USA 12 E. 3RD STREET ELMTON 99999 NY USA",
    )
    for month, raw in enumerate(raws, start=4):
        await make_txn(
            session, acct, description=raw, amount_cents=-1199, posted_on=date(2026, month, 3)
        )
    await normalize_merchants(session, llm=None)
    await renormalize_merchants(session)

    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    series = (await session.execute(select(RecurringSeries))).scalar_one()
    assert series.merchant == "Streamco"
