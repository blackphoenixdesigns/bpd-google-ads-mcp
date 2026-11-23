#!/usr/bin/env python3
"""
Black Phoenix Designs — Google Ads MCP Server
Credentials are loaded from environment variables (set in Render dashboard).
Uses mcp 2.x Streamable HTTP transport for claude.ai compatibility.
"""

import os
import json
import datetime
from google.api_core import protobuf_helpers
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse, Response
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# ── Auth token ────────────────────────────────────────────────────────────────

AUTH_TOKEN = os.environ["MCP_AUTH_TOKEN"]

# ── Credentials from environment variables ────────────────────────────────────

DEVELOPER_TOKEN = os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"]
CLIENT_ID       = os.environ["GOOGLE_ADS_CLIENT_ID"]
CLIENT_SECRET   = os.environ["GOOGLE_ADS_CLIENT_SECRET"]
REFRESH_TOKEN   = os.environ["GOOGLE_ADS_REFRESH_TOKEN"]
CUSTOMER_ID     = os.environ["GOOGLE_ADS_CUSTOMER_ID"]   # no dashes


# ── Google Ads client ─────────────────────────────────────────────────────────

def ads_client():
    return GoogleAdsClient.load_from_dict({
        "developer_token": DEVELOPER_TOKEN,
        "client_id":       CLIENT_ID,
        "client_secret":   CLIENT_SECRET,
        "refresh_token":   REFRESH_TOKEN,
        "use_proto_plus":  True,
    })


def run_query(gaql: str) -> list:
    client  = ads_client()
    service = client.get_service("GoogleAdsService")
    stream  = service.search_stream(customer_id=CUSTOMER_ID, query=gaql)
    rows = []
    for batch in stream:
        for row in batch.results:
            rows.append(row)
    return rows


def micros(val) -> float:
    return round(val / 1_000_000, 2)


def date_range_clause(days: int = 30,
                      start: str | None = None,
                      end:   str | None = None) -> str:
    if start and end:
        return f"segments.date BETWEEN '{start}' AND '{end}'"
    today = datetime.date.today()
    s = (today - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    e = today.strftime("%Y-%m-%d")
    return f"segments.date BETWEEN '{s}' AND '{e}'"


# ── Tool implementations ──────────────────────────────────────────────────────

def tool_get_account_overview(days: int = 30) -> str:
    clause = date_range_clause(days)
    rows = run_query(f"""
        SELECT metrics.impressions, metrics.clicks,
               metrics.cost_micros, metrics.conversions,
               metrics.conversions_value, metrics.ctr,
               metrics.average_cpc
        FROM customer
        WHERE {clause}
    """)
    if not rows:
        return json.dumps({"error": "No data"})
    r = rows[0]
    spend = micros(r.metrics.cost_micros)
    return json.dumps({
        "period_days":    days,
        "impressions":    r.metrics.impressions,
        "clicks":         r.metrics.clicks,
        "spend_gbp":      spend,
        "ctr_pct":        round(r.metrics.ctr * 100, 2),
        "avg_cpc_gbp":    micros(r.metrics.average_cpc),
        "conversions":    round(r.metrics.conversions, 1),
        "conv_value_gbp": round(r.metrics.conversions_value, 2),
        "roas":           round(r.metrics.conversions_value / max(spend, 0.01), 2),
    }, indent=2)


def tool_get_campaigns() -> str:
    rows = run_query("""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.advertising_channel_type,
               campaign_budget.amount_micros,
               campaign.start_date_time, campaign.end_date_time
        FROM campaign
        ORDER BY campaign.name
    """)
    results = [{
        "id":               r.campaign.id,
        "name":             r.campaign.name,
        "status":           r.campaign.status.name,
        "type":             r.campaign.advertising_channel_type.name,
        "daily_budget_gbp": micros(r.campaign_budget.amount_micros),
        "start":            r.campaign.start_date_time,
        "end":              r.campaign.end_date_time or "ongoing",
    } for r in rows]
    return json.dumps(results, indent=2)


def tool_get_performance(days: int = 30,
                         start: str | None = None,
                         end:   str | None = None) -> str:
    clause = date_range_clause(days, start, end)
    rows = run_query(f"""
        SELECT campaign.name, campaign.status,
               metrics.impressions, metrics.clicks,
               metrics.cost_micros, metrics.ctr,
               metrics.average_cpc, metrics.conversions,
               metrics.conversions_value
        FROM campaign
        WHERE {clause}
        ORDER BY metrics.cost_micros DESC
    """)
    results = [{
        "campaign":       r.campaign.name,
        "status":         r.campaign.status.name,
        "impressions":    r.metrics.impressions,
        "clicks":         r.metrics.clicks,
        "spend_gbp":      micros(r.metrics.cost_micros),
        "ctr_pct":        round(r.metrics.ctr * 100, 2),
        "avg_cpc_gbp":    micros(r.metrics.average_cpc),
        "conversions":    round(r.metrics.conversions, 1),
        "conv_value_gbp": round(r.metrics.conversions_value, 2),
    } for r in rows]
    total = sum(r["spend_gbp"] for r in results)
    return json.dumps({"period": clause, "total_spend_gbp": total,
                       "campaigns": results}, indent=2)


def tool_get_search_terms(days: int = 30) -> str:
    clause = date_range_clause(days)
    rows = run_query(f"""
        SELECT search_term_view.search_term,
               search_term_view.status,
               campaign.name,
               metrics.impressions, metrics.clicks,
               metrics.cost_micros, metrics.conversions
        FROM search_term_view
        WHERE {clause}
        ORDER BY metrics.cost_micros DESC
        LIMIT 50
    """)
    results = [{
        "term":        r.search_term_view.search_term,
        "status":      r.search_term_view.status.name,
        "campaign":    r.campaign.name,
        "impressions": r.metrics.impressions,
        "clicks":      r.metrics.clicks,
        "spend_gbp":   micros(r.metrics.cost_micros),
        "conversions": round(r.metrics.conversions, 1),
    } for r in rows]
    return json.dumps(results, indent=2)


def tool_get_keywords(campaign_name: str | None = None,
                      days: int = 30) -> str:
    clause = date_range_clause(days)
    where = f"WHERE {clause}"
    if campaign_name:
        where += f" AND campaign.name = '{campaign_name}'"
    rows = run_query(f"""
        SELECT campaign.name, ad_group.name,
               ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type,
               ad_group_criterion.status,
               metrics.impressions, metrics.clicks,
               metrics.cost_micros, metrics.conversions
        FROM keyword_view
        {where}
        ORDER BY metrics.cost_micros DESC
        LIMIT 100
    """)
    results = [{
        "keyword":     r.ad_group_criterion.keyword.text,
        "match_type":  r.ad_group_criterion.keyword.match_type.name,
        "status":      r.ad_group_criterion.status.name,
        "campaign":    r.campaign.name,
        "ad_group":    r.ad_group.name,
        "impressions": r.metrics.impressions,
        "clicks":      r.metrics.clicks,
        "spend_gbp":   micros(r.metrics.cost_micros),
        "conversions": round(r.metrics.conversions, 1),
    } for r in rows]
    return json.dumps(results, indent=2)


def tool_get_ad_groups(campaign_name: str | None = None) -> str:
    where = f"WHERE campaign.name = '{campaign_name}'" if campaign_name else ""
    rows = run_query(f"""
        SELECT campaign.name, ad_group.id, ad_group.name,
               ad_group.status, ad_group.type,
               ad_group.cpc_bid_micros
        FROM ad_group
        {where}
        ORDER BY campaign.name, ad_group.name
    """)
    results = [{
        "id":          r.ad_group.id,
        "name":        r.ad_group.name,
        "campaign":    r.campaign.name,
        "status":      r.ad_group.status.name,
        "type":        r.ad_group.type_.name,
        "cpc_bid_gbp": micros(r.ad_group.cpc_bid_micros),
    } for r in rows]
    return json.dumps(results, indent=2)


def tool_update_campaign_budget(campaign_name: str,
                                new_daily_budget_gbp: float) -> str:
    rows = run_query(f"""
        SELECT campaign.name, campaign_budget.resource_name,
               campaign_budget.amount_micros
        FROM campaign
        WHERE campaign.name = '{campaign_name}'
        LIMIT 1
    """)
    if not rows:
        return json.dumps({"error": f"Campaign '{campaign_name}' not found"})
    client  = ads_client()
    service = client.get_service("CampaignBudgetService")
    budget  = client.get_type("CampaignBudget")
    budget.resource_name = rows[0].campaign_budget.resource_name
    budget.amount_micros = int(new_daily_budget_gbp * 1_000_000)
    op = client.get_type("CampaignBudgetOperation")
    op.update = budget
    op.update_mask = protobuf_helpers.field_mask(None, budget._pb)
    service.mutate_campaign_budgets(customer_id=CUSTOMER_ID, operations=[op])
    return json.dumps({
        "success":         True,
        "campaign":        campaign_name,
        "old_budget_gbp":  micros(rows[0].campaign_budget.amount_micros),
        "new_budget_gbp":  new_daily_budget_gbp,
    })


def tool_set_campaign_status(campaign_name: str, status: str) -> str:
    status = status.upper()
    if status not in ("ENABLED", "PAUSED"):
        return json.dumps({"error": "status must be ENABLED or PAUSED"})
    rows = run_query(f"""
        SELECT campaign.resource_name, campaign.status
        FROM campaign
        WHERE campaign.name = '{campaign_name}'
        LIMIT 1
    """)
    if not rows:
        return json.dumps({"error": f"Campaign '{campaign_name}' not found"})
    client   = ads_client()
    service  = client.get_service("CampaignService")
    campaign = client.get_type("Campaign")
    campaign.resource_name = rows[0].campaign.resource_name
    campaign.status = getattr(
        client.enums.CampaignStatusEnum, status)
    op = client.get_type("CampaignOperation")
    op.update = campaign
    op.update_mask = protobuf_helpers.field_mask(None, campaign._pb)
    service.mutate_campaigns(customer_id=CUSTOMER_ID, operations=[op])
    return json.dumps({
        "success":    True,
        "campaign":   campaign_name,
        "old_status": rows[0].campaign.status.name,
        "new_status": status,
    })


def tool_add_negative_keywords(campaign_name: str, keywords: list) -> str:
    rows = run_query(f"""
        SELECT campaign.resource_name
        FROM campaign
        WHERE campaign.name = '{campaign_name}'
        LIMIT 1
    """)
    if not rows:
        return json.dumps({"error": f"Campaign '{campaign_name}' not found"})
    client  = ads_client()
    service = client.get_service("CampaignCriterionService")
    ops = []
    for kw in keywords:
        criterion = client.get_type("CampaignCriterion")
        criterion.campaign       = rows[0].campaign.resource_name
        criterion.negative       = True
        criterion.keyword.text   = kw.lower().strip()
        criterion.keyword.match_type = (
            client.enums.KeywordMatchTypeEnum.KeywordMatchType.BROAD)
        op = client.get_type("CampaignCriterionOperation")
        op.create.CopyFrom(criterion)
        ops.append(op)
    service.mutate_campaign_criteria(customer_id=CUSTOMER_ID, operations=ops)
    return json.dumps({"success": True, "campaign": campaign_name,
                       "added_negatives": keywords})


def tool_create_search_campaign(name: str,
                                daily_budget_gbp: float,
                                start_paused: bool = True) -> str:
    client = ads_client()

    budget_svc = client.get_service("CampaignBudgetService")
    budget     = client.get_type("CampaignBudget")
    budget.name            = f"{name} Budget"
    budget.amount_micros   = int(daily_budget_gbp * 1_000_000)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget_op = client.get_type("CampaignBudgetOperation")
    budget_op.create.CopyFrom(budget)
    budget_res = budget_svc.mutate_campaign_budgets(
        customer_id=CUSTOMER_ID, operations=[budget_op])
    budget_rn = budget_res.results[0].resource_name

    camp_svc = client.get_service("CampaignService")
    campaign = client.get_type("Campaign")
    campaign.name                   = name
    campaign.campaign_budget        = budget_rn
    campaign.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.SEARCH)
    campaign.status = (
        client.enums.CampaignStatusEnum.PAUSED if start_paused
        else client.enums.CampaignStatusEnum.ENABLED)
    campaign.manual_cpc.enhanced_cpc_enabled = False
    campaign.network_settings.target_google_search  = True
    campaign.network_settings.target_search_network = False
    campaign.network_settings.target_content_network = False
    campaign.start_date = datetime.date.today().strftime("%Y%m%d")

    camp_op = client.get_type("CampaignOperation")
    camp_op.create.CopyFrom(campaign)
    camp_res = camp_svc.mutate_campaigns(
        customer_id=CUSTOMER_ID, operations=[camp_op])
    camp_rn = camp_res.results[0].resource_name

    return json.dumps({
        "success":           True,
        "campaign_name":     name,
        "resource_name":     camp_rn,
        "daily_budget_gbp":  daily_budget_gbp,
        "status":            "PAUSED" if start_paused else "ENABLED",
        "note": "Add ad groups and ads before enabling.",
    })


def tool_create_ad_group(campaign_name: str,
                         ad_group_name: str,
                         cpc_bid_gbp: float) -> str:
    rows = run_query(f"""
        SELECT campaign.resource_name
        FROM campaign
        WHERE campaign.name = '{campaign_name}'
        LIMIT 1
    """)
    if not rows:
        return json.dumps({"error": f"Campaign '{campaign_name}' not found"})

    client   = ads_client()
    service  = client.get_service("AdGroupService")
    ad_group = client.get_type("AdGroup")
    ad_group.name            = ad_group_name
    ad_group.campaign        = rows[0].campaign.resource_name
    ad_group.type_           = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ad_group.cpc_bid_micros  = int(cpc_bid_gbp * 1_000_000)
    ad_group.status          = client.enums.AdGroupStatusEnum.ENABLED

    op = client.get_type("AdGroupOperation")
    op.create.CopyFrom(ad_group)
    res = service.mutate_ad_groups(customer_id=CUSTOMER_ID, operations=[op])

    return json.dumps({
        "success":       True,
        "campaign":      campaign_name,
        "ad_group":      ad_group_name,
        "resource_name": res.results[0].resource_name,
        "cpc_bid_gbp":   cpc_bid_gbp,
    })


def tool_create_responsive_search_ad(campaign_name: str,
                                     ad_group_name: str,
                                     headlines: list,
                                     descriptions: list,
                                     final_url: str) -> str:
    if len(headlines) < 3 or len(headlines) > 15:
        return json.dumps({"error": "Provide 3–15 headlines"})
    if len(descriptions) < 2 or len(descriptions) > 4:
        return json.dumps({"error": "Provide 2–4 descriptions"})
    if any(len(h) > 30 for h in headlines):
        bad = [h for h in headlines if len(h) > 30]
        return json.dumps({"error": f"Headlines over 30 chars: {bad}"})
    if any(len(d) > 90 for d in descriptions):
        bad = [d for d in descriptions if len(d) > 90]
        return json.dumps({"error": f"Descriptions over 90 chars: {bad}"})

    rows = run_query(f"""
        SELECT ad_group.resource_name
        FROM ad_group
        WHERE ad_group.name = '{ad_group_name}'
          AND campaign.name = '{campaign_name}'
        LIMIT 1
    """)
    if not rows:
        return json.dumps({"error": f"Ad group '{ad_group_name}' not found in '{campaign_name}'"})

    client  = ads_client()
    service = client.get_service("AdGroupAdService")

    ad_group_ad          = client.get_type("AdGroupAd")
    ad_group_ad.ad_group = rows[0].ad_group.resource_name
    ad_group_ad.status   = client.enums.AdGroupAdStatusEnum.ENABLED

    ad = ad_group_ad.ad
    ad.final_urls.append(final_url)

    rsa = ad.responsive_search_ad
    for text in headlines:
        asset      = client.get_type("AdTextAsset")
        asset.text = text
        rsa.headlines.append(asset)
    for text in descriptions:
        asset      = client.get_type("AdTextAsset")
        asset.text = text
        rsa.descriptions.append(asset)

    op = client.get_type("AdGroupAdOperation")
    op.create.CopyFrom(ad_group_ad)
    res = service.mutate_ad_group_ads(customer_id=CUSTOMER_ID, operations=[op])

    return json.dumps({
        "success":       True,
        "campaign":      campaign_name,
        "ad_group":      ad_group_name,
        "resource_name": res.results[0].resource_name,
        "headlines":     headlines,
        "descriptions":  descriptions,
        "final_url":     final_url,
    })


def tool_add_keywords_to_ad_group(campaign_name: str,
                                   ad_group_name: str,
                                   keywords: list) -> str:
    rows = run_query(f"""
        SELECT ad_group.resource_name
        FROM ad_group
        WHERE ad_group.name = '{ad_group_name}'
          AND campaign.name = '{campaign_name}'
        LIMIT 1
    """)
    if not rows:
        return json.dumps({"error": f"Ad group '{ad_group_name}' not found in '{campaign_name}'"})

    ad_group_rn = rows[0].ad_group.resource_name
    client      = ads_client()
    service     = client.get_service("AdGroupCriterionService")

    match_map = {
        "EXACT":  client.enums.KeywordMatchTypeEnum.EXACT,
        "PHRASE": client.enums.KeywordMatchTypeEnum.PHRASE,
        "BROAD":  client.enums.KeywordMatchTypeEnum.BROAD,
    }

    ops = []
    for kw in keywords:
        criterion             = client.get_type("AdGroupCriterion")
        criterion.ad_group    = ad_group_rn
        criterion.status      = client.enums.AdGroupCriterionStatusEnum.ENABLED
        criterion.keyword.text       = kw["text"].lower().strip()
        criterion.keyword.match_type = match_map.get(
            kw.get("match_type", "BROAD").upper(),
            client.enums.KeywordMatchTypeEnum.BROAD)
        if kw.get("cpc_bid_gbp"):
            criterion.cpc_bid_micros = int(kw["cpc_bid_gbp"] * 1_000_000)
        op = client.get_type("AdGroupCriterionOperation")
        op.create.CopyFrom(criterion)
        ops.append(op)

    service.mutate_ad_group_criteria(customer_id=CUSTOMER_ID, operations=ops)

    return json.dumps({
        "success":    True,
        "campaign":   campaign_name,
        "ad_group":   ad_group_name,
        "added":      len(ops),
        "keywords":   [{"text": kw["text"],
                         "match_type": kw.get("match_type", "BROAD")}
                        for kw in keywords],
    })


# ── MCP Server (mcp 2.x) ─────────────────────────────────────────────────────

mcp = MCPServer("bpd-google-ads")


def _safe(fn, *args, **kwargs) -> str:
    try:
        return fn(*args, **kwargs)
    except GoogleAdsException as e:
        errors = [str(err.message) for err in e.failure.errors]
        return json.dumps({"error": "Google Ads API error", "details": errors})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_account_overview(days: int = 30) -> str:
    """Overall account metrics: spend, clicks, impressions, conversions, ROAS."""
    return _safe(tool_get_account_overview, days)


@mcp.tool()
def get_campaigns() -> str:
    """List all campaigns with status, type and daily budget."""
    return _safe(tool_get_campaigns)


@mcp.tool()
def get_performance(days: int = 30, start: str = "", end: str = "") -> str:
    """Campaign-level spend, clicks, impressions, CTR and conversions."""
    return _safe(tool_get_performance, days, start or None, end or None)


@mcp.tool()
def get_search_terms(days: int = 30) -> str:
    """Top 50 search terms by spend with clicks and conversions."""
    return _safe(tool_get_search_terms, days)


@mcp.tool()
def get_keywords(campaign_name: str = "", days: int = 30) -> str:
    """Keywords with performance metrics, optionally filtered by campaign name."""
    return _safe(tool_get_keywords, campaign_name or None, days)


@mcp.tool()
def get_ad_groups(campaign_name: str = "") -> str:
    """List ad groups, optionally filtered by campaign name."""
    return _safe(tool_get_ad_groups, campaign_name or None)


@mcp.tool()
def update_campaign_budget(campaign_name: str, new_daily_budget_gbp: float) -> str:
    """Change a campaign's daily budget in GBP."""
    return _safe(tool_update_campaign_budget, campaign_name, new_daily_budget_gbp)


@mcp.tool()
def set_campaign_status(campaign_name: str, status: str) -> str:
    """Enable or pause a campaign. status must be ENABLED or PAUSED."""
    return _safe(tool_set_campaign_status, campaign_name, status)


@mcp.tool()
def add_negative_keywords(campaign_name: str, keywords: list[str]) -> str:
    """Add broad-match negative keywords to a campaign."""
    return _safe(tool_add_negative_keywords, campaign_name, keywords)


@mcp.tool()
def create_search_campaign(name: str, daily_budget_gbp: float,
                            start_paused: bool = True) -> str:
    """Create a new Google Search campaign. Starts paused by default so ads can be added before going live."""
    return _safe(tool_create_search_campaign, name, daily_budget_gbp, start_paused)


@mcp.tool()
def create_ad_group(campaign_name: str, ad_group_name: str,
                    cpc_bid_gbp: float) -> str:
    """Create an ad group inside an existing campaign with a max CPC bid in GBP."""
    return _safe(tool_create_ad_group, campaign_name, ad_group_name, cpc_bid_gbp)


@mcp.tool()
def create_responsive_search_ad(campaign_name: str, ad_group_name: str,
                                 headlines: list[str], descriptions: list[str],
                                 final_url: str) -> str:
    """Create a Responsive Search Ad. Requires 3-15 headlines (max 30 chars each) and 2-4 descriptions (max 90 chars each)."""
    return _safe(tool_create_responsive_search_ad,
                 campaign_name, ad_group_name, headlines, descriptions, final_url)


@mcp.tool()
def add_keywords_to_ad_group(campaign_name: str, ad_group_name: str,
                              keywords: list) -> str:
    """Add keywords to an ad group. Each keyword: {text, match_type (BROAD/PHRASE/EXACT), cpc_bid_gbp (optional)}."""
    return _safe(tool_add_keywords_to_ad_group,
                 campaign_name, ad_group_name, keywords)


# ── Health routes (registered before streamable_http_app is called) ───────────

@mcp.custom_route("/", methods=["GET"])
async def root(request):
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "ok"})


# ── Auth middleware ───────────────────────────────────────────────────────────

class TokenAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        # Always allow health checks
        if path in ("/", "/health"):
            return await call_next(request)
        # Allow requests continuing an established MCP session
        if "mcp-session-id" in request.headers:
            return await call_next(request)
        # Require token for new connections
        if request.query_params.get("token") != AUTH_TOKEN:
            return Response("Unauthorized", status_code=401)
        return await call_next(request)


# ── Starlette app ─────────────────────────────────────────────────────────────
# Use streamable_http_app directly (preserves its lifespan/session manager).
# host="0.0.0.0" disables localhost-only DNS rebinding protection so Render works.

starlette_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    host="0.0.0.0",
)

starlette_app.add_middleware(TokenAuthMiddleware)
starlette_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(starlette_app, host="0.0.0.0", port=port)
