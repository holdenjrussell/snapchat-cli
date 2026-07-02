# Campaign Setup

How to stand up a Snapchat account structure this stack can operate: one campaign per product line (or funnel stage), conversion-optimized ad squads, and creatives wired to your pixel. Everything below is two-phase — run the command to see the preview, then re-run with `--execute` once it looks right.

All commands: `uv run --directory cli snapchat-ads --account default ...` (add `--human` for tables).

## Prerequisites

- Auth complete (`auth status` green) with `SNAPCHAT_ORGANIZATION_ID` / `SNAPCHAT_AD_ACCOUNT_ID` set.
- Snap Pixel installed on the site and firing PURCHASE events (`pixel list`, then `pixel stats <PIXEL_ID>` to confirm event volume). Conversion optimization without a warm pixel will misbid.
- A Public Profile ID (required for most creatives): visible in Business Manager, or via `me`.
- For Collection/DPA ads: a catalog with product sets (`catalog list`, `catalog product-sets <CATALOG_ID>`), a dynamic template, and an interaction zone.

## 1. Campaign

One campaign per product line keeps budgets, reporting, and the optimizer's product routing clean.

```bash
snapchat-ads campaign create \
  --name "Product A — Prospecting" \
  --objective-v2-type WEB_CONVERSION \
  --promotion-type SALES \
  --buy-model AUCTION \
  --status PAUSED \
  --execute
```

Always create PAUSED. `objective_v2_type` is the future-proof field; the legacy `--objective` path still exists but don't use it for new builds.

## 2. Ad squad

`adsquad smart-create` assembles the payload from first-class flags — no hand-rolled JSON:

```bash
snapchat-ads adsquad smart-create <CAMPAIGN_ID> \
  --name "US 25-54 Cold" \
  --optimization-goal PIXEL_PURCHASE \
  --bid-strategy TARGET_COST \
  --bid 60 \
  --daily-budget 75 \
  --conversion-window SWIPE_28DAY_VIEW_1DAY \
  --pixel-id $SNAPCHAT_PIXEL_ID \
  --targeting-json '{"geos":[{"country_code":"us"}],"demographics":[{"min_age":25,"max_age":54}]}' \
  --execute
```

Choices that matter:

- **Bid strategy — use `TARGET_COST`** if you want the optimizer to manage the account. Its whole control model walks the CPA target up/down as the primary spend lever. `LOWEST_COST_WITH_MAX_BID` works for manual accounts but gives the optimizer only the budget lever.
- **Initial CPA target:** set it at (or slightly above) your actual target CPA. Set the initial `--bid` inside the optimizer's configured floor/ceiling band so its first adjustments aren't clamped.
- **Daily budget: set it high relative to expected spend.** Under TARGET_COST the CPA target throttles spend, not the budget. The optimizer raises budgets only when utilization ≥ 85%, and cuts CPA — not budget — first when performance dips.
- **Broad targeting** (age band + geo, no interest stacking) is the default posture; Snap's delivery does the narrowing. Use `targeting insights-breakdown` to sanity-check audience size, and `estimate audience-size` before launch.
- One squad per audience temperature (cold / retargeting) per product is enough to start. More squads fragment the pixel signal.

## 3. Media + creative

```bash
# Upload (video: 9:16 vertical, H.264/yuv420p, 3-180s; check with media status)
snapchat-ads media upload --file ad.mp4 --type VIDEO --execute   # -> MEDIA_ID

# Standard web-view (single video -> LP)
snapchat-ads creative create-web-view \
  --name "Product A — hook v1" \
  --headline "Your headline here" \
  --brand-name "$BRAND_NAME" \
  --top-snap-media-id <MEDIA_ID> \
  --url "https://example.com/products/product-a" \
  --profile-id <PUBLIC_PROFILE_ID> \
  --call-to-action SHOP_NOW \
  --execute

# Collection / DPA (catalog-driven product tiles under the video)
snapchat-ads creative create-collection \
  --name "Product A — DPA v1" \
  --brand-name "$BRAND_NAME" \
  --headline "Your headline here" \
  --top-snap-media-id <MEDIA_ID> \
  --profile-id <PUBLIC_PROFILE_ID> \
  --interaction-zone-id <INTERACTION_ZONE_ID> \
  --url "https://example.com/products/product-a" \
  --execute
```

Creative supply is the real growth lever: plan 3–5 concurrent creatives per squad and refresh weekly. If you run Meta, the Meta→Snap bridge (`skills/snapchat-ads/references/meta-to-snap-dpa-video-bridge.md`) automates pulling your proven Meta winners in.

## 4. Ad

```bash
snapchat-ads ad create <AD_SQUAD_ID> \
  --payload-json '{"name":"Product A — hook v1","creative_id":"<CREATIVE_ID>","status":"PAUSED","type":"SNAP_AD"}' \
  --execute
```

Collection ads use `"type":"COLLECTION"` (DPA additionally `"render_type":"DYNAMIC"`). Check `ad get <AD_ID>` → `review_status` / `delivery_status` before launching; fix any blocking reasons while PAUSED.

## 5. Launch + register with the optimizer

```bash
snapchat-ads ad launch <AD_SQUAD_ID> <AD_ID> --execute
snapchat-ads campaign launch <CAMPAIGN_ID> --execute
```

Then record the new IDs in `config/optimizer.json` under the matching product (`dpa_campaign_id`, `dpa_ad_squad_id`, `nondpa_*`, `product_set_id`) so the daily optimizer routes and manages them. New ads are protected by the optimizer's 72h fresh-hold before any pause logic applies.

## Ongoing cadence

- **Hourly:** heartbeat report to Slack (`reports/`).
- **Daily:** optimizer run — CPA/budget walks + underperformer pauses within guardrails (`docs/bids-and-budgets.md`).
- **Weekly:** creative refresh (Meta-winners bridge or new uploads), review blended ROAS trend vs target in the warehouse (`warehouse/table-map.md` query cookbook).
