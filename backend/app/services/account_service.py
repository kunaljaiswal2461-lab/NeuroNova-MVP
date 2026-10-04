"""Account lifecycle operations that require ownership-aware cleanup."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.conversation import ConversationSessionRow
from app.db.models.dataset import DatasetRecord, DatasetStatus
from app.db.models.source import DataSource
from app.db.models.user import User
from app.exceptions.custom_exceptions import ConflictError
from agentic_engine.tools.uploader.storage import StorageBackend


logger = get_logger("account_service")

_TERMINAL_DATASET_STATUSES = frozenset({
    DatasetStatus.COMPLETE,
    DatasetStatus.FAILED,
})


def _delete_derived_artifacts(dataset_id, settings: Settings) -> None:
    """Remove the per-dataset JSON artifacts stored outside the database."""
    for directory in (
        settings.profiles_dir,
        settings.findings_dir,
        settings.viz_dir,
        settings.llm_cache_dir,
    ):
        (directory / f"{dataset_id}.json").unlink(missing_ok=True)


async def delete_account(
    *,
    session: AsyncSession,
    storage: StorageBackend,
    settings: Settings,
    user: User,
) -> None:
    """Permanently remove a user's account, datasets, and derived records.

    The database uses ``SET NULL`` for the user references on datasets and
    conversation sessions to preserve legacy API-key workflows. Account
    deletion therefore removes owned datasets explicitly before deleting the
    user, ensuring their raw files, reports, embeddings, and chat history do
    not become orphaned.
    """
    result = await session.execute(
        select(DatasetRecord).where(DatasetRecord.user_id == user.id)
    )
    datasets = list(result.scalars().all())
    in_progress = [
        dataset for dataset in datasets
        if dataset.status not in _TERMINAL_DATASET_STATUSES
    ]
    if in_progress:
        raise ConflictError(
            "wait for dataset processing to finish before deleting your account",
            details={"dataset_ids": [str(dataset.id) for dataset in in_progress]},
        )

    try:
        # Connector ownership is deliberately non-null/RESTRICT. Remove a
        # user's sources first so neither encrypted credentials nor explicit
        # scope rows can become legacy/null-owned data after account deletion.
        await session.execute(delete(DataSource).where(DataSource.owner_user_id == user.id))

        for dataset in datasets:
            # ``DatasetRecord.filename`` is already prefixed with the UUID;
            # use the original basename expected by StorageBackend.delete.
            await storage.delete(str(dataset.id), Path(dataset.original_name).name)
            _delete_derived_artifacts(dataset.id, settings)

        for dataset in datasets:
            await session.delete(dataset)

        # Dataset deletion cascades the associated sessions. Remove any
        # remaining direct sessions too, including legacy rows without a
        # matching dataset relationship.
        await session.execute(
            delete(ConversationSessionRow).where(
                ConversationSessionRow.user_id == user.id
            )
        )
        await session.flush()
        await session.delete(user)
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    logger.info(
        "account.deleted",
        user_id=str(user.id),
        dataset_count=len(datasets),
    )
