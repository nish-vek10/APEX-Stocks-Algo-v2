Subject: OpenAPI question for developers — company fundamentals / market cap data

Hi Lucy,

Thanks for offering to relay this — appreciate it. It's a technical API capability question rather than a bug, so no screenshots needed, just the detail below for the developers.

**Context:**
I'm building an automated US equity trading system on the OpenAPI (SIM environment, developer sandbox). Part of the pipeline needs to build a tradable universe of US stocks filtered by:
- Market capitalization >= $300M
- Excluding REITs / real estate (by sector or industry classification)

**What I've checked:**
I've gone through the Reference Data documentation (`/ref/v1/instruments`, `/ref/v1/instruments/details`) and the FieldGroups available on those endpoints. They return trading/validation metadata (tick sizes, lot sizes, exchange, currency, supported order types, etc.) but I can't find any field covering market capitalization, sector, industry classification, or general company fundamentals.

**What I'm asking the developers:**
1. Is there any OpenAPI endpoint — documented or not fully surfaced in the public docs — that returns company fundamentals (market cap, sector/industry classification) for stock instruments?
2. If not available through OpenAPI, is this data exposed through any other Saxo product or feed I could get access to as a developer (e.g. an EOD file, a partner/CMS data product, or whatever powers the fundamentals shown in SaxoInvestor/SaxoTraderGO)?
3. If neither of the above, can they confirm definitively that fundamentals data is out of scope for OpenAPI, so I know to source it elsewhere?

Happy to hop on a call if that's easier for them, but the above should be enough detail to get a definitive answer either way.

Thanks,
Anish
