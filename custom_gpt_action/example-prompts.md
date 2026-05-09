# Example Prompts

Use these after the Action is imported into your Custom GPT.

```text
For Blue Jays vs Angels today, find me the best 2-leg same-game parlay from Stake props only.
```

```text
For Yankees vs Red Sox on May 9, 2026, show me the best unders only. Use Stake available props only.
```

```text
For Dodgers vs Padres today, give me 3 player prop options, but do not use any player that Stake does not offer.
```

```text
Build a 2-leg SGP from hits and total bases only for Mets vs Phillies. Explain why each leg made it.
```

The Custom GPT should call `getMlbMatchupPicks` before answering these.
