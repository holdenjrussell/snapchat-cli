# Reactivate old non-DPA campaign using DPA + Meta winners

Use this when the user asks to reactivate an old Snapchat non-DPA campaign, keep ads off, pull through top DPA ads, and add unique Meta winners for specific product lines (e.g. Product A Sheets / Product B).

## Canonical old non-DPA campaign

- Campaign: `<LEGACY_CAMPAIGN_ID>` — `My Brand_SnapAds_Apr-June_ProductB_ProductA Sheets`
- Product B ad squad: `<PRODUCT_B_ADSQUAD_ID>`
- Product A ad squad: `<PRODUCT_A_ADSQUAD_ID>`
- Public profile: `<PUBLIC_PROFILE_ID>`
- Pixel: `<PIXEL_ID>`

## Landing pages

Always verify product routing before previewing creatives:

- Product A Sheets → `https://example.com/products/product-a-sheet-set?...`
- Product B when the user says "12in variant" → `https://example.com/products/product-b-12-inch-drop?...`

Use Snap attribution macros in the URL query:

```text
nbt=nb:snapchat:{{site_source_name}}:{{campaign.id}}:{{adSet.id}}:{{ad.id}}&utm_source=snapchat&utm_campaign={{campaign.id}}&utm_content={{adSet.name}}
```

## Workflow

1. Refresh auth and inspect campaign/ad squads/ads:

```bash
uv run --directory cli snapchat-ads --account default auth refresh
uv run --directory cli snapchat-ads --account default campaign list
uv run --directory cli snapchat-ads --account default adsquad list --campaign-id <LEGACY_CAMPAIGN_ID>
uv run --directory cli snapchat-ads --account default ad list --ad-squad-id <AD_SQUAD_ID>
```

2. Preview campaign reactivation, but do not execute until approval:

```bash
uv run --directory cli snapchat-ads --account default campaign update \
  <LEGACY_CAMPAIGN_ID> \
  --status ACTIVE
```

3. Keep ads off: preview pausing all existing active non-DPA ads and create any new ads with `status: PAUSED`.

```bash
uv run --directory cli snapchat-ads --account default manage bulk-pause <AD_ID...>
```

4. Budget/target-cost: Snap paces this campaign at ad-squad level. If the user requests a campaign-level daily budget, split it across the product-line ad squads unless they say otherwise. Example for `$1500/day` total + `$75` target cost:

```bash
# Product B: $750/day, target cost $75
uv run --directory cli snapchat-ads --account default adsquad update \
  <PRODUCT_B_ADSQUAD_ID> \
  --daily-budget 750 \
  --fields-json '{"bid_strategy":"TARGET_COST","bid_micro":75000000,"target_bid":true,"auto_bid":false,"billing_event":"IMPRESSION","optimization_goal":"PIXEL_PURCHASE","conversion_window":"SWIPE_7DAY"}'

# Product A: $750/day, target cost $75
uv run --directory cli snapchat-ads --account default adsquad update \
  <PRODUCT_A_ADSQUAD_ID> \
  --daily-budget 750 \
  --fields-json '{"bid_strategy":"TARGET_COST","bid_micro":75000000,"target_bid":true,"auto_bid":false,"billing_event":"IMPRESSION","optimization_goal":"PIXEL_PURCHASE","conversion_window":"SWIPE_7DAY"}'
```

5. Select creatives:
   - Pull Snap DPA ad stats with `report stats --entity campaign --id <DPA_CAMPAIGN_ID> --granularity TOTAL --breakdown ad` and map ad IDs back to `ad list` output.
   - Pull Meta winners with the companion `meta-ads` CLI (`meta_api_helper.py top-ads --account default --date-preset last_30d --format json`) and filter for the relevant product lines.
   - Dedupe against existing Snap ads and registry `~/.cache/snapchat-bridge/registry.jsonl`.

6. Reuse existing Snap media IDs where possible instead of re-uploading:
   - Fetch the existing DPA creative with `creative get <CREATIVE_ID>`.
   - Use its `top_snap_media_id` to create a non-DPA `WEB_VIEW` creative.
   - For Meta winners already bridged into DPA, the registry often has `snap_media_id` for the source Meta ad/video.

7. Preview `WEB_VIEW` creatives, one per selected winner, with the correct LP:

```bash
uv run --directory cli snapchat-ads --account default creative create-web-view \
  --name 'NonDPA | MetaAd <META_AD_ID> | <short label>' \
  --headline '<product-specific headline>' \
  --brand-name 'My Brand' \
  --top-snap-media-id <SNAP_MEDIA_ID> \
  --url '<CORRECT_PRODUCT_LP_WITH_MACROS>' \
  --profile-id <PUBLIC_PROFILE_ID> \
  --call-to-action SHOP_NOW \
  --cta-color-display-mode AUTO_COLOR_DETECTION \
  --block-preload
```

8. After user approval, rerun previews with `--execute`, then create ads as `PAUSED`:

```bash
uv run --directory cli snapchat-ads --account default ad create <AD_SQUAD_ID> \
  --payload-json '{"name":"NonDPA | MetaAd <META_AD_ID> | <short label>","creative_id":"<CREATIVE_ID>","status":"PAUSED","type":"REMOTE_WEBPAGE"}' \
  --execute
```

9. Verify after execution:

```bash
uv run --directory cli snapchat-ads --account default campaign get <LEGACY_CAMPAIGN_ID>
uv run --directory cli snapchat-ads --account default adsquad list --campaign-id <LEGACY_CAMPAIGN_ID>
uv run --directory cli snapchat-ads --account default ad list --ad-squad-id <AD_SQUAD_ID>
uv run --directory cli snapchat-ads --account default creative get <CREATIVE_ID>
```

## Pitfalls

- Do not activate ads unless the user explicitly says to launch ads. "Reactivate campaign" + "keep ads in there off" means campaign/ad squads may be active, ads remain paused.
- Do not treat `$1500/day` as a single campaign budget if the campaign is ad-squad paced; split across active product-line ad squads and state the split in the preview.
- Do not send Product B traffic to the wrong variant or generic LP when the user says a specific size/drop.
- Do not re-upload videos when the same media already exists in Snap from prior DPA bridge runs; reuse `top_snap_media_id`/registry media IDs.
- Always show preview and wait for explicit approval before `--execute` on campaign, ad squad, creative, or ad writes.
