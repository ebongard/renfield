"""
Re-encrypt stored secrets under the current SECRET_KEY (BL-0357).

Walks every row that holds a Fernet token — today only ``user_ble_irks`` — and
rewrites tokens that were encrypted under a PREVIOUS key (listed in
``SECRET_KEY_PREVIOUS``) with the current one, so the previous key can be dropped
from the configuration. Idempotent: a token already under the current key is
left alone; a token no listed key can decrypt is counted, never touched.

Counts only — never the plaintext, never the token. Called by
``bin/rotate_secret_encryption.py``.
"""
from dataclasses import asdict, dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ha_glue.models.database import UserBleIrk
from services.secret_encryption import InvalidToken, is_current_key, rotate_secret


@dataclass
class RotationReport:
    total: int = 0
    already_current: int = 0
    rotated: int = 0
    undecryptable: int = 0
    committed: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return asdict(self)


async def rotate_irks(session: AsyncSession, *, commit: bool) -> RotationReport:
    """Re-encrypt ``user_ble_irks.irk_encrypted`` tokens under the current key.

    ``commit=False`` is a dry run: the report says what WOULD change, nothing is
    written. Rows whose token no configured key can decrypt are reported as
    ``undecryptable`` (the IRK must be re-entered via the pairing flow).
    """
    report = RotationReport()
    rows = (await session.execute(select(UserBleIrk))).scalars().all()
    for row in rows:
        report.total += 1
        try:
            if is_current_key(row.irk_encrypted):
                report.already_current += 1
                continue
            new_token = rotate_secret(row.irk_encrypted)
        except InvalidToken:
            report.undecryptable += 1
            logger.warning(f"secret rotation: IRK row id={row.id} decryptable by no configured key")
            continue
        if commit:
            row.irk_encrypted = new_token
        report.rotated += 1
    if commit:
        await session.commit()
        report.committed = True
    logger.info(f"secret rotation (irks): {report.as_dict()}")
    return report
