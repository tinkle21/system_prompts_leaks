# Wheel Strategy — Claude Skill Prompt

> Paste this into Claude (or save as a Claude Club skill) to run the wheel strategy interactively.

---

I want you to run the wheel strategy on [STOCK] using my Alpaca paper trading account.

**STAGE 1 — SELL PUTS.**
Sell a cash-secured put on [STOCK] with a strike price around 10% below the current price. Pick an expiration 2-4 weeks out. Collect the premium.

- If the put expires worthless, sell another one. Keep collecting premium.
- If I get assigned (I have to buy the stock), move to Stage 2.

**STAGE 2 — SELL CALLS.**
Once I own the shares, sell a covered call with a strike price around 10% above what I paid. Pick an expiration 2-4 weeks out. Collect the premium.

- If the call expires worthless, sell another one. Keep collecting premium.
- If my shares get called away (sold), go back to Stage 1 and start again.

**RULES:**
- Never sell a put unless I have enough cash to buy the shares if assigned.
- Never sell a call below my cost basis (what I actually paid including premiums).
- Track my total premium collected across all cycles.
- Check positions every 15 minutes during market hours.
- If a contract hits 50% profit before expiration, close it early and sell a new one.
- Give me a daily summary at market close: current stage, premium collected, positions, and total return.

Run this during market hours. Do nothing outside market hours.

---

**Credentials are saved in:** `trading/wheel_strategy/credentials.json`  
**State is persisted in:** `trading/wheel_strategy/state.json`
