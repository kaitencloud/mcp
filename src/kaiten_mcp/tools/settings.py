# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Organization settings: typed metadata fields, and connectors to outside systems."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from kaitencloud.types import MetadataResourceType
from pydantic import Field

from .. import sdk, shapes
from . import READ, WRITE


def _field(f: Any) -> dict:
    return shapes.drop_none(
        {
            "key": f.key,
            "label": f.label,
            "schema": f.json_schema,
            "order": f.display_order,
            "archived": bool(f.archived_at) or None,
        }
    )


def register(mcp: FastMCP, *, writes: bool) -> None:
    @mcp.tool(annotations=READ)
    async def kaiten_get_metadata_fields(
        resource_type: Annotated[
            MetadataResourceType, Field(description="Which resource's metadata")
        ] = "INSTANCE",
    ) -> dict:
        """The typed metadata fields the organization defined for instances or deployment zones: each key, its label
        and its JSON Schema. Metadata written on those resources is validated against them."""
        fields = await sdk.kaiten().metadata_fields.list(resource_type)
        return {"resource_type": resource_type, "fields": [_field(f) for f in fields]}

    @mcp.tool(annotations=READ)
    async def kaiten_get_connectors() -> dict:
        """Connectors to outside systems (e.g. a CRM): for each one, whether this deployment has it, whether the
        organization's license allows it, and whether it is activated."""
        k = sdk.kaiten()
        connectors = await k.connectors.list()
        states = await asyncio.gather(*(k.connectors.get_state(c.name) for c in connectors))
        return {
            "connectors": [
                {
                    "name": c.name,
                    "version": c.version,
                    "available": s.available,
                    "entitled": s.entitled,
                    "activated": s.activated,
                }
                for c, s in zip(connectors, states, strict=True)
            ]
        }

    if not writes:
        return

    @mcp.tool(annotations=WRITE)
    async def kaiten_save_metadata_field(
        key: Annotated[
            str, Field(description="Metadata key, as it appears in the resource's metadata")
        ],
        resource_type: Annotated[
            MetadataResourceType, Field(description="Which resource the field is on")
        ] = "INSTANCE",
        label: Annotated[
            str | None, Field(description="Display label (required to create)")
        ] = None,
        json_schema: Annotated[
            dict[str, Any] | None,
            Field(
                description="JSON Schema 2020-12 for the value, e.g. {'type': 'string', 'enum': ['eu', 'us']}"
            ),
        ] = None,
        archived: Annotated[
            bool | None,
            Field(
                description="true archives the field: frees its key and keeps stored values. An archived field is no longer listed, so it cannot be found again here"
            ),
        ] = None,
        force: Annotated[
            bool,
            Field(
                description="Apply a schema change even when existing values would stop validating"
            ),
        ] = False,
    ) -> dict:
        """Define a typed metadata field, or change its label, schema or archived state. A schema change is first
        dry-run against every stored value: if any would stop validating it is refused (status 'rejected', with
        samples) unless force is set."""
        k = sdk.kaiten()
        fields = [f for f in await k.metadata_fields.list(resource_type) if f.key == key]
        cur = next((f for f in fields if not f.archived_at), fields[0] if fields else None)
        if cur is None:
            if not label or json_schema is None:
                raise ToolError(
                    f"No {resource_type} field {key!r} yet: pass label and json_schema to create it."
                )
            created = await k.metadata_fields.create(
                resource_type=resource_type, key=key, label=label, json_schema=json_schema
            )
            return shapes.report("created", "metadata_field", _field(created))

        new_label = cur.label if label is None else label
        new_schema = cur.json_schema if json_schema is None else json_schema
        if new_schema != cur.json_schema:
            impact = await k.metadata_fields.dry_run(cur.id, new_schema)
            if impact.count and not force:
                return shapes.report(
                    "rejected",
                    "metadata_field",
                    _field(cur),
                    reason=f"{impact.count} stored value(s) would no longer validate",
                    samples=[s.to_dict() for s in impact.samples or []],
                    notes=[
                        "Nothing was written. Fix those values, loosen the schema, or pass force."
                    ],
                )
        # Each write answers with the field as stored; archived fields are not
        # listed any more, so that answer is the after-state.
        after = cur
        if (new_label, new_schema) != (cur.label, cur.json_schema):
            after = await k.metadata_fields.update(
                cur.id,
                resource_type=resource_type,
                key=key,
                label=new_label,
                json_schema=new_schema,
            )
        if archived is not None and archived != bool(cur.archived_at):
            after = await (k.metadata_fields.archive if archived else k.metadata_fields.unarchive)(
                cur.id
            )
        diff = shapes.changes(_field(cur), _field(after))
        return shapes.report(
            "updated" if diff else "unchanged", "metadata_field", _field(after), changes=diff
        )

    @mcp.tool(annotations=WRITE)
    async def kaiten_configure_connector(
        connector: Annotated[str, Field(description="Connector name, from kaiten_get_connectors")],
        activated: Annotated[
            bool | None, Field(description="Activate or deactivate it for the organization")
        ] = None,
        settings: Annotated[
            dict[str, Any] | None,
            Field(
                description="Settings keys to set (null removes a key), validated against the connector's settings schema"
            ),
        ] = None,
    ) -> dict:
        """Activate or deactivate a connector for the organization, and set its settings (credentials included, stored
        in Vault). Activation is refused when the organization's license does not include the connector."""
        k = sdk.kaiten()
        state = await sdk.require(k.connectors.get_state(connector), f"connector {connector!r}")
        diff: dict[str, Any] = {}
        if settings is not None:
            current = await sdk.get_or_none(k.connectors.get_settings(connector))
            values = dict(current.settings or {}) if current else {}
            merged = shapes.merge_map(values, settings)
            if merged != values:
                await k.connectors.update_settings(connector, merged)
                # Values may be credentials: say which keys changed, never what to.
                diff["settings"] = {
                    "keys": sorted(
                        k_ for k_ in set(values) | set(merged) if values.get(k_) != merged.get(k_)
                    )
                }
        if activated is not None and activated != state.activated:
            await (k.connectors.activate if activated else k.connectors.deactivate)(connector)
            diff["activated"] = {"from": state.activated, "to": activated}
        after = await k.connectors.get_state(connector)
        return shapes.report(
            "updated" if diff else "unchanged",
            "connector",
            {"name": connector, "activated": after.activated, "entitled": after.entitled},
            changes=diff,
        )
