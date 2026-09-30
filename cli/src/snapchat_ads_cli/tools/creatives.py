"""Creative CRUD + preview link."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient
from ..safety import format_preview


READ_ONLY_CREATIVE_FIELDS = {
    "created_at",
    "updated_at",
    "delivery_status",
    "deleted",
    "review_status",
    "packaging_status",
}


def _apply_common_optional(
    payload: dict[str, Any],
    *,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    chat_properties: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if profile_id:
        payload["profile_properties"] = {"profile_id": profile_id}
    if call_to_action:
        payload["call_to_action"] = call_to_action
    if cta_color_display_mode:
        payload["cta_color_display_mode"] = cta_color_display_mode
    if chat_properties:
        payload["chat_properties"] = chat_properties
    if extra:
        payload.update(extra)
    return payload


# Northbeam attribution parameters for Snapchat (Settings -> UTM guide / docs.northbeam.io
# "tracking-for-snapchat-ads"). Every web-view URL and collection fallback URL created through
# this CLI carries them by default so Northbeam can stitch campaign / ad squad / ad ids into
# Shopify attribution. Without them the ad delivers normally but its revenue is invisible to
# Northbeam. Pass northbeam_tags=False (CLI: --no-northbeam-tags) only when you know why.
NORTHBEAM_SNAP_URL_PARAMS = (
    "nbt=nb:snapchat:{{site_source_name}}:{{campaign.id}}:{{adSet.id}}:{{ad.id}}"
    "&utm_source=snapchat&utm_campaign={{campaign.id}}&utm_content={{adSet.name}}"
)


def with_northbeam_params(url: str | None) -> str | None:
    """Append the Northbeam Snapchat params to *url* unless an ``nbt=`` param is already present."""
    if not url or "nbt=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{NORTHBEAM_SNAP_URL_PARAMS}"


def _merge_update_payload(
    current: dict[str, Any],
    fields: dict[str, Any],
    creative_id: str,
) -> dict[str, Any]:
    payload = {
        k: v for k, v in current.items()
        if k not in READ_ONLY_CREATIVE_FIELDS and v is not None
    }
    payload.update(fields)
    payload["id"] = creative_id
    return payload


def list_creatives(
    client: SnapchatApiClient,
    ad_account_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    params = {"limit": min(max(limit, 1), 1000)}
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/creatives",
        "creatives",
        params=params,
    )
    return {"creatives": items, "count": len(items), "ad_account_id": ad_account_id}


def get_creative(client: SnapchatApiClient, creative_id: str) -> dict[str, Any]:
    body, _ = client.get(f"creatives/{creative_id}")
    items = body.get("creatives") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "creative" in first:
            return first["creative"]
        return first
    return body


def create_creative(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    payload = dict(payload)
    payload.setdefault("ad_account_id", ad_account_id)
    if not execute:
        return format_preview(
            f"create creative '{payload.get('name', '?')}'",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/creatives",
        json_body={"creatives": [payload]},
    )
    return body


def update_creative(
    client: SnapchatApiClient,
    ad_account_id: str,
    creative_id: str,
    account_label: str,
    *,
    fields: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not fields:
        return {"error": {"message": "no fields to update"}}

    current: dict[str, Any] = {}
    try:
        current = get_creative(client, creative_id)
    except Exception:
        pass

    payload = _merge_update_payload(current, fields, creative_id)

    if not execute:
        return format_preview(
            f"update creative {creative_id}",
            account_label,
            current_state={k: current.get(k) for k in fields if k in current} if current else None,
            proposed_state=payload,
        )

    body, _ = client.put(
        f"adaccounts/{ad_account_id}/creatives",
        json_body={"creatives": [payload]},
    )
    return body


def preview_creative(client: SnapchatApiClient, creative_id: str) -> dict[str, Any]:
    """GET /v1/creatives/{id}/creative_preview (Snap has no /previews route)."""
    body, _ = client.get(f"creatives/{creative_id}/creative_preview")
    return body


def bulk_create_creatives(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    items: list[dict[str, Any]],
    chunk_size: int = 10,
    execute: bool = False,
) -> dict[str, Any]:
    from . import _bulk
    items = [{"ad_account_id": ad_account_id, **i} for i in items]
    if not execute:
        return _bulk.preview_bulk(
            action=f"bulk-create {len(items)} creatives",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_post_array(
        client,
        path=f"adaccounts/{ad_account_id}/creatives",
        items=items,
        array_key="creatives",
        chunk_size=chunk_size,
    )


def bulk_update_creatives(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    items: list[dict[str, Any]],
    chunk_size: int = 10,
    execute: bool = False,
) -> dict[str, Any]:
    from . import _bulk
    missing = [i for i in items if not i.get("id")]
    if missing:
        return {"error": {"message": f"{len(missing)} item(s) missing required 'id' field"}}
    if not execute:
        return _bulk.preview_bulk(
            action=f"bulk-update {len(items)} creatives",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_put_array(
        client,
        path=f"adaccounts/{ad_account_id}/creatives",
        items=items,
        array_key="creatives",
        chunk_size=chunk_size,
    )


# ---------------------------------------------------------------------------
# Type-specific creative builders -- save users from composing raw payloads.
# Each helper assembles the documented sub-properties block, then defers to
# create_creative for the actual POST and preview.
# ---------------------------------------------------------------------------


def create_app_install(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    app_name: str,
    icon_media_id: str,
    ios_app_id: str | None = None,
    android_app_url: str | None = None,
    end_card_media_id: str | None = None,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    chat_properties: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "app_name": app_name,
        "icon_media_id": icon_media_id,
    }
    if ios_app_id:
        props["ios_app_id"] = ios_app_id
    if android_app_url:
        props["android_app_url"] = android_app_url
    if end_card_media_id:
        props["end_card_media_id"] = end_card_media_id

    payload: dict[str, Any] = {
        "name": name,
        "type": "APP_INSTALL",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "app_install_properties": props,
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        chat_properties=chat_properties,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_web_view(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    url: str,
    allow_snap_javascript_sdk: bool = False,
    use_immersive_mode: bool = False,
    deep_link_urls: list[str] | None = None,
    block_preload: bool = False,
    shareable: bool = True,
    call_to_action: str | None = None,
    profile_id: str | None = None,
    cta_color_display_mode: str | None = None,
    chat_properties: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    northbeam_tags: bool = True,
    execute: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "url": with_northbeam_params(url) if northbeam_tags else url,
        "allow_snap_javascript_sdk": allow_snap_javascript_sdk,
        "use_immersive_mode": use_immersive_mode,
        "block_preload": block_preload,
    }
    if deep_link_urls:
        props["deep_link_urls"] = deep_link_urls

    payload: dict[str, Any] = {
        "name": name,
        "type": "WEB_VIEW",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "web_view_properties": props,
    }
    if northbeam_tags:
        payload["url_macro_parameters"] = NORTHBEAM_SNAP_URL_PARAMS
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        chat_properties=chat_properties,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_deep_link(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    deep_link_uri: str,
    app_name: str,
    icon_media_id: str,
    ios_app_id: str | None = None,
    android_app_url: str | None = None,
    fallback_type: str = "WEB_VIEW",
    fallback_url: str | None = None,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "deep_link_uri": deep_link_uri,
        "app_name": app_name,
        "icon_media_id": icon_media_id,
        "fallback_type": fallback_type,
    }
    if ios_app_id:
        props["ios_app_id"] = ios_app_id
    if android_app_url:
        props["android_app_url"] = android_app_url
    if fallback_url:
        props["fallback_url"] = fallback_url

    payload: dict[str, Any] = {
        "name": name,
        "type": "DEEP_LINK",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "deep_link_properties": props,
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_ad_to_lens(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    lens_id: str,
    icon_media_id: str | None = None,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {"lens_media_id": lens_id}
    if icon_media_id:
        props["icon_media_id"] = icon_media_id

    payload: dict[str, Any] = {
        "name": name,
        "type": "AD_TO_LENS",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "ad_to_lens_properties": props,
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_collection(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    interaction_zone_id: str,
    default_fallback_interaction_type: str = "WEB_VIEW",
    fallback_url: str | None = None,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    northbeam_tags: bool = True,
    execute: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "interaction_zone_id": interaction_zone_id,
        "default_fallback_interaction_type": default_fallback_interaction_type,
    }
    if fallback_url:
        props["web_view_properties"] = {
            "url": with_northbeam_params(fallback_url) if northbeam_tags else fallback_url
        }

    payload: dict[str, Any] = {
        "name": name,
        "type": "COLLECTION",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "collection_properties": props,
    }
    if northbeam_tags:
        payload["url_macro_parameters"] = NORTHBEAM_SNAP_URL_PARAMS
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_longform_video(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    long_form_video_media_id: str,
    icon_media_id: str | None = None,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {"video_media_id": long_form_video_media_id}
    if icon_media_id:
        props["icon_media_id"] = icon_media_id

    payload: dict[str, Any] = {
        "name": name,
        "type": "LONGFORM_VIDEO",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "longform_video_properties": props,
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_lead_generation(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    lead_form_id: str,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {"lead_form_id": lead_form_id}
    payload: dict[str, Any] = {
        "name": name,
        "type": "LEAD_GENERATION",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "lead_generation_form_id": props["lead_form_id"],
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_snap_ad(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "type": "SNAP_AD",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_ad_to_call(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    phone_number_id: str,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "type": "AD_TO_CALL",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "ad_to_call_properties": {"phone_number_id": phone_number_id},
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_ad_to_message(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    phone_number_id: str,
    message_text: str | None = None,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {"phone_number_id": phone_number_id}
    if message_text:
        props["message_text"] = message_text
    payload: dict[str, Any] = {
        "name": name,
        "type": "AD_TO_MESSAGE",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "ad_to_message_properties": props,
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_reminder(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    event_detail_id: str,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "type": "REMINDER",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "reminder_properties": {"event_detail_id": event_detail_id},
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_story_preview(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    preview_media_id: str,
    logo_media_id: str | None = None,
    preview_headline: str | None = None,
    shareable: bool = True,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {"preview_media_id": preview_media_id}
    if logo_media_id:
        props["logo_media_id"] = logo_media_id
    if preview_headline:
        props["preview_headline"] = preview_headline
    payload: dict[str, Any] = {
        "name": name,
        "type": "PREVIEW",
        "shareable": shareable,
        "preview_properties": props,
    }
    if extra:
        payload.update(extra)
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_story_composite(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    preview_creative_id: str,
    creative_ids: list[str],
    shareable: bool = True,
    profile_id: str | None = None,
    chat_properties: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "type": "COMPOSITE",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "preview_creative_id": preview_creative_id,
        "composite_properties": {"creative_ids": creative_ids},
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        chat_properties=chat_properties,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_lens(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "type": "LENS",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "ad_product": "LENS",
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_lens_web_view(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    url: str,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "type": "LENS_WEB_VIEW",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "ad_product": "LENS",
        "web_view_properties": {"url": url},
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_lens_app_install(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    app_name: str,
    icon_media_id: str,
    ios_app_id: str | None = None,
    android_app_url: str | None = None,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {"app_name": app_name, "icon_media_id": icon_media_id}
    if ios_app_id:
        props["ios_app_id"] = ios_app_id
    if android_app_url:
        props["android_app_url"] = android_app_url
    payload: dict[str, Any] = {
        "name": name,
        "type": "LENS_APP_INSTALL",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "ad_product": "LENS",
        "app_install_properties": props,
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)


def create_lens_deep_link(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    deep_link_uri: str,
    app_name: str,
    icon_media_id: str,
    ios_app_id: str | None = None,
    android_app_url: str | None = None,
    fallback_type: str = "WEB_VIEW",
    fallback_url: str | None = None,
    shareable: bool = True,
    profile_id: str | None = None,
    call_to_action: str | None = None,
    cta_color_display_mode: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "deep_link_uri": deep_link_uri,
        "app_name": app_name,
        "icon_media_id": icon_media_id,
        "fallback_type": fallback_type,
    }
    if ios_app_id:
        props["ios_app_id"] = ios_app_id
    if android_app_url:
        props["android_app_url"] = android_app_url
    if fallback_url:
        props["fallback_url"] = fallback_url
    payload: dict[str, Any] = {
        "name": name,
        "type": "LENS_DEEP_LINK",
        "headline": headline,
        "brand_name": brand_name,
        "shareable": shareable,
        "top_snap_media_id": top_snap_media_id,
        "ad_product": "LENS",
        "deep_link_properties": props,
    }
    _apply_common_optional(
        payload,
        profile_id=profile_id,
        call_to_action=call_to_action,
        cta_color_display_mode=cta_color_display_mode,
        extra=extra,
    )
    return create_creative(client, ad_account_id, account_label, payload=payload, execute=execute)
