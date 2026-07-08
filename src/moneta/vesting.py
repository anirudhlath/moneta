"""Vesting import: moneta's own CSV schema (see README). NetBenefits mapping is backlogged."""

import csv
import io

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Holding

_EXPECTED = ["symbol", "vested_quantity", "unvested_quantity"]


class VestingRow(BaseModel):
    symbol: str
    vested_quantity: float
    unvested_quantity: float


def parse_vesting_csv(text: str) -> list[VestingRow]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != _EXPECTED:
        raise ValueError(f"expected header {','.join(_EXPECTED)!r}, got {reader.fieldnames!r}")
    rows: list[VestingRow] = []
    for row in reader:
        if any(row[field] is None for field in _EXPECTED):
            raise ValueError(f"malformed row: {row!r}")
        rows.append(
            VestingRow(
                symbol=str(row["symbol"]),
                vested_quantity=float(row["vested_quantity"]),
                unvested_quantity=float(row["unvested_quantity"]),
            )
        )
    return rows


async def apply_vesting(session: AsyncSession, rows: list[VestingRow]) -> int:
    updated = 0
    for row in rows:
        holdings = (
            (await session.execute(select(Holding).where(Holding.symbol == row.symbol)))
            .scalars()
            .all()
        )
        for h in holdings:
            h.vested_quantity = row.vested_quantity
            h.unvested_quantity = row.unvested_quantity
            updated += 1
    await session.commit()
    return updated
