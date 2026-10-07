# Prompts to try

Good for showing the whole flow:

- "Give me a risk overview of Wells Fargo's mortgage business."
- "Are mortgage complaints about Rocket spiking? Show me the last 12 months."
- "What are people complaining about with Mr. Cooper escrow handling?" (reviewer: narratives)
- "Compare JPMorgan and Bank of America denial rates in 2023."
- "What does Wells Fargo's latest 10-K say about servicing risk?"
- "Open a review ticket for the complaint spike you found, with the numbers as evidence."

Good for showing failure handling:

- Unset `FRED_API_KEY` and ask for a brief: the macro section reports unavailable, the rest still works.
- Run as `LRC_ROLE=viewer` and ask for filings or a ticket: the tools aren't even listed.
- Ask about a credit union: no CIK, so the filings section says why.
- Ask about "Chase": watch it resolve to JPMorgan Chase and report its confidence.
