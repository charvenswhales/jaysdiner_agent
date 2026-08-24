# Deploying

Backend goes to Railway. Frontend goes to Vercel. You need the backend's
real URL before the frontend's build can point at it, and you need the
frontend's real URL before the backend's CORS and Monei settings can be
locked down. So deploy the backend first.

## 1. Deploy the backend to Railway

1. Go to railway.app, create a new project, and deploy from your
   jaysdiner_agent GitHub repo.
2. Railway will detect the Dockerfile and use it. This matters, it's
   what installs Playwright's browser correctly, a plain Python
   buildpack wouldn't do this for you.
3. Add a Postgres database to the Railway project if you don't already
   have one, or keep using your existing Neon instance. Either works,
   just make sure DATABASE_URL points at whichever one you use.
4. In the service's Variables tab, set:
   ```
   OPENROUTER_API_KEY=...
   DATABASE_URL=...
   JWT_SECRET_KEY=...            (generate a real one, see .env.example)
   MONEI_CLIENT_ID=...
   MONEI_CLIENT_SECRET=...
   MONEI_TOKEN_ENCRYPTION_KEY=... (generate a real one, see .env.example)
   BROWSER_HEADLESS=true
   ```
   Leave MONEI_REDIRECT_URI, MONEI_FRONTEND_RETURN_URL, and
   ALLOWED_ORIGINS for step 3, once you know the frontend's real URL.
5. Deploy. Migrations run automatically on every startup. Once it's up,
   grab the backend's public URL from Railway's Settings tab, under
   Networking, you'll want to generate a public domain there if one
   isn't already assigned.

## 2. Deploy the frontend to Vercel

1. Go to vercel.com, click New Project, and import your
   jaysdiner-agent-frontend GitHub repo. Vercel recognizes Next.js
   automatically, no config needed on your end.
2. Before the first deploy runs, add an environment variable:
   ```
   NEXT_PUBLIC_API_BASE=https://your-backend.up.railway.app
   ```
   using the real backend URL from step 1. This has to be set before
   the build runs, Next.js bakes public env vars into the JavaScript
   bundle at build time, not at runtime, so adding it after deploying
   won't do anything until you redeploy.
3. Deploy. Vercel gives you a URL like
   https://jaysdiner-agent-frontend.vercel.app.

## 3. Wire the two together

Now that you have both real URLs, go back to the backend service in
Railway and set:

```
MONEI_REDIRECT_URI=https://your-backend.up.railway.app/connect/monei/callback
MONEI_FRONTEND_RETURN_URL=https://your-frontend.vercel.app/profile
ALLOWED_ORIGINS=https://your-frontend.vercel.app
```

Redeploy the backend so these take effect.

## 4. Update Monei's dashboard

Monei checks the redirect URI as an exact string match. Go to wherever
you registered your Monei Connect app and update the redirect URI to
the same https://your-backend.up.railway.app/connect/monei/callback
value you just set above. If these two don't match exactly, the OAuth
callback will fail.

## After it's live, actually test this

- [ ] Register a new account, log in, log out, log back in
- [ ] Ask for the menu and confirm the product cards and images load
- [ ] Place a real order through to the Monei payment card
- [ ] Connect a Monei wallet from the profile page
- [ ] Pay for an order with the PIN keypad and confirm it lands as "paid" in order history
- [ ] Open the same account from two different browsers at once and
      place orders in both. This is the concurrency fix from the
      pre-deploy audit, worth actually confirming it holds up for real,
      not just in the test suite that verified it here.
- [ ] Restart the backend service in Railway, then confirm an existing
      conversation still remembers its history afterward. This is what
      the Postgres checkpointer change is actually for.

## Ongoing deploys after this first one

Both platforms redeploy automatically on every push to your main
branch once connected. Push to the backend repo and Railway rebuilds
it. Push to the frontend repo and Vercel rebuilds that. Neither needs
you to manually trigger anything again after this initial setup.

## Known limitation, still worth knowing about

The Monei OAuth state token used during the connect flow is a
short-lived JWT good for ten minutes, not stored anywhere server-side.
That part is fine and stateless by design. What's genuinely worth
revisiting later if this grows past a few friends testing: the
LangGraph checkpointer now persists to Postgres, but there's no cleanup
job for old conversations yet, so that table will grow indefinitely.
Not a problem at this scale, but worth a note for later.