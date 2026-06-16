# Octopus Energy access — API key (v1) and OAuth (optional, later)

Source: https://auth.octopus.energy · API: https://developer.octopus.energy · GraphQL: https://api.octopus.energy/v1/graphql/

## Recommendation: API key for v1 (no registration needed)

For a self-hosted, open-source tool, the simplest and most adoptable approach is for each user to supply **their own Octopus API key** (from https://octopus.energy/dashboard/developer/) plus their account number. ChargeWise then:

- reads tariff agreements, unit rates and half-hourly consumption via the **REST API** (HTTP basic auth, API key as username);
- obtains a short-lived **Kraken token** from the API key (`obtainKrakenToken` mutation) to read **Intelligent Octopus Go dispatches** via **GraphQL**.

This needs **no OAuth application** and is already implemented in `chargewise/ingest/octopus_rest.py` and `octopus_graphql.py`. **Account number recorded: `A-A8D3E32A`.**

## Optional: OAuth (a "Connect your Octopus account" flow)

If we later want users to connect without pasting an API key, Octopus runs an OAuth 2.0 server. You register an app by emailing **agile@octopus.energy** with a short description and these four details. Proposed answers for ChargeWise:

1. **Client type** — *confidential* (ChargeWise has a server backend that can keep a client secret).
2. **Grant type** — *authorization code with PKCE* (each user authorises ChargeWise to read their own account).
3. **Allowed redirect URIs** —
   - Production: `https://<chargewise-domain>/api/auth/octopus/callback`
   - Local dev: `http://localhost:8000/api/auth/octopus/callback`
4. **Resources to access** (read-only, for the authenticated account) —
   - REST: account & tariff agreements, electricity standard unit rates, half-hourly consumption
   - GraphQL: Intelligent Octopus Go completed/planned **dispatches**

The server supports authorization-code+PKCE, client-credentials, device-code and token-exchange grants; PKCE authorization-code is the right fit for a user-facing web app.

### Ready-to-send email (only if we pursue OAuth)

> To: agile@octopus.energy
> Subject: OAuth application request — ChargeWise (personal EV charging cost tracker)
>
> Hello, I'm building ChargeWise, an open-source tool that shows the true cost of charging an EV on Intelligent Octopus Go and the savings vs petrol. I'd like to register an OAuth application so users can connect their own Octopus account.
> 1. Client type: confidential
> 2. Grant type: authorization code with PKCE
> 3. Redirect URIs: https://<domain>/api/auth/octopus/callback and http://localhost:8000/api/auth/octopus/callback
> 4. Resources: account & tariff agreements, standard unit rates, half-hourly consumption (REST); Intelligent Octopus Go dispatches (GraphQL) — all read-only.
> Thank you!

The OAuth **client secret** (if issued) is sensitive → store in Azure Key Vault / local `.env`, never in git.
