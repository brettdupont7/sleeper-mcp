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
| `trade_context` | Both rosters by position with production, projections, value over replacement, records, league-wide positional ranks, needs, traded picks and scoring rules. If you list players, it simulates both lineups week by week through the fantasy playoffs, before and after the trade. It gives Claude context and doesn't judge the trade itself. |
| `find_trades` | Searches every other roster for 1-for-1, 2-for-1 and 1-for-2 trades that improve your lineup. Trades that help both sides are listed first. One-sided trades follow, limited by `max_partner_loss`. You can rule players out with `exclude_players` or shop one with `must_send` (see below). |
| `my_lineup` | Weekly start/sit check. It compares your current starters with the best projected lineup and lists swaps, starters on bye or Out, empty slots, free agents worth adding, Questionable starters, and this week's matchup with a win probability. |
| `playoff_odds` | Simulates the rest of the season thousands of times. Each team gets projected wins plus odds to make the playoffs, earn a bye, take the #1 seed and win the title. With your team, it also shows each remaining matchup's win probability. |
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

### How trades are measured

`trade_context` and `find_trades` measure a trade by what it does to each
team's **starting lineup**, week by week, from the current week through the
last week of the fantasy playoffs. Points come from projections scored under
your league's rules. Two adjustments keep the numbers honest:

- **Replacement level.** Each position's replacement level is the average
  projection of the top 3 free agents at that position in your league. A
  player's `vs_replacement` is his projected points per game above that. It's
  the fairest single number for comparing players across positions.
- **Streaming.** If a team can't fill a slot one week (bye or injury), the
  simulation starts a replacement-level free agent there instead of scoring a
  zero. So a backup QB is worth exactly what he adds over the waiver wire in
  the weeks he'd actually start.

The output includes the change for each week, rest-of-season and
playoff-week totals, any weeks a team would have a lineup hole, and whether
anyone needs to drop a player to make room. The tools don't model things that
are hard to project: injury risk, a player's role changing, or (in keeper and
dynasty leagues) value beyond this season. That's left for Claude and you to
weigh.

`find_trades` returns two lists:

- `win_win`: both teams' lineups improve, so the other manager has a reason to say yes.
- `one_sided`: trades that help you but cost the other team. `max_partner_loss`
  sets how much they can lose, in lineup points per week. At the default of 2,
  these are small asks that a sweetener could cover. They're only listed when
  win-win trades are scarce, closest to fair first. Raise it (say to 5), or
  pass null for no limit, to always see the biggest upgrades for you, ranked by
  your gain. With no limit, expect fantasies like "your backup QB for their two
  best players". That shows you the ceiling, not a realistic offer.

Two more filters keep `find_trades` focused:

- `exclude_players` leaves players out of every idea. Use it for untouchables,
  players you won't move, or players on other teams you don't want.
- `must_send` puts one or two of your players into every idea. Use it to shop a
  player, e.g. `["Bijan Robinson"]`.

Without filters, a cheap bench player can be the "send" side of many ideas, so
no single offer appears more than twice. Players forced in with `must_send`
don't count toward that cap.

`find_trades` first screens every combination on a typical week. It then
simulates the best few dozen candidates week by week and keeps only those
that help both sides. A whole 10-team league takes well under a second.

### How playoff odds work

`playoff_odds` plays out your league's actual remaining schedule, 5,000 times
by default. Each team's projected score is its best lineup that week,
calculated the same way as in the trade tools. This week only counts the
current roster. Later weeks assume teams pick up free agents for byes and
injuries. Each score varies randomly around its projection, about ±20 points
for a typical team. Seeding uses wins, then points for. The fantasy playoffs
are simulated as a seeded bracket with byes for the top seeds. It handles
one-week rounds, two-week rounds and a two-week final, plus leagues with an
extra weekly game against the median score.

`trade_context` uses the same simulation to give both teams a `season_outlook`.
For a proposed trade, it shows how each side's playoff, bye and title odds
change. Both runs use the same random results, so the difference comes from
the trade. Changes of 1–2 points are still noise.

What it doesn't model: points already scored in the current week, future
waiver pickups and trades, and divisions (seeding ignores them).

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
- "Find me a trade that upgrades my TE without hurting my playoff weeks."
- "Show me the biggest upgrades I could get, even if they're lopsided." (`max_partner_loss` raised)
- "What could I get for Bijan? Mahomes and Walker are untouchable." (`must_send` + `exclude_players`)
- "Is my lineup set for this week?"
- "What are my playoff odds, and should I be buying or selling?"
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
- `trade.py`: replacement level, week-by-week lineup simulation, trade search
- `season.py`: weekly lineup check, season and playoff simulation
- `undocumented.py`: stats, projections and schedule
- `scoring.py`: league-specific scoring
- `players.py`: player DB cache and name resolution
- `http.py`: rate limiter, TTL cache and retries
