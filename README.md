# sleeper-mcp

A read-only [MCP](https://modelcontextprotocol.io) server for the
[Sleeper](https://sleeper.com) fantasy football API, built with FastMCP. Use it
to ask Claude things like "who should I pick up this week?" or "is this trade
good for me?" about your real league.

- **No auth, read-only.** It only calls Sleeper's public `GET` endpoints and
  can't change anything in your league.
- **Scored for your league.** All points are computed with your league's
  `scoring_settings`. This was checked against Sleeper's own matchup scores.
- **Readable output.** Every player ID is turned into a name, position and
  team, and responses are trimmed down instead of dumping raw JSON.
- **Light on Sleeper.** The player database is cached on disk and refreshed at
  most once a day. League data is cached in memory for 2–10 minutes, and calls
  are capped at 300/min (Sleeper's limit is 1000).

## Tools

| Tool | What it does |
| --- | --- |
| `waiver_wire` | Ranks unrostered players by projected points, recent production and trending adds, with injury, bye week and opponent. If you pass your team, it flags your thin positions and which players fill them. |
| `trade_context` | Both rosters by position with production, projections, records, league-wide positional ranks, needs, traded picks and scoring rules. If you list players, it also shows each side's best projected lineup before and after the trade. It gives Claude context and doesn't judge the trade itself. |
| `get_user` | Username → `user_id`. |
| `my_leagues` | A user's leagues for a season, with your `roster_id` and record in each. |
| `league_overview` | Standings, every team's `roster_id`, lineup, scoring, waiver and playoff rules. |
| `transactions` | Trades, waiver claims and free-agent moves for a week, with FAAB bids. |
| `traded_picks` | Every future pick that has changed hands. |
| `playoff_bracket` | Winners or losers bracket with team names. |
| `league_drafts` | All drafts for a league (optionally earlier seasons too), with picks if requested. |
| `trending_players` | Most added/dropped players across Sleeper over a custom `lookback_hours` / `limit`, optionally marked as available or owned in your league. |

### How `waiver_wire` ranks players

Each free agent gets a percentile **within their position** on three signals:

- 50% projected points (average of this week and the next two)
- 30% recent average over the last 3 completed weeks (only games played count)
- 20% Sleeper-wide adds in the last 48 hours

Kickers and defenses are scaled by 0.85. Players who are Out or on IR are
halved. Players who fill one of your needs get +10.

A position counts as a **need** when any of these is true:

- you have fewer healthy players there than your lineup needs, plus one backup at RB/WR
- your group ranks in the bottom third of the league by projection
- you can't field it this week because of byes or injuries

### Undocumented endpoints

Stats, projections and the NFL schedule come from endpoints that Sleeper's own
app uses but [doesn't document](https://docs.sleeper.com). They're isolated in
`src/sleeper_mcp/undocumented.py` and fail soft. If one breaks, the tools still
return everything else and add a `notes` entry saying what's missing.

## Setup

Requires Python 3.10+.

```bash
git clone git@github.com:brettdupont7/sleeper-mcp.git
cd sleeper-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

Check that it starts. It waits for an MCP client on stdin, so press Ctrl-C to quit:

```bash
.venv/bin/sleeper-mcp
```

### Claude Desktop

Open **Settings → Developer → Edit Config** and add the server to
`claude_desktop_config.json`. Use the absolute path to your clone:

```json
{
  "mcpServers": {
    "sleeper": {
      "command": "/Users/YOU/path/to/sleeper-mcp/.venv/bin/sleeper-mcp",
      "env": {
        "SLEEPER_USERNAME": "your_sleeper_username"
      }
    }
  }
}
```

Restart Claude Desktop. The Sleeper tools appear under the tools (🔨) menu.

`SLEEPER_USERNAME` is optional. With it set, `my_leagues` and `waiver_wire`
know who "me" is without you saying so each time.

If you use [uv](https://docs.astral.sh/uv/), you can skip the clone and venv:

```json
{
  "mcpServers": {
    "sleeper": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/brettdupont7/sleeper-mcp", "sleeper-mcp"],
      "env": { "SLEEPER_USERNAME": "your_sleeper_username" }
    }
  }
}
```

### Claude Code

```bash
claude mcp add sleeper -e SLEEPER_USERNAME=your_sleeper_username -- /absolute/path/to/sleeper-mcp/.venv/bin/sleeper-mcp
```

### claude.ai (web and mobile)

claude.ai connects to remote MCP servers over HTTPS, so the server has to run
somewhere reachable from the internet.

1. Run it in HTTP mode:

   ```bash
   SLEEPER_MCP_TRANSPORT=http SLEEPER_MCP_HOST=0.0.0.0 SLEEPER_MCP_PORT=8000 .venv/bin/sleeper-mcp
   ```

   The MCP endpoint is `http://<host>:8000/mcp`.

2. Put it behind HTTPS. For a quick personal setup, use a tunnel such as
   `cloudflared tunnel --url http://localhost:8000`. For something permanent,
   deploy it to any host that runs Python (Fly.io, Railway, Render, a VPS).

3. In claude.ai, go to **Settings → Connectors → Add custom connector** and
   enter `https://<your-public-host>/mcp`.

The server has no auth of its own. It only exposes public, read-only Sleeper
data, but anyone with the URL can use your server's rate-limit budget. Don't
publish the URL.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `SLEEPER_USERNAME` | — | Default user for `my_leagues` and `waiver_wire`. |
| `SLEEPER_MCP_CACHE_DIR` | `~/.cache/sleeper-mcp` | Where the player database is stored (~3MB after trimming). |
| `SLEEPER_MAX_CALLS_PER_MIN` | `300` | Request cap (hard ceiling 900). |
| `SLEEPER_MCP_TRANSPORT` | `stdio` | `stdio` for desktop apps, `http` for remote. |
| `SLEEPER_MCP_HOST` / `SLEEPER_MCP_PORT` | `127.0.0.1` / `8000` | HTTP bind address. |
| `SLEEPER_MCP_LOG` | `WARNING` | Python log level. |

## Example prompts

- "What are my Sleeper leagues?"
- "Who are the best waiver pickups at RB in my league this week?"
- "I'm thinking of trading Kirk Cousins to roster 1 for Quinshon Judkins. Give me the context and your take."
- "Show week 8 transactions in my league."
- "Who owns my 2027 picks?"

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Code layout (`src/sleeper_mcp/`):

- `server.py`: tool definitions
- `sleeper.py`: documented endpoints, league context, lineup and needs logic
- `undocumented.py`: stats, projections and schedule
- `scoring.py`: league-specific scoring
- `players.py`: player DB cache and name resolution
- `http.py`: rate limiter, TTL cache and retries
