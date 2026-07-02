# Meta Winner Videos to Snap DPA Collection Ads

Use this workflow when the user asks to move winning Meta product-line videos into Snapchat while keeping the existing Snap catalog/product-set orientation.

## Example Snap DPA Defaults

- Campaign: `<DPA_CAMPAIGN_ID>` (`DPA_ProductA_ProductB_AllProduct`)
- Product set: `<PRODUCT_SET_ID>`
- Dynamic template: `<DYNAMIC_TEMPLATE_ID>`
- Interaction zone: `<INTERACTION_ZONE_ID>`
- Pixel: `<PIXEL_ID>`
- Placement v2: `{"config":"AUTOMATIC"}`
- Public profile: `<PUBLIC_PROFILE_ID>`
- Brand: `My Brand`
- Headline: `<PRODUCT_HEADLINE>`
- Fallback URL: `https://example.com/products/product-a?nbt=nb:snapchat:{{site_source_name}}:{{campaign.id}}:{{adSet.id}}:{{ad.id}}&utm_source=snapchat&utm_campaign={{campaign.id}}&utm_content={{adSet.name}}`

## Safety Gate

Never run Snap writes with `--execute` until the user has seen the preview and explicitly approves execution. Keep every new ad and ad squad `PAUSED` unless the user separately approves launch.

Snap read responses include fields that are not valid in create payloads. Do not send returned-only fields such as `catalog_vertical`, `targeting_reach_status`, `placement`, `creation_state`, `delivery_status`, `skadnetwork_properties`, or `delivery_properties_version` when creating a copied ad squad.

Always send `placement_v2` explicitly on DPA ad squad create. The legacy `placement` field is deprecated and can read back as `UNSUPPORTED`; use `--placement-v2-json '{"config":"AUTOMATIC"}'` for automatic placements. If Snap UI returns `E21011` ("must have chat feed placement selected") while saving a lower-funnel ad set, verify the ad squad through the API with `return_placement_v2=true` and fix it to automatic placement before launch.

## Read Meta Winners

```bash
uv run --directory ~/Tools/meta-ads-cli meta-ads report top-ads \
  --keyword "product a" \
  --date-preset last_30d \
  --n 25
```

Pick the top five video ads by spend/revenue fit. Fetch each creative:

```bash
uv run --directory ~/Tools/meta-ads-cli meta-ads creative get <META_AD_ID>
```

The output exposes `media.videos[]`. For each Meta video ID, fetch `source` through the local Meta client and download it to:

```text
~/.cache/snapchat-bridge/product-a-meta-winners-YYYYMMDD/
```

Validate with `ffprobe`; Snap-ready files should be vertical H.264/yuv420p with reasonable duration.

## Preview Snap Media Uploads

```bash
uv run --directory cli snapchat-ads --account default media upload \
  --file <LOCAL_VIDEO_FILE> \
  --type VIDEO \
  --name "Meta Winner NN - <label>"
```

Run the same command with `--execute` only after user approval. Record each returned Snap `media_id`.

## Preview Paused DPA Ad Squad

Create a separate ad squad inside the existing DPA campaign so reporting stays clean while the campaign remains catalog/product-set oriented.

```bash
uv run --directory cli snapchat-ads --account default adsquad smart-create \
  <DPA_CAMPAIGN_ID> \
  --name "Meta Winners Product A DPA Video Test | YYYY-MM-DD" \
  --type SNAP_ADS \
  --placement-v2-json '{"config":"AUTOMATIC"}' \
  --optimization-goal PIXEL_PURCHASE \
  --bid-strategy TARGET_COST \
  --target-cost 72 \
  --daily-budget 1500 \
  --billing-event IMPRESSION \
  --conversion-window SWIPE_7DAY \
  --pixel-id <PIXEL_ID> \
  --targeting-json '{"regulated_content":false,"demographics":[{"min_age":"18","operation":"INCLUDE"}],"geos":[{"country_code":"us","operation":"INCLUDE"}],"product_audiences":[],"enable_targeting_expansion":true,"auto_expansion_options":{"interest_expansion_option":{"enabled":true},"custom_audience_expansion_option":{"enabled":true},"auto_expansion_type":"SMART_TARGETING"}}' \
  --status PAUSED \
  --extra-json '{"child_ad_type":"COLLECTION","product_properties":{"product_set_id":"<PRODUCT_SET_ID>"},"delivery_constraint":"DAILY_BUDGET","pacing_type":"STANDARD","forced_view_setting":"NONE","brand_safety_config":{"inventory_option":"FULL_INVENTORY"}}'
```

Run with `--execute` only after approval. Record the new ad squad ID.

Immediately verify placement using a read that returns `placement_v2`:

```bash
uv run --directory cli snapchat-ads --account default adsquad get <AD_SQUAD_ID>
```

Expected field:

```json
{ "placement_v2": { "config": "AUTOMATIC" } }
```

## Preview Collection Creatives

For each uploaded Snap media ID:

```bash
uv run --directory cli snapchat-ads --account default creative create-collection \
  --name "Meta Winner NN - <label>" \
  --headline "<PRODUCT_HEADLINE>" \
  --brand-name "My Brand" \
  --top-snap-media-id <SNAP_MEDIA_ID> \
  --interaction-zone-id <INTERACTION_ZONE_ID> \
  --fallback-url 'https://example.com/products/product-a?nbt=nb:snapchat:{{site_source_name}}:{{campaign.id}}:{{adSet.id}}:{{ad.id}}&utm_source=snapchat&utm_campaign={{campaign.id}}&utm_content={{adSet.name}}' \
  --profile-id <PUBLIC_PROFILE_ID> \
  --cta-color-display-mode AUTO_COLOR_DETECTION \
  --extra-json '{"dynamic_render_properties":{"dynamic_template_id":"<DYNAMIC_TEMPLATE_ID>","product_set_id":"<PRODUCT_SET_ID>"},"top_snap_crop_position":"MIDDLE","ad_product":"SNAP_AD","url_macro_parameters":"nbt=nb:snapchat:{{site_source_name}}:{{campaign.id}}:{{adSet.id}}:{{ad.id}}&utm_source=snapchat&utm_campaign={{campaign.id}}&utm_content={{adSet.name}}","utm_autofix_permission":"OPT_IN"}'
```

Run with `--execute` only after approval. Record each creative ID.

## Preview Paused Ads

```bash
uv run --directory cli snapchat-ads --account default ad create <AD_SQUAD_ID> \
  --payload-json '{"name":"Meta Winner NN - <label>","creative_id":"<CREATIVE_ID>","status":"PAUSED","type":"COLLECTION","render_type":"DYNAMIC"}'
```

Run with `--execute` only after approval. After creation, check review/delivery:

```bash
uv run --directory cli snapchat-ads --account default ad list --ad-squad-id <AD_SQUAD_ID>
```

Do not activate the ad squad or ads without a separate explicit launch approval.

## Dedupe Plan

Before uploading media or creating Snap ads, build a run plan keyed by deterministic IDs:

- `source_platform`: `meta`
- `source_ad_id`: Meta ad ID
- `source_video_id`: Meta video ID
- `source_sha256`: SHA256 of the downloaded video file
- `snap_campaign_id`
- `snap_product_set_id`
- `bridge_kind`: `meta_to_snap_dpa_collection`

Check existing launches in this order:

1. Local bridge registry: `~/.cache/snapchat-bridge/registry.jsonl`
2. Snap media list by name convention: `Meta <source_ad_id> <source_video_id> ...`
3. Snap creative/ad list by name convention: include `MetaAd <source_ad_id>` and `MetaVideo <source_video_id>`
4. If a dashboard table exists later, treat it as source of truth and backfill the local registry from it.

For each winner, skip media upload when either `source_video_id` or `source_sha256` already maps to a `READY` Snap media ID. Skip creative/ad creation when the registry already maps the same source video to a Snap creative/ad in the target campaign/product set.

Deletion tombstones are intentional dedupe blockers. If a user deletes a duplicate Snap ad, append a `snap_ad_removed_externally` row with `status:"REMOVED_EXTERNALLY"` and keep it permanently. Future runs must treat the latest `REMOVED_EXTERNALLY` event for the same `source_ad_id` + `source_video_id` + campaign/product set as a hard skip unless the user explicitly says to recreate/resurrect that ad.

If the registry says uploaded but Snap lookup returns missing/archived and there is no tombstone, mark the row stale and ask before re-uploading.

Use this naming convention for new objects:

```text
Media:    MetaAd <source_ad_id> MetaVideo <source_video_id> | <short label>
Creative: MetaAd <source_ad_id> MetaVideo <source_video_id> | DPA Collection | <short label>
Ad:       MetaAd <source_ad_id> MetaVideo <source_video_id> | DPA Collection | <short label>
```

Append every successful write to the registry as JSONL with timestamps and returned Snap IDs. The registry should be idempotent: repeated runs should read the latest matching row and produce a skip/attach plan instead of uploading again.
