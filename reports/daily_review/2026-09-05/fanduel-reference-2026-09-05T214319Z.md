# FanDuel reference capture — 2026-09-05T214319Z

Fresh direct read from the official FanDuel NY regional event API at 2026-09-05 21:43:19–21:43:21 UTC (17:43:19–17:43:21 EDT):

`https://sbapi.ny.sportsbook.fanduel.com/api/event-page`

Reusable public parameters: `betexRegion=GBR`, `capiJurisdiction=intl`, `currencyCode=USD`, `exchangeLocale=en_US`, `includePrices=true`, `language=en`, `priceHistory=1`, `regionCode=NAMERICA`, `_ak=[REDACTED_PUBLIC_APP_KEY]`, `eventId={event_id}`. The `_ak` value is redacted; it is a public app configuration key, not a private credential.

All records are full-game `MONEY_LINE` and `TOTAL_POINTS_(OVER/UNDER)` with `marketStatus=OPEN` and `inPlay=false`. Fetch time is observation time only; no backend last-price-update time is asserted.

| Event | Away ML | Home ML | Total | Over | Under | Raw response SHA-256 |
|---|---:|---:|---:|---:|---:|---|
| 36030294 ATL @ PHI | +138 | -164 | 8.5 | -104 | -118 | `7539ecbd8cee9c097ca9346c6f99e1b5902d9243897cd6cc4ef5cb8a33065ee9` |
| 36030289 DET @ CLE | +134 | -158 | 7 | -102 | -120 | `c94098085460e2bfa82a3badd178152b308c81e17f046821a296556f14aa2df3` |
| 36030291 LAA @ PIT | +154 | -184 | 7.5 | -118 | -104 | `6d12351327db7c32a82dfdbc8f8d234be3b8f8c274914d66627fd49540e2b49d` |
| 36030295 MIL @ CIN | -156 | +132 | 9 | -114 | -106 | `649a831dd33c83f9adf7722403663e1b37c2a6874fb70011b991af6fdf7c94d8` |
| 36030298 BOS @ BAL | -132 | +112 | 7.5 | -122 | +100 | `910d7614d28e7a328433cb8b18c1ee37caa07fbb557182af92c12cf1d63d6399` |
| 36030287 TB @ TEX | -108 | -108 | 6.5 | -124 | +102 | `d0cea3e6884d913fab7b496e1665152feff0c05c87a7da5d6317e2982b5fd62f` |
| 36030296 TOR @ KC | -106 | -110 | 9.5 | -105 | -115 | `3143afc6bb8fa6768fa8d39795a8abba2abc5b02e12a02cfd77531ad7292db88` |
| 36030297 MIN @ CWS | +106 | -124 | 8.5 | +100 | -122 | `d13d577536aac5b16feda68e4ff84aa843205ada847461bf27be9b8154623eb5` |
| 36030288 ARI @ HOU | -108 | -108 | 8.5 | -124 | +102 | `5e82961d7439b5ffc577d0b75852979173fbe84582f7da263ab5a2dfc21951ff` |
| 36030299 NYY @ SD | +110 | -130 | 7.5 | -120 | -102 | `7143a320e381b7bd2f44146f55bdcd1c0ff471a073849cc8f6f56cdd5ec739b9` |
| 36030290 STL @ COL | -120 | +102 | 10.5 | -118 | -104 | `d8dab47bdb1156e40ccce559f0ddf7ae12332629e277e00e6ab491e84eb984f8` |
| 36030293 WSH @ LAD | +180 | -215 | 7 | -110 | -110 | `74df0f0e0246e5e105853b7c725160695165e0aa582ce0d0e38b051cd6cfb86c` |
| 36030300 ATH @ SEA | +200 | -245 | 7.5 | -115 | -105 | `4c2697830f280ec6d862347e827d9a8ce9b51b0e2653ff9c56142eeaea01e796` |

Comparison with the prior 21:39:48Z capture: all 13 normalized prices and totals are unchanged. Only the raw response hash for WSH @ LAD changed (`afd601...` → `74df0f...`), indicating response-body metadata or non-market content changed; the reviewed markets did not.
