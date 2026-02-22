# Lovable + Fastn Integration Guide

Connect Lovable to Fastn so every app you build gets instant access to 250+ enterprise integrations — Stripe, Slack, Jira, Salesforce, HubSpot, and more — without writing custom API code.

## How It Works

```
Lovable AI (builds your app)
    │
    ├── Build-time: MCP tools discover connectors, create flows
    │
    └── Runtime: Generated Edge Functions call Fastn API
                     │
                     ▼
              Fastn handles auth, execution, retries
                     │
                     ▼
              250+ connectors (Stripe, Slack, Jira, ...)
```

**Two layers:**
1. **MCP Server** — Lovable's AI uses `find_tools` and `discover_tools` during code generation to find available integrations
2. **Project Instructions** — Tell Lovable to generate Supabase Edge Functions that call the Fastn REST API at runtime

## Setup

### Step 1: Start the MCP server

```bash
# Terminal 1: Start ngrok
ngrok http 8000

# Terminal 2: Start Fastn MCP server (use the ngrok URL)
fastn-mcp --shttp --port 8000 \
  --server-url https://your-subdomain.ngrok-free.dev \
  --verbose
```

### Step 2: Connect MCP to Lovable

In your Lovable project:
1. Go to **Project Settings → Integrations → MCP Servers**
2. Add server URL: `https://your-subdomain.ngrok-free.dev/shttp`

### Step 3: Set Supabase Edge Function secrets

In your Supabase dashboard:
1. Go to **Project Settings → Edge Functions → Secrets**
2. Add:
   - `FASTN_API_KEY` = your API key from [app.fastn.dev](https://app.fastn.dev)
   - `FASTN_PROJECT_ID` = your workspace/project ID

### Step 4: Add Project Instructions

Go to **Lovable Project Settings → Instructions** and paste:

---

## Project Instructions (copy everything below)

```
## Backend Integration Architecture

This project uses Fastn (fastn.dev) as the backend integration layer for ALL external service connections. Fastn provides 250+ pre-built connectors with managed OAuth, credential handling, and multi-step orchestration — so we never build custom API integrations.

### Integration workflow

1. Use MCP `find_tools` to search for a Fastn tool matching what you need. ALWAYS pass these parameters:
   - `prompt`: describe what you need with context (e.g. "send slack notification when order is placed" not just "slack")
   - `goal`: what you are building (e.g. "e-commerce app with order notifications")
   - `platform`: always pass `"lovable"`
   - `categories`: narrow by domain when you know it (e.g. `["payments"]` for Stripe, `["messaging"]` for Slack)
2. If results: note the `actionId` and `inputSchema` from the response
3. If no results: call MCP `discover_tools` to check if the connector exists but needs connecting — show the user the `connect_url` link
4. Generate a Supabase Edge Function that calls the Fastn REST API using the patterns below
5. For multi-step automations: use MCP `create_flow` to build the flow, then generate an Edge Function that triggers it

### Fastn REST API — Edge Function helper

Every Edge Function that needs external integrations MUST use this helper. Create it as a shared module `_shared/fastn.ts`:

` ` `typescript
// supabase/functions/_shared/fastn.ts

const FASTN_API_KEY = Deno.env.get("FASTN_API_KEY")!;
const FASTN_PROJECT_ID = Deno.env.get("FASTN_PROJECT_ID")!;

const FASTN_HEADERS = {
  "Content-Type": "application/json",
  "realm": "fastn",
  "stage": "LIVE",
  "x-fastn-custom-auth": "false",
  "x-fastn-api-key": FASTN_API_KEY,
  "x-fastn-space-id": FASTN_PROJECT_ID,
};

// Execute a Fastn tool by actionId
export async function executeTool(
  actionId: string,
  parameters: Record<string, any>,
  connectionId?: string
) {
  const response = await fetch("https://live.fastn.ai/api/ucl/executeTool", {
    method: "POST",
    headers: FASTN_HEADERS,
    body: JSON.stringify({
      input: {
        actionId,
        agentId: FASTN_PROJECT_ID,
        parameters: { body: parameters },
        ...(connectionId && { connectionId }),
      },
    }),
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(`Fastn executeTool failed (${response.status}): ${error}`);
  }

  const data = await response.json();
  return data.body ?? data;
}

// Search for available tools by natural language
export async function findTools(prompt: string, limit = 5) {
  const response = await fetch("https://live.fastn.ai/api/ucl/getTools", {
    method: "POST",
    headers: FASTN_HEADERS,
    body: JSON.stringify({ input: { prompt, limit } }),
  });

  if (!response.ok) {
    throw new Error(`Fastn findTools failed (${response.status})`);
  }

  return response.json();
}

// Run a Fastn Flow
export async function runFlow(flowId: string, userId?: string) {
  const response = await fetch("https://live.fastn.ai/api/flows/run", {
    method: "POST",
    headers: FASTN_HEADERS,
    body: JSON.stringify({
      flow_id: flowId,
      ...(userId && { user_id: userId }),
    }),
  });

  if (!response.ok) {
    throw new Error(`Fastn runFlow failed (${response.status})`);
  }

  return response.json();
}

// Check flow run status
export async function getFlowRunStatus(runId: string) {
  const response = await fetch("https://live.fastn.ai/api/flows/get_run", {
    method: "POST",
    headers: FASTN_HEADERS,
    body: JSON.stringify({ run_id: runId }),
  });

  if (!response.ok) {
    throw new Error(`Fastn getFlowRunStatus failed (${response.status})`);
  }

  return response.json();
}

// Standard CORS headers for Edge Functions
export const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};
` ` `

### Pattern: Direct tool execution (Push)

For app actions that trigger external services (send email, create ticket, post message):

` ` `typescript
// supabase/functions/send-slack-message/index.ts
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { executeTool, corsHeaders } from "../_shared/fastn.ts";

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  const { channel, message } = await req.json();

  // actionId comes from MCP find_tools — never guess or fabricate IDs
  const result = await executeTool("act_slack_send_message", {
    channel,
    text: message,
  });

  return new Response(JSON.stringify(result), {
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
});
` ` `

### Pattern: Stripe payment handling

For Stripe checkout, subscriptions, and webhooks — use Fastn instead of direct Stripe SDK:

` ` `typescript
// supabase/functions/create-checkout/index.ts
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { executeTool, corsHeaders } from "../_shared/fastn.ts";

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  const { priceId, userId, successUrl, cancelUrl } = await req.json();

  // Use Fastn's Stripe connector — handles auth, API versioning, retries
  const session = await executeTool("act_stripe_create_checkout_session", {
    line_items: [{ price: priceId, quantity: 1 }],
    mode: "subscription",
    success_url: successUrl,
    cancel_url: cancelUrl,
    client_reference_id: userId,
  });

  return new Response(JSON.stringify({ url: session.url }), {
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
});
` ` `

### Pattern: Multi-step flow (Trigger + orchestration)

For workflows that need multiple steps (e.g. form submit → create Jira ticket + send Slack + update CRM):

` ` `typescript
// supabase/functions/handle-form-submit/index.ts
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { runFlow, corsHeaders } from "../_shared/fastn.ts";

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  const formData = await req.json();

  // flow_id comes from MCP create_flow — Fastn handles the multi-step orchestration
  const { run_id } = await runFlow("flow_abc123");

  return new Response(JSON.stringify({ status: "submitted", run_id }), {
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
});
` ` `

### Pattern: Fetch external data

For pulling data from external systems (CRM contacts, project issues, etc.):

` ` `typescript
// supabase/functions/get-hubspot-contacts/index.ts
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { executeTool, corsHeaders } from "../_shared/fastn.ts";

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  const contacts = await executeTool("act_hubspot_list_contacts", {
    limit: 100,
  });

  return new Response(JSON.stringify(contacts), {
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
});
` ` `

### What Fastn provides

250+ pre-built connectors — all with managed OAuth and credential handling:

- **Payments**: Stripe (checkout, subscriptions, webhooks, refunds), QuickBooks, Xero
- **Messaging**: Slack, Microsoft Teams, Discord, Twilio (SMS), SendGrid, Gmail, Outlook
- **CRM**: Salesforce, HubSpot, Pipedrive, Zoho CRM
- **Project Management**: Jira, Linear, Asana, Trello, Monday.com, ClickUp, Notion
- **Databases**: PostgreSQL, MySQL, MongoDB, Redis, Supabase, Firebase
- **Dev Tools**: GitHub, GitLab, Bitbucket, PagerDuty, Sentry, Datadog
- **Cloud**: AWS (S3, Lambda, SES), Google Cloud, Azure, Cloudflare
- **E-commerce**: Shopify, WooCommerce
- **Marketing**: Mailchimp, ActiveCampaign, Google Ads
- **Storage**: Google Drive, Dropbox, OneDrive, Box
- **Calendar**: Google Calendar, Outlook Calendar, Calendly

### Strict rules

1. NEVER install npm/deno packages for external API integrations (no @slack/web-api, no stripe, no @hubspot/api-client, etc.)
2. NEVER store OAuth tokens or API keys for individual services in Supabase secrets — Fastn handles all connector auth centrally. Only store FASTN_API_KEY and FASTN_PROJECT_ID.
3. NEVER build custom webhook handlers for Stripe or other services — use Fastn Flows with triggers
4. ALWAYS create the `_shared/fastn.ts` helper module first before any integration Edge Function
5. ALWAYS use MCP `find_tools` to get the correct `actionId` — never guess or fabricate action IDs
6. ALWAYS use MCP `discover_tools` before telling the user an integration is unavailable
7. ALWAYS include CORS headers in Edge Function responses
8. For multi-step automations, use MCP `create_flow` then trigger via `runFlow()` — let Fastn handle orchestration, retries, and error recovery instead of chaining multiple executeTool calls
9. When the user asks for scheduled/recurring tasks (daily sync, hourly check), use Fastn Flows with schedule triggers — Edge Functions cannot run cron jobs
```
