from __future__ import annotations

from typing import Any

from sqlmodel import col, func, select

from astrbot.core.db.po import ConversationV2, UmoAlias, UmoSender

try:
    from .state_store import PluginStateStore
except ImportError:
    from state_store import PluginStateStore


def parse_umo(umo: str) -> dict[str, str]:
    raw = str(umo or "")
    parts = raw.split(":", 2)
    return {
        "umo": raw,
        "platform": parts[0] if len(parts) >= 1 and parts[0] else "unknown",
        "message_type": parts[1] if len(parts) >= 2 and parts[1] else "unknown",
        "session_id": parts[2] if len(parts) >= 3 else raw,
    }


def is_group_umo(umo: str) -> bool:
    lower = str(umo or "").lower()
    return ":group:" in lower or ":groupmessage:" in lower


def is_private_umo(umo: str) -> bool:
    lower = str(umo or "").lower()
    return ":private:" in lower or ":friend:" in lower or ":friendmessage:" in lower


async def _known_umos_from_db(context: Any) -> list[str]:
    db = context.get_db()
    sources = (
        select(
            col(ConversationV2.user_id).label("umo"),
            col(ConversationV2.updated_at).label("updated_at"),
        )
        .union_all(
            select(UmoSender.umo, UmoSender.updated_at),
            select(UmoAlias.umo, UmoAlias.updated_at),
        )
        .subquery()
    )
    async with db.get_db() as session:
        result = await session.execute(
            select(sources.c.umo)
            .where(sources.c.umo.is_not(None), sources.c.umo != "")
            .group_by(sources.c.umo)
            .order_by(func.max(sources.c.updated_at).desc(), sources.c.umo.asc())
        )
        return [str(row[0]) for row in result.fetchall()]


async def discover_sessions(
    context: Any,
    state_store: PluginStateStore,
    whitelist: list[Any],
) -> list[dict[str, Any]]:
    """Discover group sessions and their AstrBot display names."""

    known: list[str] = []
    aliases: dict[str, UmoAlias] = {}
    try:
        raw_aliases = await context.get_db().get_umo_aliases(None)
        for alias in raw_aliases or []:
            if alias and getattr(alias, "umo", None):
                aliases[str(alias.umo)] = alias
                known.append(str(alias.umo))
    except Exception:
        aliases = {}

    try:
        known.extend(await _known_umos_from_db(context))
    except Exception:
        pass

    unique_known: list[str] = []
    for umo in known:
        if umo and umo not in unique_known:
            unique_known.append(umo)

    whitelist_raw = [str(item).strip() for item in whitelist if str(item).strip()]
    whitelist_set = set(whitelist_raw)
    by_session_id: dict[str, str] = {}
    for umo in unique_known:
        if is_group_umo(umo):
            info = parse_umo(umo)
            by_session_id.setdefault(info["session_id"], umo)

    records: dict[str, dict[str, Any]] = {}

    def add_record(
        umo: str,
        *,
        synthetic: bool = False,
        whitelisted: bool = False,
        display_name_override: str = "",
    ) -> None:
        info = parse_umo(umo)
        alias = aliases.get(umo)
        auto_name = str(getattr(alias, "auto_name", "") or "").strip()
        user_alias = str(getattr(alias, "user_alias", "") or "").strip()
        fallback = (
            display_name_override
            or user_alias
            or auto_name
            or (f"群 {info['session_id']}" if info["session_id"] else umo)
        )
        session_keys = [umo]
        if info["session_id"] and info["session_id"] not in session_keys:
            session_keys.append(info["session_id"])
        binding_count = state_store.session_binding_count(session_keys)
        existing = records.get(umo)
        if existing:
            existing["whitelisted"] = bool(existing["whitelisted"] or whitelisted)
            existing["bound_instance_count"] = binding_count
            existing["bound"] = binding_count > 0
            return
        records[umo] = {
            **info,
            "display_name": fallback,
            "auto_name": auto_name,
            "user_alias": user_alias,
            "whitelisted": whitelisted,
            "synthetic": synthetic,
            "bound_instance_count": binding_count,
            "bound": binding_count > 0,
        }

    for umo in unique_known:
        if not is_group_umo(umo):
            continue
        add_record(umo, whitelisted=umo in whitelist_set)

    for item in whitelist_raw:
        if ":" in item:
            info = parse_umo(item)
            if is_group_umo(item):
                add_record(item, whitelisted=True)
            elif is_private_umo(item):
                continue
            else:
                add_record(item, whitelisted=True)
            continue

        known_umo = by_session_id.get(item)
        if known_umo:
            add_record(known_umo, whitelisted=True)
        else:
            add_record(
                item,
                synthetic=True,
                whitelisted=True,
                display_name_override=f"群 {item}",
            )

    result = list(records.values())
    result.sort(
        key=lambda item: (
            0 if item.get("bound") else 1,
            0 if item.get("whitelisted") else 1,
            str(item.get("display_name") or "").casefold(),
            str(item.get("umo") or ""),
        )
    )
    return result
