from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from app.core.idempotency import (
    IdempotencyRaceError,
    mutation_scope,
    replay_or_conflict,
    request_fingerprint,
    store_record,
    validate_idempotency_key,
)
from app.core.pagination import PaginationService
from app.core.security import hash_user_password, verify_user_password
from app.users.model import UserModel
from app.users.repository import (
    create as create_user,
    get_by_email,
    get_by_id,
    list_users_page,
    normalize_user_email,
    update as update_user_model,
)
from app.users.schemas import (
    CreateCustomerRequest,
    PageMeta,
    UpdateUserRequest,
    UserPage,
    UserResponse,
    UserRoleEnum,
    UserStatusEnum,
    user_response_from_model,
)

PUBLIC_USER_CREATE_SCOPE = mutation_scope(
    actor_id=None,
    method="POST",
    path="/v1/users",
)
ADMIN_USER_LIST_RESOURCE = "users"
ADMIN_USER_VIEW = {"role": UserRoleEnum.ADMIN.value}


def list_users_admin(
    db: Session,
    *,
    name: str | None = None,
    email: str | None = None,
    role: UserRoleEnum | str | None = None,
    status: UserStatusEnum | str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    pagination: PaginationService | None = None,
) -> UserPage:
    """List users for an administrator with signed, filter-bound cursors."""

    pagination_service = pagination or PaginationService()
    effective_name = name.strip() if name is not None else None
    effective_email = normalize_user_email(email) if email is not None else None
    effective_role = role.value if isinstance(role, UserRoleEnum) else role
    effective_status = (
        status.value if isinstance(status, UserStatusEnum) else status
    ) or UserStatusEnum.ACTIVE.value
    filters = {
        key: value
        for key, value in {
            "name": effective_name,
            "email": effective_email,
            "role": effective_role,
            "status": effective_status,
        }.items()
        if value is not None
    }
    view = ADMIN_USER_VIEW
    validated_limit = pagination_service.validate_limit(limit)
    after = None
    if cursor is not None:
        after = pagination_service.decode_cursor(
            cursor,
            resource=ADMIN_USER_LIST_RESOURCE,
            filters=filters,
            view=view,
        ).position

    users = list_users_page(
        db,
        name=effective_name,
        email=effective_email,
        role=effective_role,
        status=effective_status,
        after=after,
        limit=validated_limit,
    )
    page = pagination_service.build_page(
        users,
        limit=validated_limit,
        resource=ADMIN_USER_LIST_RESOURCE,
        filters=filters,
        view=view,
    )
    return UserPage(
        data=[user_response_from_model(user) for user in page.data],
        meta=PageMeta(
            limit=page.meta.limit,
            next_cursor=page.meta.next_cursor,
            has_next=page.meta.has_next,
        ),
    )


def create_customer(
    payload: CreateCustomerRequest,
    db: Session,
    *,
    idempotency_key: str | None = None,
) -> UserResponse:
    """Create a public customer, optionally replaying a stored result.

    The user row and its completed idempotency response are committed together.
    The request body is represented only by a one-way fingerprint, so a password
    is never persisted in the idempotency record.
    """

    key = validate_idempotency_key(idempotency_key)
    fingerprint = (
        request_fingerprint(
            method="POST",
            path="/v1/users",
            body=payload.model_dump(mode="json"),
        )
        if key is not None
        else None
    )

    if key is not None and fingerprint is not None:
        stored = replay_or_conflict(
            db,
            scope=PUBLIC_USER_CREATE_SCOPE,
            key=key,
            request_fingerprint_value=fingerprint,
        )
        if stored is not None:
            return UserResponse.model_validate(stored.response_body)

    normalized_email = normalize_user_email(payload.email)

    if get_by_email(db, normalized_email):
        raise ConflictError(detail="A user with this email already exists.")

    now = datetime.now(UTC)
    user = UserModel(
        id=uuid4(),
        name=payload.name.strip(),
        email=normalized_email,
        password=hash_user_password(payload.password),
        role=UserRoleEnum.CUSTOMER,
        status=UserStatusEnum.ACTIVE,
        created_at=now,
        updated_at=now,
    )

    create_user(db, user)
    response = user_response_from_model(user)

    if key is not None and fingerprint is not None:
        try:
            store_record(
                db,
                scope=PUBLIC_USER_CREATE_SCOPE,
                key=key,
                request_fingerprint_value=fingerprint,
                status_code=201,
                response_body=response.model_dump(mode="json"),
            )
        except IdempotencyRaceError:
            stored = replay_or_conflict(
                db,
                scope=PUBLIC_USER_CREATE_SCOPE,
                key=key,
                request_fingerprint_value=fingerprint,
            )
            assert stored is not None
            return UserResponse.model_validate(stored.response_body)

    db.commit()
    db.refresh(user)
    return response


def _get_user_or_not_found(db: Session, user_id: UUID) -> UserModel:
    user = get_by_id(db, user_id)
    if user is None:
        raise NotFoundError()
    return user


def _get_visible_user(
    db: Session,
    requester: UserModel,
    user_id: UUID,
) -> UserModel:
    """Resolve a target according to the caller's user visibility."""

    user = _get_user_or_not_found(db, user_id)
    if requester.role is UserRoleEnum.ADMIN:
        return user
    if requester.id != user.id or user.status is not UserStatusEnum.ACTIVE:
        raise NotFoundError()
    return user


def get_user(db: Session, requester: UserModel, user_id: UUID) -> UserResponse:
    """Return a user visible to the authenticated requester."""

    return user_response_from_model(_get_visible_user(db, requester, user_id))


def update_user(
    db: Session,
    requester: UserModel,
    user_id: UUID,
    payload: UpdateUserRequest,
) -> UserResponse:
    """Apply an authorized merge-patch to a user profile."""

    user = _get_visible_user(db, requester, user_id)
    changes = payload.changes()

    if requester.role is not UserRoleEnum.ADMIN:
        if set(changes) - {"name", "email"}:
            raise ForbiddenError()

    if "email" in changes:
        existing = get_by_email(db, changes["email"])
        if existing is not None and existing.id != user.id:
            raise ConflictError(detail="A user with this email already exists.")

    update_user_model(db, user, changes)
    db.commit()
    db.refresh(user)
    return user_response_from_model(user)


def deactivate_user(db: Session, requester: UserModel, user_id: UUID) -> None:
    """Logically deactivate an authorized user without deleting the row."""

    user = _get_visible_user(db, requester, user_id)
    if user.status is not UserStatusEnum.INACTIVE:
        user.status = UserStatusEnum.INACTIVE
        db.commit()
    return None


def change_user_password(
    db: Session,
    requester: UserModel,
    user_id: UUID,
    current_password: str,
    new_password: str,
) -> None:
    """Change a customer's own password after verifying the current one."""

    user = _get_user_or_not_found(db, user_id)
    if requester.role is not UserRoleEnum.CUSTOMER or requester.id != user.id:
        if requester.role is UserRoleEnum.CUSTOMER and requester.id != user.id:
            raise NotFoundError()
        raise ForbiddenError()
    if user.status is not UserStatusEnum.ACTIVE:
        raise NotFoundError()
    if not verify_user_password(user.password, current_password):
        raise UnauthorizedError(detail="The current password is incorrect.")

    user.password = hash_user_password(new_password)
    db.commit()
