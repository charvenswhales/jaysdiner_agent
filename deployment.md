# Deploying to Railway

## Deploy order: backend first, then frontend, then wire them together

You need the backend's real URL before the frontend's build can point at
it, and you need the frontend's real URL before the backend's CORS and
Monei settings can be locked down. So the order matters here.

### 1. Push and deploy the backend

1. Push `jaysdiner_agent` to its own GitHub repo.
2. Create a new Railway project and deploy from that repo. Railway will
   detect the `Dockerfile` and use it. This matters, it's what installs
   Playwright's browser correctly.
3. Add a Postgres database to the Railway project if you haven't already,
   or keep using your existing Neon instance. Either works, just make
   sure `DATABASE_URL` points at whichever one you use.
4. Set these environment variables in Railway:
   ```
   OPENROUTER_API_KEY=...
   DATABASE_URL=...
   JWT_SECRET_KEY=...            (generate a real one, see .env.example)
   MONEI_CLIENT_ID=...
   MONEI_CLIENT_SECRET=...
   MONEI_TOKEN_ENCRYPTION_KEY=... (generate a real one, see .env.example)
   BROWSER_HEADLESS=true
   ```
   Leave `MONEI_REDIRECT_URI`, `MONEI_FRONTEND_RETURN_URL`, and
   `ALLOWED_ORIGINS` for step 3, once you know both real URLs.
5. Deploy. Migrations run automatically on every startup. Once it's up,
   note the backend's public Railway URL.

### 2. Push and deploy the frontend

1. Push `jaysdiner-frontend` to its own GitHub repo.
2. Create a second Railway project, or a second service in the same
   project, from that repo. Railway auto-detects Next.js, no Dockerfile
   needed here.
3. Set `NEXT_PUBLIC_API_BASE` to the backend's real URL from step 1.
   This has to be set before the build runs, since Next.js bakes public
   env vars into the JS bundle at build time, not at runtime.
4. Deploy. Note the frontend's public Railway URL.

### 3. Wire the two together

Now that you have both real URLs, go back to the backend service in
Railway and set:

```
MONEI_REDIRECT_URI=https://your-backend.up.railway.app/connect/monei/callback
MONEI_FRONTEND_RETURN_URL=https://your-frontend.up.railway.app/profile
ALLOWED_ORIGINS=https://your-frontend.up.railway.app
```

Redeploy the backend so these take effect.

### 4. Update Monei's dashboard

Monei checks the redirect URI as an exact string match. Go to wherever
you registered your Monei Connect app and update the redirect URI to the
same `https://your-backend.up.railway.app/connect/monei/callback` value
you just set above. If these two don't match exactly, the OAuth callback
will fail.

## After it's live, actually test this

- [ ] Register a new account, log in, log out, log back in
- [ ] Ask for the menu and confirm the product cards and images load
- [ ] Place a real order through to the Monei payment card
- [ ] Connect a Monei wallet from the profile page
- [ ] Pay for an order with the PIN keypad and confirm it lands as "paid" in order history
- [ ] Open the same account from two different browsers at once and place
      orders in both. This is the concurrency fix from the pre-deploy
      audit, worth actually confirming it holds up for real, not just in
      the test suite that verified it here.
- [ ] Restart the backend service in Railway, then confirm an existing
      conversation still remembers its history afterward. This is what
      the Postgres checkpointer change is actually for.

## Known limitation, still worth knowing about

The Monei OAuth `state` token used during the connect flow is a
short-lived JWT good for ten minutes, not stored anywhere server-side.
That part is fine and stateless by design. What's genuinely worth
revisiting later if this grows past a few friends testing: the LangGraph
checkpointer now persists to Postgres, but there's no cleanup job for
old conversations yet, so that table will grow indefinitely. Not a
problem at this scale, but worth a note for later.