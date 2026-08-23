# Jay's Diner Agent

An AI agent that actually orders food from a real restaurant and pays for
it with real money, on its own. Built as a live demo of agentic commerce,
powered by [Monei](https://monei.cc).

You chat with it like you would chat with anyone. It checks a real
restaurant's real menu, builds a real cart, confirms the order with you,
places it, and pays for it directly from your connected Monei wallet. No
mock data anywhere in the loop.

## How it actually works

The interesting part of this project isn't the chat interface, it's what
happens underneath it. Early on, this was built as a browser automation
agent that clicked around the restaurant's website like a person would.
That turned out to be slow and occasionally hallucinated menu items, so
most of the actual ordering flow got rebuilt as plain deterministic HTTP
requests that replicate exactly what the restaurant's own website does
when you order through a browser: fetching the menu, adding items to
cart, submitting checkout. An AI agent still drives the conversation and
decides what to do, but the actual mechanics of talking to the restaurant
don't depend on an LLM interpreting a webpage. It's faster and far more
reliable.

The one place a real browser is still involved is reading the payment
gateway's dynamically generated bank details, since those only exist
inside a JavaScript widget with no API. Plain Playwright handles that
one narrow step, no AI involved.

Payment itself goes through Monei Connect. A user links their Monei
wallet once through OAuth, and from then on the agent can trigger a real
bank transfer payout on their behalf, gated behind their own transaction
PIN entered directly into a dedicated secure keypad. That PIN never
touches the chat or the LLM's context. It goes straight from the frontend
to the backend to Monei.

## Tech stack

- **FastAPI** for the API layer
- **LangGraph** and **LangChain** for the tool-calling conversational
  agent, running on a cheap OpenAI model via OpenRouter
- **SQLAlchemy** and **Alembic** for the database and migrations,
  targeting Postgres in production
- **Playwright** for the one browser-dependent step (reading payment
  gateway details)
- **httpx** and **BeautifulSoup** for everything else that talks to the
  restaurant's site directly

## Project structure

```
jaysdiner_agent/
├── api.py                FastAPI app, all the routes live here or get included from routers
├── agent.py               The LangGraph agent, its tools, and the SSE streaming logic
├── browser_tools.py        Deterministic HTTP ordering logic, plus the one Playwright step
├── schemas.py               Pydantic models for the ordering domain
├── config.py                 Environment variable loading
│
├── auth/                      Registration, login, JWT issuance
├── db/                          SQLAlchemy models and session setup
├── profiles/                     Saved checkout profiles, so returning users don't retype their address
├── orders/                        Order history
├── connect/                        Monei Connect OAuth flow and the payout logic
└── alembic/                          Database migrations
```

## Getting started

You'll need Python 3.12 or newer, an OpenRouter API key, and a Postgres
database (a free [Neon](https://neon.tech) instance works fine).

```bash
git clone https://github.com/Deji-404/jaysdiner_agent.git
cd jaysdiner_agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and fill it in. At minimum you need
`OPENROUTER_API_KEY` and `DATABASE_URL` to get the app running at all.
The Monei-related variables are only needed once you're ready to test
the actual payment flow, everything else works without them.

```bash
alembic upgrade head
uvicorn api:app --reload --port 8000
```

The interactive API docs are at `http://localhost:8000/docs` once it's
running.

## The frontend

This repo is the backend only. The actual chat interface lives in a
separate Next.js project: `https://github.com/Deji-404/jaysdiner-agent-frontend.git`. You'll
want both running to actually use this.

## Deploying

See [DEPLOYMENT.md](./DEPLOYMENT.md) for a full walkthrough of getting
this running on Railway, including the parts that are easy to miss, like
Playwright needing its own system dependencies that a plain Python
buildpack won't install for you.

## Known limitations

This was built as a working demo for a small group of testers, not a
production payments system serving the general public. A few things
worth knowing if you're picking this up:

- The LangGraph checkpointer persists conversation history to Postgres,
  but nothing cleans up old conversations yet. Fine at small scale, a
  real problem eventually.
- The saved-checkout-info flow and the payment flow both assume one
  active order at a time per conversation. Placing two orders back to
  back in the same conversation works, but the mental model throughout
  the codebase is "the most recent order in this thread," not a general
  multi-order queue.
- There's no automated test suite. Every fix in this codebase's history
  was verified by hand with real, one-off test scripts against real data
  before being folded into the actual code. Worth turning into a proper
  suite if this grows.

## License

Apache 2.0. See [LICENSE](./LICENSE).