"""Account identity requires evidence, and explicit decisions survive reconciliation."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, ReviewItem, ReviewKind, ReviewStatus
from tests.factories import make_account


async def pair(session: AsyncSession) -> tuple[Account, Account]:
    root = await make_account(
        session,
        source="simplefin",
        org_name="Chase",
        name="Freedom (1234)",
        mask="1234",
        type=AccountType.credit,
        balance_cents=-10000,
        promo_expires_on=date(2027, 1, 1),
        financing_mode=True,
    )
    alias = await make_account(
        session,
        source="plaid",
        org_name="JPMorgan Chase Bank",
        name="Freedom",
        mask="1234",
        type=AccountType.credit,
        balance_cents=-12000,
    )
    return root, alias


async def reviews(session: AsyncSession) -> list[ReviewItem]:
    return list(
        await session.scalars(
            select(ReviewItem)
            .where(ReviewItem.kind == ReviewKind.account_overlap)
            .order_by(ReviewItem.id)
        )
    )


async def test_same_suffix_distinct_sources_form_one_group(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root, alias = await pair(session)
    await session.commit()
    assert await propose_account_links(session) == 1
    assert root.canonical_account_id is None
    assert alias.canonical_account_id == root.id
    assert (root.balance_cents, alias.balance_cents) == (-10000, -12000)
    assert root.financing_mode and root.promo_expires_on == date(2027, 1, 1)
    items = await reviews(session)
    assert len(items) == 1 and items[0].status == ReviewStatus.resolved
    assert items[0].payload["account_id"] == alias.id
    assert items[0].payload["candidates"] == [root.id]
    assert items[0].payload["evidence"]["mask"] == "1234"
    assert items[0].resolution == {"canonical_account_id": root.id, "resolved_by": "rule"}
    assert await propose_account_links(session) == 0
    assert len(await reviews(session)) == 1
    root_id, alias_id = root.id, alias.id
    await session.rollback()
    session.expire_all()
    fresh = await session.get(Account, alias_id)
    assert fresh is not None and fresh.canonical_account_id is None
    assert await session.get(Account, root_id) is not None
    assert await reviews(session) == []


async def test_equal_balances_do_not_establish_identity(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root, alias = await pair(session)
    alias.balance_cents = root.balance_cents
    alias.mask = "9999"
    assert await propose_account_links(session) == 0
    assert alias.canonical_account_id is None


@pytest.mark.parametrize("missing", [True, False])
async def test_ambiguous_or_missing_mask_needs_review(session: AsyncSession, missing: bool) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root, alias = await pair(session)
    if missing:
        alias.mask = None
    else:
        await make_account(
            session,
            source="simplefin",
            org_name="Chase",
            mask="1234",
            name="Freedom",
            type=AccountType.credit,
        )
    assert await propose_account_links(session) == 0
    assert root.canonical_account_id is None and alias.canonical_account_id is None
    items = await reviews(session)
    assert items and all(item.status == ReviewStatus.open for item in items)
    count = len(items)
    assert await propose_account_links(session) == 0
    assert len(await reviews(session)) == count


async def test_same_mask_conflicting_products_needs_review(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root, alias = await pair(session)
    alias.name = "Sapphire Preferred"
    assert await propose_account_links(session) == 0
    assert alias.canonical_account_id is None
    assert any(root.id in item.payload["candidates"] for item in await reviews(session))


@pytest.mark.parametrize("change", ["currency", "institution", "type", "unknown", "source", "mask"])
async def test_insufficient_or_conflicting_evidence_never_links(
    session: AsyncSession,
    change: str,
) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root, alias = await pair(session)
    if change == "currency":
        alias.currency = "CAD"
    elif change == "institution":
        alias.org_name = "Chase Community Credit Union"
    elif change == "type":
        alias.type = AccountType.checking
    elif change == "unknown":
        alias.type = AccountType.unknown
    elif change == "source":
        alias.source = root.source
    else:
        alias.mask = root.mask = ""
    assert await propose_account_links(session) == 0
    assert alias.canonical_account_id is None


async def test_manual_unlink_survives_next_reconciliation(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import propose_account_links, set_account_link

    root, alias = await pair(session)
    await set_account_link(session, alias.id, root.id)
    await set_account_link(session, alias.id, None)
    await session.commit()
    session.expire_all()
    assert await propose_account_links(session) == 0
    accounts = list(await session.scalars(select(Account)))
    assert all(row.canonical_account_id is None for row in accounts)
    history = await reviews(session)
    assert [item.resolution for item in history] == [
        {"canonical_account_id": accounts[0].id, "resolved_by": "manual"},
        {"canonical_account_id": None, "resolved_by": "manual"},
    ]


async def test_manual_link_overrides_unlink_and_preserves_root_metadata(
    session: AsyncSession,
) -> None:
    from moneta.pipelines.account_overlap import propose_account_links, set_account_link

    root, alias = await pair(session)
    await set_account_link(session, alias.id, None)
    await set_account_link(session, alias.id, root.id)
    assert await propose_account_links(session) == 0
    assert alias.canonical_account_id == root.id
    assert root.name == "Freedom (1234)" and root.financing_mode
    assert len(await reviews(session)) == 2


@pytest.mark.parametrize("invalid", ["self", "cycle", "currency", "missing", "source_missing"])
async def test_rejected_manual_links_leave_relationships_and_history_unchanged(
    session: AsyncSession,
    invalid: str,
) -> None:
    from moneta.pipelines.account_overlap import set_account_link

    root, alias = await pair(session)
    await set_account_link(session, alias.id, root.id)
    source, target = root.id, alias.id
    if invalid == "self":
        target = source
    elif invalid == "currency":
        foreign = await make_account(session, currency="EUR")
        target = foreign.id
    elif invalid == "missing":
        target = 999999
    elif invalid == "source_missing":
        source, target = 999999, root.id
    before = [(row.id, row.canonical_account_id) for row in await session.scalars(select(Account))]
    with pytest.raises(ValueError):
        await set_account_link(session, source, target)
    await session.flush()
    assert [
        (row.id, row.canonical_account_id) for row in await session.scalars(select(Account))
    ] == before
    assert len(await reviews(session)) == 1


async def test_currency_validation_includes_moving_descendants(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import set_account_link

    root, alias = await pair(session)
    alias.canonical_account_id = root.id
    alias.currency = "EUR"
    target = await make_account(session, currency="USD")
    with pytest.raises(ValueError):
        await set_account_link(session, root.id, target.id)
    assert root.canonical_account_id is None and alias.canonical_account_id == root.id
    assert await reviews(session) == []


async def test_explicit_target_is_stored_as_root(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import set_account_link

    root, alias = await pair(session)
    alias.canonical_account_id = root.id
    third = await make_account(session, type=AccountType.credit)
    await set_account_link(session, third.id, alias.id)
    assert third.canonical_account_id == root.id
    item = (await reviews(session))[0]
    assert item.payload == {"account_id": third.id, "candidates": [root.id]}


async def test_manual_decision_protects_root_from_reverse_auto_link(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import propose_account_links, set_account_link

    root, alias = await pair(session)
    await set_account_link(session, root.id, None)
    assert await propose_account_links(session) == 0
    assert alias.canonical_account_id is None and root.canonical_account_id is None


async def test_existing_group_cannot_absorb_second_observation_from_same_provider(
    session: AsyncSession,
) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root, alias = await pair(session)
    alias.canonical_account_id = root.id
    other = await make_account(
        session,
        source="plaid",
        org_name="Chase",
        name="Freedom",
        mask="1234",
        type=AccountType.credit,
    )
    assert await propose_account_links(session) == 0
    assert other.canonical_account_id is None
    assert await reviews(session)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("  JPMorgan   Chase Bank  ", "chase"),
        ("CHASE", "chase"),
        ("Chase Community Credit Union", "chase community credit union"),
        ("  Small   Local Bank ", "small local bank"),
    ],
)
def test_normalize_institution_uses_exact_aliases(name: str, expected: str) -> None:
    from moneta.identity import normalize_institution

    assert normalize_institution(name) == expected


async def test_manual_decision_closes_its_open_question(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import propose_account_links, set_account_link

    root, alias = await pair(session)
    alias.mask = None
    await propose_account_links(session)
    await set_account_link(session, alias.id, root.id)
    assert all(item.status == ReviewStatus.resolved for item in await reviews(session))
    assert (await reviews(session))[-1].resolution == {
        "canonical_account_id": root.id,
        "resolved_by": "manual",
    }


async def test_new_evidence_resolves_stale_open_questions(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root, alias = await pair(session)
    alias.mask = None
    await propose_account_links(session)
    alias.mask = "1234"
    assert await propose_account_links(session) == 1
    assert all(item.status == ReviewStatus.resolved for item in await reviews(session))
    assert alias.canonical_account_id == root.id


async def test_new_provider_keeps_established_root_even_if_older(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    newcomer = await make_account(
        session,
        source="third",
        org_name="Chase",
        name="Freedom",
        mask="1234",
        type=AccountType.credit,
    )
    root, alias = await pair(session)
    alias.canonical_account_id = root.id
    assert await propose_account_links(session) == 1
    assert newcomer.canonical_account_id == root.id
    assert root.canonical_account_id is None and root.financing_mode


@pytest.mark.parametrize("invalid", ["cycle", "dangling"])
async def test_invalid_existing_graph_is_rejected_before_proposal(
    session: AsyncSession,
    invalid: str,
) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root, alias = await pair(session)
    root.canonical_account_id = alias.id if invalid == "cycle" else 999999
    if invalid == "cycle":
        alias.canonical_account_id = root.id
    with pytest.raises(ValueError):
        await propose_account_links(session)
    assert await reviews(session) == []


async def test_explicit_decision_is_rolled_back_with_callers_transaction(
    session: AsyncSession,
) -> None:
    from moneta.pipelines.account_overlap import set_account_link

    root, alias = await pair(session)
    root_id, alias_id = root.id, alias.id
    await session.commit()
    await set_account_link(session, alias_id, root_id)
    await session.rollback()
    fresh = await session.get(Account, alias_id)
    assert fresh is not None and fresh.canonical_account_id is None
    assert await reviews(session) == []


async def conflicting_metadata(session: AsyncSession, account: Account, field: str) -> None:
    if field == "type":
        account.type = AccountType.savings
    elif field == "promotion":
        account.promo_expires_on = date(2028, 1, 1)
    else:
        account.financing_mode = False
        session.add(
            ReviewItem(
                kind=ReviewKind.financing_account,
                question="Treat as financing?",
                payload={"account_id": account.id},
                status=ReviewStatus.resolved,
                resolution={"financing": False, "resolved_by": "manual"},
            )
        )
    await session.flush()


@pytest.mark.parametrize("field", ["type", "promotion", "financing"])
@pytest.mark.parametrize("location", ["source", "source_descendant", "target_descendant"])
async def test_manual_metadata_conflict_rejects_atomically(
    session: AsyncSession,
    field: str,
    location: str,
) -> None:
    from copy import deepcopy

    from moneta.pipelines.account_overlap import set_account_link

    root, alias = await pair(session)
    conflicting = alias
    if location != "source":
        conflicting = await make_account(
            session,
            source="third",
            name="Freedom",
            org_name="Chase",
            mask="1234",
            type=AccountType.credit,
            canonical_account_id=alias.id if location == "source_descendant" else root.id,
        )
    await conflicting_metadata(session, conflicting, field)
    session.add(
        ReviewItem(
            kind=ReviewKind.account_overlap,
            question="Same account?",
            payload={"account_id": alias.id, "candidates": [root.id]},
        )
    )
    await session.flush()
    before_accounts = [
        (a.id, a.canonical_account_id, a.type, a.promo_expires_on, a.financing_mode)
        for a in await session.scalars(select(Account).order_by(Account.id))
    ]
    before_reviews = deepcopy(
        [
            (r.id, r.payload, r.status, r.resolution)
            for r in await session.scalars(select(ReviewItem).order_by(ReviewItem.id))
        ]
    )
    with pytest.raises(ValueError):
        await set_account_link(session, alias.id, root.id)
    await session.flush()
    assert [
        (a.id, a.canonical_account_id, a.type, a.promo_expires_on, a.financing_mode)
        for a in await session.scalars(select(Account).order_by(Account.id))
    ] == before_accounts
    assert [
        (r.id, r.payload, r.status, r.resolution)
        for r in await session.scalars(select(ReviewItem).order_by(ReviewItem.id))
    ] == before_reviews


@pytest.mark.parametrize("field", ["type", "promotion", "financing"])
@pytest.mark.parametrize("descendant", [False, True])
async def test_automatic_metadata_conflict_needs_review(
    session: AsyncSession,
    field: str,
    descendant: bool,
) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root, alias = await pair(session)
    conflicting = alias
    if descendant:
        conflicting = await make_account(
            session,
            source="third",
            name="Freedom",
            org_name="Chase",
            mask="1234",
            type=AccountType.credit,
            canonical_account_id=alias.id,
        )
    await conflicting_metadata(session, conflicting, field)
    assert await propose_account_links(session) == 0
    assert root.canonical_account_id is None and alias.canonical_account_id is None
    assert root.type == AccountType.credit
    assert root.promo_expires_on == date(2027, 1, 1) and root.financing_mode
    items = await reviews(session)
    assert items and all(item.status == ReviewStatus.open for item in items)
    expected_conflict = {
        "type": "type",
        "promotion": "promo_expires_on",
        "financing": "financing_mode",
    }[field]
    assert all(expected_conflict in item.payload["metadata_conflicts"] for item in items)
    assert any(
        item.payload["account_id"] == alias.id and root.id in item.payload["candidates"]
        for item in items
    )


async def test_unknown_and_default_metadata_do_not_override_root_decisions(
    session: AsyncSession,
) -> None:
    from moneta.pipelines.account_overlap import set_account_link

    root, alias = await pair(session)
    alias.type = AccountType.unknown
    await set_account_link(session, alias.id, root.id)
    assert alias.canonical_account_id == root.id
    assert root.type == AccountType.credit
    assert root.promo_expires_on == date(2027, 1, 1) and root.financing_mode
    assert alias.type == AccountType.unknown
    assert alias.promo_expires_on is None and not alias.financing_mode


async def test_latest_financing_decision_supersedes_older_conflict(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root, alias = await pair(session)
    await conflicting_metadata(session, alias, "financing")
    session.add(
        ReviewItem(
            kind=ReviewKind.financing_account,
            question="Treat as financing?",
            payload={"account_id": alias.id},
            status=ReviewStatus.resolved,
            resolution={"financing": True, "resolved_by": "manual"},
        )
    )
    alias.financing_mode = True
    assert await propose_account_links(session) == 1
    assert alias.canonical_account_id == root.id and root.financing_mode


async def test_latest_negative_financing_decision_blocks_link(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root, alias = await pair(session)
    session.add(
        ReviewItem(
            kind=ReviewKind.financing_account,
            question="Treat as financing?",
            payload={"account_id": alias.id},
            status=ReviewStatus.resolved,
            resolution={"financing": True, "resolved_by": "manual"},
        )
    )
    await session.flush()
    await conflicting_metadata(session, alias, "financing")
    assert await propose_account_links(session) == 0
    assert root.canonical_account_id is None and alias.canonical_account_id is None


async def test_unlink_can_separate_conflicting_metadata(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import set_account_link

    root, alias = await pair(session)
    alias.canonical_account_id = root.id
    alias.promo_expires_on = date(2028, 1, 1)
    await set_account_link(session, alias.id, None)
    assert root.canonical_account_id is None and alias.canonical_account_id is None
    assert root.promo_expires_on == date(2027, 1, 1)
    assert alias.promo_expires_on == date(2028, 1, 1)
