#!/usr/bin/env python3
"""
Black Phoenix Designs — Google Ads MCP Server
Credentials are loaded from environment variables (set in Render dashboard).
"""

import os
import json
import datetime
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse, Response
from starlette.middleware.cors import CORSMiddleware
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
               campaign.start_date, campaign.end_date
        FROM campaign
        ORDER BY campaign.name
    """)
    results = [{
        "id":               r.campaign.id,
        "name":             r.campaign.name,
        "status":           r.campaign.status.name,
        "type":             r.campaign.advertising_channel_type.name,
        "daily_budget_gbp": micros(r.campaign_budget.amount_micros),
        "start":            r.campaign.start_date,
        "end":              r.campaign.end_date or "ongoing",
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
    op.update.CopyFrom(budget)
    op.update_mask.paths.append("amount_micros")
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
        client.enums.CampaignStatusEnum.CampaignStatus, status)
    op = client.get_type("CampaignOperation")
    op.update.CopyFrom(campaign)
    op.update_mask.paths.append("status")
    service.mutate_campaigns(customer_id=CUSTOMER_ID, operations=[op])
    return json.dumps({
        "success":    True,
        "campaign":   campaign_name,
        "old_status": rows[0].campaign.status.name,
        "new_status": status,
    })


def tool_add_negative_keywords(campaign_name: str,
                                keywords: list) -> str:
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


# ── MCP server ────────────────────────────────────────────────────────────────

server = Server("bpd-google-ads")

TOOLS = [
    Tool(name="get_account_overview",
         description="Overall account metrics: spend, clicks, impressions, conversions, ROAS.",
         inputSchema={"type":"object","properties":{
             "days":{"type":"integer","description":"Days back from today (default 30)"}}}),
    Tool(name="get_campaigns",
         description="List all campaigns with status, type and daily budget.",
         inputSchema={"type":"object","properties":{}}),
    Tool(name="get_performance",
         description="Campaign-level spend, clicks, impressions, CTR and conversions.",
         inputSchema={"type":"object","properties":{
             "days":{"type":"integer","description":"Days back (default 30)"},
             "start":{"type":"string","description":"Start date YYYY-MM-DD"},
             "end":{"type":"string","description":"End date YYYY-MM-DD"}}}),
    Tool(name="get_search_terms",
         description="Top search terms by spend with clicks and conversions.",
         inputSchema={"type":"object","properties":{
             "days":{"type":"integer","description":"Days back (default 30)"}}}),
    Tool(name="get_keywords",
         description="Keywords with performance metrics, optionally filtered by campaign.",
         inputSchema={"type":"object","properties":{
             "campaign_name":{"type":"string","description":"Filter by campaign (optional)"},
             "days":{"type":"integer","description":"Days back (default 30)"}}}),
    Tool(name="get_ad_groups",
         description="List ad groups, optionally filtered by campaign.",
         inputSchema={"type":"object","properties":{
             "campaign_name":{"type":"string","description":"Campaign name (optional)"}}}),
    Tool(name="update_campaign_budget",
         description="Change a campaign's daily budget in GBP.",
         inputSchema={"type":"object","required":["campaign_name","new_daily_budget_gbp"],
                      "properties":{
             "campaign_name":{"type":"string"},
             "new_daily_budget_gbp":{"type":"number","description":"New daily budget in £"}}}),
    Tool(name="set_campaign_status",
         description="Enable or pause a campaign.",
         inputSchema={"type":"object","required":["campaign_name","status"],
                      "properties":{
             "campaign_name":{"type":"string"},
             "status":{"type":"string","enum":["ENABLED","PAUSED"]}}}),
    Tool(name="add_negative_keywords",
         description="Add broad-match negative keywords to a campaign.",
         inputSchema={"type":"object","required":["campaign_name","keywords"],
                      "properties":{
             "campaign_name":{"type":"string"},
             "keywords":{"type":"array","items":{"type":"string"}}}}),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        match name:
            case "get_account_overview":
                result = tool_get_account_overview(arguments.get("days", 30))
            case "get_campaigns":
                result = tool_get_campaigns()
            case "get_performance":
                result = tool_get_performance(
                    arguments.get("days", 30),
                    arguments.get("start"), arguments.get("end"))
            case "get_search_terms":
                result = tool_get_search_terms(arguments.get("days", 30))
            case "get_keywords":
                result = tool_get_keywords(
                    arguments.get("campaign_name"), arguments.get("days", 30))
            case "get_ad_groups":
                result = tool_get_ad_groups(arguments.get("campaign_name"))
            case "update_campaign_budget":
                result = tool_update_campaign_budget(
                    arguments["campaign_name"], arguments["new_daily_budget_gbp"])
            case "set_campaign_status":
                result = tool_set_campaign_status(
                    arguments["campaign_name"], arguments["status"])
            case "add_negative_keywords":
                result = tool_add_negative_keywords(
                    arguments["campaign_name"], arguments["keywords"])
            case _:
                result = json.dumps({"error": f"Unknown tool: {name}"})
    except GoogleAdsException as e:
        errors = [str(err.message) for err in e.failure.errors]
        result = json.dumps({"error": "Google Ads API error", "details": errors})
    except Exception as e:
        result = json.dumps({"error": str(e)})
    return [TextContent(type="text", text=result)]


# ── Starlette app ─────────────────────────────────────────────────────────────

sse = SseServerTransport("/messages/")


async def handle_sse(request):
    if request.query_params.get("token") != AUTH_TOKEN:
        return Response("Unauthorized", status_code=401)
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(streams[0], streams[1],
                         server.create_initialization_options())


async def health(request):
    return JSONResponse({"status": "ok"})


starlette_app = Starlette(routes=[
    Route("/",       endpoint=health),
    Route("/health", endpoint=health),
    Route("/sse",    endpoint=handle_sse),
    Mount("/messages", app=sse.handle_post_message),
])

starlette_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(starlette_app, host="0.0.0.0", port=port)
